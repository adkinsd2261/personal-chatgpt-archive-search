from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archive_lib import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP,
    active_path,
    clean_text,
    connect_db,
    content_hash,
    configure_console,
    create_schema,
    extract_content_text,
    infer_acceptance,
    iter_conversation_files,
    iter_conversations,
    safe_float,
    split_text,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local ChatGPT archive search database.")
    parser.add_argument("--raw", type=Path, default=Path("raw/chatgpt-export"))
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    parser.add_argument("--manifest", type=Path, default=Path("manifests/index_manifest.json"))
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    return parser.parse_args()


def as_int_bool(value: Any) -> int:
    return 1 if bool(value) else 0


def message_record(
    conversation_id: str,
    node_id: str,
    node: dict[str, Any],
    is_active: bool,
    active_position: int | None,
    source_file: str,
) -> dict[str, Any] | None:
    message = node.get("message")
    if not isinstance(message, dict):
        return None
    message_id = str(message.get("id") or node_id)
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    role = clean_text(str(author.get("role") or "unknown")).lower() or "unknown"
    author_name = clean_text(str(author.get("name") or "")) or None
    content = message.get("content")
    content_type = content.get("content_type") if isinstance(content, dict) else None
    text = extract_content_text(content)
    metadata = message.get("metadata")
    metadata_json = None
    if metadata:
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        metadata_json = encoded[:65535]
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "node_id": node_id,
        "parent_id": node.get("parent"),
        "active_position": active_position,
        "is_active": 1 if is_active else 0,
        "role": role,
        "author_name": author_name,
        "create_time": safe_float(message.get("create_time")),
        "content_type": content_type,
        "text": text,
        "content_hash": content_hash(text),
        "source_file": source_file,
        "metadata_json": metadata_json,
    }


def build_turns(active_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for message in active_messages:
        role = message["role"]
        text = message["text"]
        if role == "user":
            if current is not None:
                turns.append(current)
            current = {
                "create_time": message["create_time"],
                "user_message_ids": [message["message_id"]],
                "assistant_message_ids": [],
                "user_messages": [message],
                "assistant_messages": [],
                "user_text": text,
                "assistant_text": "",
            }
        elif role == "assistant" and current is not None:
            current["assistant_message_ids"].append(message["message_id"])
            current["assistant_messages"].append(message)
            if text:
                current["assistant_text"] = clean_text(
                    current["assistant_text"] + ("\n\n" if current["assistant_text"] else "") + text
                )
    if current is not None:
        turns.append(current)

    for index, turn in enumerate(turns):
        next_user_text = turns[index + 1]["user_text"] if index + 1 < len(turns) else ""
        turn["acceptance"] = infer_acceptance(next_user_text)
    return turns


def main() -> int:
    configure_console()
    args = parse_args()
    raw_dir = args.raw.resolve()
    db_path = args.db.resolve()
    files = list(iter_conversation_files(raw_dir))
    if not files:
        print(f"No conversations-*.json files found in {raw_dir}", file=sys.stderr)
        return 2

    if db_path.exists():
        db_path.unlink()
    conn = connect_db(db_path)
    create_schema(conn)
    counters: Counter[str] = Counter()
    source_stats: list[dict[str, Any]] = []

    conversation_sql = """
        INSERT INTO conversations(
            conversation_id, title, create_time, update_time, current_node,
            default_model_slug, is_archived, is_starred, source_file,
            active_message_count, total_message_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    message_sql = """
        INSERT OR REPLACE INTO messages(
            conversation_id, message_id, node_id, parent_id, active_position,
            is_active, role, author_name, create_time, content_type, text,
            content_hash, source_file, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    try:
        for file_index, path in enumerate(files, start=1):
            file_conversations = 0
            file_messages = 0
            for conversation in iter_conversations(path):
                mapping = conversation.get("mapping")
                if not isinstance(mapping, dict):
                    mapping = {}
                conversation_id = str(conversation.get("conversation_id") or conversation.get("id") or "")
                if not conversation_id:
                    counters["skipped_conversations"] += 1
                    continue
                title = clean_text(str(conversation.get("title") or "Untitled conversation"))
                path_ids = active_path(mapping, conversation.get("current_node"))
                path_positions = {node_id: index for index, node_id in enumerate(path_ids)}
                records: list[dict[str, Any]] = []
                for node_id, node_value in mapping.items():
                    if not isinstance(node_value, dict):
                        continue
                    record = message_record(
                        conversation_id,
                        str(node_id),
                        node_value,
                        str(node_id) in path_positions,
                        path_positions.get(str(node_id)),
                        path.name,
                    )
                    if record is not None:
                        records.append(record)
                        counters[f"role_{record['role']}"] += 1
                        counters[f"content_{record['content_type'] or 'unknown'}"] += 1

                active_records = sorted(
                    (record for record in records if record["is_active"]),
                    key=lambda record: record["active_position"],
                )
                conn.execute(
                    conversation_sql,
                    (
                        conversation_id,
                        title,
                        safe_float(conversation.get("create_time")),
                        safe_float(conversation.get("update_time")),
                        conversation.get("current_node"),
                        conversation.get("default_model_slug"),
                        as_int_bool(conversation.get("is_archived")),
                        as_int_bool(conversation.get("is_starred")),
                        path.name,
                        len(active_records),
                        len(records),
                    ),
                )
                conn.executemany(
                    message_sql,
                    [
                        (
                            r["conversation_id"], r["message_id"], r["node_id"], r["parent_id"],
                            r["active_position"], r["is_active"], r["role"], r["author_name"],
                            r["create_time"], r["content_type"], r["text"], r["content_hash"],
                            r["source_file"], r["metadata_json"],
                        )
                        for r in records
                    ],
                )

                turns = build_turns(active_records)
                for turn_index, turn in enumerate(turns):
                    conn.execute(
                        """
                        INSERT INTO turns(
                            conversation_id, turn_index, create_time, user_message_ids,
                            assistant_message_ids, user_text, assistant_text, acceptance
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            turn_index,
                            turn["create_time"],
                            json.dumps(turn["user_message_ids"]),
                            json.dumps(turn["assistant_message_ids"]),
                            turn["user_text"],
                            turn["assistant_text"],
                            turn["acceptance"],
                        ),
                    )
                    for role_key in ("user_messages", "assistant_messages"):
                        for message in turn[role_key]:
                            if not message["text"]:
                                continue
                            pieces = split_text(message["text"], args.chunk_chars, args.chunk_overlap)
                            for chunk_index, piece in enumerate(pieces):
                                cursor = conn.execute(
                                    """
                                    INSERT INTO chunks(
                                        conversation_id, turn_index, message_id, chunk_index,
                                        role, create_time, title, text, content_hash
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        conversation_id,
                                        turn_index,
                                        message["message_id"],
                                        chunk_index,
                                        message["role"],
                                        message["create_time"] or turn["create_time"],
                                        title,
                                        piece,
                                        content_hash(piece),
                                    ),
                                )
                                chunk_id = int(cursor.lastrowid)
                                conn.execute(
                                    """
                                    INSERT INTO chunks_fts(
                                        rowid, chunk_id, conversation_id, turn_index,
                                        message_id, role, title, text
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        chunk_id, chunk_id, conversation_id, turn_index,
                                        message["message_id"], message["role"], title, piece,
                                    ),
                                )
                                counters["chunks"] += 1
                    counters["turns"] += 1

                counters["conversations"] += 1
                counters["messages"] += len(records)
                counters["active_messages"] += len(active_records)
                file_conversations += 1
                file_messages += len(records)
                if counters["conversations"] % 100 == 0:
                    conn.commit()

            conn.commit()
            source_stats.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "conversations": file_conversations,
                    "messages": file_messages,
                }
            )
            print(
                f"[{file_index:02d}/{len(files):02d}] {path.name}: "
                f"{file_conversations} conversations, {file_messages} messages",
                flush=True,
            )

        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('counts', ?)", (json.dumps(counters),))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('raw_path', ?)", (str(raw_dir),))
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "database": str(db_path),
        "raw_directory": str(raw_dir),
        "chunk_chars": args.chunk_chars,
        "chunk_overlap": args.chunk_overlap,
        "counts": dict(counters),
        "source_files": source_stats,
        "database_bytes": db_path.stat().st_size,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest["counts"], indent=2))
    print(f"Database: {db_path} ({db_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
