from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from archive_lib import (
    aggregate_turn_hits,
    configure_console,
    connect_db,
    diversify_timeline,
    fetch_turn_window,
    fuse_ranked_hits,
    lexical_search,
    semantic_search,
    shorten,
    source_uri,
    timestamp_iso,
)


def parse_date(value: str | None, end: bool = False) -> float | None:
    if not value:
        return None
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.timestamp()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search the local ChatGPT archive.")
    parser.add_argument("query", nargs="?", help="Natural-language or exact search query")
    parser.add_argument("--query", dest="query_flag", help="Natural-language or exact search query")
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    parser.add_argument("--semantic-dir", type=Path, default=Path("index/semantic"))
    parser.add_argument("--lexical-only", action="store_true")
    parser.add_argument("--semantic-only", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--candidate-limit", type=int, default=250)
    parser.add_argument("--from", dest="date_from")
    parser.add_argument("--to", dest="date_to")
    parser.add_argument("--conversation-id")
    parser.add_argument("--timeline", action="store_true", help="Diversify results across calendar quarters")
    parser.add_argument(
        "--order", choices=("auto", "relevance", "earliest", "latest"), default="auto",
        help="Sort relevant candidates by relevance or time; auto recognizes first/latest questions.",
    )
    parser.add_argument("--context", type=int, default=0, help="Include N turns before and after each hit")
    parser.add_argument("--max-chars", type=int, default=1800)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    configure_console()
    args = parse_args()
    query = args.query_flag or args.query
    if not query:
        raise SystemExit("A search query is required.")
    conn = connect_db(args.db, readonly=True)
    try:
        candidate_limit = max(args.candidate_limit, args.limit * 10)
        date_from = parse_date(args.date_from)
        date_to = parse_date(args.date_to, end=True)
        lexical_hits = [] if args.semantic_only else lexical_search(
            conn, query, candidate_limit=candidate_limit, date_from=date_from,
            date_to=date_to, conversation_id=args.conversation_id,
        )
        semantic_hits = []
        semantic_available = (args.semantic_dir / "manifest.json").exists()
        if not args.lexical_only and semantic_available:
            semantic_hits = semantic_search(
                conn, query, args.semantic_dir, candidate_limit=candidate_limit,
                date_from=date_from, date_to=date_to,
                conversation_id=args.conversation_id,
            )
        if semantic_hits:
            lexical_weight, semantic_weight = ((0.9, 1.4) if args.timeline else (1.6, 1.0))
            hits = fuse_ranked_hits(
                lexical_hits, semantic_hits,
                lexical_weight=lexical_weight, semantic_weight=semantic_weight,
            )
        else:
            hits = lexical_hits
        aggregated = aggregate_turn_hits(hits, max(args.limit * 25, 250))
        requested_order = args.order
        if requested_order == "auto":
            lowered = query.lower()
            if re.search(r"\b(first time|earliest|when did i (?:first )?start|first found|first began)\b", lowered):
                requested_order = "earliest"
            elif re.search(r"\b(latest|most recent|recently|last time)\b", lowered):
                requested_order = "latest"
            else:
                requested_order = "relevance"
        if args.timeline:
            selected = diversify_timeline(aggregated, args.limit)
        elif requested_order == "earliest":
            selected = sorted(
                (hit for hit in aggregated if hit.create_time is not None),
                key=lambda hit: hit.create_time,
            )[: args.limit]
        elif requested_order == "latest":
            selected = sorted(
                (hit for hit in aggregated if hit.create_time is not None),
                key=lambda hit: hit.create_time,
                reverse=True,
            )[: args.limit]
        else:
            selected = aggregated[: args.limit]

        records = []
        for rank, hit in enumerate(selected, start=1):
            window = fetch_turn_window(
                conn, hit.conversation_id, hit.turn_index, before=args.context, after=args.context
            )
            records.append(
                {
                    "rank": rank,
                    "score": round(hit.score, 8),
                    "date": timestamp_iso(hit.create_time),
                    "title": hit.title,
                    "conversation_id": hit.conversation_id,
                    "turn_index": hit.turn_index,
                    "matched_role": hit.role,
                    "matched_message_id": hit.message_id,
                    "matched_text": shorten(hit.text, args.max_chars),
                    "source": source_uri(hit.conversation_id, hit.turn_index),
                    "window": [
                        {
                            "turn_index": row["turn_index"],
                            "date": timestamp_iso(row["create_time"]),
                            "user": shorten(row["user_text"], args.max_chars),
                            "assistant": shorten(row["assistant_text"], args.max_chars),
                            "acceptance": row["acceptance"],
                        }
                        for row in window
                    ],
                }
            )
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"query": query, "count": len(records), "semantic": semantic_available and not args.lexical_only, "results": records}, indent=2, ensure_ascii=False))
        return 0

    print(f"# Archive search: {query}\n")
    print(f"Results: {len(records)}\n")
    for record in records:
        print(f"## {record['rank']}. {record['title']} — {record['date']}")
        print(f"Source: `{record['source']}`")
        print(f"Matched {record['matched_role']} message `{record['matched_message_id']}`\n")
        print(record["matched_text"] or "[no extracted text]")
        if record["window"]:
            print("\nContext:")
            for turn in record["window"]:
                print(f"\n- Turn {turn['turn_index']} ({turn['date']}, {turn['acceptance']})")
                if turn["user"]:
                    print(f"  - User: {turn['user']}")
                if turn["assistant"]:
                    print(f"  - Assistant: {turn['assistant']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
