from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest

from jobsearch_mcp_server.webapp import create_server
from tests.helpers import SAMPLE_RESUME, make_settings


@contextmanager
def running_server(settings):
    server = create_server(settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def live_server(tmp_path: Path):
    settings = make_settings(tmp_path).with_overrides(
        background_workers_enabled=True,
        worker_poll_seconds=1,
        worker_lease_seconds=60,
    )
    with running_server(settings) as root:
        yield root


def request_json(
    root: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(root + path, data=data, method=method, headers=request_headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        return error.code, json.load(error), error.headers
    with response:
        return response.status, json.load(response), response.headers


def test_health_static_security_and_versioned_contract(live_server: str) -> None:
    with urlopen(live_server + "/", timeout=5) as response:
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert b"DAILY CAREER RADAR" in response.read()

    status, payload, headers = request_json(live_server, "/api/v1/health")
    assert status == 200
    assert payload["success"] is True
    assert payload["data"]["status"] == "ok"
    assert headers["Access-Control-Allow-Origin"] is None

    for health_path in ("/livez", "/readyz"):
        status, payload, headers = request_json(live_server, health_path)
        assert status == 200
        assert payload["success"] is True
        assert payload["data"]["status"] == "ok"
        assert headers["Content-Type"].startswith("application/json")


def test_full_api_workflow_and_persistence(live_server: str) -> None:
    status, resume, _ = request_json(
        live_server,
        "/api/v1/resumes/analyze",
        method="POST",
        body={"text": SAMPLE_RESUME},
    )
    assert status == 201
    assert resume["data"]["id"]

    status, radar, _ = request_json(
        live_server,
        "/api/v1/radar/run",
        method="POST",
        body={"sources": ["demo"]},
    )
    assert status == 200
    assert radar["data"]["summary"]["shortlisted"] > 0

    top = radar["data"]["items"][0]
    assert len(top["job_key"]) == 32
    status, feedback, _ = request_json(
        live_server,
        "/api/v1/jobs/feedback",
        method="POST",
        body={"job_key": top["job_key"], "action": "saved", "job": top},
    )
    assert status == 200
    assert feedback["data"]["action"] == "saved"
    assert top["resume_evidence"]
    assert top["resume_evidence"][0]["text"]
    persisted_evidence = json.dumps(top["resume_evidence"], ensure_ascii=False)
    assert "13800138000" not in persisted_evidence
    assert "student@example.com" not in persisted_evidence
    status, application, _ = request_json(
        live_server,
        "/api/v1/applications",
        method="POST",
        body={
            "company_name": top["company"],
            "job_title": top["title"],
            "job_key": top["job_key"],
            "next_action": "准备投递材料",
        },
    )
    assert status == 201

    status, listing, _ = request_json(live_server, "/api/v1/applications")
    assert status == 200
    assert listing["data"]["statistics"]["total"] == 1
    assert listing["data"]["items"][0]["next_action"] == "准备投递材料"
    assert listing["data"]["items"][0]["id"] == application["data"]["id"]
    status, dashboard, _ = request_json(live_server, "/api/v1/dashboard")
    assert status == 200
    assert "result" not in dashboard["data"]["radar"]["last_run"]


def test_errors_use_http_status_and_safe_envelope(live_server: str) -> None:
    status, payload, _ = request_json(
        live_server,
        "/api/v1/radar/run",
        method="POST",
        body={"sources": ["demo"]},
    )
    assert status == 409
    assert payload["success"] is False
    assert payload["error"]["code"] == "resume_unavailable"
    assert payload["meta"]["request_id"]

    request = Request(
        live_server + "/api/v1/jobs/import",
        data=b"[]",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as captured:
        urlopen(request, timeout=5)
    assert captured.value.code == 422


def test_async_radar_is_idempotent_pollable_and_sanitised(live_server: str) -> None:
    status, _, _ = request_json(
        live_server,
        "/api/v1/resumes/analyze",
        method="POST",
        body={"text": SAMPLE_RESUME},
    )
    assert status == 201
    request_headers = {"Idempotency-Key": "browser-radar-run-0001"}

    status, accepted, response_headers = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={"sources": ["demo"]},
        headers=request_headers,
    )
    assert status == 202
    operation = accepted["data"]
    assert operation["status"] in {"queued", "running", "succeeded"}
    assert operation["deduplicated"] is False
    assert response_headers["Location"] == operation["status_url"]
    assert response_headers["Retry-After"] == "1"

    status, repeated, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={"sources": ["demo"]},
        headers=request_headers,
    )
    assert status == 202
    assert repeated["data"]["id"] == operation["id"]
    assert repeated["data"]["deduplicated"] is True

    for _ in range(40):
        status, polled, _ = request_json(live_server, operation["status_url"])
        assert status == 200
        task = polled["data"]
        if task["status"] in {"succeeded", "failed"}:
            break
        threading.Event().wait(0.1)
    assert task["status"] == "succeeded"
    assert task["started_at"]
    assert task["result"]["summary"]["shortlisted"] > 0
    assert not {"payload", "idempotency_key", "claim_token", "worker_id"} & set(task)

    status, listing, _ = request_json(live_server, "/api/v1/radar/runs")
    assert status == 200
    assert listing["data"]["items"][0]["id"] == operation["id"]
    assert listing["data"]["items"][0]["result"] is None
    assert "idempotency_key" not in json.dumps(listing, ensure_ascii=False)

    status, conflict, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={"sources": ["latest"]},
        headers=request_headers,
    )
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"


def test_async_radar_rejects_unsafe_idempotency_key(live_server: str) -> None:
    status, payload, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={},
        headers={"Idempotency-Key": "contains spaces"},
    )
    assert status == 422
    assert payload["error"]["code"] == "invalid_idempotency_key"


def test_async_radar_requires_an_idempotency_key(live_server: str) -> None:
    status, payload, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={},
    )
    assert status == 400
    assert payload["error"]["code"] == "idempotency_key_required"


def test_async_radar_failure_exposes_a_stable_error_envelope(live_server: str) -> None:
    status, accepted, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={"sources": ["demo"]},
        headers={"Idempotency-Key": "missing-resume-operation"},
    )
    assert status == 202

    for _ in range(40):
        status, polled, _ = request_json(
            live_server,
            accepted["data"]["status_url"],
        )
        assert status == 200
        task = polled["data"]
        if task["status"] == "failed":
            break
        threading.Event().wait(0.1)

    assert task["status"] == "failed"
    assert task["error"] == {
        "code": "resume_unavailable",
        "message": "求职雷达需要一份简历画像，请先在「简历画像」完成分析",
        "retryable": False,
    }


def test_untrusted_host_is_rejected_before_api_access(live_server: str) -> None:
    status, payload, _ = request_json(
        live_server,
        "/api/v1/dashboard",
        headers={"Host": "evil.example:3000"},
    )
    assert status == 421
    assert payload["error"]["code"] == "host_not_allowed"

    status, payload, _ = request_json(
        live_server,
        "/api/v1/radar/runs",
        method="POST",
        body={},
        headers={
            "Host": "evil.example:3000",
            "Idempotency-Key": "evil-host-radar",
        },
    )
    assert status == 421
    assert payload["error"]["code"] == "host_not_allowed"


def test_duplicate_host_headers_are_rejected(live_server: str) -> None:
    parsed = urlparse(live_server)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.putrequest("GET", "/api/v1/dashboard", skip_host=True)
        connection.putheader("Host", f"{parsed.hostname}:{parsed.port}")
        connection.putheader("Host", "evil.example:3000")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()

    assert response.status == 421
    assert payload["error"]["code"] == "host_not_allowed"


def test_head_cannot_bypass_host_or_route_validation(live_server: str) -> None:
    parsed = urlparse(live_server)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.putrequest("HEAD", "/api/v1/dashboard", skip_host=True)
        connection.putheader("Host", "evil.example:3000")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 421

        connection.close()
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.request("HEAD", "/")
        response = connection.getresponse()
        response.read()
        assert response.status == 405
        assert response.headers["Allow"] == "GET, POST, PATCH, DELETE, OPTIONS"
    finally:
        connection.close()


def test_disabled_workers_are_visible_and_do_not_accept_async_work(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path).with_overrides(
        background_workers_enabled=False,
        scheduler_enabled=True,
    )
    with running_server(settings) as root:
        status, readiness, _ = request_json(root, "/readyz")
        assert status == 503
        assert readiness["data"]["status"] == "degraded"
        assert readiness["data"]["async_radar"] == "unavailable"
        assert readiness["data"]["scheduler"] == "blocked_without_workers"

        status, rejected, _ = request_json(
            root,
            "/api/v1/radar/runs",
            method="POST",
            body={},
            headers={"Idempotency-Key": "workers-disabled-radar"},
        )
        assert status == 503
        assert rejected["error"]["code"] == "background_workers_disabled"


def test_async_endpoint_rejects_work_when_worker_threads_are_down(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path).with_overrides(background_workers_enabled=True)
    server = create_server(settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert server.workers.stop()
        status, rejected, headers = request_json(
            root,
            "/api/v1/radar/runs",
            method="POST",
            body={},
            headers={"Idempotency-Key": "workers-unavailable-radar"},
        )
        assert status == 503
        assert rejected["error"]["code"] == "background_workers_unavailable"
        assert headers["Retry-After"] == "5"
        assert server.service.repository.list_operation_jobs() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_probes_are_not_consumed_by_business_rate_limit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).with_overrides(
        background_workers_enabled=True,
        rate_limit_per_minute=10,
    )
    with running_server(settings) as root:
        for _ in range(10):
            request_json(root, "/api/v1/dashboard")
        status, _, _ = request_json(root, "/api/v1/dashboard")
        assert status == 429

        status, liveness, _ = request_json(root, "/livez")
        assert status == 200
        assert liveness["data"]["status"] == "ok"
        status, readiness, _ = request_json(root, "/readyz")
        assert status == 200
        assert readiness["data"]["status"] == "ok"
