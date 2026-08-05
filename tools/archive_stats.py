from __future__ import annotations

import argparse
import json
from pathlib import Path

from archive_lib import configure_console, connect_db


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Show archive database counts and date range.")
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    args = parser.parse_args()
    conn = connect_db(args.db, readonly=True)
    try:
        counts = {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "active_messages": conn.execute("SELECT COUNT(*) FROM messages WHERE is_active=1").fetchone()[0],
            "user_messages": conn.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0],
            "assistant_messages": conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0],
            "turns": conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "user_chunks": conn.execute("SELECT COUNT(*) FROM chunks WHERE role='user'").fetchone()[0],
            "assistant_chunks": conn.execute("SELECT COUNT(*) FROM chunks WHERE role='assistant'").fetchone()[0],
        }
        date_range = conn.execute("SELECT MIN(create_time), MAX(create_time) FROM messages WHERE create_time IS NOT NULL").fetchone()
        counts["first_timestamp"] = date_range[0]
        counts["last_timestamp"] = date_range[1]
        counts["accepted_turns"] = conn.execute("SELECT COUNT(*) FROM turns WHERE acceptance='accepted'").fetchone()[0]
        counts["rejected_turns"] = conn.execute("SELECT COUNT(*) FROM turns WHERE acceptance='rejected'").fetchone()[0]
    finally:
        conn.close()
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
