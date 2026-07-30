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

# Marketing/security mail commonly hides a long "preheader" block via CSS so
# the inbox preview line shows custom text instead of whatever's visually
# first in the email. BeautifulSoup.get_text() has no concept of CSS, so a
# display:none block is just as visible to it as real content - left
# unhandled, one hidden block can outweigh the actual message by 100x.
_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|max-height\s*:\s*0",
    re.IGNORECASE,
)


def _strip_hidden(soup: BeautifulSoup) -> None:
    """Remove elements that are invisible to a human reader."""
    for tag in soup.find_all(style=True):
        if _HIDDEN_STYLE_RE.search(tag.get("style", "")):
            tag.decompose()

    # A common Outlook/Litmus convention for hiding preview text.
    for tag in soup.find_all(class_=re.compile(r"preheader|hidden|display-none", re.IGNORECASE)):
        tag.decompose()


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

    _strip_hidden(soup)

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
    r"^\s*On\s+.{6,200}\s+wrote:\s*$",
    # Outlook / Exchange style block headers
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
    r"^\s*_{5,}\s*$",
    r"^\s*-{10,}\s*$",
    r"^\s*From:\s*.+$",              # Outlook forwarded/replied header block
    r"^\s*Sent:\s*.+$",
    # Gmail forward marker
    r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
    # Apple Mail
    r"^\s*Begin forwarded message:\s*$",
    # Generic "wrote:" fallback with an email address on the line
    r"^\s*.{0,120}<[^>]+@[^>]+>\s*wrote:\s*$",
    # Bare continuation of a wrapped header (see _wrapped_quote_header_index)
    r"^\s*wrote:\s*$",
]

QUOTE_HEADER_RE = re.compile("|".join(QUOTE_HEADERS), re.IGNORECASE | re.MULTILINE)

# Gmail hard-wraps long lines around 78 chars, so the attribution header
# frequently splits:
#     On Fri, Jul 10, 2026 at 5:01 PM Anshul Tripathi <a@palantir.com>
#     wrote:
# Neither half matches a single-line pattern, so look ahead a couple of lines.
_ON_LINE_RE = re.compile(r"^\s*On\s+\w", re.IGNORECASE)
_WROTE_RE = re.compile(r"^\s*.{0,200}wrote:\s*$", re.IGNORECASE)


def _wrapped_quote_header_index(lines: list[str]) -> int | None:
    """Index of an 'On ... wrote:' header that wraps across up to 3 lines."""
    for i, line in enumerate(lines):
        if not _ON_LINE_RE.match(line):
            continue
        # "wrote:" may land on this line or the next couple.
        for j in range(i, min(i + 3, len(lines))):
            if _WROTE_RE.match(lines[j]):
                return i
    return None


def strip_quoted(text: str) -> tuple[str, bool]:
    """Remove quoted history. Returns (clean_text, found_quote).

    Boundary is whichever of these appears first:
      - an explicit quote header ("On ... wrote:", "-----Original Message-----")
      - a wrapped "On ... / wrote:" header split across lines
      - a run of '>' prefixed lines, the classic quote marker

    Any stray '>' lines surviving that cut are then dropped as a safety net:
    a line beginning with '>' in an email body is quoted material by
    convention, so this catches boundaries the patterns above missed.
    """
    if not text:
        return "", False

    lines = text.split("\n")
    candidates = []

    # Signal 1: explicit single-line quote header.
    for i, line in enumerate(lines):
        if QUOTE_HEADER_RE.match(line):
            candidates.append(i)
            break

    # Signal 2: header wrapped across lines.
    wrapped = _wrapped_quote_header_index(lines)
    if wrapped is not None:
        candidates.append(wrapped)

    # Signal 3: sustained '>' quoting. Require two in a row so a single
    # stray '>' inside prose isn't mistaken for a quote boundary.
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith(">") and lines[i + 1].lstrip().startswith(">"):
            candidates.append(i)
            break

    found = bool(candidates)
    if candidates:
        lines = lines[: min(candidates)]

    # Safety net: drop any remaining quoted lines.
    remaining = [ln for ln in lines if not ln.lstrip().startswith(">")]
    if len(remaining) != len(lines):
        found = True

    return "\n".join(remaining).strip(), found


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
    # Image alt-text left behind when a signature logo is flattened to text
    r"^\s*\[.*Description automatically generated.*\]\s*$",
    r"^\s*\[image:.*\]\s*$",
    r"^\s*\[cid:.*\]\s*$",
]

SIGNATURE_RE = re.compile("|".join(SIGNATURE_MARKERS), re.IGNORECASE | re.MULTILINE)

# Corporate signatures often carry no delimiter and no sign-off word - they
# just start. Detect them structurally instead, by the contact-detail lines
# they almost always contain.
CONTACT_LINE_RE = re.compile(
    r"^\s*("
    r"T:\s*\+?[\d\s()\-\.]{7,}"                    # T: +1 (215)-713-7516
    r"|M:\s*\+?[\d\s()\-\.]{7,}"
    r"|E:\s*\S+@\S+"                                # E: someone@example.com
    r"|P:\s*\+?[\d\s()\-\.]{7,}"
    r"|Tel:?\s*\+?[\d\s()\-\.]{7,}"
    r"|Mobile:?\s*\+?[\d\s()\-\.]{7,}"
    r"|Phone:?\s*\+?[\d\s()\-\.]{7,}"
    r"|\+\d[\d\s()\-\.]{9,}\s*$"                    # bare international number
    r"|(www\.|https?://)\S+\s*$"                    # bare URL line
    r")",
    re.IGNORECASE,
)

# Job-title / org lines that typically sit directly under a name in a sig.
TITLE_LINE_RE = re.compile(
    r"^\s*[\w\s\.\-&,']{3,60}"
    r"(Engineer|Developer|Manager|Director|Officer|President|Analyst|Consultant"
    r"|Architect|Scientist|Designer|Partner|Associate|Lead|Head of|VP|CEO|CTO|CFO|COO"
    r"|Intern|Specialist|Administrator|Coordinator|Executive|Founder)"
    r"[\w\s\.\-&,'|]*$",
    re.IGNORECASE,
)


def _structural_signature_index(lines: list[str], tail_window: int = 14) -> int | None:
    """Find where a delimiter-less signature block starts.

    Looks in the tail for a contact-detail line (phone/email/URL), then walks
    upward past title and short name-like lines to find the true start.
    """
    search_start = max(0, len(lines) - tail_window)

    anchor = None
    for i in range(search_start, len(lines)):
        if CONTACT_LINE_RE.match(lines[i]):
            anchor = i
            break

    if anchor is None:
        return None

    # Walk up while lines still look like signature material: a job title, a
    # short line with no sentence punctuation (a name or org), or blank.
    start = anchor
    for i in range(anchor - 1, max(0, anchor - 6) - 1, -1):
        line = lines[i].strip()
        if not line:
            continue
        looks_like_sig = (
            TITLE_LINE_RE.match(line)
            or CONTACT_LINE_RE.match(line)
            or (len(line) < 50 and not re.search(r"[.!?]$", line) and line.count(" ") <= 6)
        )
        if looks_like_sig:
            start = i
        else:
            break

    return start

# Legal/unsubscribe boilerplate that shows up at the bottom of bulk mail,
# plus the corporate disclaimers enterprise mail servers append.
BOILERPLATE_RE = re.compile(
    r"^\s*\*?\s*(This (e-?mail|message) (and any attachments )?(is|are) (confidential|intended)"
    r"|CONFIDENTIALITY NOTICE"
    r"|CAUTION:\s*This email originates"
    r"|EXTERNAL(:| EMAIL)"
    r"|My inbox is not an approved location"
    r"|If you (are not the intended recipient|no longer wish to receive|believe this message)"
    r"|To unsubscribe"
    r"|Unsubscribe\b"
    r"|You are receiving this (e-?mail|message) because"
    r"|View this email in your browser"
    r"|Manage your (notification )?preferences"
    r"|\u00a9\s*\d{4}\b).*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_signature(text: str, max_tail_lines: int = 14, sender_name: str | None = None) -> tuple[str, bool]:
    """Remove a trailing signature block. Returns (clean_text, found_sig).

    Combines three detectors, taking whichever cuts earliest (but still in
    the tail region):
      - explicit markers ("--", "Best regards,", "Sent from my iPhone")
      - structural detection for delimiter-less corporate signatures
      - the sender's own name left dangling on the last line

    Only the tail is searched, so a sign-off like "Thanks," in the middle of
    a message stays as content.
    """
    if not text:
        return "", False

    lines = text.split("\n")
    search_start = max(0, len(lines) - max_tail_lines)
    candidates = []

    for i in range(search_start, len(lines)):
        if SIGNATURE_RE.match(lines[i]):
            candidates.append(i)
            break

    structural = _structural_signature_index(lines, max_tail_lines)
    if structural is not None:
        candidates.append(structural)

    if candidates:
        lines = lines[: min(candidates)]

    found = bool(candidates)

    # A bare "Yash Patel" on the final line is a sign-off, not content.
    if sender_name:
        name = sender_name.strip().lower()
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().lower() == name:
            lines.pop()
            found = True

    return "\n".join(lines).strip(), found


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

def clean_message(body_text: str, body_html: str, sender_name: str | None = None) -> dict:
    """Produce clean text for one message, plus what was removed and why."""
    source = "text"
    raw = (body_text or "").strip()

    # Fall back to HTML when there's no usable plain-text part.
    if not raw and body_html:
        raw = html_to_text(body_html)
        source = "html"

    original_len = len(raw)

    text, had_quote = strip_quoted(raw)

    # Signature and boilerplate interleave (signature, then PHI disclaimer,
    # then more signature), so run the pair twice to peel both layers.
    text, had_boiler = strip_boilerplate(text)
    text, had_sig = strip_signature(text, sender_name=sender_name)
    text, boiler2 = strip_boilerplate(text)
    text, sig2 = strip_signature(text, sender_name=sender_name)

    had_boiler = had_boiler or boiler2
    had_sig = had_sig or sig2

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
    sql = f"SELECT id, body_text, body_html, from_name FROM messages {where}"
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    print(f"Cleaning {len(rows):,} messages...")

    for i, row in enumerate(rows, 1):
        result = clean_message(row["body_text"], row["body_html"], row["from_name"])

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
