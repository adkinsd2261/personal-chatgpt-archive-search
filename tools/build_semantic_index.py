from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from archive_lib import configure_console, connect_db, shorten


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the optional local semantic embedding index.")
    parser.add_argument("--db", type=Path, default=Path("index/archive.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("index/semantic"))
    parser.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--roles", choices=("user", "all"), default="user",
        help="Embed user-authored evidence by default; assistant text remains in FTS and turn context.",
    )
    parser.add_argument("--max-text-chars", type=int, default=2400)
    parser.add_argument("--limit", type=int, help="Optional benchmark/debug limit")
    return parser.parse_args()


def main() -> int:
    configure_console()
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise SystemExit(
            "Semantic dependencies are missing. Install requirements-semantic.txt first."
        ) from exc

    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cache_dir = args.output / "model-cache"
    model = TextEmbedding(model_name=args.model, cache_dir=str(cache_dir))
    conn = connect_db(args.db, readonly=True)
    where = "WHERE role='user'" if args.roles == "user" else ""
    total = conn.execute(f"SELECT COUNT(*) FROM chunks {where}").fetchone()[0]
    if args.limit:
        total = min(total, args.limit)
    if total == 0:
        raise SystemExit("No chunks found. Build the archive database first.")

    rows = conn.execute(
        f"SELECT chunk_id, title, text FROM chunks {where} ORDER BY chunk_id"
        + (" LIMIT ?" if args.limit else ""),
        (args.limit,) if args.limit else (),
    )
    first_batch = rows.fetchmany(args.batch_size)
    first_texts = [f"{row['title']}\n\n{shorten(row['text'], args.max_text_chars)}" for row in first_batch]
    first_vectors = np.asarray(
        list(model.embed(first_texts, batch_size=args.batch_size)), dtype=np.float32
    )
    first_vectors /= np.maximum(np.linalg.norm(first_vectors, axis=1, keepdims=True), 1e-12)
    dimension = int(first_vectors.shape[1])
    vectors = np.lib.format.open_memmap(
        args.output / "vectors.npy", mode="w+", dtype=np.float32, shape=(total, dimension)
    )
    ids = np.lib.format.open_memmap(
        args.output / "chunk_ids.npy", mode="w+", dtype=np.int64, shape=(total,)
    )
    vectors[: len(first_batch)] = first_vectors
    ids[: len(first_batch)] = [row["chunk_id"] for row in first_batch]
    offset = len(first_batch)
    print(f"Embedded {offset}/{total}", flush=True)

    while batch := rows.fetchmany(args.batch_size):
        texts = [f"{row['title']}\n\n{shorten(row['text'], args.max_text_chars)}" for row in batch]
        embedded = np.asarray(
            list(model.embed(texts, batch_size=args.batch_size)), dtype=np.float32
        )
        embedded /= np.maximum(np.linalg.norm(embedded, axis=1, keepdims=True), 1e-12)
        vectors[offset : offset + len(batch)] = embedded
        ids[offset : offset + len(batch)] = [row["chunk_id"] for row in batch]
        offset += len(batch)
        if offset % (args.batch_size * 10) == 0 or offset == total:
            vectors.flush()
            ids.flush()
            print(f"Embedded {offset}/{total}", flush=True)
    conn.close()
    vectors.flush()
    ids.flush()
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "count": total,
        "dimension": dimension,
        "normalized": True,
        "roles": args.roles,
        "max_text_chars": args.max_text_chars,
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
