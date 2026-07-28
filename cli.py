#!/usr/bin/env python3
"""Command line interface for the Gmail ingest layer.

  python cli.py auth                     verify OAuth works
  python cli.py sync --full              first run: walk the whole mailbox
  python cli.py sync --full --limit 200  first run, capped (good for testing)
  python cli.py sync                     incremental: only what's new
  python cli.py stats                    what's in the local database
  python cli.py show <message_id>        dump one stored message
  python cli.py thread <thread_id>       dump a whole conversation
"""

import argparse
import json
import textwrap
from datetime import datetime

import auth
import db
import ingest
import normalize

# Reasonable default: recent mail, minus the obvious noise buckets.
DEFAULT_QUERY = "newer_than:90d -category:promotions -category:social"


def cmd_auth(args):
    service = auth.get_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as : {profile['emailAddress']}")
    print(f"Total messages   : {profile['messagesTotal']:,}")
    print(f"Current historyId: {profile['historyId']}")


def cmd_sync(args):
    service, credentials = auth.get_service_and_credentials()
    conn = db.connect()
    db.init_db(conn)

    if args.full:
        query = None if args.all_mail else (args.query or DEFAULT_QUERY)
        ingest.full_sync(service, conn, credentials, query=query, max_messages=args.limit)
    else:
        ingest.incremental_sync(service, conn, credentials)

    conn.close()


def cmd_stats(args):
    conn = db.connect()
    db.init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    threads = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM messages").fetchone()[0]

    print(f"Messages : {total:,}")
    print(f"Threads  : {threads:,}")

    if total:
        lo, hi = conn.execute(
            "SELECT MIN(internal_date), MAX(internal_date) FROM messages"
        ).fetchone()
        fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        print(f"Range    : {fmt(lo)} to {fmt(hi)}")

        print("\nTop senders:")
        rows = conn.execute(
            """
            SELECT from_email, COUNT(*) n FROM messages
            WHERE from_email IS NOT NULL
            GROUP BY from_email ORDER BY n DESC LIMIT 10
            """
        ).fetchall()
        for r in rows:
            print(f"  {r['n']:>5}  {r['from_email']}")

    last_sync = db.get_state(conn, "last_full_sync")
    history_id = db.get_state(conn, "last_history_id")
    print(f"\nLast full sync: {last_sync or 'never'}")
    print(f"historyId     : {history_id or 'none'}")

    conn.close()


def cmd_normalize(args):
    conn = db.connect()
    db.init_db(conn)

    stats = normalize.normalize_all(conn, limit=args.limit, rebuild=args.rebuild)

    before, after = stats["chars_before"], stats["chars_after"]
    pct = (1 - after / before) * 100 if before else 0

    print()
    print(f"Messages cleaned   : {stats['messages_cleaned']:,}")
    print(f"  from HTML body   : {stats['from_html']:,}")
    print(f"  had quoted reply : {stats['had_quote']:,}")
    print(f"  had signature    : {stats['had_signature']:,}")
    print(f"  had boilerplate  : {stats['had_boilerplate']:,}")
    print(f"  empty after clean: {stats['empty_after_clean']:,}")
    print(f"Threads built      : {stats['threads_built']:,}")
    print()
    print(f"Characters before  : {before:,}")
    print(f"Characters after   : {after:,}")
    print(f"Reduction          : {pct:.1f}%")

    conn.close()


def cmd_threads(args):
    """List threads, biggest conversations first - the ones worth inspecting."""
    conn = db.connect()
    db.init_db(conn)

    rows = conn.execute(
        """
        SELECT thread_id, subject, message_count, char_count, last_date
          FROM threads
         WHERE message_count >= ?
         ORDER BY message_count DESC, last_date DESC
         LIMIT ?
        """,
        (args.min_messages, args.limit),
    ).fetchall()

    if not rows:
        print("No threads found. Run 'python cli.py normalize' first.")
        return

    print(f"{'messages':>8}  {'chars':>7}  {'last':<11}  thread_id            subject")
    print("-" * 100)
    for r in rows:
        when = datetime.fromtimestamp(r["last_date"] / 1000).strftime("%Y-%m-%d")
        subject = (r["subject"] or "(no subject)")[:44]
        print(
            f"{r['message_count']:>8}  {r['char_count']:>7,}  {when:<11}  "
            f"{r['thread_id']:<20} {subject}"
        )

    conn.close()


def cmd_show(args):
    conn = db.connect()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (args.message_id,)).fetchone()

    if not row:
        print(f"No message {args.message_id} stored.")
        return

    when = datetime.fromtimestamp(row["internal_date"] / 1000)
    print(f"From    : {row['from_name']} <{row['from_email']}>")
    print(f"To      : {', '.join(json.loads(row['to_emails']))}")
    print(f"Date    : {when:%Y-%m-%d %H:%M}")
    print(f"Subject : {row['subject']}")
    print(f"Labels  : {', '.join(json.loads(row['label_ids']))}")
    print(f"Thread  : {row['thread_id']}")
    print("-" * 70)
    body = row["body_text"] or row["snippet"] or "(no text body)"
    print(body[: args.chars])
    if len(body) > args.chars:
        print(f"\n... [{len(body) - args.chars:,} more characters]")

    conn.close()


def cmd_thread(args):
    conn = db.connect()
    db.init_db(conn)

    # Default: show the cleaned transcript, which is what extraction will read.
    if not args.raw:
        row = conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (args.thread_id,)
        ).fetchone()

        if row:
            print(f"Thread {args.thread_id} - {row['message_count']} message(s)")
            print(f"Subject: {row['subject']}")
            print(f"Participants: {', '.join(json.loads(row['participants']))}")
            print(f"Transcript: {row['char_count']:,} chars")
            print("=" * 70)
            print(row["transcript"] or "(nothing left after cleaning)")
            conn.close()
            return

        print("(not normalized yet - showing raw. Run 'normalize' first.)\n")

    # --raw, or no transcript available: show original message bodies.
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY internal_date",
        (args.thread_id,),
    ).fetchall()

    if not rows:
        print(f"No thread {args.thread_id} stored.")
        return

    print(f"Thread {args.thread_id} - {len(rows)} message(s) [RAW]")
    print(f"Subject: {rows[0]['subject']}\n")

    for i, row in enumerate(rows, 1):
        when = datetime.fromtimestamp(row["internal_date"] / 1000)
        print("=" * 70)
        print(f"[{i}] {row['from_email']}  {when:%Y-%m-%d %H:%M}")
        print("=" * 70)
        body = (row["body_text"] or row["snippet"] or "").strip()
        print(textwrap.shorten(body, width=args.chars, placeholder=" ...") or "(empty)")
        print()

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Gmail ingest layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="verify OAuth works").set_defaults(func=cmd_auth)

    p_sync = sub.add_parser("sync", help="fetch messages")
    p_sync.add_argument("--full", action="store_true", help="full sync instead of incremental")
    p_sync.add_argument("--limit", type=int, help="cap number of messages (testing)")
    p_sync.add_argument("--query", help=f"Gmail search query (default: {DEFAULT_QUERY!r})")
    p_sync.add_argument("--all-mail", action="store_true", help="ignore default query, fetch everything")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("stats", help="local database summary").set_defaults(func=cmd_stats)

    p_norm = sub.add_parser("normalize", help="clean messages and build thread transcripts")
    p_norm.add_argument("--limit", type=int, help="cap messages processed (testing)")
    p_norm.add_argument("--rebuild", action="store_true", help="re-clean already-cleaned messages")
    p_norm.set_defaults(func=cmd_normalize)

    p_threads = sub.add_parser("threads", help="list threads, longest conversations first")
    p_threads.add_argument("--limit", type=int, default=20)
    p_threads.add_argument("--min-messages", type=int, default=2,
                           help="only threads with at least this many messages (default 2)")
    p_threads.set_defaults(func=cmd_threads)

    p_show = sub.add_parser("show", help="dump one message")
    p_show.add_argument("message_id")
    p_show.add_argument("--chars", type=int, default=2000)
    p_show.set_defaults(func=cmd_show)

    p_thread = sub.add_parser("thread", help="dump a conversation (cleaned transcript)")
    p_thread.add_argument("thread_id")
    p_thread.add_argument("--raw", action="store_true", help="show original bodies instead")
    p_thread.add_argument("--chars", type=int, default=600)
    p_thread.set_defaults(func=cmd_thread)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
