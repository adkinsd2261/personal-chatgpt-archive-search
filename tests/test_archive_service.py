from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_context import ArchiveRuntime, ContextEngine
from archive_context.models import ALGORITHM_VERSION
from archive_context.security import BearerTokenVerifier, TokenConfigurationError
from archive_context.service import (
    ContextRequest,
    ContextService,
    RequestProblem,
    ServiceSettings,
    create_server,
)
from tools.archive_lib import connect_db, create_schema


TOKEN = "test-token-0123456789-0123456789-abcdef"


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def context(self, query, depth, date_from, date_to):
        self.calls.append((query, depth, date_from, date_to))
        return {
            "schema_version": "test",
            "intent": {"primary_mode": "recall"},
            "episodes": [],
            "trace": {"candidate_counts": {"unique_chunks": 0}},
        }


def insert_turn(conn, conversation_id: str, turn_index: int, timestamp: float, user: str, assistant: str, acceptance: str = "unknown") -> None:
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
            user,
            assistant,
            acceptance,
        ),
    )
    for role, text in (("user", user), ("assistant", assistant)):
        message_id = f"{role}-{turn_index}"
        cursor = conn.execute(
            """
            INSERT INTO chunks(
                conversation_id, turn_index, message_id, chunk_index, role,
                create_time, title, text, content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (conversation_id, turn_index, message_id, 0, role, timestamp, "Fixture", text, f"h-{message_id}"),
        )
        chunk_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO chunks_fts(
                rowid, chunk_id, conversation_id, turn_index, message_id,
                role, title, text
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (chunk_id, chunk_id, conversation_id, turn_index, message_id, role, "Fixture", text),
        )


class RunningServer:
    def __init__(self, engine, settings: ServiceSettings | None = None) -> None:
        self.settings = settings or ServiceSettings(port=0)
        self.server = create_server(engine=engine, token=TOKEN, settings=self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, value=None, token: str | None = TOKEN, headers: dict[str, str] | None = None):
        body = None if value is None else json.dumps(value).encode("utf-8")
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(raw.decode("utf-8")))
        connection.close()
        return result


class ArchiveServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = FakeEngine()
        cls.running = RunningServer(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.running.close()

    def assert_receipt_matches_header(self, body, headers) -> None:
        self.assertEqual(body["receipt"]["request_id"], headers["X-Request-ID"])
        self.assertEqual(len(body["receipt"]["request_id"]), 32)
        self.assertTrue(body["receipt"]["issued_at_utc"].endswith("Z"))
        self.assertEqual(body["receipt"]["algorithm_version"], ALGORITHM_VERSION)

    def test_action_shaped_request_is_authenticated_and_bounded(self) -> None:
        self.assertEqual(ServiceSettings().host, "127.0.0.1")
        status, headers, body = self.running.request(
            "POST",
            "/api/context",
            {"query": "Crowley identity", "depth": "light", "date_from": "2026-01-01"},
        )
        self.assertEqual(status, 200)
        self.assertIs(body["success"], True)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["evidence_count"], 0)
        self.assertEqual(body["schema_version"], "test")
        self.assert_receipt_matches_header(body, headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.engine.calls[-1], ("Crowley identity", "light", "2026-01-01", None))

    def test_receipt_is_new_for_each_action_request(self) -> None:
        first = self.running.request("POST", "/api/context", {"query": "first"})
        second = self.running.request("POST", "/api/context", {"query": "second"})
        self.assertNotEqual(first[2]["receipt"]["request_id"], second[2]["receipt"]["request_id"])

    def test_authentication_fails_closed(self) -> None:
        for token in (None, "wrong-token-that-is-long-enough-000000000"):
            status, headers, body = self.running.request("POST", "/api/context", {"query": "private"}, token=token)
            self.assertEqual(status, 401)
            self.assertIs(body["success"], False)
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["evidence_count"], 0)
            self.assertEqual(body["error"]["code"], "unauthorized")
            self.assertIs(body["error"]["retryable"], False)
            self.assert_receipt_matches_header(body, headers)
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_validation_errors_do_not_echo_private_input(self) -> None:
        private_marker = "DO-NOT-ECHO-PRIVATE-MARKER"
        status, _headers, body = self.running.request(
            "POST",
            "/api/context",
            {"query": private_marker, "unexpected": True},
        )
        self.assertEqual(status, 422)
        self.assertNotIn(private_marker, json.dumps(body))

    def test_invalid_dates_and_depth_are_rejected(self) -> None:
        cases = (
            {"query": "q", "depth": "unbounded"},
            {"query": "q", "date_from": "2026-02-30"},
            {"query": "q", "date_from": "2026-02-02", "date_to": "2026-01-01"},
        )
        for value in cases:
            status, _headers, body = self.running.request("POST", "/api/context", value)
            self.assertEqual(status, 422)
            self.assertIs(body["success"], False)
            self.assertEqual(body["error"]["code"], "invalid_request")

    def test_body_limit_is_enforced_before_json_parsing(self) -> None:
        oversized = {"query": "x" * 17_000}
        status, _headers, body = self.running.request("POST", "/api/context", oversized)
        self.assertEqual(status, 413)
        self.assertIs(body["success"], False)
        self.assertEqual(body["error"]["code"], "request_too_large")

    def test_timeout_returns_a_model_visible_retryable_failure(self) -> None:
        class SlowEngine:
            def context(self, *_args):
                time.sleep(0.4)
                return {"intent": {}, "episodes": [], "trace": {}}

        running = RunningServer(
            SlowEngine(),
            ServiceSettings(port=0, max_concurrency=1, request_timeout_seconds=0.25),
        )
        try:
            status, headers, body = running.request("POST", "/api/context", {"query": "slow"})
            self.assertEqual(status, 504)
            self.assertIs(body["success"], False)
            self.assertEqual(body["error"]["code"], "request_timeout")
            self.assertIs(body["error"]["retryable"], True)
            self.assert_receipt_matches_header(body, headers)
        finally:
            running.close()

    def test_engine_cannot_override_the_server_issued_envelope(self) -> None:
        class InvalidEngine:
            def context(self, *_args):
                return {"success": False, "episodes": []}

        running = RunningServer(InvalidEngine())
        try:
            status, headers, body = running.request("POST", "/api/context", {"query": "private"})
            self.assertEqual(status, 500)
            self.assertIs(body["success"], False)
            self.assertEqual(body["error"]["code"], "invalid_engine_response")
            self.assertIs(body["error"]["retryable"], True)
            self.assert_receipt_matches_header(body, headers)
        finally:
            running.close()

    def test_health_is_authenticated_but_not_an_action(self) -> None:
        status, _headers, body = self.running.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        schema = json.loads(Path("openapi-action.json").read_text(encoding="utf-8"))
        operations = [
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        ]
        self.assertEqual(operations, ["crowley_context"])
        self.assertEqual(set(schema["paths"]), {"/api/context"})
        operation = schema["paths"]["/api/context"]["post"]
        self.assertIn("Never retry it for the same user message", operation["description"])
        self.assertIs(operation["x-openai-isConsequential"], False)
        self.assertEqual(schema["servers"], [{"url": "https://archive.javlin.ai"}])
        self.assertEqual(operation["security"], [{"bearerAuth": []}])
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["required"], ["query"])
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertTrue(
            {"success", "receipt", "evidence_count", "episodes"}
            <= set(response_schema["properties"])
        )
        self.assertEqual(
            schema["components"]["securitySchemes"]["bearerAuth"],
            {"type": "http", "scheme": "bearer"},
        )

    def test_missing_service_token_prevents_startup(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(TokenConfigurationError):
                create_server(engine=FakeEngine(), settings=ServiceSettings(port=0))

    def test_token_verifier_uses_constant_time_digest_comparison(self) -> None:
        verifier = BearerTokenVerifier(TOKEN)
        self.assertTrue(verifier.matches_authorization_header(f"Bearer {TOKEN}"))
        self.assertFalse(verifier.matches_authorization_header("Basic anything"))
        self.assertFalse(hasattr(verifier, "token"))

    def test_timeout_keeps_capacity_reserved_until_worker_finishes(self) -> None:
        class SlowEngine:
            def context(self, *_args):
                time.sleep(0.4)
                return {"intent": {}, "episodes": [], "trace": {}}

        settings = ServiceSettings(
            port=0,
            max_concurrency=1,
            queue_timeout_seconds=0.05,
            request_timeout_seconds=0.25,
        )
        service = ContextService(SlowEngine(), BearerTokenVerifier(TOKEN), settings)
        request = ContextRequest("slow")
        try:
            with self.assertRaises(RequestProblem) as first:
                service.retrieve(request, "first")
            self.assertEqual(first.exception.code, "request_timeout")
            with self.assertRaises(RequestProblem) as second:
                service.retrieve(request, "second")
            self.assertEqual(second.exception.code, "service_busy")
            time.sleep(0.2)
        finally:
            service.close()


class RealEngineServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "service-fixture.sqlite"
        conn = connect_db(database)
        create_schema(conn)
        conn.execute("INSERT INTO conversations(conversation_id,title,source_file) VALUES('c1','Fixture','fixture.json')")
        insert_turn(conn, "c1", 0, 1000.0, "What is Crowley?", "Crowley and the agents are separate.", "rejected")
        insert_turn(conn, "c1", 1, 2000.0, "No, all my agents will be Crowley too. One identity.", "Understood.")
        conn.commit()
        conn.close()
        self.running = RunningServer(ContextEngine(ArchiveRuntime(database, load_semantic=False)))

    def tearDown(self) -> None:
        self.running.close()
        self.temp.cleanup()

    def test_one_http_action_reaches_the_real_algorithm_without_a_subprocess(self) -> None:
        status, headers, body = self.running.request(
            "POST",
            "/api/context",
            {"query": "corrected final position Crowley agents", "depth": "light"},
        )
        self.assertEqual(status, 200)
        self.assertIs(body["success"], True)
        self.assertEqual(body["evidence_count"], len(body["episodes"]))
        self.assertEqual(body["receipt"]["request_id"], headers["X-Request-ID"])
        self.assertEqual(body["episodes"][0]["source_uri"], "archive://conversation/c1/turn/1")
        self.assertEqual(body["episodes"][0]["primary_evidence"]["role"], "user")
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), body["limits"]["character_limit"])
        self.assertEqual(len(encoded), body["limits"]["serialized_characters"])


if __name__ == "__main__":
    unittest.main()
