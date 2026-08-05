from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .engine import ContextEngine
from .models import ALGORITHM_VERSION
from .runtime import ArchiveRuntime
from .security import BearerTokenVerifier, TOKEN_ENV


LOGGER = logging.getLogger("archive_context.service")


def _environment_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc


def _environment_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric.") from exc


@dataclass(frozen=True)
class ServiceSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    max_body_bytes: int = 16_384
    max_query_bytes: int = 12_000
    max_concurrency: int = 2
    queue_timeout_seconds: float = 1.0
    request_timeout_seconds: float = 10.0
    socket_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not (0 <= self.port <= 65_535):
            raise ValueError("port must be between 0 and 65535")
        if not (1_024 <= self.max_body_bytes <= 1_048_576):
            raise ValueError("max_body_bytes must be between 1024 and 1048576")
        if not (256 <= self.max_query_bytes <= self.max_body_bytes):
            raise ValueError("max_query_bytes must be between 256 and max_body_bytes")
        if not (1 <= self.max_concurrency <= 16):
            raise ValueError("max_concurrency must be between 1 and 16")
        if not (0.05 <= self.queue_timeout_seconds <= 30.0):
            raise ValueError("queue_timeout_seconds must be between 0.05 and 30 seconds")
        if not (0.25 <= self.request_timeout_seconds <= 120.0):
            raise ValueError("request_timeout_seconds must be between 0.25 and 120 seconds")
        if not (1.0 <= self.socket_timeout_seconds <= 120.0):
            raise ValueError("socket_timeout_seconds must be between 1 and 120 seconds")

    @classmethod
    def from_environment(cls) -> "ServiceSettings":
        return cls(
            host=os.getenv("ARCHIVE_CONTEXT_HOST", "127.0.0.1"),
            port=_environment_int("ARCHIVE_CONTEXT_PORT", 8765),
            max_body_bytes=_environment_int("ARCHIVE_CONTEXT_MAX_BODY_BYTES", 16_384),
            max_query_bytes=_environment_int("ARCHIVE_CONTEXT_MAX_QUERY_BYTES", 12_000),
            max_concurrency=_environment_int("ARCHIVE_CONTEXT_MAX_CONCURRENCY", 2),
            queue_timeout_seconds=_environment_float("ARCHIVE_CONTEXT_QUEUE_TIMEOUT_SECONDS", 1.0),
            request_timeout_seconds=_environment_float("ARCHIVE_CONTEXT_REQUEST_TIMEOUT_SECONDS", 10.0),
            socket_timeout_seconds=_environment_float("ARCHIVE_CONTEXT_SOCKET_TIMEOUT_SECONDS", 15.0),
        )


class RequestProblem(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContextRequest:
    query: str
    depth: str = "medium"
    date_from: str | None = None
    date_to: str | None = None

    @classmethod
    def parse(cls, value: Any, settings: ServiceSettings) -> "ContextRequest":
        if not isinstance(value, dict):
            raise RequestProblem(422, "invalid_request", "The JSON body must be an object.")
        allowed = {"query", "depth", "date_from", "date_to"}
        if set(value) - allowed:
            raise RequestProblem(422, "invalid_request", "The request contains unsupported fields.")
        query = value.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4_000:
            raise RequestProblem(422, "invalid_request", "query must be a non-blank string of at most 4000 characters.")
        if len(query.encode("utf-8")) > settings.max_query_bytes:
            raise RequestProblem(422, "query_too_large", "Query exceeds the configured UTF-8 byte limit.")
        depth = value.get("depth", "medium")
        if not isinstance(depth, str) or depth not in {"light", "medium", "deep"}:
            raise RequestProblem(422, "invalid_request", "depth must be light, medium, or deep.")
        date_from = _validated_date(value.get("date_from"), "date_from")
        date_to = _validated_date(value.get("date_to"), "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise RequestProblem(422, "invalid_request", "date_from must not be after date_to.")
        return cls(query=query, depth=depth, date_from=date_from, date_to=date_to)


def _validated_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestProblem(422, "invalid_request", f"{field} must be a YYYY-MM-DD string.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RequestProblem(422, "invalid_request", f"{field} must be a valid YYYY-MM-DD date.") from exc
    if value != parsed.isoformat():
        raise RequestProblem(422, "invalid_request", f"{field} must use canonical YYYY-MM-DD format.")
    return value


class ContextService:
    """Own the resident engine, fixed worker pool, and overload boundary."""

    def __init__(
        self,
        engine: ContextEngine | Any,
        verifier: BearerTokenVerifier,
        settings: ServiceSettings,
    ) -> None:
        self.engine = engine
        self.verifier = verifier
        self.settings = settings
        self._capacity = threading.BoundedSemaphore(settings.max_concurrency)
        self._workers = ThreadPoolExecutor(
            max_workers=settings.max_concurrency,
            thread_name_prefix="archive-context",
        )

    def close(self) -> None:
        self._workers.shutdown(wait=True, cancel_futures=False)

    def retrieve(self, request: ContextRequest, request_id: str) -> dict[str, Any]:
        acquired = self._capacity.acquire(timeout=self.settings.queue_timeout_seconds)
        if not acquired:
            raise RequestProblem(503, "service_busy", "The context service is at capacity.")
        future: Future | None = None
        release_deferred = False
        started = time.perf_counter()
        try:
            future = self._workers.submit(
                self.engine.context,
                request.query,
                request.depth,
                request.date_from,
                request.date_to,
            )
            try:
                result = future.result(timeout=self.settings.request_timeout_seconds)
            except FutureTimeoutError as exc:
                release_deferred = True
                future.add_done_callback(lambda _future: self._capacity.release())
                raise RequestProblem(504, "request_timeout", "Context retrieval exceeded the service timeout.") from exc
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            counts = result.get("trace", {}).get("candidate_counts", {})
            LOGGER.info(
                "request_id=%s mode=%s elapsed_ms=%s episodes=%s candidates=%s status=ok",
                request_id,
                result.get("intent", {}).get("primary_mode", "unknown"),
                elapsed_ms,
                len(result.get("episodes", [])),
                counts.get("unique_chunks", "unknown"),
            )
            return result
        except RequestProblem:
            raise
        except ValueError as exc:
            LOGGER.warning("request_id=%s status=invalid error_type=%s", request_id, type(exc).__name__)
            raise RequestProblem(422, "invalid_request", "The request could not be processed.") from exc
        except Exception as exc:
            LOGGER.error("request_id=%s status=error error_type=%s", request_id, type(exc).__name__)
            raise RequestProblem(500, "internal_error", "Context retrieval failed safely.") from exc
        finally:
            if not release_deferred:
                self._capacity.release()


class ContextHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, service: ContextService):
        self.context_service = service
        super().__init__(server_address, ContextRequestHandler)

    def handle_error(self, _request, _client_address) -> None:
        error_type = sys.exc_info()[0]
        LOGGER.error("uncaught_request_error type=%s", getattr(error_type, "__name__", "unknown"))

    def server_close(self) -> None:
        super().server_close()
        self.context_service.close()


class ContextRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArchiveContext/1"
    sys_version = ""

    @property
    def context_server(self) -> ContextHTTPServer:
        return self.server  # type: ignore[return-value]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.context_server.context_service.settings.socket_timeout_seconds)

    def do_POST(self) -> None:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        try:
            self._authorize()
            if self.path != "/api/context":
                raise RequestProblem(404, "not_found", "Route not found.")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type != "application/json":
                raise RequestProblem(415, "unsupported_media_type", "Content-Type must be application/json.")
            if self.headers.get("Content-Encoding", "identity").casefold() != "identity":
                raise RequestProblem(415, "unsupported_content_encoding", "Compressed request bodies are not accepted.")
            body = self._read_body()
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestProblem(400, "invalid_json", "Request body must be valid UTF-8 JSON.") from exc
            context_request = ContextRequest.parse(value, self.context_server.context_service.settings)
            result = self.context_server.context_service.retrieve(context_request, request_id)
            self._send_json(200, result, request_id, started)
        except RequestProblem as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                request_id,
                started,
                authenticate=exc.status == 401,
            )
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            LOGGER.warning("request_id=%s status=client_disconnected", request_id)
        except Exception as exc:
            LOGGER.error("request_id=%s status=error error_type=%s", request_id, type(exc).__name__)
            try:
                self._send_json(
                    500,
                    {"error": {"code": "internal_error", "message": "The request failed safely."}},
                    request_id,
                    started,
                )
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

    def do_GET(self) -> None:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        try:
            self._authorize()
            if self.path != "/api/health":
                raise RequestProblem(404, "not_found", "Route not found.")
            runtime = getattr(self.context_server.context_service.engine, "runtime", None)
            self._send_json(
                200,
                {
                    "status": "ok",
                    "semantic_ready": bool(getattr(runtime, "semantic_ready", False)),
                    "algorithm_version": ALGORITHM_VERSION,
                },
                request_id,
                started,
            )
        except RequestProblem as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                request_id,
                started,
                authenticate=exc.status == 401,
            )

    def _authorize(self) -> None:
        if not self.context_server.context_service.verifier.matches_authorization_header(
            self.headers.get("Authorization")
        ):
            raise RequestProblem(401, "unauthorized", "A valid bearer token is required.")

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestProblem(411, "length_required", "Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestProblem(400, "invalid_content_length", "Content-Length must be an integer.") from exc
        if length < 0 or length > self.context_server.context_service.settings.max_body_bytes:
            raise RequestProblem(413, "request_too_large", "Request body exceeds the configured limit.")
        return self.rfile.read(length)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        request_id: str,
        started: float,
        authenticate: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Request-ID", request_id)
        self.send_header("X-Response-Time-Ms", str(round((time.perf_counter() - started) * 1000, 1)))
        if authenticate:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        safe_path = self.path.partition("?")[0][:200]
        LOGGER.info("client=%s method=%s path=%s", self.client_address[0], self.command, safe_path)


def create_server(
    *,
    engine: ContextEngine | Any | None = None,
    token: str | None = None,
    settings: ServiceSettings | None = None,
) -> ContextHTTPServer:
    active_settings = settings or ServiceSettings.from_environment()
    configured_token = token if token is not None else os.getenv(TOKEN_ENV)
    verifier = BearerTokenVerifier(configured_token)
    active_engine = engine if engine is not None else ContextEngine(ArchiveRuntime())
    service = ContextService(active_engine, verifier, active_settings)
    return ContextHTTPServer((active_settings.host, active_settings.port), service)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local read-only archive context service.")
    parser.add_argument("--log-level", choices=("critical", "error", "warning", "info"), default="info")
    args = parser.parse_args()
    settings = ServiceSettings.from_environment()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(levelname)s %(name)s %(message)s")
    server = create_server(settings=settings)
    LOGGER.info("listening host=%s port=%s workers=%s", settings.host, settings.port, settings.max_concurrency)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
