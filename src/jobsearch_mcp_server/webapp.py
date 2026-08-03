"""Secure, dependency-free HTTP application for 智职引擎."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import signal
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Settings
from .scheduler import RadarScheduler
from .services import AppError, CareerService
from .worker import DurableWorkerSupervisor

LOGGER = logging.getLogger("jobsearch.web")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
APPLICATION_PATH = re.compile(r"^/api/v1/applications/(\d+)$")
RADAR_OPERATION_PATH = re.compile(r"^/api/v1/radar/runs/(\d+)$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SlidingWindowRateLimiter:
    """Small in-memory limiter suitable for a single local process."""

    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[client]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])))
                return False, retry_after
            hits.append(now)
            if not hits:
                self._hits.pop(client, None)
        return True, 0


class CareerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        service: CareerService | None = None,
    ):
        self.settings = settings
        self.service = service or CareerService(settings)
        self.rate_limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
        self.request_slots = threading.BoundedSemaphore(settings.max_concurrent_requests)
        super().__init__(address, CareerRequestHandler)
        self.workers = DurableWorkerSupervisor(
            self.service.repository,
            {"radar_run": self.service.execute_radar_operation},
            self.service.send_outbox_message,
            poll_interval=settings.worker_poll_seconds,
            lease_seconds=settings.worker_lease_seconds,
        )
        if settings.background_workers_enabled:
            self.workers.start()
        self.scheduler = RadarScheduler(self.service, settings.timezone)
        if settings.scheduler_enabled and settings.background_workers_enabled:
            self.scheduler.start()

    def server_close(self) -> None:
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        if hasattr(self, "workers") and not self.workers.stop(timeout=10):
            LOGGER.warning("background workers did not stop before shutdown timeout")
        super().server_close()


class CareerRequestHandler(SimpleHTTPRequestHandler):
    server: CareerHTTPServer
    server_version = f"CareerEngine/{__version__}"
    sys_version = ""
    # Short-lived local API requests do not benefit enough from keep-alive to
    # justify one waiting thread per idle browser socket.
    protocol_version = "HTTP/1.0"

    def __init__(self, *args: Any, **kwargs: Any):
        self.request_id = ""
        self.request_started = 0.0
        super().__init__(
            *args, directory=str(args[2].settings.web_dir) if len(args) > 2 else None, **kwargs
        )

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(65)

    def _prepare_request(self) -> None:
        requested_id = self.headers.get("X-Request-ID", "")
        self.request_id = (
            requested_id if REQUEST_ID_PATTERN.fullmatch(requested_id) else uuid.uuid4().hex
        )
        self.request_started = time.monotonic()
        self.response_headers: dict[str, str] = {}

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.send_header("X-Request-ID", self.request_id or uuid.uuid4().hex)
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin in self.server.settings.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header(
                "Access-Control-Expose-Headers",
                "Location, Retry-After, X-Request-ID",
            )
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self._prepare_request()
        if not self._host_allowed():
            self._send_error(AppError(421, "host_not_allowed", "请求主机未被允许"))
            return
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin not in self.server.settings.allowed_origins:
            self._send_error(AppError(403, "origin_forbidden", "该跨域来源未被允许"))
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Request-ID, Idempotency-Key",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._prepare_request()
        if not self._host_allowed():
            self.send_response(HTTPStatus.MISDIRECTED_REQUEST)
        else:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        self._prepare_request()
        if not self._host_allowed():
            self._send_error(AppError(421, "host_not_allowed", "请求主机未被允许"))
            return
        parsed = urlparse(self.path)
        is_api = parsed.path.startswith("/api/") or parsed.path in {"/livez", "/readyz"}
        if not is_api:
            if method != "GET":
                self._send_error(AppError(405, "method_not_allowed", "该资源不支持此请求方法"))
                return
            self._serve_static(parsed.path)
            return

        if method == "GET" and parsed.path == "/livez":
            self._send_success({"status": "ok", "version": __version__}, 200)
            return
        if method == "GET" and parsed.path == "/readyz":
            try:
                health = self._runtime_health()
            except Exception:
                LOGGER.exception("readiness probe failed")
                self._send_error(AppError(503, "not_ready", "服务尚未就绪"))
                return
            self._send_success(health, 200 if health["status"] == "ok" else 503)
            return

        allowed, retry_after = self.server.rate_limiter.allow(self.client_address[0])
        if not allowed:
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Retry-After", str(retry_after))
            self._write_json(
                {
                    "success": False,
                    "error": {
                        "code": "rate_limited",
                        "message": "请求过于频繁，请稍后再试",
                    },
                    "meta": {"request_id": self.request_id, "timestamp": _utc_now()},
                }
            )
            return

        if not self.server.request_slots.acquire(blocking=False):
            self._send_error(
                AppError(503, "server_busy", "服务繁忙，请稍后重试"),
                extra_headers={"Retry-After": "2"},
            )
            return
        try:
            payload = self._read_json() if method in {"POST", "PATCH"} else {}
            data, status = self._dispatch(method, parsed.path, parse_qs(parsed.query), payload)
            self._send_success(data, status, self.response_headers)
        except AppError as exc:
            self._send_error(exc, self.response_headers)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("client disconnected request_id=%s", self.request_id)
        except Exception:
            LOGGER.exception("unhandled request error request_id=%s", self.request_id)
            self._send_error(AppError(500, "internal_error", "服务处理失败，请稍后重试"))
        finally:
            self.server.request_slots.release()

    def _dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        payload: dict[str, Any],
    ) -> tuple[Any, int]:
        service = self.server.service
        if method == "GET" and path in {"/api/health", "/api/v1/health"}:
            return self._runtime_health(), 200
        if method == "GET" and path == "/api/v1/dashboard":
            return service.dashboard(), 200
        if method == "GET" and path == "/api/v1/radar":
            return service.radar_overview(), 200
        if method == "GET" and path == "/api/v1/radar/runs":
            limit = self._query_int(query, "limit", 20)
            return service.list_radar_operations(limit), 200
        operation_match = RADAR_OPERATION_PATH.fullmatch(path)
        if method == "GET" and operation_match:
            return service.get_radar_operation(int(operation_match.group(1))), 200
        if method == "PATCH" and path == "/api/v1/radar/settings":
            return service.update_radar_settings(payload), 200
        if method == "POST" and path == "/api/v1/radar/runs":
            if not self.server.settings.background_workers_enabled:
                raise AppError(
                    503,
                    "background_workers_disabled",
                    "异步雷达不可用：请启用 BACKGROUND_WORKERS_ENABLED",
                )
            if not self.server.workers.is_running:
                self.response_headers = {"Retry-After": "5"}
                raise AppError(
                    503,
                    "background_workers_unavailable",
                    "异步雷达暂时不可用，请稍后重试",
                )
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            if not idempotency_key:
                raise AppError(
                    400,
                    "idempotency_key_required",
                    "异步雷达请求必须提供 Idempotency-Key",
                )
            operation = service.enqueue_radar_operation(
                payload,
                idempotency_key=idempotency_key,
                trigger_type="manual",
            )
            self.server.workers.wake()
            self.response_headers = {
                "Location": operation["status_url"],
                "Retry-After": "1",
            }
            return operation, 202
        if method == "POST" and path == "/api/v1/radar/run":
            return service.run_radar(payload, trigger_type="manual"), 200
        if method == "POST" and path == "/api/v1/jobs/crawl":
            return service.crawl_jobs(payload), 200
        if method == "GET" and path == "/api/v1/jobs":
            return service.get_job_pool(), 200
        if method == "GET" and path == "/api/v1/jobs/feedback":
            return service.list_job_feedback(), 200
        if method == "POST" and path == "/api/v1/jobs/feedback":
            return service.update_job_feedback(payload), 200
        if method == "POST" and path == "/api/v1/jobs/import":
            return service.import_jobs(payload), 201
        if method == "POST" and path == "/api/v1/jobs/match":
            return service.match_jobs(payload), 200
        if method == "POST" and path == "/api/v1/resumes/analyze":
            return service.analyse_resume(payload), 201
        if method == "POST" and path == "/api/v1/resumes/enhance":
            return service.enhance_resume(payload), 200
        if method == "GET" and path == "/api/v1/applications":
            status = self._query_value(query, "status")
            search = self._query_value(query, "query")
            limit = self._query_int(query, "limit", 100)
            offset = self._query_int(query, "offset", 0)
            return service.list_applications(status, search, limit, offset), 200
        if method == "POST" and path == "/api/v1/applications":
            return service.add_application(payload), 201
        match = APPLICATION_PATH.fullmatch(path)
        if match:
            application_id = int(match.group(1))
            if method == "PATCH":
                return service.update_application(application_id, payload), 200
            if method == "DELETE":
                return service.delete_application(application_id), 200
        raise AppError(404, "route_not_found", "接口不存在")

    def _runtime_health(self) -> dict[str, Any]:
        health = self.server.service.health()
        workers_expected = self.server.settings.background_workers_enabled
        scheduler_expected = self.server.settings.scheduler_enabled
        workers_ok = self.server.workers.is_running
        scheduler_ok = self.server.scheduler.is_running
        health["background_workers"] = (
            "ok" if workers_ok else "disabled" if not workers_expected else "error"
        )
        if scheduler_expected and not workers_expected:
            health["scheduler"] = "blocked_without_workers"
        else:
            health["scheduler"] = (
                "ok" if scheduler_ok else "disabled" if not scheduler_expected else "error"
            )
        health["async_radar"] = "ok" if workers_ok else "unavailable"
        if (
            not workers_expected
            or (workers_expected and not workers_ok)
            or (scheduler_expected and not scheduler_ok)
        ):
            health["status"] = "degraded"
        return health

    def _host_allowed(self) -> bool:
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1:
            return False
        raw_host = host_headers[0].strip()
        if not raw_host or any(character.isspace() for character in raw_host):
            return False
        try:
            parsed = urlparse(f"//{raw_host}")
            hostname = (parsed.hostname or "").lower().rstrip(".")
            _ = parsed.port
        except ValueError:
            return False
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        return hostname in self.server.settings.allowed_hosts

    @staticmethod
    def _query_value(query: dict[str, list[str]], name: str) -> str:
        values = query.get(name, [])
        return values[0].strip() if values else ""

    def _query_int(self, query: dict[str, list[str]], name: str, default: int) -> int:
        value = self._query_value(query, name)
        if not value:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise AppError(422, "invalid_query", f"{name} 必须是整数") from exc

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise AppError(415, "unsupported_media_type", "请求必须使用 application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise AppError(411, "length_required", "缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AppError(400, "invalid_content_length", "Content-Length 无效") from exc
        if length < 0:
            raise AppError(400, "invalid_content_length", "Content-Length 无效")
        if length > self.server.settings.max_body_bytes:
            # Do not leave an oversized body unread on a persistent connection.
            self.close_connection = True
            raise AppError(413, "request_too_large", "请求体超过允许上限")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(400, "invalid_json", "JSON 请求体格式错误") from exc
        if not isinstance(payload, dict):
            raise AppError(422, "invalid_payload", "JSON 请求体必须是对象")
        return payload

    def _send_success(
        self,
        data: Any,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._write_json(
            {
                "success": True,
                "data": data,
                "meta": {"request_id": self.request_id, "timestamp": _utc_now()},
            }
        )

    def _send_error(self, error: AppError, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(error.status)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        body: dict[str, Any] = {
            "success": False,
            "error": {"code": error.code, "message": error.message},
            "meta": {"request_id": self.request_id, "timestamp": _utc_now()},
        }
        if error.details:
            body["error"]["details"] = error.details
        self._write_json(body)

    def _write_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, request_path: str) -> None:
        if request_path in {"", "/"}:
            self.path = "/index.html"
        elif request_path.endswith("/"):
            self.path = request_path + "index.html"
        else:
            self.path = request_path
        candidate = (self.server.settings.web_dir / self.path.lstrip("/")).resolve()
        web_root = self.server.settings.web_dir.resolve()
        if not candidate.is_relative_to(web_root):
            self._send_error(AppError(403, "path_forbidden", "资源路径无效"))
            return
        if not candidate.exists() or not candidate.is_file():
            # SPA routes fall back to the shell; files with extensions remain 404.
            if "." not in Path(self.path).name:
                self.path = "/index.html"
            else:
                self._send_error(AppError(404, "asset_not_found", "静态资源不存在"))
                return
        super().do_GET()

    def send_head(self):  # type: ignore[no-untyped-def]
        path = self.translate_path(self.path)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            file_handle = open(path, "rb")
        except OSError:
            self._send_error(AppError(404, "asset_not_found", "静态资源不存在"))
            return None
        try:
            stat = os.fstat(file_handle.fileno())
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            if Path(path).suffix in {".css", ".js", ".svg", ".png", ".webp"}:
                self.send_header("Cache-Control", "public, max-age=3600")
            else:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return file_handle
        except Exception:
            file_handle.close()
            raise

    def log_message(self, fmt: str, *args: Any) -> None:
        elapsed_ms = round((time.monotonic() - self.request_started) * 1000, 1)
        LOGGER.info(
            "http_request method=%s path=%s client=%s request_id=%s duration_ms=%s message=%s",
            self.command,
            urlparse(self.path).path,
            self.client_address[0],
            self.request_id,
            elapsed_ms,
            fmt % args,
        )


def create_server(
    settings: Settings | None = None, service: CareerService | None = None
) -> CareerHTTPServer:
    settings = settings or Settings.from_env()
    if not settings.web_dir.joinpath("index.html").is_file():
        raise RuntimeError(f"Web 资源不存在: {settings.web_dir}")
    return CareerHTTPServer((settings.host, settings.port), settings, service)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    defaults = Settings.from_env()
    parser = argparse.ArgumentParser(description="启动智职引擎 Web 服务")
    parser.add_argument("--host", default=defaults.host, help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=defaults.port, help="监听端口")
    parser.add_argument("--data-dir", type=Path, default=defaults.data_dir, help="数据目录")
    parser.add_argument("--web-dir", type=Path, default=defaults.web_dir, help="静态资源目录")
    args = parser.parse_args()
    settings = defaults.with_overrides(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir.resolve(),
        web_dir=args.web_dir.resolve(),
    )
    _configure_logging(settings.log_level)
    server = create_server(settings)

    def stop_server(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_server)
        signal.signal(signal.SIGINT, stop_server)
    host, port = server.server_address
    LOGGER.info("智职引擎已启动 url=http://%s:%s data_dir=%s", host, port, settings.data_dir)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        LOGGER.info("智职引擎已停止")


if __name__ == "__main__":
    main()
