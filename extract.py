"""Turn clean thread transcripts into a validated action register.

Design rules, in order of importance:

  1. Every action must carry a quote that appears VERBATIM in one of the
     thread's messages. The quote is checked in code against the stored
     text. If it isn't there, the model invented it and the action is
     discarded. This is the anti-hallucination guard.

  2. The source message is resolved deterministically - we find which
     message contains the verbatim quote. The model never names it, so it
     cannot mis-attribute.

  3. Nothing calculable is asked of the model. Thread age, message counts,
     sender history and the coverage window are computed in SQL and handed
     to it as context.

  4. Precision over recall. A missed action is recoverable; a fabricated
     one destroys trust in the whole register.
"""

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError

import db

MODEL = os.environ.get("EXTRACTION_MODEL", "claude-sonnet-5")
MAX_WORKERS = int(os.environ.get("EXTRACTION_WORKERS", "2"))
CONFIDENCE_THRESHOLD = 0.6      # below this, action goes to the review bucket
MAX_TRANSCRIPT_CHARS = 12000    # long threads get truncated from the middle


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------

class ExtractedAction(BaseModel):
    action: str = Field(min_length=3)
    action_type: Literal["reply", "decision", "deliverable", "meeting", "task", "fyi"]
    owner: Literal["user", "other", "unclear"]
    deadline_text: str | None = None
    deadline_date: str | None = None
    urgency: Literal["high", "medium", "low"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=8)
    reasoning: str = ""


class ExtractionResult(BaseModel):
    requires_action: bool
    actions: list[ExtractedAction] = []


# --------------------------------------------------------------------------
# Prefilter - keep bulk mail away from the API entirely
# --------------------------------------------------------------------------

AUTOMATED_SENDER_RE = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?@|alerts?@|mailer@|bounce|"
    r"newsletter|updates?@|digest@|marketing@|news@|info@|support@zendesk)",
    re.IGNORECASE,
)

BULK_DOMAINS = {
    "jobright.ai", "linkedin.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
    "redditmail.com", "tldrnewsletter.com", "morningbrew.com", "nytimes.com",
    "wsj.com", "substack.com", "medium.com", "draftkings.com", "kith.com",
    "mlbemail.com", "espn.com", "eventbrite.com", "meetup.com",
}

BULK_PHRASES = [
    "unsubscribe", "view this email in your browser", "manage your preferences",
    "new jobs match", "jobs for you", "your job alert", "recommended for you",
    "daily digest", "weekly digest", "your order", "shipping confirmation",
    "subscription newsletter", "your subscription",
]


def _is_bulk_domain(address: str) -> bool:
    """Match the domain and any subdomain of it.

    Senders use subdomains heavily - access@interactive.wsj.com,
    draftkings@auth.draftkings.com - so an exact set lookup misses them.
    """
    domain = address.split("@")[-1].lower().strip()
    return any(domain == d or domain.endswith("." + d) for d in BULK_DOMAINS)


def prefilter(thread: dict) -> str | None:
    """Return a skip reason, or None if the thread should go to extraction."""
    participants = json.loads(thread["participants"] or "[]")
    transcript = (thread["transcript"] or "").lower()

    if not transcript.strip():
        return "empty transcript"

    if len(transcript) < 40:
        return "too short to contain an action"

    # Every participant is an automated sender - nobody is actually asking.
    human_senders = [
        p for p in participants
        if not AUTOMATED_SENDER_RE.search(p) and not _is_bulk_domain(p)
    ]
    if not human_senders:
        return "automated sender only"

    # Single-message thread from a bulk domain with marketing language.
    if thread["message_count"] == 1:
        if any(_is_bulk_domain(p) for p in participants):
            return "bulk sender"
        if any(phrase in transcript for phrase in BULK_PHRASES):
            return "bulk content"

    return None


# --------------------------------------------------------------------------
# Deterministic context
# --------------------------------------------------------------------------

def get_user_email(conn) -> str:
    """The mailbox owner's address. Stored at sync time; inferred if absent."""
    stored = db.get_state(conn, "user_email")
    if stored:
        return stored

    # Fallback: the address that appears most often as a recipient is almost
    # certainly the owner of this mailbox.
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT to_emails FROM messages WHERE to_emails IS NOT NULL"):
        for addr in json.loads(row[0] or "[]"):
            counts[addr.lower()] = counts.get(addr.lower(), 0) + 1

    if not counts:
        raise RuntimeError("Cannot determine mailbox owner - pass --me you@example.com")

    inferred = max(counts, key=counts.get)
    db.set_state(conn, "user_email", inferred)
    return inferred


def build_context(conn, thread: dict, user_email: str) -> dict:
    """Facts computed in SQL, never asked of the model."""
    msgs = conn.execute(
        """
        SELECT from_email, internal_date FROM messages
         WHERE thread_id = ? ORDER BY internal_date
        """,
        (thread["thread_id"],),
    ).fetchall()

    now = datetime.now(timezone.utc)
    last_dt = datetime.fromtimestamp(thread["last_date"] / 1000, timezone.utc)
    first_dt = datetime.fromtimestamp(thread["first_date"] / 1000, timezone.utc)

    last_sender = msgs[-1]["from_email"] if msgs else None
    user_sent_last = (last_sender or "").lower() == user_email.lower()

    others = [m["from_email"] for m in msgs if (m["from_email"] or "").lower() != user_email.lower()]
    counterparty = others[-1] if others else None

    history = 0
    if counterparty:
        history = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE from_email = ?", (counterparty,)
        ).fetchone()[0]

    return {
        "today": now.strftime("%Y-%m-%d (%A)"),
        "thread_started": first_dt.strftime("%Y-%m-%d"),
        "last_message": last_dt.strftime("%Y-%m-%d"),
        "days_since_last_message": (now - last_dt).days,
        "message_count": thread["message_count"],
        "user_sent_last_message": user_sent_last,
        "counterparty": counterparty,
        "counterparty_total_messages": history,
    }


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You extract action items from email threads for a busy executive's action register.

You will be given one email conversation and factual metadata about it.

WHAT COUNTS AS AN ACTION
An action is something THE USER must personally do, decide, or respond to. Examples:
- Someone asked the user a direct question that is still unanswered
- Someone is waiting on a deliverable, document, or approval from the user
- A decision is required from the user
- A meeting needs scheduling, confirming, or preparing for
- The user committed to doing something and has not confirmed it is done
- An administrative or one-off task the user needs to complete (submit a form,
  update a setting, log hours, follow a new process going forward)

ACTION_TYPE - pick exactly one:
- "reply"       a question or message is waiting on a response from the user
- "decision"    the user must choose or approve something
- "deliverable" the user owes a document, file, or piece of work to someone
- "meeting"     something to schedule, confirm, or prepare for
- "task"        a concrete to-do that isn't owed to a specific person - forms,
                 account setup, logging hours, process/policy changes to follow
- "fyi"         informational only, no response required (rare - most fyi mail
                 should return requires_action: false instead of an fyi action)

WHAT IS NOT AN ACTION
- Newsletters, job alerts, marketing, notifications, receipts, automated mail
- Something already completed or resolved later in the same thread
- Something the OTHER person owes the user with nothing required from the user
- Vague pleasantries ("let's catch up sometime", "keep me posted")
- Anything you infer but which is not actually stated in the thread

HARD RULES
1. Every action MUST include "evidence_quote": a span copied EXACTLY, character for
   character, from the transcript. Do not paraphrase, correct, reformat, or shorten it
   with ellipses. It is checked programmatically against the source text, and any
   action whose quote does not match exactly is discarded.
2. Never invent a deadline. Set "deadline_text" only if the thread literally states
   timing, and copy that wording verbatim. If there is no stated deadline, both
   deadline fields must be null.
3. "deadline_date" resolves deadline_text to an ISO date using today's date from the
   metadata. If deadline_text is null, deadline_date must be null.
4. If the conversation resolved (question answered, meeting confirmed, item delivered),
   return no action for it.
5. When uncertain whether something is a real action, do not return it. A missed action
   is recoverable; a fabricated one is not.
6. Set "confidence" honestly: 0.9+ only for an explicit unambiguous request, 0.7-0.9 for
   a clear but implicit one, below 0.6 when genuinely unsure.

OUTPUT
Return ONLY a JSON object, no prose, no markdown fences:

{
  "requires_action": true,
  "actions": [
    {
      "action": "Send the root-cause report to Klaus before the QBR",
      "action_type": "deliverable",
      "owner": "user",
      "deadline_text": "by Friday the 25th",
      "deadline_date": "2026-07-25",
      "urgency": "high",
      "confidence": 0.92,
      "evidence_quote": "We need the root-cause report by Friday the 25th",
      "reasoning": "Explicit request with a stated date, not acknowledged later in the thread"
    }
  ]
}

If nothing requires the user, return {"requires_action": false, "actions": []}."""


def build_user_prompt(thread: dict, context: dict, user_email: str) -> str:
    transcript = thread["transcript"] or ""

    # Very long threads: keep the head and tail, drop the middle. The ask and
    # the current state almost always live at the edges.
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        half = MAX_TRANSCRIPT_CHARS // 2
        transcript = (
            transcript[:half]
            + "\n\n[... middle of thread truncated ...]\n\n"
            + transcript[-half:]
        )

    return f"""MAILBOX OWNER (the user): {user_email}

FACTS (computed, treat as authoritative - do not recalculate):
- Today: {context['today']}
- Thread started: {context['thread_started']}
- Last message: {context['last_message']} ({context['days_since_last_message']} days ago)
- Messages in thread: {context['message_count']}
- User sent the last message: {context['user_sent_last_message']}
- Counterparty: {context['counterparty']}
- Total messages ever received from counterparty: {context['counterparty_total_messages']}

SUBJECT: {thread['subject']}

TRANSCRIPT:
{transcript}"""


# --------------------------------------------------------------------------
# Validation - the anti-hallucination guard
# --------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase, for tolerant quote matching."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def resolve_source_message(conn, thread_id: str, quote: str) -> str | None:
    """Find which message contains this quote verbatim.

    Returns the message id, or None if no message contains it - which means
    the model fabricated or altered the quote, and the action is rejected.
    """
    needle = _normalize(quote)
    if len(needle) < 8:
        return None

    rows = conn.execute(
        "SELECT id, clean_text FROM messages WHERE thread_id = ? ORDER BY internal_date",
        (thread_id,),
    ).fetchall()

    for row in rows:
        if needle in _normalize(row["clean_text"]):
            return row["id"]
    return None


def action_id(thread_id: str, action_text: str) -> str:
    """Stable id so re-running extraction updates rather than duplicates."""
    key = f"{thread_id}::{_normalize(action_text)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def validate_action(conn, thread_id: str, action: ExtractedAction) -> tuple[str | None, list[str]]:
    """Check one action. Returns (source_message_id, problems).

    A non-empty problems list means the action is rejected.
    """
    problems = []

    source_id = resolve_source_message(conn, thread_id, action.evidence_quote)
    if source_id is None:
        problems.append("evidence quote not found verbatim in any message")

    # A date with no supporting text means the model invented the timing.
    if action.deadline_date and not action.deadline_text:
        problems.append("deadline_date present with no deadline_text")

    if action.deadline_date:
        try:
            parsed = datetime.strptime(action.deadline_date, "%Y-%m-%d")
            now = datetime.now()
            if not (now - timedelta(days=365) < parsed < now + timedelta(days=730)):
                problems.append(f"deadline_date implausible: {action.deadline_date}")
        except ValueError:
            problems.append(f"deadline_date not ISO format: {action.deadline_date}")

    # The register tracks what the USER owes. Other people's items are noise here.
    if action.owner == "other":
        problems.append("owner is the counterparty, not the user")

    return source_id, problems


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def parse_response(text: str) -> ExtractionResult:
    """Parse the model's JSON, tolerating markdown fences."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)

    # If there is stray prose, grab the outermost JSON object.
    if not cleaned.lstrip().startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

    return ExtractionResult.model_validate(json.loads(cleaned))


def extract_thread(client: Anthropic, thread: dict, context: dict, user_email: str,
                   retries: int = 3) -> ExtractionResult | None:
    """One API call for one thread."""
    prompt = build_user_prompt(thread, context, user_email)
    last_body = None

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            body = "".join(b.text for b in response.content if b.type == "text")
            last_body = body
            return parse_response(body)

        except (json.JSONDecodeError, ValidationError) as e:
            if attempt < retries:
                time.sleep(1)
                continue
            # Print what the model actually sent back - "unparseable" alone
            # doesn't say whether it added prose, used the wrong enum value,
            # or something else. Seeing the real text is how you fix the
            # prompt instead of guessing at it.
            snippet = (last_body or "")[:250].replace("\n", " ")
            print(f"  ! {thread['thread_id']}: unparseable response ({type(e).__name__})")
            print(f"      raw: {snippet}{'...' if last_body and len(last_body) > 250 else ''}")
            return None

        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
                continue
            print(f"  ! {thread['thread_id']}: {type(e).__name__}: {e}")
            return None

    return None


def store_action(conn, thread_id: str, source_message_id: str, action: ExtractedAction) -> None:
    """Insert or update, preserving any status a human already set."""
    aid = action_id(thread_id, action.action)
    review = int(action.confidence < CONFIDENCE_THRESHOLD)

    existing = conn.execute("SELECT status FROM actions WHERE id = ?", (aid,)).fetchone()
    status = existing["status"] if existing else "open"

    conn.execute(
        """
        INSERT OR REPLACE INTO actions
            (id, thread_id, source_message_id, action, action_type, owner,
             deadline_text, deadline_date, urgency, confidence, evidence_quote,
             reasoning, status, review_flag, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """,
        (
            aid, thread_id, source_message_id, action.action, action.action_type,
            action.owner, action.deadline_text, action.deadline_date, action.urgency,
            action.confidence, action.evidence_quote, action.reasoning, status, review,
        ),
    )


def extract_all(conn, client: Anthropic, days: int = 7, limit: int | None = None,
                rebuild: bool = False, dry_run: bool = False) -> dict:
    """Run extraction over threads active in the last `days` days."""
    user_email = get_user_email(conn)
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    where = "WHERE last_date >= ?"
    if not rebuild:
        where += " AND extracted_at IS NULL"

    sql = f"SELECT * FROM threads {where} ORDER BY last_date DESC"
    if limit:
        sql += f" LIMIT {limit}"

    threads = [dict(r) for r in conn.execute(sql, (cutoff_ms,))]

    stats = {
        "threads_considered": len(threads),
        "skipped_prefilter": 0,
        "sent_to_model": 0,
        "no_action_found": 0,
        "actions_proposed": 0,
        "actions_stored": 0,
        "actions_rejected": 0,
        "rejection_reasons": {},
        "flagged_for_review": 0,
    }

    # Prefilter first - this is what keeps cost down.
    to_process = []
    for t in threads:
        reason = prefilter(t)
        if reason:
            stats["skipped_prefilter"] += 1
            conn.execute(
                "UPDATE threads SET skip_reason = ?, extracted_at = CURRENT_TIMESTAMP WHERE thread_id = ?",
                (reason, t["thread_id"]),
            )
        else:
            to_process.append(t)
    conn.commit()

    print(f"Window          : last {days} days")
    print(f"Threads in scope: {len(threads):,}")
    print(f"Skipped by prefilter: {stats['skipped_prefilter']:,}")
    print(f"To extract      : {len(to_process):,}")

    if dry_run:
        print("\n(dry run - no API calls made)")
        return stats

    if not to_process:
        return stats

    print(f"\nExtracting with {MODEL}...")

    contexts = {t["thread_id"]: build_context(conn, t, user_email) for t in to_process}

    def work(t):
        return t, extract_thread(client, t, contexts[t["thread_id"]], user_email)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i, (thread, result) in enumerate(pool.map(work, to_process), 1):
            stats["sent_to_model"] += 1

            if result is None:
                continue

            if not result.actions:
                stats["no_action_found"] += 1
            else:
                for action in result.actions:
                    stats["actions_proposed"] += 1
                    source_id, problems = validate_action(conn, thread["thread_id"], action)

                    if problems:
                        stats["actions_rejected"] += 1
                        for p in problems:
                            key = p.split(":")[0]
                            stats["rejection_reasons"][key] = stats["rejection_reasons"].get(key, 0) + 1
                        continue

                    store_action(conn, thread["thread_id"], source_id, action)
                    stats["actions_stored"] += 1
                    if action.confidence < CONFIDENCE_THRESHOLD:
                        stats["flagged_for_review"] += 1

            conn.execute(
                "UPDATE threads SET extracted_at = CURRENT_TIMESTAMP WHERE thread_id = ?",
                (thread["thread_id"],),
            )

            if i % 10 == 0:
                conn.commit()
                print(f"  {i}/{len(to_process)} threads")

    conn.commit()
    return stats
