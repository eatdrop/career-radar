import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobsearch_mcp_server.repository import SQLiteRepository
from jobsearch_mcp_server.services import AppError
from jobsearch_mcp_server.worker import (
    DeliveryUnknownError,
    DurableWorkerSupervisor,
    RetryWithoutAttemptError,
)


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def _supervisor(
    repository: SQLiteRepository,
    handlers,
    sender=lambda _message: None,
) -> DurableWorkerSupervisor:
    return DurableWorkerSupervisor(
        repository,
        handlers,
        sender,
        poll_interval=0.01,
        lease_seconds=1,
        retry_base_seconds=0.02,
        retry_max_seconds=0.04,
        retry_jitter_ratio=0,
        worker_id="test-worker",
    )


def test_supervisor_completes_operation_and_outbox(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    delivered: list[tuple[str, dict]] = []
    job = repository.enqueue_operation_job(
        "echo",
        {"value": 7},
        idempotency_key="worker-success",
    )
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="mail-success",
    )
    supervisor = _supervisor(
        repository,
        {"echo": lambda payload: {"answer": payload["value"] * 2}},
        lambda item: delivered.append((item["recipient"], item["payload"])),
    )

    assert supervisor.start() is True
    assert supervisor.start() is False
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "succeeded")
    _wait_until(lambda: repository.get_outbox_message(message["id"])["status"] == "sent")
    assert supervisor.stop() is True

    completed = repository.get_operation_job(job["id"])
    assert completed is not None
    assert completed["result"] == {"answer": 14}
    assert delivered == [("student@example.com", {"subject": "日报"})]


def test_retryable_app_error_is_retried_with_backoff(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    attempts = 0

    def eventually_succeeds(payload: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AppError(409, "temporarily_busy", "资源暂时繁忙")
        return payload

    job = repository.enqueue_operation_job(
        "flaky",
        {"ok": True},
        idempotency_key="worker-retry",
        max_attempts=3,
    )
    supervisor = _supervisor(repository, {"flaky": eventually_succeeds})

    supervisor.start()
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "succeeded")
    supervisor.stop()

    completed = repository.get_operation_job(job["id"])
    assert completed is not None
    assert completed["attempts"] == 2
    assert completed["result"] == {"ok": True}
    assert attempts == 2


def test_non_retryable_client_error_fails_permanently(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    calls = 0

    def invalid_payload(_payload: dict) -> None:
        nonlocal calls
        calls += 1
        raise AppError(422, "invalid_payload", "参数格式不正确")

    job = repository.enqueue_operation_job(
        "validate",
        {},
        idempotency_key="worker-permanent",
        max_attempts=5,
    )
    supervisor = _supervisor(repository, {"validate": invalid_payload})

    supervisor.start()
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "failed")
    supervisor.stop()

    failed = repository.get_operation_job(job["id"])
    assert failed is not None
    assert failed["attempts"] == 1
    assert json.loads(failed["error"]) == {
        "code": "invalid_payload",
        "message": "参数格式不正确",
        "retryable": False,
    }
    assert calls == 1


def test_known_contention_is_deferred_without_consuming_budget(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    calls = 0

    def waits_for_lock(payload: dict) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryWithoutAttemptError("radar is busy", retry_after_seconds=0.02)
        return payload

    job = repository.enqueue_operation_job(
        "radar",
        {"safe": True},
        idempotency_key="worker-deferred-contention",
        max_attempts=1,
    )
    supervisor = _supervisor(repository, {"radar": waits_for_lock})

    supervisor.start()
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "succeeded")
    supervisor.stop()

    completed = repository.get_operation_job(job["id"])
    assert completed is not None
    assert completed["attempts"] == 1
    assert completed["max_attempts"] == 1
    assert calls == 2


def test_operation_completion_write_failure_uses_bounded_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {"safe": True},
        idempotency_key="operation-completion-write-failure",
        max_attempts=1,
    )
    supervisor = _supervisor(repository, {"radar": lambda payload: payload})
    complete = repository.complete_operation_job

    def unavailable_completion(*_args, **_kwargs):
        raise sqlite3.OperationalError("database temporarily unavailable")

    monkeypatch.setattr(repository, "complete_operation_job", unavailable_completion)
    assert supervisor._process_one_operation() is True
    running = repository.get_operation_job(job["id"])
    assert running is not None
    assert running["status"] == "running"
    assert running["attempts"] == 1

    monkeypatch.setattr(repository, "complete_operation_job", complete)
    future = datetime.now(UTC) + timedelta(minutes=10)
    reconciliation = repository.claim_operation_job(
        worker_id="reconciler",
        now=future,
        stale_before=future - timedelta(minutes=1),
        lease_seconds=60,
    )
    assert reconciliation is not None
    assert reconciliation["attempts"] == 2
    assert reconciliation["max_attempts"] == 2
    completed = repository.complete_operation_job(
        job["id"],
        reconciliation["claim_token"],
        reconciliation["payload"],
    )
    assert completed is not None
    assert completed["status"] == "succeeded"


def test_outbox_completion_write_failure_is_quarantined_without_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    delivery_count = 0
    retry_called = False
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="outbox-completion-write-failure",
    )

    def sender(_message: dict) -> None:
        nonlocal delivery_count
        delivery_count += 1

    def unavailable_completion(*_args, **_kwargs):
        raise sqlite3.OperationalError("database temporarily unavailable")

    def forbidden_retry(*_args, **_kwargs):
        nonlocal retry_called
        retry_called = True
        raise AssertionError("a delivered message must not be retried")

    monkeypatch.setattr(repository, "complete_outbox_message", unavailable_completion)
    monkeypatch.setattr(repository, "retry_outbox_message", forbidden_retry)
    supervisor = _supervisor(repository, {}, sender)

    assert supervisor._process_one_outbox_message() is True
    stored = repository.get_outbox_message(message["id"])
    assert stored is not None
    assert stored["status"] == "delivery_unknown"
    assert delivery_count == 1
    assert retry_called is False


def test_stop_prevents_new_work_and_supervisor_can_restart(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    processed = threading.Event()
    supervisor = _supervisor(
        repository,
        {"work": lambda payload: processed.set() or payload},
    )

    supervisor.start()
    assert supervisor.stop() is True
    assert supervisor.is_running is False
    job = repository.enqueue_operation_job(
        "work",
        {},
        idempotency_key="worker-after-stop",
    )
    supervisor.wake()
    time.sleep(0.05)
    assert processed.is_set() is False
    assert repository.get_operation_job(job["id"])["status"] == "queued"

    assert supervisor.start() is True
    _wait_until(lambda: processed.is_set())
    assert supervisor.stop() is True


def test_expired_operation_claim_is_recovered(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    old_now = datetime.now(UTC) - timedelta(minutes=10)
    job = repository.enqueue_operation_job(
        "recover",
        {"run": True},
        idempotency_key="worker-expired",
        available_at=old_now,
    )
    abandoned = repository.claim_operation_job(
        worker_id="crashed-worker",
        now=old_now,
        lease_seconds=1,
    )
    assert abandoned is not None

    supervisor = _supervisor(repository, {"recover": lambda payload: payload})
    supervisor.start()
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "succeeded")
    supervisor.stop()

    recovered = repository.get_operation_job(job["id"])
    assert recovered is not None
    assert recovered["attempts"] == 2
    assert recovered["claim_token"] is None


def test_long_operation_renews_its_lease(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    callback_started = threading.Event()
    callback_release = threading.Event()

    def long_operation(payload: dict) -> dict:
        callback_started.set()
        assert callback_release.wait(2)
        return payload

    job = repository.enqueue_operation_job(
        "long",
        {"safe": True},
        idempotency_key="worker-heartbeat",
    )
    supervisor = _supervisor(repository, {"long": long_operation})
    supervisor.start()
    assert callback_started.wait(1)
    initial = repository.get_operation_job(job["id"])
    assert initial is not None
    initial_heartbeat = initial["heartbeat_at"]

    _wait_until(
        lambda: repository.get_operation_job(job["id"])["heartbeat_at"] != initial_heartbeat
    )
    competing_claim = repository.claim_operation_job(
        worker_id="competing-worker",
        lease_seconds=1,
    )
    assert competing_claim is None

    callback_release.set()
    _wait_until(lambda: repository.get_operation_job(job["id"])["status"] == "succeeded")
    assert supervisor.stop() is True


def test_delivery_unknown_is_quarantined_without_retry(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    delivery_attempts = 0

    def ambiguous_delivery(_message: dict) -> None:
        nonlocal delivery_attempts
        delivery_attempts += 1
        raise DeliveryUnknownError("SMTP connection closed after DATA")

    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="mail-ambiguous",
        max_attempts=5,
    )
    supervisor = _supervisor(repository, {}, ambiguous_delivery)

    supervisor.start()
    _wait_until(
        lambda: repository.get_outbox_message(message["id"])["status"] == "delivery_unknown"
    )
    time.sleep(0.08)
    assert supervisor.stop() is True

    ambiguous = repository.get_outbox_message(message["id"])
    assert ambiguous is not None
    assert ambiguous["attempts"] == 1
    assert delivery_attempts == 1
