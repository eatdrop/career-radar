import base64
import json
import ssl
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

import jobsearch_mcp_server.repository as repository_module
from jobsearch_mcp_server.services import (
    MAX_DOCX_DOCUMENT_BYTES,
    MAX_RESUME_LINE_CHARS,
    AppError,
    CareerService,
)
from tests.helpers import SAMPLE_RESUME, make_settings


def test_core_workflow_runs_without_optional_dependencies(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))

    assert service.health()["status"] == "ok"
    resume = service.analyse_resume({"text": SAMPLE_RESUME})
    assert resume["score"]["total"] > 0
    assert "Python" in resume["structured"]["skills"]["programming_languages"]

    service.update_radar_settings({"sources": ["demo"], "min_score": 0})
    digest = service.run_radar({"sources": ["demo"]})

    assert digest["summary"]["collected"] == 3
    assert digest["summary"]["shortlisted"] == 3
    assert all("why_fit" in item for item in digest["items"])
    assert all(item["resume_tips"] for item in digest["items"])


def test_radar_requires_resume_evidence(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))

    with pytest.raises(AppError) as captured:
        service.run_radar({"sources": ["demo"]})

    assert captured.value.code == "resume_unavailable"
    assert service.repository.list_radar_runs(1)[0]["status"] == "failed"


def test_new_grad_filter_removes_senior_and_experience_gate(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})
    service.repository.set_state(
        "latest_jobs",
        [
            {
                "title": "Senior Python Engineer",
                "company": "A",
                "description": "要求 5 年以上经验",
            },
            {
                "title": "Python 开发实习生",
                "company": "B",
                "description": "Python FastAPI 校招",
            },
        ],
    )
    service.update_radar_settings(
        {
            "sources": ["latest"],
            "new_grad_only": True,
            "exclude_keywords": ["Senior"],
            "min_score": 0,
        }
    )

    digest = service.run_radar()

    assert digest["summary"]["collected"] == 2
    assert digest["summary"]["filtered_out"] == 1
    assert digest["items"][0]["company"] == "B"


def test_application_crud_and_status_mapping(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    created = service.add_application(
        {
            "company_name": "星云智能",
            "job_title": "大模型应用开发实习生",
            "status": "submitted",
            "applied_at": "2026-07-29",
        }
    )
    assert created["status"] == "submitted"

    updated = service.update_application(
        created["id"], {"status": "interviewing", "notes": "准备项目复盘"}
    )
    assert updated["status"] == "interviewing"
    assert service.list_applications()["statistics"]["total"] == 1
    assert service.delete_application(created["id"])["deleted"] is True
    assert service.list_applications()["statistics"]["total"] == 0


def test_invalid_schedule_and_email_are_rejected(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))

    with pytest.raises(AppError) as invalid_schedule:
        service.update_radar_settings({"schedule_time": "25:80"})
    assert invalid_schedule.value.code == "invalid_schedule"

    with pytest.raises(AppError) as invalid_email:
        service.update_radar_settings({"email_to": "not-an-email"})
    assert invalid_email.value.code == "invalid_email"


@pytest.mark.parametrize(
    ("method_name", "payload"),
    [
        ("update_radar_settings", {"enabled": "false"}),
        ("update_radar_settings", {"new_grad_only": 1}),
        ("update_radar_settings", {"sources": "demo"}),
        ("update_radar_settings", {"sources": [{}]}),
        ("crawl_jobs", {"keyword": "Python", "city": "上海", "use_demo": "false"}),
    ],
)
def test_boolean_and_list_inputs_are_strictly_validated(
    tmp_path: Path, method_name: str, payload: dict
) -> None:
    service = CareerService(make_settings(tmp_path))

    with pytest.raises(AppError) as captured:
        getattr(service, method_name)(payload)

    assert captured.value.status == 422


def test_radar_payload_boolean_and_sources_are_strict(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})

    for payload in ({"use_demo": "false"}, {"send_email": 1}, {"sources": [1]}):
        with pytest.raises(AppError) as captured:
            service.run_radar(payload)
        assert captured.value.status == 422


def test_privacy_mode_keeps_redacted_evidence_without_raw_contacts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).with_overrides(store_raw_resume=False)
    service = CareerService(settings)
    service.analyse_resume({"text": SAMPLE_RESUME})

    stored = service.repository.get_latest_resume()
    assert stored is not None
    assert stored["raw_text"] == ""
    assert stored["structured"]["basic_info"]["email"] == ""
    assert stored["structured"]["basic_info"]["phone"] == ""

    evidence = service.repository.get_resume_evidence_text(stored["id"])
    assert evidence
    assert "student@example.com" not in evidence
    assert "13800138000" not in evidence
    assert "[邮箱已隐藏]" in evidence
    assert "[手机号已隐藏]" in evidence

    service.update_radar_settings({"sources": ["demo"], "min_score": 0})
    digest = service.run_radar()
    persisted = json.dumps(
        service.repository.get_state("latest_radar"), ensure_ascii=False
    )
    assert digest["summary"]["shortlisted"] == 3
    assert "student@example.com" not in persisted
    assert "13800138000" not in persisted


def test_resume_evidence_is_redacted_even_when_raw_storage_is_enabled(
    tmp_path: Path,
) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})

    stored = service.repository.get_latest_resume()
    assert stored is not None
    assert "student@example.com" in stored["raw_text"]
    evidence = service.repository.get_resume_evidence_text(stored["id"])
    assert "student@example.com" not in evidence
    assert "13800138000" not in evidence


def test_compressed_resume_rejects_long_line_before_analysis(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    long_line = "技" * (MAX_RESUME_LINE_CHARS + 1)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{long_line}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)

    with pytest.raises(AppError) as captured:
        service.analyse_resume(
            {
                "file": {
                    "name": "resume.docx",
                    "content_base64": base64.b64encode(buffer.getvalue()).decode(),
                }
            }
        )

    assert captured.value.code == "resume_line_too_long"


def test_docx_rejects_oversized_expanded_document(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            b"x" * (MAX_DOCX_DOCUMENT_BYTES + 1),
        )

    with pytest.raises(AppError) as captured:
        service.analyse_resume(
            {
                "file": {
                    "name": "oversized.docx",
                    "content_base64": base64.b64encode(buffer.getvalue()).decode(),
                }
            }
        )

    assert captured.value.code == "expanded_file_too_large"


def test_radar_rejects_a_second_in_process_run(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    assert service._radar_lock.acquire(blocking=False)
    try:
        with pytest.raises(AppError) as captured:
            service.run_radar()
    finally:
        service._radar_lock.release()

    assert captured.value.status == 409
    assert captured.value.code == "radar_already_running"
    assert service.repository.list_radar_runs() == []


def test_latest_resume_uses_rowid_as_timestamp_tiebreaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CareerService(make_settings(tmp_path))
    monkeypatch.setattr(
        repository_module,
        "utc_now",
        lambda: "2026-07-29T10:00:00.000000+00:00",
    )

    first = service.analyse_resume({"text": SAMPLE_RESUME})
    second = service.analyse_resume({"text": SAMPLE_RESUME.replace("沈同学", "林同学")})

    assert first["id"] != second["id"]
    assert service.repository.get_latest_resume()["id"] == second["id"]


def test_smtp_starttls_uses_verified_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ssl.SSLContext] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int):
            assert host == "smtp.example.com"
            assert port == 587
            assert timeout == 20

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def starttls(self, *, context: ssl.SSLContext) -> None:
            contexts.append(context)

        def login(self, user: str, password: str) -> None:
            assert user == "sender@example.com"
            assert password == "secret"

        def send_message(self, message: object) -> None:
            assert message is not None

    monkeypatch.setattr("jobsearch_mcp_server.services.smtplib.SMTP", FakeSMTP)
    settings = make_settings(tmp_path).with_overrides(
        smtp_host="smtp.example.com",
        smtp_user="sender@example.com",
        smtp_password="secret",
        smtp_from="sender@example.com",
        smtp_use_tls=True,
    )
    service = CareerService(settings)

    result = service._send_digest_email(
        "student@example.com",
        {
            "items": [],
            "summary": {"collected": 0, "after_filter": 0, "shortlisted": 0},
        },
    )

    assert result["status"] == "sent"
    assert len(contexts) == 1
    assert contexts[0].check_hostname is True
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED


def test_resume_enhancement_requires_explicit_ai_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CareerService(
        make_settings(tmp_path).with_overrides(ai_api_key="configured-for-test")
    )
    service.analyse_resume({"text": SAMPLE_RESUME})
    calls: list[bool] = []

    def fake_ai(*args: object, **kwargs: object) -> str:
        calls.append(True)
        return "AI result"

    monkeypatch.setattr(service, "_ai_enhance", fake_ai)
    payload = {
        "resume_source": "latest",
        "jd": "岗位名称：Python 开发实习生\n职责：使用 Python 和 FastAPI 开发接口。",
        "template": "standard",
    }

    local = service.enhance_resume(payload)
    assert local["mode"] == "local"
    assert calls == []

    ai = service.enhance_resume({**payload, "allow_ai": True})
    assert ai["mode"] == "ai"
    assert calls == [True]

    with pytest.raises(AppError) as captured:
        service.enhance_resume({**payload, "allow_ai": "true"})
    assert captured.value.status == 422
