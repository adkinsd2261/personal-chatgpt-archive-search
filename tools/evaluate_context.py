from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from archive_context import ArchiveRuntime, ContextEngine


DEFAULT_CASES = Path("evals/context_gold.local.json")
HASHED_INPUTS = (
    Path("index/archive.sqlite"),
    Path("index/semantic/vectors.npy"),
    Path("index/semantic/chunk_ids.npy"),
    Path("index/semantic/manifest.json"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def input_hashes(paths: Iterable[Path] = HASHED_INPUTS) -> dict[str, str]:
    return {path.as_posix(): sha256_file(path) for path in paths}


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent * len(ordered)) - 1))
    return round(ordered[index], 4)


def source_key(item: dict[str, Any]) -> tuple[str, int]:
    return item["conversation_id"], int(item["turn_index"])


def evaluate_case(engine: ContextEngine, case: dict[str, Any], repeat: int) -> tuple[dict[str, Any], list[float]]:
    query = case["query"]
    depth = case.get("depth", "medium")
    outputs: list[dict[str, Any]] = []
    latencies: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        outputs.append(
            engine.context(
                query,
                depth,
                case.get("date_from"),
                case.get("date_to"),
            )
        )
        latencies.append(time.perf_counter() - started)
    packet = outputs[0]
    sources = {source_key(episode): episode["rank"] for episode in packet["episodes"]}
    expected = [
        (item["conversation_id"], int(item["turn_index"]))
        for item in case.get("expected_sources", [])
    ]
    ranks = [sources.get(item) for item in expected]
    found_ranks = [rank for rank in ranks if rank is not None]
    best_rank = min(found_ranks) if found_ranks else None
    primary_roles = [episode["primary_evidence"]["role"] for episode in packet["episodes"]]
    possible_rejected_context = [
        item
        for episode in packet["episodes"]
        for item in episode["context"]
        if item.get("role") == "assistant"
        and (item.get("relation") == "preceding_assistant" or "rejected" in item.get("status", ""))
    ]
    rejected_leakage = [
        item for item in possible_rejected_context if "rejected" not in item.get("status", "")
    ]
    normalized = [" ".join(episode["primary_evidence"]["text"].casefold().split()) for episode in packet["episodes"]]
    duplicate_count = sum(count - 1 for count in Counter(normalized).values() if count > 1)
    dates = [episode["date_utc"] for episode in packet["episodes"] if episode.get("date_utc")]
    quarters = {f"{value[:4]}-Q{((int(value[5:7]) - 1) // 3) + 1}" for value in dates}
    result = {
        "name": case["name"],
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "tags": case.get("tags", []),
        "depth": depth,
        "expected_source_count": len(expected),
        "expected_ranks": ranks,
        "best_expected_rank": best_rank,
        "recall_at_5": bool(expected) and any(rank is not None and rank <= 5 for rank in ranks),
        "recall_at_10": bool(expected) and any(rank is not None and rank <= 10 for rank in ranks),
        "reciprocal_rank": round(1.0 / best_rank, 6) if best_rank else 0.0,
        "baseline_rank": case.get("baseline_rank"),
        "episode_count": len(packet["episodes"]),
        "user_primary_precision": round(primary_roles.count("user") / len(primary_roles), 6) if primary_roles else 1.0,
        "rejected_assistant_context_count": len(possible_rejected_context),
        "rejected_assistant_leakage_count": len(rejected_leakage),
        "selected_exact_duplicate_rate": round(duplicate_count / len(normalized), 6) if normalized else 0.0,
        "conversation_count": len({episode["conversation_id"] for episode in packet["episodes"]}),
        "conversation_diversity": round(
            len({episode["conversation_id"] for episode in packet["episodes"]}) / len(packet["episodes"]), 6
        ) if packet["episodes"] else 1.0,
        "temporal_quarter_count": len(quarters),
        "serialized_characters": packet["limits"]["serialized_characters"],
        "character_limit": packet["limits"]["character_limit"],
        "budget_passed": packet["limits"]["serialized_characters"] <= packet["limits"]["character_limit"],
        "deterministic": all(output == packet for output in outputs[1:]),
        "latency_seconds": [round(value, 4) for value in latencies],
    }
    return result, latencies


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the deterministic context engine against ignored local cases.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--only", help="Run one case by its local name.")
    parser.add_argument("--skip-input-hashes", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.repeat <= 10):
        parser.error("--repeat must be between 1 and 10")
    if not args.cases.exists():
        parser.error(f"local case file not found: {args.cases}")
    case_document = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = case_document.get("cases")
    if not isinstance(cases, list) or not cases:
        parser.error("case file must contain a non-empty cases array")
    if args.only:
        cases = [case for case in cases if case.get("name") == args.only]
        if not cases:
            parser.error(f"case not found: {args.only}")

    hashes_before = {} if args.skip_input_hashes else input_hashes()
    cold_started = time.perf_counter()
    runtime = ArchiveRuntime()
    engine = ContextEngine(runtime)
    runtime_load_seconds = time.perf_counter() - cold_started
    case_results: list[dict[str, Any]] = []
    warm_latencies: list[float] = []
    for index, case in enumerate(cases, start=1):
        print(f"evaluating_case={index}/{len(cases)} name={case['name']}", file=sys.stderr, flush=True)
        result, latencies = evaluate_case(engine, case, args.repeat)
        case_results.append(result)
        warm_latencies.extend(latencies[1:])
    hashes_after = {} if args.skip_input_hashes else input_hashes()
    hashes_unchanged = None if args.skip_input_hashes else hashes_before == hashes_after
    expected_cases = [case for case in case_results if case["expected_source_count"]]
    report = {
        "schema_version": 1,
        "case_file": args.cases.as_posix(),
        "case_count": len(case_results),
        "repeat_count": args.repeat,
        "summary": {
            "recall_at_5": round(sum(case["recall_at_5"] for case in expected_cases) / len(expected_cases), 6) if expected_cases else None,
            "recall_at_10": round(sum(case["recall_at_10"] for case in expected_cases) / len(expected_cases), 6) if expected_cases else None,
            "mean_reciprocal_rank": round(sum(case["reciprocal_rank"] for case in expected_cases) / len(expected_cases), 6) if expected_cases else None,
            "user_primary_precision": round(sum(case["user_primary_precision"] for case in case_results) / len(case_results), 6),
            "rejected_assistant_leakage_count": sum(case["rejected_assistant_leakage_count"] for case in case_results),
            "mean_conversation_diversity": round(sum(case["conversation_diversity"] for case in case_results) / len(case_results), 6),
            "all_budgets_passed": all(case["budget_passed"] for case in case_results),
            "all_deterministic": all(case["deterministic"] for case in case_results),
            "runtime_load_seconds": round(runtime_load_seconds, 4),
            "first_request_seconds": case_results[0]["latency_seconds"][0],
            "cold_runtime_plus_first_request_seconds": round(runtime_load_seconds + case_results[0]["latency_seconds"][0], 4),
            "warm_p50_seconds": percentile(warm_latencies, 0.50),
            "warm_p95_seconds": percentile(warm_latencies, 0.95),
            "input_hashes_unchanged": hashes_unchanged,
        },
        "cases": case_results,
        "input_hashes_before": hashes_before,
        "input_hashes_after": hashes_after,
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    passed = (
        report["summary"]["all_budgets_passed"]
        and report["summary"]["all_deterministic"]
        and report["summary"]["input_hashes_unchanged"] is not False
        and report["summary"]["rejected_assistant_leakage_count"] == 0
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
