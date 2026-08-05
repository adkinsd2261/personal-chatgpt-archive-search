from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from archive_lib import (
    aggregate_turn_hits,
    configure_console,
    connect_db,
    fuse_ranked_hits,
    lexical_search,
    semantic_search,
)


EXPECTED_COUNTS = {
    "conversations": 3119,
    "user_messages": 54864,
    "assistant_messages": 67518,
}

CASES = [
    {
        "query": "Almost sold myself to God",
        "expected_title_contains": "Building rap skills",
    },
    {
        "query": "Crowley memory corpus",
        "expected_text_contains": "crowley",
    },
    {
        "query": "lost 90 pounds and had almost no clothes",
        "expected_text_contains": "90 pounds",
    },
    {
        "query": "survival reclaiming my identity and learning to live for myself",
        "expected_text_contains": "surviv",
    },
]


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Validate archive integrity and retrieval smoke tests.")
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    parser.add_argument("--semantic-dir", type=Path, default=Path("index/semantic"))
    parser.add_argument("--output", type=Path, default=Path("manifests/validation.json"))
    args = parser.parse_args()
    conn = connect_db(args.db, readonly=True)
    actual_counts = {
        "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "user_messages": conn.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0],
        "assistant_messages": conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0],
    }
    count_checks = {
        key: {"expected": expected, "actual": actual_counts[key], "passed": actual_counts[key] == expected}
        for key, expected in EXPECTED_COUNTS.items()
    }
    case_results = []
    semantic_ready = (args.semantic_dir / "manifest.json").exists()
    for case in CASES:
        lexical = lexical_search(conn, case["query"], candidate_limit=100)
        semantic = semantic_search(conn, case["query"], args.semantic_dir, candidate_limit=100) if semantic_ready else []
        fused = fuse_ranked_hits(lexical, semantic) if semantic else lexical
        hits = aggregate_turn_hits(fused, 10)
        joined_titles = "\n".join(hit.title for hit in hits).lower()
        joined_text = "\n".join(hit.text for hit in hits).lower()
        if "expected_title_contains" in case:
            needle = case["expected_title_contains"].lower()
            passed = needle in joined_titles
        else:
            needle = case["expected_text_contains"].lower()
            passed = needle in joined_text
        case_results.append(
            {
                "query": case["query"],
                "needle": needle,
                "passed": passed,
                "top_sources": [
                    {
                        "title": hit.title,
                        "conversation_id": hit.conversation_id,
                        "turn_index": hit.turn_index,
                        "role": hit.role,
                    }
                    for hit in hits[:5]
                ],
            }
        )
    conn.close()
    passed = all(item["passed"] for item in count_checks.values()) and all(
        item["passed"] for item in case_results
    )
    report = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "semantic_ready": semantic_ready,
        "counts": count_checks,
        "retrieval_cases": case_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

