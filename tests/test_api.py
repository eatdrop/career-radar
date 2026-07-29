from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from jobsearch_mcp_server.webapp import create_server
from tests.helpers import SAMPLE_RESUME, make_settings


@pytest.fixture()
def live_server(tmp_path: Path):
    server = create_server(make_settings(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(
    root: str, path: str, *, method: str = "GET", body: dict | None = None
) -> tuple[int, dict, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(root + path, data=data, method=method, headers=headers)
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
    assert top["resume_evidence"]
    assert top["resume_evidence"][0]["text"]
    persisted_evidence = json.dumps(top["resume_evidence"], ensure_ascii=False)
    assert "13800138000" not in persisted_evidence
    assert "student@example.com" not in persisted_evidence
    status, application, _ = request_json(
        live_server,
        "/api/v1/applications",
        method="POST",
        body={"company_name": top["company"], "job_title": top["title"]},
    )
    assert status == 201

    status, listing, _ = request_json(live_server, "/api/v1/applications")
    assert status == 200
    assert listing["data"]["statistics"]["total"] == 1
    assert listing["data"]["items"][0]["id"] == application["data"]["id"]


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
