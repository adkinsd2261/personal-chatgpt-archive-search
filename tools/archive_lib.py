from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote


SCHEMA_VERSION = 1
DEFAULT_CHUNK_CHARS = 2400
DEFAULT_CHUNK_OVERLAP = 240

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "hers", "him", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "me", "my", "of", "on", "or", "our", "she", "so",
    "that", "the", "their", "them", "there", "they", "this", "to", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "with", "you", "your",
}

ACCEPT_RE = re.compile(
    r"\b(this is (?:it|tuff|tough|perfect)|you nailed it|nailed it|exactly|"
    r"yeah(?:,| )? this (?:is|it)|we(?:'| a)?re good|this works|fire|hard)\b",
    re.IGNORECASE,
)
REJECT_RE = re.compile(
    r"^\s*(nah|nope|no\b|not it\b|wrong\b)|\b(doesn(?:'|’)t work|"
    r"isn(?:'|’)t it|you missed|lacking|losing the emotion|flow(?:'| i)s everywhere)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    conversation_id: str
    turn_index: int
    message_id: str
    role: str
    title: str
    create_time: float | None
    text: str
    score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None


def connect_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path = path.resolve()
    if readonly:
        encoded = quote(path.as_posix(), safe="/:")
        conn = sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -200000")
    return conn


def configure_console() -> None:
    """Keep Windows consoles from failing on archive punctuation or Unicode."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS chunks_fts;
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS turns;
        DROP TABLE IF EXISTS messages;
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS meta;

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            create_time REAL,
            update_time REAL,
            current_node TEXT,
            default_model_slug TEXT,
            is_archived INTEGER NOT NULL DEFAULT 0,
            is_starred INTEGER NOT NULL DEFAULT 0,
            source_file TEXT NOT NULL,
            active_message_count INTEGER NOT NULL DEFAULT 0,
            total_message_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE messages (
            conversation_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            parent_id TEXT,
            active_position INTEGER,
            is_active INTEGER NOT NULL,
            role TEXT NOT NULL,
            author_name TEXT,
            create_time REAL,
            content_type TEXT,
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_file TEXT NOT NULL,
            metadata_json TEXT,
            PRIMARY KEY (conversation_id, message_id),
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE INDEX idx_messages_conversation_position
            ON messages(conversation_id, is_active, active_position);
        CREATE INDEX idx_messages_role_time ON messages(role, create_time);
        CREATE INDEX idx_messages_hash ON messages(content_hash);

        CREATE TABLE turns (
            conversation_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            create_time REAL,
            user_message_ids TEXT NOT NULL,
            assistant_message_ids TEXT NOT NULL,
            user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL,
            acceptance TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (conversation_id, turn_index),
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE INDEX idx_turns_time ON turns(create_time);

        CREATE TABLE chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            create_time REAL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            FOREIGN KEY (conversation_id, turn_index)
                REFERENCES turns(conversation_id, turn_index)
        );

        CREATE INDEX idx_chunks_turn ON chunks(conversation_id, turn_index);
        CREATE INDEX idx_chunks_time_role ON chunks(create_time, role);

        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED,
            conversation_id UNINDEXED,
            turn_index UNINDEXED,
            message_id UNINDEXED,
            role UNINDEXED,
            title,
            text,
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4'
        );
        """
    )
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [
            ("schema_version", str(SCHEMA_VERSION)),
            ("built_at", datetime.now(timezone.utc).isoformat()),
        ],
    )
    conn.commit()


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _collect_text(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 12 or value is None:
        return
    if isinstance(value, str):
        cleaned = clean_text(value)
        if cleaned:
            out.append(cleaned)
        return
    if isinstance(value, (int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, out, depth + 1)
        return
    if not isinstance(value, dict):
        return

    preferred = (
        "parts", "text", "content", "summary", "code", "output",
        "result", "caption", "transcript", "query", "response",
    )
    found = False
    for key in preferred:
        if key in value:
            found = True
            _collect_text(value[key], out, depth + 1)
    if found:
        return

    ignored = {
        "asset_pointer", "url", "domain", "content_type", "mime_type",
        "model_slug", "recipient", "status", "id", "name", "type",
    }
    for key, item in value.items():
        if key not in ignored:
            _collect_text(item, out, depth + 1)


def extract_content_text(content: Any) -> str:
    parts: list[str] = []
    _collect_text(content, parts)
    deduped: list[str] = []
    for part in parts:
        if not deduped or part != deduped[-1]:
            deduped.append(part)
    return clean_text("\n\n".join(deduped))


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def timestamp_iso(value: float | None) -> str:
    if value is None:
        return "unknown-date"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")


def active_path(mapping: dict[str, Any], current_node: str | None) -> list[str]:
    if not mapping:
        return []
    node_id = current_node if current_node in mapping else None
    if node_id is None:
        children = {node.get("parent") for node in mapping.values() if isinstance(node, dict)}
        leaves = [key for key in mapping if key not in children]
        candidates = leaves or list(mapping)
        node_id = max(
            candidates,
            key=lambda key: safe_float(((mapping.get(key) or {}).get("message") or {}).get("create_time")) or -1,
        )
    seen: set[str] = set()
    reverse_path: list[str] = []
    while node_id and node_id not in seen and node_id in mapping:
        seen.add(node_id)
        reverse_path.append(node_id)
        node = mapping.get(node_id) or {}
        node_id = node.get("parent")
    reverse_path.reverse()
    return reverse_path


def iter_conversation_files(raw_dir: Path) -> Iterator[Path]:
    yield from sorted(raw_dir.glob("conversations-*.json"))


def iter_conversations(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON list in {path}")
    for item in payload:
        if isinstance(item, dict):
            yield item


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        target = min(length, start + max_chars)
        end = target
        if target < length:
            lower = min(length, start + max_chars // 2)
            candidates = [
                text.rfind("\n\n", lower, target),
                text.rfind("\n", lower, target),
                text.rfind(". ", lower, target),
                text.rfind(" ", lower, target),
            ]
            end = max(candidates)
            if end < lower:
                end = target
            elif text[end:end + 2] == ". ":
                end += 1
        chunk = clean_text(text[start:end])
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return chunks


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def infer_acceptance(next_user_text: str) -> str:
    sample = clean_text(next_user_text)[:1000]
    if not sample:
        return "unknown"
    if REJECT_RE.search(sample):
        return "rejected"
    if ACCEPT_RE.search(sample):
        return "accepted"
    return "unknown"


def searchable_terms(query: str) -> list[str]:
    tokens = re.findall(r"[\w’']+", query.lower(), flags=re.UNICODE)
    meaningful = [token.strip("’'") for token in tokens]
    meaningful = [token for token in meaningful if len(token) > 1 and token not in STOP_WORDS]
    return list(dict.fromkeys(meaningful))


def fts_escape(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_fts_queries(query: str) -> list[tuple[str, float]]:
    terms = searchable_terms(query)
    if not terms:
        terms = [token for token in re.findall(r"\w+", query.lower()) if token]
    if not terms:
        return []
    queries: list[tuple[str, float]] = []
    generic_capitals = {
        "all", "can", "could", "did", "do", "find", "first", "how", "i",
        "latest", "most", "my", "tell", "the", "what", "when", "where",
        "which", "who", "why", "would",
    }
    anchors = [
        token for token in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", query)
        if token.lower() not in generic_capitals
    ]
    for anchor in dict.fromkeys(anchors):
        queries.append((fts_escape(anchor.lower()), 2.0))
    if 1 < len(terms) <= 10:
        queries.append((fts_escape(" ".join(terms)), 1.7))
    if len(terms) <= 8:
        queries.append((" AND ".join(fts_escape(term) for term in terms), 1.35))
    queries.append((" OR ".join(fts_escape(term) for term in terms), 1.0))
    return queries


def lexical_search(
    conn: sqlite3.Connection,
    query: str,
    candidate_limit: int = 200,
    date_from: float | None = None,
    date_to: float | None = None,
    conversation_id: str | None = None,
) -> list[SearchHit]:
    fused: dict[int, dict[str, Any]] = {}
    for fts_query, weight in build_fts_queries(query):
        conditions = ["chunks_fts MATCH ?"]
        params: list[Any] = [fts_query]
        if date_from is not None:
            conditions.append("c.create_time >= ?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("c.create_time <= ?")
            params.append(date_to)
        if conversation_id:
            conditions.append("c.conversation_id = ?")
            params.append(conversation_id)
        params.append(candidate_limit)
        sql = f"""
            SELECT c.chunk_id, c.conversation_id, c.turn_index, c.message_id,
                   c.role, c.title, c.create_time, c.text,
                   bm25(chunks_fts, 0, 0, 0, 0, 0, 2.0, 5.0) AS bm25_score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.rowid
            WHERE {' AND '.join(conditions)}
            ORDER BY bm25_score
            LIMIT ?
        """
        for rank, row in enumerate(conn.execute(sql, params), start=1):
            role_weight = 1.35 if row["role"] == "user" else 0.45
            rrf = weight * role_weight / (60.0 + rank)
            item = fused.setdefault(row["chunk_id"], {"row": row, "score": 0.0, "rank": rank})
            item["score"] += rrf
            item["rank"] = min(item["rank"], rank)
    hits = [
        SearchHit(
            chunk_id=chunk_id,
            conversation_id=item["row"]["conversation_id"],
            turn_index=item["row"]["turn_index"],
            message_id=item["row"]["message_id"],
            role=item["row"]["role"],
            title=item["row"]["title"],
            create_time=item["row"]["create_time"],
            text=item["row"]["text"],
            score=item["score"],
            lexical_rank=item["rank"],
        )
        for chunk_id, item in fused.items()
    ]
    hits.sort(key=lambda item: item.score, reverse=True)
    return hits


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    semantic_dir: Path,
    candidate_limit: int = 200,
    date_from: float | None = None,
    date_to: float | None = None,
    conversation_id: str | None = None,
) -> list[SearchHit]:
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError("Semantic search dependencies are not installed.") from exc

    semantic_dir = semantic_dir.resolve()
    manifest_path = semantic_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vectors = np.load(semantic_dir / "vectors.npy", mmap_mode="r")
    chunk_ids = np.load(semantic_dir / "chunk_ids.npy", mmap_mode="r")
    if vectors.shape[0] != chunk_ids.shape[0]:
        raise RuntimeError("Semantic vectors and chunk IDs have different lengths.")

    model = TextEmbedding(
        model_name=manifest["model"],
        cache_dir=str(semantic_dir / "model-cache"),
    )
    if hasattr(model, "query_embed"):
        query_vector = np.asarray(list(model.query_embed(query))[0], dtype=np.float32)
    else:
        query_vector = np.asarray(list(model.embed([query]))[0], dtype=np.float32)
    norm = float(np.linalg.norm(query_vector))
    if norm:
        query_vector /= norm
    scores = np.asarray(vectors @ query_vector)
    take = min(max(candidate_limit * 3, candidate_limit), scores.shape[0])
    if take == 0:
        return []
    indexes = np.argpartition(scores, -take)[-take:]
    indexes = indexes[np.argsort(scores[indexes])[::-1]]
    ranked_ids = [int(chunk_ids[index]) for index in indexes]
    score_by_id = {int(chunk_ids[index]): float(scores[index]) for index in indexes}

    placeholders = ",".join("?" for _ in ranked_ids)
    conditions = [f"chunk_id IN ({placeholders})"]
    params: list[Any] = list(ranked_ids)
    if date_from is not None:
        conditions.append("create_time >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("create_time <= ?")
        params.append(date_to)
    if conversation_id:
        conditions.append("conversation_id = ?")
        params.append(conversation_id)
    rows = conn.execute(
        f"""
        SELECT chunk_id, conversation_id, turn_index, message_id, role,
               title, create_time, text
        FROM chunks
        WHERE {' AND '.join(conditions)}
        """,
        params,
    ).fetchall()
    row_by_id = {row["chunk_id"]: row for row in rows}
    hits: list[SearchHit] = []
    for semantic_rank, chunk_id in enumerate(ranked_ids, start=1):
        row = row_by_id.get(chunk_id)
        if row is None:
            continue
        role_weight = 1.1 if row["role"] == "user" else 0.95
        hits.append(
            SearchHit(
                chunk_id=chunk_id,
                conversation_id=row["conversation_id"],
                turn_index=row["turn_index"],
                message_id=row["message_id"],
                role=row["role"],
                title=row["title"],
                create_time=row["create_time"],
                text=row["text"],
                score=score_by_id[chunk_id] * role_weight,
                semantic_rank=semantic_rank,
            )
        )
        if len(hits) >= candidate_limit:
            break
    return hits


def fuse_ranked_hits(
    lexical_hits: Sequence[SearchHit],
    semantic_hits: Sequence[SearchHit],
    lexical_weight: float = 1.0,
    semantic_weight: float = 1.0,
) -> list[SearchHit]:
    fused: dict[int, dict[str, Any]] = {}
    for label, hits, weight in (
        ("lexical", lexical_hits, lexical_weight),
        ("semantic", semantic_hits, semantic_weight),
    ):
        for rank, hit in enumerate(hits, start=1):
            item = fused.setdefault(
                hit.chunk_id,
                {"hit": hit, "score": 0.0, "lexical_rank": None, "semantic_rank": None},
            )
            evidence_weight = 1.15 if hit.role == "user" else 0.70
            item["score"] += weight * evidence_weight / (60.0 + rank)
            item[f"{label}_rank"] = rank
            if label == "lexical" or item["hit"] is None:
                item["hit"] = hit
    output = []
    for item in fused.values():
        hit = item["hit"]
        output.append(
            SearchHit(
                chunk_id=hit.chunk_id,
                conversation_id=hit.conversation_id,
                turn_index=hit.turn_index,
                message_id=hit.message_id,
                role=hit.role,
                title=hit.title,
                create_time=hit.create_time,
                text=hit.text,
                score=item["score"],
                lexical_rank=item["lexical_rank"],
                semantic_rank=item["semantic_rank"],
            )
        )
    output.sort(key=lambda item: item.score, reverse=True)
    return output


def aggregate_turn_hits(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
    by_turn: dict[tuple[str, int], SearchHit] = {}
    for hit in hits:
        key = (hit.conversation_id, hit.turn_index)
        previous = by_turn.get(key)
        if previous is None or hit.score > previous.score:
            by_turn[key] = hit
        elif previous:
            combined_score = previous.score + hit.score * 0.2
            by_turn[key] = SearchHit(**{**previous.__dict__, "score": combined_score})
    ranked = sorted(by_turn.values(), key=lambda item: item.score, reverse=True)
    return ranked[:limit]


def diversify_timeline(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
    buckets: dict[str, list[SearchHit]] = defaultdict(list)
    for hit in hits:
        if hit.create_time is None:
            bucket = "unknown"
        else:
            dt = datetime.fromtimestamp(hit.create_time, tz=timezone.utc)
            bucket = f"{dt.year}-Q{((dt.month - 1) // 3) + 1}"
        buckets[bucket].append(hit)
    ordered_buckets = sorted(buckets)
    result: list[SearchHit] = []
    index = 0
    while len(result) < limit:
        added = False
        for bucket in ordered_buckets:
            if index < len(buckets[bucket]):
                result.append(buckets[bucket][index])
                added = True
                if len(result) >= limit:
                    break
        if not added:
            break
        index += 1
    return result


def fetch_turn_window(
    conn: sqlite3.Connection,
    conversation_id: str,
    turn_index: int,
    before: int = 1,
    after: int = 1,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT t.*, c.title
            FROM turns t
            JOIN conversations c USING(conversation_id)
            WHERE t.conversation_id = ? AND t.turn_index BETWEEN ? AND ?
            ORDER BY t.turn_index
            """,
            (conversation_id, max(0, turn_index - before), turn_index + after),
        )
    )


def source_uri(conversation_id: str, turn_index: int) -> str:
    return f"archive://conversation/{conversation_id}/turn/{turn_index}"


def shorten(text: str, max_chars: int) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"
