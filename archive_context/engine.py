from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .expansion import build_episode
from .intent import generate_variants, interpret_query
from .models import DEPTH_CONFIGS
from .packing import pack_result, select_episodes
from .retrieval import aggregate_turn_candidates, generate_chunk_candidates, temporally_order
from .runtime import ArchiveRuntime


def _date_bound(value: str | None, end: bool = False) -> float | None:
    if value is None:
        return None
    parsed = date.fromisoformat(value)
    if end:
        parsed += timedelta(days=1)
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc).timestamp()


class ContextEngine:
    def __init__(self, runtime: ArchiveRuntime) -> None:
        self.runtime = runtime

    def context(
        self,
        query: str,
        depth: str = "medium",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        if depth not in DEPTH_CONFIGS:
            raise ValueError("depth must be light, medium, or deep")
        if not query or not query.strip():
            raise ValueError("query must not be blank")
        start = _date_bound(date_from)
        end = _date_bound(date_to, end=True)
        if start is not None and end is not None and start >= end:
            raise ValueError("date_from must not be after date_to")
        config = DEPTH_CONFIGS[depth]
        intent = interpret_query(query)
        variants = generate_variants(query, intent)
        conn = self.runtime.connect()
        try:
            chunks, counts = generate_chunk_candidates(
                conn, self.runtime, query, intent, variants, config, start, end
            )
            turns = aggregate_turn_candidates(chunks, config.turn_limit)
            turns = temporally_order(turns, intent)
            pool_limit = min(len(turns), config.episode_limit * 4)
            episodes = [
                build_episode(conn, candidate, intent, expanded=index < config.expansion_seeds)
                for index, candidate in enumerate(turns[:pool_limit])
            ]
        finally:
            conn.close()
        selected, selection_trace = select_episodes(episodes, config, intent)
        trace = {
            "candidate_counts": {**counts, "turns": len(turns), "episode_pool": len(episodes)},
            **selection_trace,
        }
        return pack_result(intent, variants, selected, config, trace)
