from __future__ import annotations

import argparse
from pathlib import Path

from archive_lib import configure_console, connect_db, fetch_turn_window, source_uri, timestamp_iso


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open full surrounding turns from an archive search result.")
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--turn", required=True, type=int)
    parser.add_argument("--before", type=int, default=2)
    parser.add_argument("--after", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    configure_console()
    args = parse_args()
    conn = connect_db(args.db, readonly=True)
    try:
        rows = fetch_turn_window(
            conn, args.conversation_id, args.turn, before=args.before, after=args.after
        )
    finally:
        conn.close()
    if not rows:
        raise SystemExit("No matching conversation/turn found.")
    print(f"# {rows[0]['title']}")
    print(f"Conversation: `{args.conversation_id}`\n")
    for row in rows:
        marker = " ← matched" if row["turn_index"] == args.turn else ""
        print(f"## Turn {row['turn_index']} — {timestamp_iso(row['create_time'])}{marker}")
        print(f"Source: `{source_uri(args.conversation_id, row['turn_index'])}`")
        print(f"Status of preceding assistant draft: `{row['acceptance']}`\n")
        if row["user_text"]:
            print("### User\n")
            print(row["user_text"])
            print()
        if row["assistant_text"]:
            print("### Assistant\n")
            print(row["assistant_text"])
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
