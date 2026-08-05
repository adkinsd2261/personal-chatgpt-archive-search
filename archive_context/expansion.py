from __future__ import annotations

import json
from typing import Any

from tools.archive_lib import fetch_turn_window, shorten, source_uri, timestamp_iso

from .intent import classify_user_signal
from .models import QueryIntent, TurnCandidate


def _assistant_status(acceptance: str) -> str:
    if acceptance == "rejected":
        return "rejected_by_following_user_heuristic"
    if acceptance == "accepted":
        return "possibly_accepted_by_following_user"
    return "assistant_context_unverified"


def build_episode(conn, candidate: TurnCandidate, intent: QueryIntent, expanded: bool) -> dict[str, Any]:
    window = fetch_turn_window(conn, candidate.conversation_id, candidate.turn_index, before=1, after=1)
    rows = {int(row["turn_index"]): row for row in window}
    seed = rows[candidate.turn_index]
    previous = rows.get(candidate.turn_index - 1)
    following = rows.get(candidate.turn_index + 1)
    signals: list[dict[str, str]] = []
    context: list[dict[str, Any]] = []

    current_signal = classify_user_signal(seed["user_text"])
    if current_signal:
        signals.append({"kind": current_signal, "basis": "seed user language"})
    if previous is not None and previous["acceptance"] == "rejected":
        signals.append({"kind": "possible_correction_or_refinement", "basis": "preceding assistant was rejected by following user heuristic"})
        if expanded and previous["assistant_text"]:
            context.append(
                {
                    "relation": "preceding_assistant",
                    "role": "assistant",
                    "status": "rejected_context_unverified",
                    "turn_index": int(previous["turn_index"]),
                    "source_uri": source_uri(candidate.conversation_id, int(previous["turn_index"])),
                    "text": shorten(previous["assistant_text"], 1200),
                }
            )

    include_seed_assistant = expanded and bool(seed["assistant_text"]) and (
        candidate.matched_role == "assistant"
        or seed["acceptance"] != "unknown"
        or intent.primary_mode in {"correction", "decision"}
    )
    if include_seed_assistant:
        context.append(
            {
                "relation": "same_turn_assistant",
                "role": "assistant",
                "status": _assistant_status(seed["acceptance"]),
                "turn_index": candidate.turn_index,
                "source_uri": source_uri(candidate.conversation_id, candidate.turn_index),
                "text": shorten(seed["assistant_text"], 1200),
            }
        )

    if expanded and following is not None and seed["acceptance"] != "unknown" and following["user_text"]:
        follow_signal = classify_user_signal(following["user_text"])
        if follow_signal:
            signals.append({"kind": follow_signal, "basis": "following user language"})
        context.append(
            {
                "relation": "following_user",
                "role": "user",
                "status": follow_signal or "neighboring_user_evidence",
                "turn_index": int(following["turn_index"]),
                "source_uri": source_uri(candidate.conversation_id, int(following["turn_index"])),
                "text": shorten(following["user_text"], 1000),
            }
        )

    try:
        message_ids = json.loads(seed["user_message_ids"])
    except (TypeError, json.JSONDecodeError):
        message_ids = []
    return {
        "rank": 0,
        "source_uri": source_uri(candidate.conversation_id, candidate.turn_index),
        "conversation_id": candidate.conversation_id,
        "turn_index": candidate.turn_index,
        "title": candidate.title,
        "date_utc": timestamp_iso(candidate.create_time),
        "matched_role": candidate.matched_role,
        "primary_evidence": {
            "role": "user",
            "text": shorten(seed["user_text"], 2200),
            "message_ids": message_ids,
        },
        "context": context,
        "signals": signals,
        "score": {
            "profile": "correction" if intent.primary_mode == "correction" else ("decision" if intent.primary_mode == "decision" else ("exact" if "exact_phrase" in intent.flags else "recall")),
            "total": candidate.score,
            "components": candidate.score_components,
        },
        "contributing_chunk_ids": candidate.chunk_ids,
    }
