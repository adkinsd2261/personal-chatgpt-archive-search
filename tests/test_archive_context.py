from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from archive_context import ArchiveRuntime, ContextEngine
from archive_context.intent import generate_variants, interpret_query
from archive_context.models import DEPTH_CONFIGS, QueryIntent, TurnCandidate
from archive_context.packing import pack_result, sanitize_text, select_episodes
from archive_context.retrieval import temporally_order
from tools.archive_lib import connect_db, create_schema


def insert_turn(
    conn,
    conversation_id: str,
    turn_index: int,
    timestamp: float,
    user_text: str,
    assistant_text: str,
    acceptance: str = "unknown",
) -> None:
    conn.execute(
        """
        INSERT INTO turns(
            conversation_id, turn_index, create_time, user_message_ids,
            assistant_message_ids, user_text, assistant_text, acceptance
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            conversation_id,
            turn_index,
            timestamp,
            json.dumps([f"u-{turn_index}"]),
            json.dumps([f"a-{turn_index}"]),
            user_text,
            assistant_text,
            acceptance,
        ),
    )
    for role, message_id, text in (
        ("user", f"u-{turn_index}", user_text),
        ("assistant", f"a-{turn_index}", assistant_text),
    ):
        cursor = conn.execute(
            """
            INSERT INTO chunks(
                conversation_id, turn_index, message_id, chunk_index, role,
                create_time, title, text, content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (conversation_id, turn_index, message_id, 0, role, timestamp, "Test Crowley", text, f"h-{conversation_id}-{message_id}"),
        )
        chunk_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO chunks_fts(
                rowid, chunk_id, conversation_id, turn_index, message_id,
                role, title, text
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (chunk_id, chunk_id, conversation_id, turn_index, message_id, role, "Test Crowley", text),
        )


class ArchiveContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "fixture.sqlite"
        conn = connect_db(self.db)
        create_schema(conn)
        conn.execute("INSERT INTO conversations(conversation_id,title,source_file) VALUES('c1','Test Crowley','fixture.json')")
        conn.execute("INSERT INTO conversations(conversation_id,title,source_file) VALUES('c2','Independent','fixture.json')")
        insert_turn(conn, "c1", 0, 1000.0, "What is the map?", "Crowley and the agents are separate.", "rejected")
        insert_turn(conn, "c1", 1, 2000.0, "No, all my agents will be Crowley too. I want one identity.", "Crowley is the identity layer.")
        insert_turn(conn, "c1", 2, 3000.0, "Exactly, this works.", "Confirmed.", "accepted")
        insert_turn(conn, "c2", 0, 1500.0, "Crowley should retrieve independent evidence.", "Unverified context.")
        conn.commit()
        conn.close()
        self.runtime = ArchiveRuntime(self.db, load_semantic=False)
        self.engine = ContextEngine(self.runtime)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_intent_uses_primary_mode_plus_flags(self) -> None:
        intent = interpret_query('What was my corrected final position on "Crowley"?')
        self.assertEqual(intent.primary_mode, "correction")
        self.assertIn("exact_phrase", intent.flags)
        self.assertIn("final_position", intent.flags)

    def test_longitudinal_mode_wins_over_latest_wording(self) -> None:
        intent = interpret_query("How did Crowley change over time from early work to the latest plan?")
        self.assertEqual(intent.primary_mode, "longitudinal")
        self.assertIn("current_state", intent.flags)
        self.assertIn("change_over_time", intent.flags)
        kinds = [variant.kind for variant in generate_variants("How did Crowley change over time?", intent)]
        self.assertIn("temporal_earliest", kinds)
        self.assertIn("temporal_latest", kinds)

    def test_variants_are_bounded_and_deterministic(self) -> None:
        query = 'Find "Crowley identity" in Cursor Codex GPT'
        intent = interpret_query(query)
        first = generate_variants(query, intent)
        self.assertEqual(first, generate_variants(query, intent))
        self.assertLessEqual(len(first), 8)
        self.assertEqual(first[0].kind, "semantic_original")

    def test_engine_prioritizes_user_correction(self) -> None:
        result = self.engine.context("corrected final position Crowley agents", "light")
        self.assertEqual(result["episodes"][0]["source_uri"], "archive://conversation/c1/turn/1")
        self.assertEqual(result["episodes"][0]["primary_evidence"]["role"], "user")
        rejected = [item for item in result["episodes"][0]["context"] if item["relation"] == "preceding_assistant"]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["status"], "rejected_context_unverified")

    def test_engine_is_deterministic(self) -> None:
        first = self.engine.context("Crowley identity", "medium")
        second = self.engine.context("Crowley identity", "medium")
        self.assertEqual(first, second)

    def test_engine_applies_complete_packet_budget(self) -> None:
        result = self.engine.context("Crowley", "light")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 9_000)
        self.assertEqual(len(encoded), result["limits"]["serialized_characters"])

    def test_sanitizer_exposes_bidi_and_removes_control_characters(self) -> None:
        cleaned = sanitize_text("safe\x00\x07\x85\u200f\u202eevil")
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertNotIn("\x85", cleaned)
        self.assertIn("<U+200F>", cleaned)
        self.assertIn("<U+202E>", cleaned)

    def test_duplicate_and_conversation_caps(self) -> None:
        base = {
            "rank": 0,
            "source_uri": "archive://conversation/c1/turn/1",
            "conversation_id": "c1",
            "turn_index": 1,
            "title": "T",
            "date_utc": "2026-01-01",
            "matched_role": "user",
            "primary_evidence": {"role": "user", "text": "same evidence words repeated here", "message_ids": []},
            "context": [],
            "signals": [],
            "score": {"profile": "recall", "total": .5, "components": []},
            "contributing_chunk_ids": [1],
        }
        episodes = [dict(base), {**base, "turn_index": 2, "source_uri": "archive://conversation/c1/turn/2"}]
        selected, trace = select_episodes(episodes, DEPTH_CONFIGS["light"], QueryIntent("recall", (), "q"))
        self.assertEqual(len(selected), 1)
        self.assertEqual(trace["exact_duplicates_removed"], 1)

    def test_temporal_order_uses_relevance_floor(self) -> None:
        strong = TurnCandidate("c1", 1, "T", 2000.0, "user", "u", "x", "x", "", "unknown", "unknown", [1], .8, [])
        weak_old = TurnCandidate("c2", 1, "T", 1.0, "user", "u", "x", "x", "", "unknown", "unknown", [2], .1, [])
        intent = QueryIntent("earliest", ("earliest",), "q")
        self.assertEqual(temporally_order([strong, weak_old], intent), [strong])

    def test_pack_result_never_slices_json(self) -> None:
        result = self.engine.context("Crowley", "light")
        self.assertIsInstance(json.loads(json.dumps(result)), dict)

    def test_pack_result_makes_progress_at_minimum_excerpt_boundary(self) -> None:
        episode = {
            "rank": 1,
            "source_uri": "archive://conversation/c1/turn/1",
            "conversation_id": "c1",
            "turn_index": 1,
            "title": "T",
            "date_utc": "2026-01-01",
            "matched_role": "user",
            "primary_evidence": {"role": "user", "text": "x" * 501, "message_ids": []},
            "context": [],
            "signals": [],
            "score": {"profile": "recall", "total": .5, "components": []},
            "contributing_chunk_ids": [1],
        }
        intent = QueryIntent("recall", (), "q")
        roomy = type(DEPTH_CONFIGS["light"])("test", 1, 9_999, 1, 1, 0, 1)
        probe = pack_result(intent, [], [deepcopy(episode)], roomy, {})
        tight = type(roomy)("test", 1, probe["limits"]["serialized_characters"] - 1, 1, 1, 0, 1)
        result = pack_result(intent, [], [deepcopy(episode)], tight, {})
        self.assertEqual(len(result["episodes"][0]["primary_evidence"]["text"]), 500)
        self.assertLessEqual(result["limits"]["serialized_characters"], tight.character_limit)


if __name__ == "__main__":
    unittest.main()
