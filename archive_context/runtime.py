from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from tools.archive_lib import connect_db


class ArchiveRuntime:
    """Resident read-only resources shared by every context request."""

    def __init__(
        self,
        db_path: Path = Path("index/archive.sqlite"),
        semantic_dir: Path = Path("index/semantic"),
        load_semantic: bool = True,
        embedding_model: Any | None = None,
    ) -> None:
        self.db_path = db_path.resolve()
        self.semantic_dir = semantic_dir.resolve()
        self._encoder_lock = threading.Lock()
        self.model: Any | None = embedding_model
        self.vectors: Any | None = None
        self.chunk_ids: Any | None = None
        self.semantic_manifest: dict[str, Any] | None = None
        if load_semantic:
            self._load_semantic()

    def _load_semantic(self) -> None:
        import numpy as np

        manifest_path = self.semantic_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Semantic manifest is missing.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        vectors = np.load(self.semantic_dir / "vectors.npy", mmap_mode="r")
        chunk_ids = np.load(self.semantic_dir / "chunk_ids.npy", mmap_mode="r")
        if vectors.ndim != 2 or chunk_ids.ndim != 1 or vectors.shape[0] != chunk_ids.shape[0]:
            raise RuntimeError("Semantic vector and chunk-ID shapes do not match.")
        if int(manifest.get("count", -1)) != int(chunk_ids.shape[0]):
            raise RuntimeError("Semantic manifest count does not match the index.")
        if int(manifest.get("dimension", -1)) != int(vectors.shape[1]):
            raise RuntimeError("Semantic manifest dimension does not match the vectors.")
        if self.model is None:
            from fastembed import TextEmbedding

            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            self.model = TextEmbedding(
                model_name=manifest["model"],
                cache_dir=str(self.semantic_dir / "model-cache"),
                local_files_only=True,
                lazy_load=False,
            )
        self.semantic_manifest = manifest
        self.vectors = vectors
        self.chunk_ids = chunk_ids

    @property
    def semantic_ready(self) -> bool:
        return self.model is not None and self.vectors is not None and self.chunk_ids is not None

    def connect(self):
        conn = connect_db(self.db_path, readonly=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def semantic_top(self, query: str, limit: int) -> list[tuple[int, float, int]]:
        if not self.semantic_ready:
            return []
        import numpy as np

        with self._encoder_lock:
            if hasattr(self.model, "query_embed"):
                vector = np.asarray(list(self.model.query_embed(query))[0], dtype=np.float32)
            else:
                vector = np.asarray(list(self.model.embed([query]))[0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm:
            vector /= norm
        scores = np.asarray(self.vectors @ vector, dtype=np.float32)
        order = np.lexsort((self.chunk_ids, -scores))[: min(limit, scores.shape[0])]
        return [
            (int(self.chunk_ids[index]), float(scores[index]), rank)
            for rank, index in enumerate(order, start=1)
        ]
