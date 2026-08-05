from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1
ALGORITHM_VERSION = "archive-context-v1"


@dataclass(frozen=True)
class DepthConfig:
    name: str
    episode_limit: int
    character_limit: int
    candidate_limit: int
    turn_limit: int
    expansion_seeds: int
    conversation_cap: int


DEPTH_CONFIGS = {
    "light": DepthConfig("light", 5, 9_000, 160, 80, 3, 2),
    "medium": DepthConfig("medium", 10, 18_000, 300, 160, 5, 2),
    "deep": DepthConfig("deep", 15, 30_000, 500, 260, 8, 3),
}


@dataclass(frozen=True)
class QueryIntent:
    primary_mode: str
    flags: tuple[str, ...]
    normalized_query: str
    quoted_phrases: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"primary_mode": self.primary_mode, "flags": list(self.flags)}


@dataclass(frozen=True)
class QueryVariant:
    kind: str
    text: str
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "weight": self.weight}


@dataclass
class ChunkCandidate:
    chunk_id: int
    conversation_id: str
    turn_index: int
    message_id: str
    role: str
    title: str
    create_time: float | None
    text: str
    user_text: str
    assistant_text: str
    acceptance: str
    previous_acceptance: str
    lexical_matches: list[tuple[str, float, int]] = field(default_factory=list)
    semantic_similarity: float = 0.0
    semantic_rank: int | None = None
    correction_signal: float = 0.0
    score: float = 0.0
    score_components: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnCandidate:
    conversation_id: str
    turn_index: int
    title: str
    create_time: float | None
    matched_role: str
    matched_message_id: str
    matched_text: str
    user_text: str
    assistant_text: str
    acceptance: str
    previous_acceptance: str
    chunk_ids: list[int]
    score: float
    score_components: list[dict[str, Any]]
    correction_signal: float = 0.0

    @property
    def stable_chunk_id(self) -> int:
        return min(self.chunk_ids) if self.chunk_ids else 0
