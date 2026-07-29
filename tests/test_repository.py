import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jobsearch_mcp_server.repository import SCHEMA_VERSION, SQLiteRepository

READY_AT = "2026-07-29T08:00:00+00:00"
RETRY_AT = "2026-07-29T09:00:00+00:00"
AFTER_RETRY = "2026-07-29T09:01:00+00:00"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable")
def test_repository_restricts_personal_data_permissions(tmp_path: Path) -> None:
    data_dir = tmp_path / "personal-data"
    repository = SQLiteRepository(data_dir)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(repository.path.stat().st_mode) == 0o600


def test_schema_migration_upgrades_v1_without_losing_records(tmp_path: Path) -> None:
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir()
    database = data_dir / "job_tracker.db"
    with sqlite3.connect(database) as connection:
        SQLiteRepository._migrate_v1(connection)
        connection.execute(
            """
            CREATE TABLE schema_meta (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (1, ?)",
            (READY_AT,),
        )
        connection.execute(
            """
            INSERT INTO app_state(key, value_json, updated_at)
            VALUES ('legacy-setting', '{"enabled":true}', ?)
            """,
            (READY_AT,),
        )
        connection.commit()

    repository = SQLiteRepository(data_dir)

    assert repository.get_schema_version() == SCHEMA_VERSION
    assert repository.get_state("legacy-setting") == {"enabled": True}
    with sqlite3.connect(repository.path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        versions = [
            row[0] for row in connection.execute("SELECT version FROM schema_meta ORDER BY version")
        ]
    assert {"operation_jobs", "outbox_messages"} <= tables
    assert versions == [1, 2, 3]


def test_schema_migration_upgrades_v2_queue_rows_and_adds_leases(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "v2-data"
    data_dir.mkdir()
    database = data_dir / "job_tracker.db"
    with sqlite3.connect(database) as connection:
        SQLiteRepository._migrate_v1(connection)
        SQLiteRepository._migrate_v2(connection)
        connection.execute(
            """
            CREATE TABLE schema_meta (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            [(1, READY_AT), (2, READY_AT)],
        )
        connection.execute(
            """
            INSERT INTO operation_jobs(
                job_type, payload, available_at, idempotency_key,
                created_at, updated_at
            )
            VALUES ('radar', '{"source":"legacy"}', ?, 'legacy-job', ?, ?)
            """,
            (READY_AT, READY_AT, READY_AT),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                recipient, payload, available_at, idempotency_key,
                created_at, updated_at
            )
            VALUES (
                'student@example.com', '{"subject":"legacy"}', ?,
                'legacy-message', ?, ?
            )
            """,
            (READY_AT, READY_AT, READY_AT),
        )
        connection.commit()

    repository = SQLiteRepository(data_dir)

    assert repository.get_schema_version() == 3
    assert repository.list_operation_jobs()[0]["payload"] == {"source": "legacy"}
    assert repository.list_outbox_messages()[0]["payload"] == {"subject": "legacy"}
    with sqlite3.connect(database) as connection:
        operation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(operation_jobs)")
        }
        outbox_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(outbox_messages)")
        }
    assert {"lease_until_epoch", "heartbeat_at"} <= operation_columns
    assert {"lease_until_epoch", "heartbeat_at"} <= outbox_columns


def test_unversioned_legacy_database_is_adopted_idempotently(tmp_path: Path) -> None:
    data_dir = tmp_path / "unversioned-data"
    data_dir.mkdir()
    database = data_dir / "job_tracker.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE app_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO app_state(key, value_json, updated_at)
            VALUES ('preserved', '"yes"', ?)
            """,
            (READY_AT,),
        )
        connection.commit()

    first = SQLiteRepository(data_dir)
    second = SQLiteRepository(data_dir)

    assert first.get_state("preserved") == "yes"
    assert second.get_schema_version() == SCHEMA_VERSION
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
    assert count == SCHEMA_VERSION


def test_future_schema_version_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "future-data"
    data_dir.mkdir()
    database = data_dir / "job_tracker.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, READY_AT),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="newer than this application"):
        SQLiteRepository(data_dir)


def test_operation_and_outbox_enqueue_are_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")

    first_job = repository.enqueue_operation_job(
        "radar",
        {"source": "demo"},
        idempotency_key="radar:2026-07-29",
        max_attempts=4,
        available_at=READY_AT,
    )
    repeated_job = repository.enqueue_operation_job(
        "radar",
        {"source": "must-not-replace-original"},
        idempotency_key="radar:2026-07-29",
        max_attempts=1,
        available_at=RETRY_AT,
    )
    same_job, was_created = repository.enqueue_operation_job_once(
        "radar",
        {"source": "must-not-replace-original"},
        idempotency_key="radar:2026-07-29",
        available_at=RETRY_AT,
    )
    first_message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="digest:2026-07-29:student",
        max_attempts=5,
        available_at=READY_AT,
    )
    repeated_message = repository.enqueue_outbox_message(
        "other@example.com",
        {"subject": "must-not-replace-original"},
        idempotency_key="digest:2026-07-29:student",
        available_at=RETRY_AT,
    )

    assert repeated_job["id"] == first_job["id"]
    assert same_job["id"] == first_job["id"]
    assert was_created is False
    assert repository.get_operation_job_by_idempotency_key("radar:2026-07-29") == first_job
    assert repeated_job["payload"] == {"source": "demo"}
    assert repeated_job["max_attempts"] == 4
    assert repository.list_operation_jobs() == [first_job]
    assert repeated_message["id"] == first_message["id"]
    assert repeated_message["recipient"] == "student@example.com"
    assert repeated_message["payload"] == {"subject": "日报"}
    assert repository.list_outbox_messages() == [first_message]


def test_operation_claim_is_atomic_across_threads(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {"sources": ["demo"]},
        idempotency_key="atomic-radar",
        available_at=READY_AT,
    )
    barrier = threading.Barrier(2)

    def claim(worker_id: str) -> dict | None:
        barrier.wait()
        return repository.claim_operation_job(
            worker_id=worker_id,
            now=READY_AT,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job["id"]
    assert claimed[0]["status"] == "running"
    assert claimed[0]["attempts"] == 1
    assert len(claimed[0]["claim_token"]) == 32


def test_operation_retry_honours_available_at_and_attempt_budget(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {},
        idempotency_key="retry-radar",
        max_attempts=2,
        available_at=READY_AT,
    )
    first_claim = repository.claim_operation_job(
        worker_id="worker-a",
        now=READY_AT,
    )
    assert first_claim is not None

    retrying = repository.retry_operation_job(
        job["id"],
        first_claim["claim_token"],
        error="temporary upstream error",
        available_at=RETRY_AT,
    )
    assert retrying is not None
    assert retrying["status"] == "retrying"
    assert retrying["error"] == "temporary upstream error"
    assert (
        repository.claim_operation_job(
            worker_id="worker-b",
            now="2026-07-29T08:59:59+00:00",
        )
        is None
    )

    second_claim = repository.claim_operation_job(
        worker_id="worker-b",
        now=AFTER_RETRY,
    )
    assert second_claim is not None
    assert second_claim["attempts"] == 2
    exhausted = repository.retry_operation_job(
        job["id"],
        second_claim["claim_token"],
        error="still unavailable",
        available_at="2026-07-29T10:00:00+00:00",
    )
    assert exhausted is not None
    assert exhausted["status"] == "failed"
    assert exhausted["completed_at"] is not None
    assert (
        repository.claim_operation_job(
            worker_id="worker-c",
            now="2026-07-29T11:00:00+00:00",
        )
        is None
    )


def test_operation_contention_can_be_deferred_without_spending_retry_budget(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {},
        idempotency_key="deferred-radar",
        max_attempts=1,
        available_at=READY_AT,
    )
    claimed = repository.claim_operation_job(worker_id="worker-a", now=READY_AT)
    assert claimed is not None

    deferred = repository.defer_operation_job(
        job["id"],
        claimed["claim_token"],
        error="radar lock is busy",
        available_at=RETRY_AT,
    )

    assert deferred is not None
    assert deferred["status"] == "retrying"
    assert deferred["attempts"] == 0
    reclaimed = repository.claim_operation_job(worker_id="worker-b", now=AFTER_RETRY)
    assert reclaimed is not None
    assert reclaimed["attempts"] == 1


def test_last_operation_claim_gets_one_bounded_reconciliation_attempt(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {},
        idempotency_key="last-attempt-reconciliation",
        max_attempts=1,
        available_at=READY_AT,
    )
    original = repository.claim_operation_job(worker_id="worker-a", now=READY_AT)
    assert original is not None

    reconciliation = repository.claim_operation_job(
        worker_id="worker-b",
        now=RETRY_AT,
        stale_before="2026-07-29T08:30:00+00:00",
    )
    assert reconciliation is not None
    assert reconciliation["attempts"] == 2
    assert reconciliation["max_attempts"] == 2
    assert reconciliation["claim_token"] != original["claim_token"]

    assert (
        repository.claim_operation_job(
            worker_id="worker-c",
            now="2026-07-29T10:00:00+00:00",
            stale_before="2026-07-29T09:30:00+00:00",
        )
        is None
    )
    failed = repository.get_operation_job(job["id"])
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["attempts"] == 2
    assert failed["max_attempts"] == 2


def test_reconciliation_budget_is_only_granted_to_the_claimed_stale_job(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    jobs = [
        repository.enqueue_operation_job(
            "radar",
            {},
            idempotency_key=f"bounded-reconciliation-{index}",
            max_attempts=1,
            available_at=READY_AT,
        )
        for index in range(2)
    ]
    original_claims = [
        repository.claim_operation_job(
            worker_id=f"worker-{index}",
            now=READY_AT,
        )
        for index in range(2)
    ]
    assert all(original_claims)

    selected = repository.claim_operation_job(
        worker_id="reconciler",
        now=RETRY_AT,
        stale_before="2026-07-29T08:30:00+00:00",
    )
    assert selected is not None
    unselected_index = 1 if selected["id"] == jobs[0]["id"] else 0
    unselected = repository.get_operation_job(jobs[unselected_index]["id"])
    assert unselected is not None
    assert unselected["max_attempts"] == 1
    assert unselected["attempts"] == 1

    late_failure = repository.retry_operation_job(
        unselected["id"],
        original_claims[unselected_index]["claim_token"],
        error="late worker failure",
        available_at=AFTER_RETRY,
    )
    assert late_failure is not None
    assert late_failure["status"] == "failed"
    assert late_failure["max_attempts"] == 1


def test_stale_operation_is_reclaimed_and_old_worker_is_fenced(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {},
        idempotency_key="stale-radar",
        available_at=READY_AT,
    )
    original = repository.claim_operation_job(
        worker_id="worker-a",
        now=READY_AT,
    )
    assert original is not None
    renewed = repository.renew_operation_job(
        job["id"],
        original["claim_token"],
        now="2026-07-29T08:04:00+00:00",
        lease_seconds=300,
    )
    assert renewed is not None
    assert isinstance(renewed["lease_until_epoch"], int)
    assert renewed["heartbeat_at"].startswith("2026-07-29T08:04:00")
    assert (
        repository.claim_operation_job(
            worker_id="worker-too-early",
            now="2026-07-29T08:06:00+00:00",
        )
        is None
    )
    reclaimed = repository.claim_operation_job(
        worker_id="worker-b",
        now="2026-07-29T09:00:00+00:00",
        stale_before="2026-07-29T08:30:00+00:00",
    )

    assert reclaimed is not None
    assert reclaimed["id"] == job["id"]
    assert reclaimed["attempts"] == 2
    assert reclaimed["claim_token"] != original["claim_token"]
    assert (
        repository.complete_operation_job(
            job["id"],
            original["claim_token"],
            {"ignored": True},
        )
        is None
    )
    completed = repository.complete_operation_job(
        job["id"],
        reclaimed["claim_token"],
        {"shortlisted": 8},
    )
    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["result"] == {"shortlisted": 8}


def test_outbox_retry_stale_reclaim_and_terminal_transitions(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"html": "<p>日报</p>"},
        idempotency_key="outbox-retry",
        max_attempts=3,
        available_at=READY_AT,
    )
    first = repository.claim_outbox_message(
        worker_id="mailer-a",
        now=READY_AT,
    )
    assert first is not None
    retrying = repository.retry_outbox_message(
        message["id"],
        first["claim_token"],
        error="smtp timeout",
        available_at=RETRY_AT,
    )
    assert retrying is not None
    assert retrying["status"] == "retrying"
    assert retrying["last_error"] == "smtp timeout"

    second = repository.claim_outbox_message(
        worker_id="mailer-b",
        now=AFTER_RETRY,
    )
    assert second is not None
    reclaimed = repository.claim_outbox_message(
        worker_id="mailer-c",
        now="2026-07-29T10:00:00+00:00",
        stale_before="2026-07-29T09:30:00+00:00",
    )
    assert reclaimed is None
    unknown = repository.get_outbox_message(message["id"])
    assert unknown is not None
    assert unknown["status"] == "delivery_unknown"
    assert unknown["attempts"] == 2
    assert repository.complete_outbox_message(message["id"], second["claim_token"]) is None
    scheduled = repository.resolve_outbox_delivery_unknown(
        message["id"],
        delivered=False,
        available_at="2026-07-29T10:01:00+00:00",
        note="provider confirms no acceptance",
    )
    assert scheduled is not None
    reclaimed = repository.claim_outbox_message(
        worker_id="mailer-c",
        now="2026-07-29T10:02:00+00:00",
    )
    assert reclaimed is not None
    assert reclaimed["attempts"] == 3
    sent = repository.complete_outbox_message(
        message["id"],
        reclaimed["claim_token"],
    )
    assert sent is not None
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None

    fatal = repository.enqueue_outbox_message(
        "invalid@example.com",
        {},
        idempotency_key="outbox-fatal",
        available_at=READY_AT,
    )
    fatal_claim = repository.claim_outbox_message(
        worker_id="mailer-d",
        now="2026-07-29T11:00:00+00:00",
    )
    assert fatal_claim is not None
    failed = repository.fail_outbox_message(
        fatal["id"],
        fatal_claim["claim_token"],
        error="recipient rejected",
    )
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["last_error"] == "recipient rejected"


def test_outbox_delivery_unknown_requires_explicit_reconciliation(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="ambiguous-delivery",
        available_at=READY_AT,
    )
    claimed = repository.claim_outbox_message(
        worker_id="mailer-a",
        now=READY_AT,
    )
    assert claimed is not None
    renewed = repository.renew_outbox_message(
        message["id"],
        claimed["claim_token"],
        now="2026-07-29T08:04:00+00:00",
    )
    assert renewed is not None
    assert renewed["heartbeat_at"].startswith("2026-07-29T08:04:00")

    unknown = repository.mark_outbox_delivery_unknown(
        message["id"],
        claimed["claim_token"],
        error="connection closed after SMTP DATA",
    )
    assert unknown is not None
    assert unknown["status"] == "delivery_unknown"
    assert (
        repository.claim_outbox_message(
            worker_id="mailer-b",
            now="2026-07-30T08:00:00+00:00",
        )
        is None
    )

    scheduled = repository.resolve_outbox_delivery_unknown(
        message["id"],
        delivered=False,
        available_at=RETRY_AT,
        note="provider log confirms no acceptance",
    )
    assert scheduled is not None
    assert scheduled["status"] == "retrying"
    retried = repository.claim_outbox_message(
        worker_id="mailer-b",
        now=AFTER_RETRY,
    )
    assert retried is not None
    sent = repository.complete_outbox_message(
        message["id"],
        retried["claim_token"],
    )
    assert sent is not None
    assert sent["status"] == "sent"


def test_delivery_unknown_retry_extends_an_exhausted_budget_once(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="final-attempt-unknown",
        max_attempts=1,
        available_at=READY_AT,
    )
    claimed = repository.claim_outbox_message(worker_id="mailer-a", now=READY_AT)
    assert claimed is not None
    repository.mark_outbox_delivery_unknown(
        message["id"],
        claimed["claim_token"],
        error="connection closed after DATA",
    )

    scheduled = repository.resolve_outbox_delivery_unknown(
        message["id"],
        delivered=False,
        available_at=RETRY_AT,
        note="provider confirms no acceptance",
    )

    assert scheduled is not None
    assert scheduled["max_attempts"] == 2
    retried = repository.claim_outbox_message(worker_id="mailer-b", now=AFTER_RETRY)
    assert retried is not None
    assert retried["attempts"] == 2


def test_malformed_persisted_json_is_decoded_safely(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    job = repository.enqueue_operation_job(
        "radar",
        {},
        idempotency_key="corrupt-job",
        available_at=READY_AT,
    )
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {},
        idempotency_key="corrupt-message",
        available_at=READY_AT,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE operation_jobs SET payload = '{', result = '[' WHERE id = ?",
            (job["id"],),
        )
        connection.execute(
            "UPDATE outbox_messages SET payload = x'80' WHERE id = ?",
            (message["id"],),
        )
        connection.commit()

    decoded_job = repository.get_operation_job(job["id"])
    decoded_message = repository.get_outbox_message(message["id"])
    assert decoded_job is not None
    assert decoded_job["payload"] == {}
    assert decoded_job["result"] is None
    assert decoded_message is not None
    assert decoded_message["payload"] == {}


def test_radar_completion_and_email_intent_are_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "data")
    repository.create_radar_run("run-1", "scheduled")
    digest = {
        "run_id": "run-1",
        "summary": {"shortlisted": 2},
        "items": [],
        "email_delivery": {"status": "queued"},
    }

    completed, message = repository.complete_radar_run(
        "run-1",
        source_count=1,
        candidate_count=4,
        shortlisted_count=2,
        result=digest,
        outbox_recipient="student@example.com",
        outbox_idempotency_key="radar-email:daily",
    )

    assert message is not None
    assert completed["email_delivery"] == {
        "status": "pending",
        "message_id": message["id"],
    }
    assert repository.get_state("latest_radar") == completed
    assert repository.get_radar_run("run-1")["result"] == completed
    assert repository.get_outbox_message(message["id"])["payload"] == completed

    repository.create_radar_run("run-2", "scheduled")
    _, repeated_message = repository.complete_radar_run(
        "run-2",
        source_count=1,
        candidate_count=4,
        shortlisted_count=2,
        result={**digest, "run_id": "run-2"},
        outbox_recipient="student@example.com",
        outbox_idempotency_key="radar-email:daily",
    )
    assert repeated_message is not None
    assert repeated_message["id"] == message["id"]
    assert len(repository.list_outbox_messages()) == 1
