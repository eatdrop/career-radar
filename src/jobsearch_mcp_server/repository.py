"""SQLite persistence for the web application.

Every operation uses a short-lived connection, which keeps the repository safe
when the threaded HTTP server handles multiple requests concurrently.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
DEFAULT_LEASE_SECONDS = 300
_OPERATION_RECONCILIATION_MARKER = (
    "worker lease expired at retry limit; one reconciliation attempt granted"
)
OPERATION_JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "retrying"})
OUTBOX_MESSAGE_STATUSES = frozenset(
    {"pending", "sending", "sent", "failed", "retrying", "delivery_unknown"}
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class SQLiteRepository:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_permissions(self.data_dir, 0o700)
        # Reuse the existing tracker database so upgrades preserve user records.
        self.path = self.data_dir / "job_tracker.db"
        self._initialise()
        self._restrict_permissions(self.path, 0o600)

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        """Best-effort private permissions for local personally identifiable data."""
        if os.name == "nt":
            return
        try:
            path.chmod(mode)
        except OSError:
            # Some mounted filesystems do not expose POSIX permission bits.
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_meta"
                ).fetchone()
                current_version = int(row["version"])
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        "Database schema is newer than this application "
                        f"({current_version} > {SCHEMA_VERSION})"
                    )
                migrations = {
                    1: self._migrate_v1,
                    2: self._migrate_v2,
                    3: self._migrate_v3,
                }
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    migrations[version](connection)
                    connection.execute(
                        "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                        (version, utc_now()),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        """Create the original application schema without replacing user data."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                job_title TEXT NOT NULL,
                job_description TEXT DEFAULT '',
                salary_range TEXT DEFAULT '',
                location TEXT DEFAULT '',
                resume_version TEXT DEFAULT '',
                status TEXT DEFAULT '已投递',
                notes TEXT DEFAULT '',
                applied_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resumes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL,
                structured_json TEXT NOT NULL,
                score_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_evidence (
                resume_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY(resume_id, chunk_index),
                FOREIGN KEY(resume_id) REFERENCES resumes(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                shortlisted_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_applications_updated ON applications(updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_resume_evidence_resume ON resume_evidence(resume_id)"
        )

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add durable background operations and transactional email delivery."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'retrying')),
                payload TEXT NOT NULL DEFAULT '{}',
                result TEXT,
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
                available_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                worker_id TEXT NOT NULL DEFAULT '',
                claim_token TEXT,
                claimed_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'retrying')),
                recipient TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
                available_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                last_error TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                claim_token TEXT,
                claimed_at TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_operation_jobs_claim
            ON operation_jobs(status, available_at, created_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_operation_jobs_stale
            ON operation_jobs(status, claimed_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_messages_claim
            ON outbox_messages(status, available_at, created_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_messages_stale
            ON outbox_messages(status, claimed_at)
            """
        )

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Add renewable leases and isolate ambiguous SMTP delivery outcomes."""
        connection.execute("ALTER TABLE operation_jobs ADD COLUMN lease_until_epoch INTEGER")
        connection.execute("ALTER TABLE operation_jobs ADD COLUMN heartbeat_at TEXT")
        connection.execute(
            """
            UPDATE operation_jobs
            SET lease_until_epoch = CAST(strftime('%s', claimed_at) AS INTEGER) + ?,
                heartbeat_at = claimed_at
            WHERE status = 'running' AND claimed_at IS NOT NULL
            """,
            (DEFAULT_LEASE_SECONDS,),
        )
        connection.execute("ALTER TABLE outbox_messages RENAME TO outbox_messages_v2")
        connection.execute(
            """
            CREATE TABLE outbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending', 'sending', 'sent', 'failed', 'retrying',
                        'delivery_unknown'
                    )),
                recipient TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
                available_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                last_error TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                claim_token TEXT,
                claimed_at TEXT,
                lease_until_epoch INTEGER,
                heartbeat_at TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                id, status, recipient, payload, attempts, max_attempts,
                available_at, idempotency_key, last_error, worker_id,
                claim_token, claimed_at, lease_until_epoch, heartbeat_at,
                sent_at, created_at, updated_at
            )
            SELECT
                id, status, recipient, payload, attempts, max_attempts,
                available_at, idempotency_key, last_error, worker_id,
                claim_token, claimed_at,
                CASE
                    WHEN status = 'sending' AND claimed_at IS NOT NULL
                    THEN CAST(strftime('%s', claimed_at) AS INTEGER) + ?
                    ELSE NULL
                END,
                CASE WHEN status = 'sending' THEN claimed_at ELSE NULL END,
                sent_at, created_at, updated_at
            FROM outbox_messages_v2
            """,
            (DEFAULT_LEASE_SECONDS,),
        )
        connection.execute("DROP TABLE outbox_messages_v2")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_messages_claim
            ON outbox_messages(status, available_at, created_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_messages_stale
            ON outbox_messages(status, claimed_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_operation_jobs_lease
            ON operation_jobs(status, lease_until_epoch)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_messages_lease
            ON outbox_messages(status, lease_until_epoch)
            """
        )

    def get_schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_meta").fetchone()
        return int(row[0])

    def queue_statistics(self) -> dict[str, dict[str, int]]:
        with self._connection() as connection:
            operation_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM operation_jobs GROUP BY status"
            ).fetchall()
            outbox_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM outbox_messages GROUP BY status"
            ).fetchall()
        return {
            "operations": {row["status"]: int(row["count"]) for row in operation_rows},
            "outbox": {row["status"]: int(row["count"]) for row in outbox_rows},
        }

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode_json(value: Any, default: Any) -> Any:
        if not isinstance(value, (str, bytes, bytearray)):
            return default
        try:
            return json.loads(value)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _timestamp(value: str | datetime | None = None) -> str:
        if value is None:
            value = utc_now()
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("timestamp must be a valid ISO-8601 value") from error
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise TypeError("timestamp must be a string, datetime, or None")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")

    @classmethod
    def _claim_window(
        cls,
        now: str | datetime | None,
        stale_before: str | datetime | None,
        lease_seconds: int,
    ) -> tuple[str, int, int, str]:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        now_timestamp = cls._timestamp(now)
        parsed_now = datetime.fromisoformat(now_timestamp)
        stale_timestamp = (
            cls._timestamp(stale_before)
            if stale_before is not None
            else (parsed_now - timedelta(seconds=DEFAULT_LEASE_SECONDS)).isoformat(
                timespec="microseconds"
            )
        )
        now_epoch = int(parsed_now.timestamp())
        return (
            now_timestamp,
            now_epoch,
            now_epoch + lease_seconds,
            stale_timestamp,
        )

    @staticmethod
    def _validate_queue_values(*, idempotency_key: str, max_attempts: int) -> tuple[str, int]:
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key or len(key) > 255:
            raise ValueError("idempotency_key must contain 1-255 characters")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        return key, max_attempts

    @classmethod
    def _decode_operation_job(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = cls._decode_json(item["payload"], {})
        item["result"] = cls._decode_json(item["result"], None)
        return item

    @classmethod
    def _decode_outbox_message(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = cls._decode_json(item["payload"], {})
        return item

    def enqueue_operation_job(
        self,
        job_type: str,
        payload: Any,
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        available_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        job, _ = self.enqueue_operation_job_once(
            job_type,
            payload,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            available_at=available_at,
        )
        return job

    def enqueue_operation_job_once(
        self,
        job_type: str,
        payload: Any,
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        available_at: str | datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(job_type, str) or not job_type.strip() or len(job_type.strip()) > 100:
            raise ValueError("job_type must contain 1-100 characters")
        key, max_attempts = self._validate_queue_values(
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        now = utc_now()
        encoded_payload = self._encode_json(payload)
        available = self._timestamp(available_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO operation_jobs(
                        job_type, payload, max_attempts, available_at,
                        idempotency_key, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (
                        job_type.strip(),
                        encoded_payload,
                        max_attempts,
                        available,
                        key,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM operation_jobs WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_operation_job(row), cursor.rowcount == 1

    def get_operation_job(self, job_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._decode_operation_job(row) if row else None

    def get_operation_job_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operation_jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._decode_operation_job(row) if row else None

    def list_operation_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in OPERATION_JOB_STATUSES:
            raise ValueError(f"unsupported operation job status: {status}")
        clauses: list[str] = []
        parameters: list[Any] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if job_type:
            clauses.append("job_type = ?")
            parameters.append(job_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend([max(1, min(500, limit)), max(0, offset)])
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM operation_jobs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_operation_job(row) for row in rows]

    def claim_operation_job(
        self,
        *,
        worker_id: str,
        job_type: str | None = None,
        now: str | datetime | None = None,
        stale_before: str | datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id is required")
        (
            now_timestamp,
            now_epoch,
            lease_until_epoch,
            stale_timestamp,
        ) = self._claim_window(now, stale_before, lease_seconds)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE operation_jobs
                    SET status = 'failed',
                        error = CASE WHEN error = ''
                            THEN 'worker lease expired and retry budget was exhausted'
                            ELSE error END,
                        claim_token = NULL,
                        worker_id = '',
                        claimed_at = NULL,
                        lease_until_epoch = NULL,
                        heartbeat_at = NULL,
                        completed_at = ?,
                        updated_at = ?
                    WHERE status = 'running'
                      AND (
                          lease_until_epoch <= ?
                          OR (lease_until_epoch IS NULL AND claimed_at <= ?)
                      )
                      AND attempts >= max_attempts
                      AND error = ?
                    """,
                    (
                        now_timestamp,
                        now_timestamp,
                        now_epoch,
                        stale_timestamp,
                        _OPERATION_RECONCILIATION_MARKER,
                    ),
                )
                type_clause = "AND job_type = ?" if job_type else ""
                parameters: list[Any] = [
                    now_timestamp,
                    now_epoch,
                    stale_timestamp,
                    _OPERATION_RECONCILIATION_MARKER,
                    now_epoch,
                    stale_timestamp,
                ]
                if job_type:
                    parameters.append(job_type)
                row = connection.execute(
                    f"""
                    SELECT id FROM operation_jobs
                    WHERE (
                        (
                            attempts < max_attempts
                            AND (
                                (status IN ('queued', 'retrying') AND available_at <= ?)
                                OR
                                (
                                    status = 'running'
                                    AND (
                                        lease_until_epoch <= ?
                                        OR (
                                            lease_until_epoch IS NULL
                                            AND claimed_at <= ?
                                        )
                                    )
                                )
                            )
                        )
                        OR (
                            status = 'running'
                            AND attempts >= max_attempts
                            AND error != ?
                            AND (
                                lease_until_epoch <= ?
                                OR (
                                    lease_until_epoch IS NULL
                                    AND claimed_at <= ?
                                )
                              )
                        )
                    )
                      {type_clause}
                    ORDER BY
                        CASE WHEN status = 'running' THEN 0 ELSE 1 END,
                        available_at,
                        created_at,
                        id
                    LIMIT 1
                    """,
                    parameters,
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                claim_token = uuid.uuid4().hex
                connection.execute(
                    """
                    UPDATE operation_jobs
                    SET status = 'running',
                        max_attempts = CASE
                            WHEN attempts >= max_attempts THEN max_attempts + 1
                            ELSE max_attempts
                        END,
                        error = CASE
                            WHEN attempts >= max_attempts THEN ?
                            ELSE error
                        END,
                        attempts = attempts + 1,
                        worker_id = ?,
                        claim_token = ?,
                        claimed_at = ?,
                        lease_until_epoch = ?,
                        heartbeat_at = ?,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _OPERATION_RECONCILIATION_MARKER,
                        worker_id.strip()[:255],
                        claim_token,
                        now_timestamp,
                        lease_until_epoch,
                        now_timestamp,
                        now_timestamp,
                        row["id"],
                    ),
                )
                claimed = connection.execute(
                    "SELECT * FROM operation_jobs WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_operation_job(claimed)

    def renew_operation_job(
        self,
        job_id: int,
        claim_token: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        (
            now_timestamp,
            _,
            lease_until_epoch,
            _,
        ) = self._claim_window(now, None, lease_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_jobs
                SET lease_until_epoch = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (
                    lease_until_epoch,
                    now_timestamp,
                    now_timestamp,
                    job_id,
                    claim_token,
                ),
            )
            connection.commit()
        return self.get_operation_job(job_id) if cursor.rowcount else None

    def complete_operation_job(
        self,
        job_id: int,
        claim_token: str,
        result: Any = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        encoded_result = self._encode_json(result)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_jobs
                SET status = 'succeeded',
                    result = ?,
                    error = '',
                    worker_id = '',
                    claim_token = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (encoded_result, now, now, job_id, claim_token),
            )
            connection.commit()
        return self.get_operation_job(job_id) if cursor.rowcount else None

    def retry_operation_job(
        self,
        job_id: int,
        claim_token: str,
        *,
        error: str,
        available_at: str | datetime,
    ) -> dict[str, Any] | None:
        now = utc_now()
        available = self._timestamp(available_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT attempts, max_attempts FROM operation_jobs
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (job_id, claim_token),
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                exhausted = int(row["attempts"]) >= int(row["max_attempts"])
                status = "failed" if exhausted else "retrying"
                completed_at = now if exhausted else None
                connection.execute(
                    """
                    UPDATE operation_jobs
                    SET status = ?,
                        error = ?,
                        available_at = ?,
                        worker_id = '',
                        claim_token = NULL,
                        claimed_at = CASE WHEN ? = 'failed' THEN claimed_at ELSE NULL END,
                        lease_until_epoch = NULL,
                        heartbeat_at = NULL,
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        str(error),
                        available,
                        status,
                        completed_at,
                        now,
                        job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM operation_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_operation_job(updated)

    def defer_operation_job(
        self,
        job_id: int,
        claim_token: str,
        *,
        error: str,
        available_at: str | datetime,
    ) -> dict[str, Any] | None:
        """Release a claim without consuming retry budget for known contention."""
        now = utc_now()
        available = self._timestamp(available_at)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_jobs
                SET status = 'retrying',
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    error = CASE WHEN error = ? THEN error ELSE ? END,
                    available_at = ?,
                    worker_id = '',
                    claim_token = NULL,
                    claimed_at = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    completed_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (
                    _OPERATION_RECONCILIATION_MARKER,
                    str(error),
                    available,
                    now,
                    job_id,
                    claim_token,
                ),
            )
            connection.commit()
        return self.get_operation_job(job_id) if cursor.rowcount else None

    def fail_operation_job(
        self,
        job_id: int,
        claim_token: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_jobs
                SET status = 'failed',
                    error = ?,
                    worker_id = '',
                    claim_token = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (str(error), now, now, job_id, claim_token),
            )
            connection.commit()
        return self.get_operation_job(job_id) if cursor.rowcount else None

    def enqueue_outbox_message(
        self,
        recipient: str,
        payload: Any,
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        available_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("recipient is required")
        key, max_attempts = self._validate_queue_values(
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        now = utc_now()
        encoded_payload = self._encode_json(payload)
        available = self._timestamp(available_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO outbox_messages(
                        recipient, payload, max_attempts, available_at,
                        idempotency_key, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (
                        recipient.strip(),
                        encoded_payload,
                        max_attempts,
                        available,
                        key,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM outbox_messages WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_outbox_message(row)

    def get_outbox_message(self, message_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM outbox_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._decode_outbox_message(row) if row else None

    def list_outbox_messages(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in OUTBOX_MESSAGE_STATUSES:
            raise ValueError(f"unsupported outbox message status: {status}")
        where = "WHERE status = ?" if status else ""
        parameters: list[Any] = [status] if status else []
        parameters.extend([max(1, min(500, limit)), max(0, offset)])
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM outbox_messages
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_outbox_message(row) for row in rows]

    def claim_outbox_message(
        self,
        *,
        worker_id: str,
        now: str | datetime | None = None,
        stale_before: str | datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id is required")
        (
            now_timestamp,
            now_epoch,
            lease_until_epoch,
            stale_timestamp,
        ) = self._claim_window(now, stale_before, lease_seconds)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'delivery_unknown',
                        last_error = CASE WHEN last_error = ''
                            THEN 'worker lease expired after delivery may have started'
                            ELSE last_error END,
                        claim_token = NULL,
                        worker_id = '',
                        claimed_at = NULL,
                        lease_until_epoch = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE status = 'sending'
                      AND (
                          lease_until_epoch <= ?
                          OR (lease_until_epoch IS NULL AND claimed_at <= ?)
                      )
                    """,
                    (now_timestamp, now_epoch, stale_timestamp),
                )
                row = connection.execute(
                    """
                    SELECT id FROM outbox_messages
                    WHERE attempts < max_attempts
                      AND status IN ('pending', 'retrying')
                      AND available_at <= ?
                    ORDER BY
                        available_at,
                        created_at,
                        id
                    LIMIT 1
                    """,
                    (now_timestamp,),
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                claim_token = uuid.uuid4().hex
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'sending',
                        attempts = attempts + 1,
                        worker_id = ?,
                        claim_token = ?,
                        claimed_at = ?,
                        lease_until_epoch = ?,
                        heartbeat_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        worker_id.strip()[:255],
                        claim_token,
                        now_timestamp,
                        lease_until_epoch,
                        now_timestamp,
                        now_timestamp,
                        row["id"],
                    ),
                )
                claimed = connection.execute(
                    "SELECT * FROM outbox_messages WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_outbox_message(claimed)

    def renew_outbox_message(
        self,
        message_id: int,
        claim_token: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        (
            now_timestamp,
            _,
            lease_until_epoch,
            _,
        ) = self._claim_window(now, None, lease_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET lease_until_epoch = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (
                    lease_until_epoch,
                    now_timestamp,
                    now_timestamp,
                    message_id,
                    claim_token,
                ),
            )
            connection.commit()
        return self.get_outbox_message(message_id) if cursor.rowcount else None

    def complete_outbox_message(
        self,
        message_id: int,
        claim_token: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'sent',
                    last_error = '',
                    worker_id = '',
                    claim_token = NULL,
                    claimed_at = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    sent_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (now, now, message_id, claim_token),
            )
            connection.commit()
        return self.get_outbox_message(message_id) if cursor.rowcount else None

    def retry_outbox_message(
        self,
        message_id: int,
        claim_token: str,
        *,
        error: str,
        available_at: str | datetime,
    ) -> dict[str, Any] | None:
        now = utc_now()
        available = self._timestamp(available_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT attempts, max_attempts FROM outbox_messages
                    WHERE id = ? AND status = 'sending' AND claim_token = ?
                    """,
                    (message_id, claim_token),
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                exhausted = int(row["attempts"]) >= int(row["max_attempts"])
                status = "failed" if exhausted else "retrying"
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = ?,
                        last_error = ?,
                        available_at = ?,
                        worker_id = '',
                        claim_token = NULL,
                        claimed_at = NULL,
                        lease_until_epoch = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (status, str(error), available, now, message_id),
                )
                updated = connection.execute(
                    "SELECT * FROM outbox_messages WHERE id = ?", (message_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._decode_outbox_message(updated)

    def fail_outbox_message(
        self,
        message_id: int,
        claim_token: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'failed',
                    last_error = ?,
                    worker_id = '',
                    claim_token = NULL,
                    claimed_at = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (str(error), now, message_id, claim_token),
            )
            connection.commit()
        return self.get_outbox_message(message_id) if cursor.rowcount else None

    def mark_outbox_delivery_unknown(
        self,
        message_id: int,
        claim_token: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        """Fence an SMTP DATA ambiguity until an operator reconciles it."""
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'delivery_unknown',
                    last_error = ?,
                    worker_id = '',
                    claim_token = NULL,
                    claimed_at = NULL,
                    lease_until_epoch = NULL,
                    heartbeat_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (str(error), now, message_id, claim_token),
            )
            connection.commit()
        return self.get_outbox_message(message_id) if cursor.rowcount else None

    def resolve_outbox_delivery_unknown(
        self,
        message_id: int,
        *,
        delivered: bool,
        available_at: str | datetime | None = None,
        note: str = "",
    ) -> dict[str, Any] | None:
        """Manually confirm delivery or explicitly schedule one controlled retry."""
        now = utc_now()
        if delivered:
            status = "sent"
            sent_at = now
            available = self._timestamp(available_at)
        else:
            if available_at is None:
                raise ValueError("available_at is required when scheduling a retry")
            status = "retrying"
            sent_at = None
            available = self._timestamp(available_at)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_messages
                SET status = ?,
                    available_at = ?,
                    last_error = ?,
                    sent_at = ?,
                    max_attempts = CASE
                        WHEN ? = 1 AND max_attempts <= attempts THEN attempts + 1
                        ELSE max_attempts
                    END,
                    updated_at = ?
                WHERE id = ? AND status = 'delivery_unknown'
                """,
                (
                    status,
                    available,
                    str(note),
                    sent_at,
                    0 if delivered else 1,
                    now,
                    message_id,
                ),
            )
            connection.commit()
        return self.get_outbox_message(message_id) if cursor.rowcount else None

    def set_state(self, key: str, value: Any) -> None:
        now = utc_now()
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO app_state(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, now),
            )
            connection.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def save_resume(
        self,
        resume_id: str,
        name: str,
        source_name: str,
        raw_text: str,
        evidence_text: str,
        summary: str,
        structured: dict[str, Any],
        score: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO resumes(
                    id, name, source_name, raw_text, summary, structured_json,
                    score_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    source_name = excluded.source_name,
                    raw_text = excluded.raw_text,
                    summary = excluded.summary,
                    structured_json = excluded.structured_json,
                    score_json = excluded.score_json,
                    updated_at = excluded.updated_at
                """,
                (
                    resume_id,
                    name,
                    source_name,
                    raw_text,
                    summary,
                    json.dumps(structured, ensure_ascii=False),
                    json.dumps(score, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.execute("DELETE FROM resume_evidence WHERE resume_id = ?", (resume_id,))
            chunks = self._chunk_resume(evidence_text)
            connection.executemany(
                """
                INSERT INTO resume_evidence(resume_id, chunk_index, text)
                VALUES (?, ?, ?)
                """,
                [(resume_id, index, chunk) for index, chunk in enumerate(chunks)],
            )
            connection.commit()

    @staticmethod
    def _chunk_resume(text: str, target_size: int = 520) -> list[str]:
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for paragraph in paragraphs:
            if current and current_size + len(paragraph) > target_size:
                chunks.append("\n".join(current))
                # One-paragraph overlap preserves context between adjacent chunks.
                current = current[-1:]
                current_size = sum(len(item) for item in current)
            current.append(paragraph)
            current_size += len(paragraph)
        if current:
            chunks.append("\n".join(current))
        return chunks

    @staticmethod
    def _retrieval_tokens(text: str) -> set[str]:
        lowered = text.lower()
        latin = set(re.findall(r"[a-z][a-z0-9.+#-]{1,24}", lowered))
        sequences = re.findall(r"[\u4e00-\u9fff]+", lowered)
        chinese = {
            sequence[index : index + 2]
            for sequence in sequences
            for index in range(max(0, len(sequence) - 1))
        }
        return latin | chinese

    def retrieve_resume_evidence(
        self, resume_id: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        query_tokens = self._retrieval_tokens(query)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT chunk_index, text FROM resume_evidence
                WHERE resume_id = ?
                ORDER BY chunk_index
                """,
                (resume_id,),
            ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            chunk_tokens = self._retrieval_tokens(row["text"])
            overlap = query_tokens & chunk_tokens
            score = len(overlap) / max(1, len(query_tokens))
            ranked.append(
                {
                    "chunk_index": int(row["chunk_index"]),
                    "text": row["text"],
                    "score": round(score, 4),
                    "matched_terms": sorted(overlap)[:20],
                }
            )
        ranked.sort(key=lambda item: (item["score"], -item["chunk_index"]), reverse=True)
        return ranked[: max(1, min(20, limit))]

    def count_resume_evidence(self, resume_id: str) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM resume_evidence WHERE resume_id = ?",
                    (resume_id,),
                ).fetchone()[0]
            )

    def get_resume_evidence_text(self, resume_id: str) -> str:
        """Return the redacted evidence corpus in its original chunk order."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT text FROM resume_evidence
                WHERE resume_id = ?
                ORDER BY chunk_index
                """,
                (resume_id,),
            ).fetchall()
        return "\n".join(row["text"] for row in rows if row["text"])

    def get_latest_resume(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM resumes ORDER BY updated_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return self._decode_resume(row) if row else None

    def get_resume(self, resume_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        return self._decode_resume(row) if row else None

    @staticmethod
    def _decode_resume(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("structured_json", "score_json"):
            output_key = key.removesuffix("_json")
            try:
                data[output_key] = json.loads(data.pop(key))
            except (TypeError, json.JSONDecodeError):
                data[output_key] = {}
        return data

    def count_resumes(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM resumes").fetchone()[0])

    def add_application(self, values: dict[str, str]) -> dict[str, Any]:
        now = utc_now()
        applied_at = values.get("applied_at") or now
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO applications(
                    company_name, job_title, job_description, salary_range,
                    location, resume_version, status, notes, applied_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["company_name"],
                    values["job_title"],
                    values.get("job_description", ""),
                    values.get("salary_range", ""),
                    values.get("location", ""),
                    values.get("resume_version", ""),
                    values["status"],
                    values.get("notes", ""),
                    applied_at,
                    now,
                ),
            )
            application_id = int(cursor.lastrowid)
            connection.commit()
        return self.get_application(application_id) or {}

    def get_application(self, application_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_applications(
        self,
        status: str | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if query:
            clauses.append("(company_name LIKE ? ESCAPE '\\' OR job_title LIKE ? ESCAPE '\\')")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM applications
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_application(
        self, application_id: int, fields: dict[str, str]
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get_application(application_id)
        allowed = {"status", "notes", "salary_range", "location", "job_description"}
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return self.get_application(application_id)
        selected["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        params = [*selected.values(), application_id]
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE applications SET {assignments} WHERE id = ?", params
            )
            connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_application(application_id)

    def delete_application(self, application_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM applications WHERE id = ?", (application_id,))
            connection.commit()
        return cursor.rowcount > 0

    def application_statistics(self) -> dict[str, Any]:
        with self._connection() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0])
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM applications GROUP BY status"
            ).fetchall()
            recent = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM applications
                    WHERE datetime(applied_at) >= datetime('now', '-7 days')
                    """
                ).fetchone()[0]
            )
        return {
            "total": total,
            "by_status": {row["status"]: int(row["count"]) for row in rows},
            "recent_week": recent,
        }

    def create_radar_run(self, run_id: str, trigger_type: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO radar_runs(id, status, trigger_type, started_at)
                VALUES (?, 'running', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'running',
                    trigger_type = excluded.trigger_type,
                    source_count = 0,
                    candidate_count = 0,
                    shortlisted_count = 0,
                    result_json = '{}',
                    error_message = '',
                    started_at = excluded.started_at,
                    finished_at = ''
                WHERE radar_runs.status != 'completed'
                """,
                (run_id, trigger_type, utc_now()),
            )
            connection.commit()

    def finish_radar_run(
        self,
        run_id: str,
        *,
        status: str,
        source_count: int = 0,
        candidate_count: int = 0,
        shortlisted_count: int = 0,
        result: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE radar_runs SET
                    status = ?, source_count = ?, candidate_count = ?,
                    shortlisted_count = ?, result_json = ?, error_message = ?,
                    finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    source_count,
                    candidate_count,
                    shortlisted_count,
                    json.dumps(result or {}, ensure_ascii=False),
                    error_message,
                    utc_now(),
                    run_id,
                ),
            )
            connection.commit()

    def complete_radar_run(
        self,
        run_id: str,
        *,
        source_count: int,
        candidate_count: int,
        shortlisted_count: int,
        result: dict[str, Any],
        outbox_recipient: str = "",
        outbox_idempotency_key: str = "",
        outbox_max_attempts: int = 3,
        outbox_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Atomically persist a successful radar result and its email intent."""
        completed_at = utc_now()
        final_result = dict(result)
        outbox_row: sqlite3.Row | None = None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if outbox_recipient:
                    if not outbox_recipient.strip():
                        raise ValueError("outbox recipient is required")
                    key, max_attempts = self._validate_queue_values(
                        idempotency_key=outbox_idempotency_key,
                        max_attempts=outbox_max_attempts,
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO outbox_messages(
                            recipient, payload, max_attempts, available_at,
                            idempotency_key, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(idempotency_key) DO NOTHING
                        """,
                        (
                            outbox_recipient.strip(),
                            self._encode_json(outbox_payload or final_result),
                            max_attempts,
                            completed_at,
                            key,
                            completed_at,
                            completed_at,
                        ),
                    )
                    outbox_row = connection.execute(
                        "SELECT * FROM outbox_messages WHERE idempotency_key = ?",
                        (key,),
                    ).fetchone()
                    final_result["email_delivery"] = {
                        "status": str(outbox_row["status"]),
                        "message_id": int(outbox_row["id"]),
                    }
                    if cursor.rowcount == 1:
                        connection.execute(
                            "UPDATE outbox_messages SET payload = ? WHERE id = ?",
                            (
                                self._encode_json(outbox_payload or final_result),
                                int(outbox_row["id"]),
                            ),
                        )

                encoded_result = self._encode_json(final_result)
                cursor = connection.execute(
                    """
                    UPDATE radar_runs SET
                        status = 'completed', source_count = ?, candidate_count = ?,
                        shortlisted_count = ?, result_json = ?, error_message = '',
                        finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        source_count,
                        candidate_count,
                        shortlisted_count,
                        encoded_result,
                        completed_at,
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("radar run is missing or already complete")
                connection.execute(
                    """
                    INSERT INTO app_state(key, value_json, updated_at)
                    VALUES ('latest_radar', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (encoded_result, completed_at),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        decoded_outbox = self._decode_outbox_message(outbox_row) if outbox_row else None
        return final_result, decoded_outbox

    def list_radar_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM radar_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (max(1, min(100, limit)),),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.pop("result_json"))
            except (TypeError, json.JSONDecodeError):
                item["result"] = {}
            output.append(item)
        return output

    def get_radar_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM radar_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["result"] = json.loads(item.pop("result_json"))
        except (TypeError, json.JSONDecodeError):
            item["result"] = {}
        return item
