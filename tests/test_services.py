import base64
import json
import smtplib
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
from jobsearch_mcp_server.worker import DeliveryUnknownError, RetryWithoutAttemptError
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
    private_resume = (
        SAMPLE_RESUME
        + "\n项目联系人：project-owner@example.com，国际电话：+1 (415) 555-2671"
        + "\n主页：https://linkedin.com/in/private-student"
        + "\n微信号：private_wechat"
    )
    service.analyse_resume({"text": private_resume})

    stored = service.repository.get_latest_resume()
    assert stored is not None
    assert stored["raw_text"] == ""
    assert stored["structured"]["basic_info"]["email"] == ""
    assert stored["structured"]["basic_info"]["phone"] == ""
    persisted_profile = json.dumps(stored, ensure_ascii=False)
    assert "project-owner@example.com" not in persisted_profile
    assert "415" not in persisted_profile
    assert "private-student" not in persisted_profile
    assert "private_wechat" not in persisted_profile

    evidence = service.repository.get_resume_evidence_text(stored["id"])
    assert evidence
    assert "student@example.com" not in evidence
    assert "13800138000" not in evidence
    assert "[邮箱已隐藏]" in evidence
    assert "[手机号已隐藏]" in evidence

    service.update_radar_settings({"sources": ["demo"], "min_score": 0})
    digest = service.run_radar()
    persisted = json.dumps(service.repository.get_state("latest_radar"), ensure_ascii=False)
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


def test_async_radar_lock_contention_requests_budget_free_defer(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    accepted = service.enqueue_radar_operation(
        {"sources": ["demo"]},
        idempotency_key="locked-radar-operation",
    )
    stored_job = service.repository.get_operation_job(int(accepted["id"]))
    assert stored_job is not None
    assert service._radar_lock.acquire(blocking=False)
    try:
        with pytest.raises(RetryWithoutAttemptError):
            service.execute_radar_operation(stored_job["payload"])
    finally:
        service._radar_lock.release()


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


def test_scheduled_radar_commits_email_outbox_and_hydrates_delivery(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path).with_overrides(
        smtp_host="smtp.example.com",
        smtp_user="sender@example.com",
        smtp_password="secret",
        smtp_from="sender@example.com",
    )
    service = CareerService(settings)
    service.analyse_resume({"text": SAMPLE_RESUME})
    service.update_radar_settings(
        {
            "sources": ["demo"],
            "min_score": 0,
            "email_enabled": True,
            "email_to": "student@example.com",
        }
    )

    digest = service.run_radar(
        trigger_type="scheduled",
        email_idempotency_key="radar-email:scheduled-test",
    )

    assert digest["email_delivery"]["status"] == "pending"
    message_id = digest["email_delivery"]["message_id"]
    message = service.repository.get_outbox_message(message_id)
    assert message is not None
    assert message["payload"]["summary"] == digest["summary"]
    assert message["payload"]["items"]
    assert "resume_evidence" not in message["payload"]["items"][0]
    assert set(message["payload"]["items"][0]) == {
        "title",
        "score",
        "company",
        "location",
        "salary",
        "why_fit",
        "resume_tips",
        "url",
    }
    claimed = service.repository.claim_outbox_message(worker_id="test-mailer")
    assert claimed is not None
    service.repository.complete_outbox_message(message_id, claimed["claim_token"])
    assert service.radar_overview()["latest"]["email_delivery"]["status"] == "sent"


def test_reclaimed_operation_reuses_completed_radar_result(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})
    accepted = service.enqueue_radar_operation(
        {"sources": ["demo"]},
        idempotency_key="restart-safe-radar",
    )
    stored_job = service.repository.get_operation_job(int(accepted["id"]))
    assert stored_job is not None

    first = service.execute_radar_operation(stored_job["payload"])
    second = service.execute_radar_operation(stored_job["payload"])

    assert second == first
    assert first["run_id"].startswith("operation-")
    assert len(service.repository.list_radar_runs()) == 1


@pytest.mark.parametrize("prior_status", ["running", "failed"])
def test_reclaimed_operation_resumes_a_noncompleted_radar_run(
    tmp_path: Path,
    prior_status: str,
) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})
    accepted = service.enqueue_radar_operation(
        {"sources": ["demo"]},
        idempotency_key=f"recover-{prior_status}-radar",
    )
    stored_job = service.repository.get_operation_job(int(accepted["id"]))
    assert stored_job is not None
    run_id = stored_job["payload"]["radar_run_id"]
    service.repository.create_radar_run(run_id, "manual")
    if prior_status == "failed":
        service.repository.finish_radar_run(
            run_id,
            status="failed",
            error_message="interrupted",
        )

    result = service.execute_radar_operation(stored_job["payload"])

    stored_run = service.repository.get_radar_run(run_id)
    assert result["summary"]["shortlisted"] > 0
    assert stored_run is not None
    assert stored_run["status"] == "completed"
    assert len([item for item in service.repository.list_radar_runs() if item["id"] == run_id]) == 1


def test_manual_and_scheduled_operations_have_distinct_run_identity(
    tmp_path: Path,
) -> None:
    service = CareerService(make_settings(tmp_path))
    shared_key = "scheduled:Asia/Shanghai:2026-07-29"
    manual = service.enqueue_radar_operation(
        {"sources": ["demo"]},
        idempotency_key=shared_key,
        trigger_type="manual",
    )
    scheduled = service.enqueue_radar_operation(
        {"sources": ["demo"]},
        idempotency_key=shared_key,
        trigger_type="scheduled",
    )
    manual_job = service.repository.get_operation_job(int(manual["id"]))
    scheduled_job = service.repository.get_operation_job(int(scheduled["id"]))

    assert manual_job is not None
    assert scheduled_job is not None
    assert manual_job["payload"]["radar_run_id"] != scheduled_job["payload"]["radar_run_id"]


@pytest.mark.parametrize(
    "delivery_error",
    [
        smtplib.SMTPServerDisconnected("connection closed"),
        TimeoutError("acknowledgement timeout"),
    ],
)
def test_ambiguous_smtp_transport_failure_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_error: Exception,
) -> None:
    class DisconnectingSMTP:
        def __init__(self, *_args: object, **_kwargs: object):
            pass

        def __enter__(self) -> "DisconnectingSMTP":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self, **_kwargs: object) -> None:
            pass

        def login(self, *_args: object) -> None:
            pass

        def send_message(self, _message: object) -> None:
            raise delivery_error

    monkeypatch.setattr(
        "jobsearch_mcp_server.services.smtplib.SMTP",
        DisconnectingSMTP,
    )
    service = CareerService(
        make_settings(tmp_path).with_overrides(
            smtp_host="smtp.example.com",
            smtp_user="sender@example.com",
            smtp_password="secret",
            smtp_from="sender@example.com",
        )
    )
    message = {
        "id": 42,
        "recipient": "student@example.com",
        "payload": {
            "items": [],
            "summary": {"collected": 0, "after_filter": 0, "shortlisted": 0},
        },
    }

    with pytest.raises(DeliveryUnknownError):
        service.send_outbox_message(message)


def test_email_digest_rejects_non_http_job_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_messages: list[str] = []

    class CapturingSMTP:
        def __init__(self, *_args: object, **_kwargs: object):
            pass

        def __enter__(self) -> "CapturingSMTP":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self, **_kwargs: object) -> None:
            pass

        def login(self, *_args: object) -> None:
            pass

        def send_message(self, message: object) -> None:
            sent_messages.append(str(message))

    monkeypatch.setattr("jobsearch_mcp_server.services.smtplib.SMTP", CapturingSMTP)
    service = CareerService(
        make_settings(tmp_path).with_overrides(
            smtp_host="smtp.example.com",
            smtp_user="sender@example.com",
            smtp_password="secret",
            smtp_from="sender@example.com",
        )
    )
    service._send_digest_email(
        "student@example.com",
        {
            "summary": {"collected": 1, "after_filter": 1, "shortlisted": 1},
            "items": [
                {
                    "title": "Python 实习生",
                    "score": 90,
                    "company": "示例公司",
                    "location": "上海",
                    "salary": "面议",
                    "why_fit": "技能匹配",
                    "resume_tips": ["突出 Python"],
                    "url": "javascript:alert(1)",
                }
            ],
        },
    )

    assert len(sent_messages) == 1
    assert "javascript:" not in sent_messages[0].lower()


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
