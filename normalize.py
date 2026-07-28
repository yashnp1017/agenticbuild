"""Turn raw stored messages into clean text, then into thread transcripts.

Three jobs:
  1. If a message has no plain-text body, derive one from its HTML.
  2. Strip quoted reply chains and signature blocks, leaving only what
     that message actually added to the conversation.
  3. Group messages by thread into one chronological transcript, which is
     the unit extraction will run on.

Deliberately no LLM here - this is all deterministic text processing, so it
can be re-run freely and its behaviour is fully inspectable.
"""

import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

import db

# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    """Flatten an HTML body to readable plain text.

    Needed because a large share of automated mail (job alerts, newsletters)
    ships HTML only, with no text/plain alternative at all.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Script/style content is never readable text.
    for tag in soup(["script", "style", "head", "meta", "title"]):
        tag.decompose()

    # Preserve block structure as newlines so paragraphs don't run together.
    for tag in soup(["br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"]):
        tag.append("\n")

    text = soup.get_text()

    # Collapse the runs of blank lines that HTML flattening tends to produce.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

    return text.strip()


# --------------------------------------------------------------------------
# Quoted reply detection
# --------------------------------------------------------------------------

# Header lines that mark the start of a quoted previous message. Once we hit
# one of these, everything below it is history, not new content.
QUOTE_HEADERS = [
    # "On Wed, Jul 23, 2026 at 2:14 PM Klaus Bergmann <k@x.com> wrote:"
    r"^\s*On\s+.{6,120}\s+wrote:\s*$",
    # "On Wed, Jul 23, 2026 at 2:14 PM Klaus wrote:" spilling onto two lines
    r"^\s*On\s+.{6,120},\s*$",
    # Outlook / Exchange style block headers
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
    r"^\s*_{5,}\s*$",
    r"^\s*From:\s*.+$",              # Outlook forwarded/replied header block
    r"^\s*Sent:\s*.+$",
    # Gmail forward marker
    r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
    # Apple Mail
    r"^\s*Begin forwarded message:\s*$",
    # Generic "wrote:" fallback with an email address on the line
    r"^\s*.{0,80}<[^>]+@[^>]+>\s*wrote:\s*$",
]

QUOTE_HEADER_RE = re.compile("|".join(QUOTE_HEADERS), re.IGNORECASE | re.MULTILINE)


def strip_quoted(text: str) -> tuple[str, bool]:
    """Remove quoted history. Returns (clean_text, found_quote).

    Two signals are used, whichever appears first:
      - a quote header line ("On ... wrote:", "-----Original Message-----")
      - a run of '>' prefixed lines, which is the classic quote marker
    """
    if not text:
        return "", False

    lines = text.split("\n")
    cut_at = None

    # Signal 1: an explicit quote header.
    for i, line in enumerate(lines):
        if QUOTE_HEADER_RE.match(line):
            cut_at = i
            break

    # Signal 2: the start of a sustained '>' quoted block. A single '>' line
    # can be a false positive (someone quoting a word), so require two in a
    # row before treating it as the boundary.
    if cut_at is None:
        for i in range(len(lines) - 1):
            if lines[i].lstrip().startswith(">") and lines[i + 1].lstrip().startswith(">"):
                cut_at = i
                break

    if cut_at is None:
        return text.strip(), False

    return "\n".join(lines[:cut_at]).strip(), True


# --------------------------------------------------------------------------
# Signature detection
# --------------------------------------------------------------------------

SIGNATURE_MARKERS = [
    r"^\s*--\s*$",                          # RFC 3676 standard delimiter
    r"^\s*__+\s*$",
    r"^\s*Sent from my (iPhone|iPad|Android|Samsung|BlackBerry|mobile).*$",
    r"^\s*Get Outlook for (iOS|Android).*$",
    r"^\s*Best regards?,?\s*$",
    r"^\s*Kind regards?,?\s*$",
    r"^\s*Warm regards?,?\s*$",
    r"^\s*Regards,?\s*$",
    r"^\s*Thanks(\s+again)?,?\s*$",
    r"^\s*Thank you,?\s*$",
    r"^\s*Cheers,?\s*$",
    r"^\s*Sincerely,?\s*$",
]

SIGNATURE_RE = re.compile("|".join(SIGNATURE_MARKERS), re.IGNORECASE | re.MULTILINE)

# Legal/unsubscribe boilerplate that shows up at the bottom of bulk mail.
BOILERPLATE_RE = re.compile(
    r"^\s*(This (e-?mail|message) (and any attachments )?(is|are) (confidential|intended)"
    r"|CONFIDENTIALITY NOTICE"
    r"|If you (are not the intended recipient|no longer wish to receive)"
    r"|To unsubscribe"
    r"|Unsubscribe\b"
    r"|You are receiving this (e-?mail|message) because"
    r"|View this email in your browser"
    r"|Manage your (notification )?preferences"
    r"|\u00a9\s*\d{4}\b).*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_signature(text: str, max_tail_lines: int = 12) -> tuple[str, bool]:
    """Remove a trailing signature block. Returns (clean_text, found_sig).

    Only looks in the last `max_tail_lines` lines. A sign-off like "Thanks,"
    appearing in the middle of a message is part of the content; the same
    line at the very bottom is the start of a signature.
    """
    if not text:
        return "", False

    lines = text.split("\n")
    search_start = max(0, len(lines) - max_tail_lines)
    cut_at = None

    for i in range(search_start, len(lines)):
        if SIGNATURE_RE.match(lines[i]):
            cut_at = i
            break

    if cut_at is None:
        return text.strip(), False

    return "\n".join(lines[:cut_at]).strip(), True


def strip_boilerplate(text: str) -> tuple[str, bool]:
    """Cut everything from the first legal/unsubscribe boilerplate line down."""
    if not text:
        return "", False

    match = BOILERPLATE_RE.search(text)
    if not match:
        return text.strip(), False

    return text[: match.start()].strip(), True


# --------------------------------------------------------------------------
# Full per-message cleaning
# --------------------------------------------------------------------------

def clean_message(body_text: str, body_html: str) -> dict:
    """Produce clean text for one message, plus what was removed and why."""
    source = "text"
    raw = (body_text or "").strip()

    # Fall back to HTML when there's no usable plain-text part.
    if not raw and body_html:
        raw = html_to_text(body_html)
        source = "html"

    original_len = len(raw)

    text, had_quote = strip_quoted(raw)
    text, had_boiler = strip_boilerplate(text)
    text, had_sig = strip_signature(text)

    # Tidy up whitespace left behind by the cuts.
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    return {
        "clean_text": text,
        "source": source,
        "original_len": original_len,
        "clean_len": len(text),
        "had_quote": had_quote,
        "had_signature": had_sig,
        "had_boilerplate": had_boiler,
    }


# --------------------------------------------------------------------------
# Thread assembly
# --------------------------------------------------------------------------

def build_transcript(messages: list[dict], max_chars_per_message: int | None = None) -> str:
    """Turn a thread's cleaned messages into one chronological transcript.

    This is the document extraction actually reads - the whole conversation
    in order, so a later message that changes a deadline is visible in the
    same pass as the message that set it.
    """
    parts = []

    for msg in messages:
        text = (msg["clean_text"] or "").strip()
        if not text:
            continue  # nothing new was added by this message

        if max_chars_per_message and len(text) > max_chars_per_message:
            text = text[:max_chars_per_message].rstrip() + " [...truncated]"

        when = datetime.fromtimestamp(msg["internal_date"] / 1000)
        sender = msg["from_name"] or msg["from_email"] or "unknown"

        parts.append(f"[{when:%Y-%m-%d %H:%M}] {sender} <{msg['from_email']}>\n{text}")

    return "\n\n---\n\n".join(parts)


def normalize_all(conn, limit: int | None = None, rebuild: bool = False) -> dict:
    """Clean every stored message, then assemble every thread.

    Safe to re-run: with `rebuild` off it skips messages already cleaned.
    """
    stats = {
        "messages_cleaned": 0,
        "from_html": 0,
        "had_quote": 0,
        "had_signature": 0,
        "had_boilerplate": 0,
        "empty_after_clean": 0,
        "threads_built": 0,
        "chars_before": 0,
        "chars_after": 0,
    }

    where = "" if rebuild else "WHERE clean_text IS NULL"
    sql = f"SELECT id, body_text, body_html FROM messages {where}"
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    print(f"Cleaning {len(rows):,} messages...")

    for i, row in enumerate(rows, 1):
        result = clean_message(row["body_text"], row["body_html"])

        conn.execute(
            """
            UPDATE messages
               SET clean_text = ?, clean_source = ?, had_quote = ?, had_signature = ?
             WHERE id = ?
            """,
            (
                result["clean_text"],
                result["source"],
                int(result["had_quote"]),
                int(result["had_signature"]),
                row["id"],
            ),
        )

        stats["messages_cleaned"] += 1
        stats["chars_before"] += result["original_len"]
        stats["chars_after"] += result["clean_len"]
        stats["from_html"] += result["source"] == "html"
        stats["had_quote"] += result["had_quote"]
        stats["had_signature"] += result["had_signature"]
        stats["had_boilerplate"] += result["had_boilerplate"]
        stats["empty_after_clean"] += not result["clean_text"]

        if i % 500 == 0:
            conn.commit()
            print(f"  cleaned {i:,}/{len(rows):,}")

    conn.commit()

    # --- assemble threads -------------------------------------------------
    print("Assembling thread transcripts...")

    thread_ids = [
        r["thread_id"] for r in conn.execute("SELECT DISTINCT thread_id FROM messages")
    ]

    for i, thread_id in enumerate(thread_ids, 1):
        msgs = conn.execute(
            """
            SELECT id, from_name, from_email, subject, internal_date, clean_text
              FROM messages
             WHERE thread_id = ?
             ORDER BY internal_date
            """,
            (thread_id,),
        ).fetchall()

        if not msgs:
            continue

        msgs = [dict(m) for m in msgs]
        transcript = build_transcript(msgs)

        participants = sorted({m["from_email"] for m in msgs if m["from_email"]})

        conn.execute(
            """
            INSERT OR REPLACE INTO threads
                (thread_id, subject, participants, message_count,
                 first_date, last_date, transcript, char_count, normalized_at)
            VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                thread_id,
                msgs[0]["subject"],
                json.dumps(participants),
                len(msgs),
                msgs[0]["internal_date"],
                msgs[-1]["internal_date"],
                transcript,
                len(transcript),
            ),
        )

        stats["threads_built"] += 1

        if i % 500 == 0:
            conn.commit()
            print(f"  built {i:,}/{len(thread_ids):,}")

    conn.commit()
    return stats
