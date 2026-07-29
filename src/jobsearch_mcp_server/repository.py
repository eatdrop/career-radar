"""SQLite persistence for the web application.

Every operation uses a short-lived connection, which keeps the repository safe
when the threaded HTTP server handles multiple requests concurrently.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
                "CREATE INDEX IF NOT EXISTS idx_applications_updated "
                "ON applications(updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_resume_evidence_resume "
                "ON resume_evidence(resume_id)"
            )
            connection.commit()

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
                WHERE id = ?
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
