from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from tools.archive_lib import fts_escape, searchable_terms

from .intent import classify_user_signal, normalize_for_match
from .models import ChunkCandidate, DepthConfig, QueryIntent, QueryVariant, TurnCandidate
from .runtime import ArchiveRuntime


PROFILES: dict[str, dict[str, float]] = {
    "recall": {"lexical": .28, "semantic": .34, "exact": .10, "title": .06, "user": .12, "correction": .04, "corroboration": .06, "current": 0.0},
    "exact": {"lexical": .30, "semantic": .12, "exact": .32, "title": .10, "user": .10, "correction": .03, "corroboration": .03, "current": 0.0},
    "decision": {"lexical": .24, "semantic": .29, "exact": .10, "title": .07, "user": .13, "correction": .08, "corroboration": .04, "current": .05},
    "correction": {"lexical": .22, "semantic": .22, "exact": .10, "title": .05, "user": .15, "correction": .15, "corroboration": .06, "current": .05},
}


def _profile_name(intent: QueryIntent) -> str:
    if intent.primary_mode == "correction":
        return "correction"
    if intent.primary_mode == "decision":
        return "decision"
    if "exact_phrase" in intent.flags:
        return "exact"
    return "recall"


ROW_COLUMNS = """
    c.chunk_id, c.conversation_id, c.turn_index, c.message_id,
    c.role, c.title, c.create_time, c.text,
    t.user_text, t.assistant_text, t.acceptance,
    COALESCE(p.acceptance, 'unknown') AS previous_acceptance
"""


def _row_sql(conditions: str) -> str:
    return f"""
        SELECT {ROW_COLUMNS}
        FROM chunks c
        JOIN turns t ON t.conversation_id=c.conversation_id AND t.turn_index=c.turn_index
        LEFT JOIN turns p ON p.conversation_id=c.conversation_id AND p.turn_index=c.turn_index-1
        WHERE {conditions}
    """


def _fts_expression(variant: QueryVariant) -> str | None:
    terms = searchable_terms(variant.text)
    if variant.kind in {"quoted_phrase", "identifier"}:
        return fts_escape(variant.text.casefold())
    if not terms:
        return None
    if variant.kind == "lexical_and":
        return " AND ".join(fts_escape(term) for term in terms)
    return " OR ".join(fts_escape(term) for term in terms)


def _candidate_from_row(row: Any) -> ChunkCandidate:
    return ChunkCandidate(
        chunk_id=int(row["chunk_id"]),
        conversation_id=row["conversation_id"],
        turn_index=int(row["turn_index"]),
        message_id=row["message_id"],
        role=row["role"],
        title=row["title"],
        create_time=row["create_time"],
        text=row["text"],
        user_text=row["user_text"],
        assistant_text=row["assistant_text"],
        acceptance=row["acceptance"],
        previous_acceptance=row["previous_acceptance"],
    )


def _date_conditions(date_from: float | None, date_to: float | None) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if date_from is not None:
        conditions.append("c.create_time >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("c.create_time < ?")
        params.append(date_to)
    return conditions, params


def generate_chunk_candidates(
    conn,
    runtime: ArchiveRuntime,
    query: str,
    intent: QueryIntent,
    variants: Sequence[QueryVariant],
    depth: DepthConfig,
    date_from: float | None,
    date_to: float | None,
) -> tuple[list[ChunkCandidate], dict[str, int]]:
    candidates: dict[int, ChunkCandidate] = {}
    lexical_rows = 0
    date_clauses, date_params = _date_conditions(date_from, date_to)
    for variant in variants:
        if variant.kind == "semantic_original":
            continue
        expression = _fts_expression(variant)
        if not expression:
            continue
        clauses = ["chunks_fts MATCH ?", *date_clauses]
        params: list[Any] = [expression, *date_params, depth.candidate_limit]
        if variant.kind == "temporal_earliest":
            order_clause = "c.create_time IS NULL, c.create_time, c.chunk_id"
        elif variant.kind == "temporal_latest":
            order_clause = "c.create_time IS NULL, c.create_time DESC, c.chunk_id"
        else:
            order_clause = "bm25_score, c.chunk_id"
        sql = f"""
            SELECT {ROW_COLUMNS},
                   bm25(chunks_fts, 0, 0, 0, 0, 0, 2.0, 5.0) AS bm25_score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id=chunks_fts.rowid
            JOIN turns t ON t.conversation_id=c.conversation_id AND t.turn_index=c.turn_index
            LEFT JOIN turns p ON p.conversation_id=c.conversation_id AND p.turn_index=c.turn_index-1
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_clause}
            LIMIT ?
        """
        for rank, row in enumerate(conn.execute(sql, params), start=1):
            lexical_rows += 1
            candidate = candidates.setdefault(int(row["chunk_id"]), _candidate_from_row(row))
            candidate.lexical_matches.append((variant.kind, variant.weight, rank))

    semantic = runtime.semantic_top(query, depth.candidate_limit)
    semantic_by_id = {chunk_id: (similarity, rank) for chunk_id, similarity, rank in semantic}
    missing = [chunk_id for chunk_id in semantic_by_id if chunk_id not in candidates]
    if missing:
        placeholders = ",".join("?" for _ in missing)
        clauses = [f"c.chunk_id IN ({placeholders})", *date_clauses]
        params = [*missing, *date_params]
        for row in conn.execute(_row_sql(" AND ".join(clauses)), params):
            candidates[int(row["chunk_id"])] = _candidate_from_row(row)
    for chunk_id, (similarity, rank) in semantic_by_id.items():
        candidate = candidates.get(chunk_id)
        if candidate is not None:
            candidate.semantic_similarity = similarity
            candidate.semantic_rank = rank

    output = list(candidates.values())
    _score_candidates(output, query, intent)
    output.sort(key=_chunk_sort_key)
    return output, {
        "lexical_rows": lexical_rows,
        "semantic_rows": len(semantic),
        "unique_chunks": len(output),
    }


def _lexical_strength(matches: Sequence[tuple[str, float, int]]) -> float:
    values = sorted((weight * (61.0 / (60.0 + rank)) for _, weight, rank in matches), reverse=True)
    if not values:
        return 0.0
    return min(1.0, values[0] + 0.12 * sum(values[1:3]))


def _exact_strength(candidate: ChunkCandidate, query: str, intent: QueryIntent) -> float:
    haystack = normalize_for_match(candidate.title + "\n" + candidate.text)
    if any(normalize_for_match(phrase) in haystack for phrase in intent.quoted_phrases):
        return 1.0
    if intent.identifiers and any(normalize_for_match(item) in haystack for item in intent.identifiers):
        return 0.75
    normalized_query = normalize_for_match(query)
    if intent.primary_mode == "exact" and 2 <= len(normalized_query.split()) <= 18 and normalized_query in haystack:
        return 0.9
    return 0.0


def _title_overlap(title: str, query: str) -> float:
    query_terms = set(searchable_terms(query))
    title_terms = set(searchable_terms(title))
    if not query_terms or not title_terms:
        return 0.0
    return len(query_terms & title_terms) / len(query_terms | title_terms)


def _score_candidates(candidates: list[ChunkCandidate], query: str, intent: QueryIntent) -> None:
    profile_name = _profile_name(intent)
    weights = PROFILES[profile_name]
    conversations: dict[str, set[int]] = defaultdict(set)
    dated = [candidate.create_time for candidate in candidates if candidate.create_time is not None]
    oldest, newest = (min(dated), max(dated)) if dated else (0.0, 0.0)
    for candidate in candidates:
        conversations[candidate.conversation_id].add(candidate.turn_index)
    for candidate in candidates:
        signal = classify_user_signal(candidate.user_text)
        if candidate.previous_acceptance == "rejected":
            correction = 1.0
        elif signal in {"possible_rejection", "possible_correction_or_refinement"}:
            correction = 0.75
        else:
            correction = 0.0
        candidate.correction_signal = correction
        corroborating_turns = len(conversations[candidate.conversation_id])
        corroboration = min(1.0, math.log2(1 + corroborating_turns) / 3.0)
        current = 0.0
        if candidate.create_time is not None and newest > oldest:
            current = (candidate.create_time - oldest) / (newest - oldest)
        raw = {
            "lexical": _lexical_strength(candidate.lexical_matches),
            "semantic": max(0.0, min(1.0, candidate.semantic_similarity)),
            "exact": _exact_strength(candidate, query, intent),
            "title": _title_overlap(candidate.title, query),
            "user": 1.0 if candidate.role == "user" else 0.0,
            "correction": correction,
            "corroboration": corroboration,
            "current": current if "current_state" in intent.flags else 0.0,
        }
        components: list[dict[str, Any]] = []
        total = 0.0
        for name in ("lexical", "semantic", "exact", "title", "user", "correction", "corroboration", "current"):
            contribution = round(raw[name] * weights[name], 8)
            total += contribution
            components.append({"name": name, "raw": round(raw[name], 8), "weight": weights[name], "contribution": contribution})
        if candidate.role == "assistant" and candidate.acceptance == "rejected":
            total -= 0.20
            components.append({"name": "rejected_assistant", "raw": 1.0, "weight": -0.20, "contribution": -0.20})
        elif candidate.role == "assistant" and candidate.acceptance == "accepted":
            total += 0.02
            components.append({"name": "possible_acceptance", "raw": 1.0, "weight": 0.02, "contribution": 0.02})
        candidate.score = round(max(0.0, min(1.0, total)), 8)
        candidate.score_components = components


def _chunk_sort_key(candidate: ChunkCandidate):
    exact = next((part["raw"] for part in candidate.score_components if part["name"] == "exact"), 0.0)
    date = candidate.create_time if candidate.create_time is not None else float("inf")
    return (-candidate.score, -exact, 0 if candidate.role == "user" else 1, date, candidate.conversation_id, candidate.turn_index, candidate.chunk_id)


def aggregate_turn_candidates(candidates: Sequence[ChunkCandidate], limit: int) -> list[TurnCandidate]:
    grouped: dict[tuple[str, int], list[ChunkCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.conversation_id, candidate.turn_index)].append(candidate)
    turns: list[TurnCandidate] = []
    for group in grouped.values():
        ordered = sorted(group, key=_chunk_sort_key)
        representative = ordered[0]
        support = round(min(0.08, sum(item.score for item in ordered[1:5]) * 0.03), 8)
        components = [*representative.score_components]
        if support:
            components.append({"name": "aggregation_support", "raw": round(sum(item.score for item in ordered[1:5]), 8), "weight": 0.03, "contribution": support})
        turns.append(
            TurnCandidate(
                conversation_id=representative.conversation_id,
                turn_index=representative.turn_index,
                title=representative.title,
                create_time=representative.create_time,
                matched_role=representative.role,
                matched_message_id=representative.message_id,
                matched_text=representative.text,
                user_text=representative.user_text,
                assistant_text=representative.assistant_text,
                acceptance=representative.acceptance,
                previous_acceptance=representative.previous_acceptance,
                chunk_ids=sorted(item.chunk_id for item in ordered),
                score=round(min(1.0, representative.score + support), 8),
                score_components=components,
                correction_signal=max(item.correction_signal for item in ordered),
            )
        )
    turns.sort(key=_turn_sort_key)
    return turns[:limit]


def _turn_sort_key(candidate: TurnCandidate):
    exact = next((part["raw"] for part in candidate.score_components if part["name"] == "exact"), 0.0)
    date = candidate.create_time if candidate.create_time is not None else float("inf")
    return (-candidate.score, -exact, 0 if candidate.matched_role == "user" else 1, date, candidate.conversation_id, candidate.turn_index, candidate.stable_chunk_id)


def temporally_order(turns: Sequence[TurnCandidate], intent: QueryIntent) -> list[TurnCandidate]:
    if not turns or intent.primary_mode not in {"earliest", "latest", "longitudinal"}:
        return list(turns)
    best = turns[0].score
    factor = 0.40 if intent.primary_mode == "longitudinal" else 0.55
    floor = max(0.25, best * factor)
    qualified = [turn for turn in turns if turn.score >= floor and turn.create_time is not None]
    if intent.primary_mode == "earliest":
        return sorted(qualified, key=lambda item: (item.create_time, -item.score, item.conversation_id, item.turn_index))
    if intent.primary_mode == "latest":
        return sorted(qualified, key=lambda item: (-float(item.create_time), -item.score, item.conversation_id, item.turn_index))

    buckets: dict[str, list[TurnCandidate]] = defaultdict(list)
    for turn in qualified:
        dt = datetime.fromtimestamp(float(turn.create_time), tz=timezone.utc)
        buckets[f"{dt.year}-Q{((dt.month - 1) // 3) + 1}"].append(turn)
    representatives = [sorted(buckets[key], key=_turn_sort_key)[0] for key in sorted(buckets)]
    chosen_keys = {(item.conversation_id, item.turn_index) for item in representatives}
    backfill = [item for item in qualified if (item.conversation_id, item.turn_index) not in chosen_keys]
    return representatives + backfill
