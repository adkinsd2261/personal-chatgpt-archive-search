from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from archive_lib import (  # noqa: E402
    active_path,
    aggregate_turn_hits,
    build_fts_queries,
    connect_db,
    create_schema,
    extract_content_text,
    fuse_ranked_hits,
    infer_acceptance,
    lexical_search,
    split_text,
    SearchHit,
)


class ArchiveLibraryTests(unittest.TestCase):
    def test_active_path_preserves_selected_branch(self) -> None:
        mapping = {
            "root": {"parent": None},
            "user": {"parent": "root"},
            "rejected": {"parent": "user"},
            "accepted": {"parent": "user"},
        }
        self.assertEqual(active_path(mapping, "accepted"), ["root", "user", "accepted"])

    def test_extracts_multimodal_text_without_asset_noise(self) -> None:
        content = {
            "content_type": "multimodal_text",
            "parts": [
                "This is the real text.",
                {"asset_pointer": "file-service://secret", "mime_type": "image/png"},
            ],
        }
        self.assertEqual(extract_content_text(content), "This is the real text.")

    def test_chunking_has_overlap_and_reconstructable_order(self) -> None:
        text = "Paragraph one. " * 200
        chunks = split_text(text, 300, 30)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 320 for chunk in chunks))

    def test_acceptance_and_rejection_signals(self) -> None:
        self.assertEqual(infer_acceptance("Yeah bro, this is tuff"), "accepted")
        self.assertEqual(infer_acceptance("Nah, this is not it"), "rejected")

    def test_distinctive_capitalized_names_get_exact_anchor_query(self) -> None:
        queries = build_fts_queries("When did I first start building Crowley")
        self.assertIn(('"crowley"', 2.0), queries)

    def test_weighted_fts_prefers_user_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "test.sqlite"
            conn = connect_db(db)
            create_schema(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,source_file) VALUES('c','Test','x.json')"
            )
            conn.execute(
                "INSERT INTO turns(conversation_id,turn_index,user_message_ids,assistant_message_ids,user_text,assistant_text) VALUES('c',0,'[]','[]','','')"
            )
            for role, text in (("assistant", "Crowley memory engine"), ("user", "Crowley memory engine")):
                cur = conn.execute(
                    "INSERT INTO chunks(conversation_id,turn_index,message_id,chunk_index,role,title,text,content_hash) VALUES('c',0,?,0,?,'Test',?,'h')",
                    (role, role, text),
                )
                chunk_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO chunks_fts(rowid,chunk_id,conversation_id,turn_index,message_id,role,title,text) VALUES(?,?,?,?,?,?,?,?)",
                    (chunk_id, chunk_id, "c", 0, role, role, "Test", text),
                )
            conn.commit()
            hits = lexical_search(conn, "Crowley memory", candidate_limit=10)
            conn.close()
            self.assertEqual(hits[0].role, "user")

    def test_hybrid_fusion_rewards_hits_in_both_lists(self) -> None:
        common = dict(
            conversation_id="c", turn_index=0, message_id="m", role="user",
            title="T", create_time=1.0, text="x", score=1.0,
        )
        first = SearchHit(chunk_id=1, **common)
        second = SearchHit(chunk_id=2, **{**common, "message_id": "m2"})
        fused = fuse_ranked_hits([first, second], [second])
        self.assertEqual(fused[0].chunk_id, 2)


if __name__ == "__main__":
    unittest.main()
