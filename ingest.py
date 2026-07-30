"""Fetch messages from Gmail and store them locally.

Two modes:
  full_sync        - walk the mailbox via messages.list (first run, or recovery)
  incremental_sync - ask history.list what changed since last time (every run after)
"""

import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses, parseaddr

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import db

# Gmail allows ~250 quota units/user/second; messages.get costs 5.
# 5 workers keeps us comfortably under while still being ~5x faster than serial.
MAX_WORKERS = 5

# The googleapiclient service object wraps a single network connection that
# is NOT safe to share across threads - concurrent use corrupts the TLS
# state and throws intermittent SSL errors ("bad record mac"). Each worker
# thread gets its own private service instance instead, built once and
# reused only by that thread.
_thread_local = threading.local()


def _thread_service(credentials):
    if not hasattr(_thread_local, "service"):
        _thread_local.service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
    return _thread_local.service


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _decode(data: str) -> str:
    """Gmail returns bodies base64url-encoded, often without padding."""
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Walk the MIME tree and pull out plain-text and HTML bodies.

    An email is a nested tree, not a string: multipart/alternative holding a
    text/plain and a text/html twin, possibly wrapped in multipart/mixed with
    attachments. We collect every text part we find at any depth.
    """
    text_parts, html_parts = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if data:
            if mime == "text/plain":
                text_parts.append(_decode(data))
            elif mime == "text/html":
                html_parts.append(_decode(data))

        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def _headers_to_dict(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def parse_message(raw: dict) -> dict:
    """Turn a raw Gmail API message into a flat row we can store."""
    payload = raw.get("payload", {})
    headers = _headers_to_dict(payload)

    from_name, from_email = parseaddr(headers.get("from", ""))
    to_emails = [addr for _, addr in getaddresses([headers.get("to", "")]) if addr]
    cc_emails = [addr for _, addr in getaddresses([headers.get("cc", "")]) if addr]

    body_text, body_html = _extract_bodies(payload)

    return {
        "id": raw["id"],
        "thread_id": raw["threadId"],
        "history_id": int(raw.get("historyId", 0)),
        "internal_date": int(raw.get("internalDate", 0)),
        "from_name": from_name or None,
        "from_email": from_email.lower() or None,
        "to_emails": to_emails,
        "cc_emails": cc_emails,
        "subject": headers.get("subject"),
        "snippet": raw.get("snippet"),
        "label_ids": raw.get("labelIds", []),
        "body_text": body_text,
        "body_html": body_html,
        "raw_payload": raw,
    }


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _fetch_one(credentials, msg_id: str, retries: int = 3) -> dict | None:
    """Fetch a single message, backing off on rate limits.

    Takes credentials rather than a service object so each thread can build
    (and reuse) its own private connection - see _thread_service above.
    """
    service = _thread_service(credentials)
    for attempt in range(retries):
        try:
            return (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as e:
            if e.resp.status in (403, 429, 500, 503) and attempt < retries - 1:
                time.sleep(2**attempt)  # 1s, 2s, 4s
                continue
            print(f"  ! failed {msg_id}: {e}")
            return None
    return None


def _list_message_ids(service, query: str | None, max_messages: int | None) -> list[str]:
    """Page through messages.list collecting ids (cheap: 5 units per page of 500)."""
    ids, page_token = [], None

    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        print(f"  listed {len(ids):,} ids...")

        page_token = resp.get("nextPageToken")
        if not page_token or (max_messages and len(ids) >= max_messages):
            break

    return ids[:max_messages] if max_messages else ids


def _fetch_and_store(service, conn, msg_ids: list[str], credentials) -> int:
    """Fetch messages in parallel, parse, and write to SQLite.

    `service` is used for nothing here except staying consistent with the
    rest of the module's signatures; the parallel fetches use `credentials`
    directly so each thread can build its own private connection.
    """
    stored = 0

    # Writes stay on the main thread - SQLite connections aren't thread-safe.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i, raw in enumerate(pool.map(lambda m: _fetch_one(credentials, m), msg_ids), 1):
            if raw is None:
                continue
            db.upsert_message(conn, parse_message(raw))
            stored += 1

            if i % 100 == 0:
                conn.commit()
                print(f"  stored {i:,}/{len(msg_ids):,}")

    conn.commit()
    return stored


# --------------------------------------------------------------------------
# Sync modes
# --------------------------------------------------------------------------

def full_sync(
    service, conn, credentials, query: str | None = None, max_messages: int | None = None
) -> int:
    """Walk the mailbox and store everything matching `query`.

    Capture historyId BEFORE fetching, so anything that arrives mid-sync gets
    picked up by the next incremental run instead of being silently skipped.
    """
    profile = service.users().getProfile(userId="me").execute()
    start_history_id = profile["historyId"]
    db.set_state(conn, "user_email", profile["emailAddress"])

    print(f"Full sync for {profile['emailAddress']}")
    if query:
        print(f"Query: {query}")

    all_ids = _list_message_ids(service, query, max_messages)
    already_have = db.existing_ids(conn)
    new_ids = [i for i in all_ids if i not in already_have]

    print(f"Found {len(all_ids):,} messages, {len(new_ids):,} new")
    if not new_ids:
        db.set_state(conn, "last_history_id", start_history_id)
        return 0

    stored = _fetch_and_store(service, conn, new_ids, credentials)

    db.set_state(conn, "last_history_id", start_history_id)
    db.set_state(conn, "last_full_sync", time.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"Stored {stored:,} messages. historyId now {start_history_id}")
    return stored


def incremental_sync(service, conn, credentials) -> int:
    """Fetch only what changed since the last recorded historyId.

    Gmail only retains history for about a week. If our stored id is older than
    that the API returns 404, and the correct response is a full resync.
    """
    last_id = db.get_state(conn, "last_history_id")
    if not last_id:
        print("No historyId stored - run a full sync first.")
        return 0

    print(f"Incremental sync from historyId {last_id}")

    new_ids, page_token, latest_history_id = set(), None, last_id

    while True:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=last_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                print("historyId too old (Gmail keeps ~1 week). Falling back to full sync.")
                return full_sync(service, conn, credentials)
            raise

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                new_ids.add(added["message"]["id"])

        latest_history_id = resp.get("historyId", latest_history_id)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    already_have = db.existing_ids(conn)
    to_fetch = [i for i in new_ids if i not in already_have]

    print(f"{len(new_ids)} new message events, {len(to_fetch)} to fetch")

    stored = _fetch_and_store(service, conn, to_fetch, credentials) if to_fetch else 0

    db.set_state(conn, "last_history_id", latest_history_id)
    print(f"Stored {stored} messages. historyId now {latest_history_id}")
    return stored
