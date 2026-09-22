"""Background job store (the A7/A6 job pattern).

Mirrors the repo's storage pattern: a small repository interface with two
concrete backends, so jobs run against PostgreSQL in production and fall back
to a zero-config SQLite file (`jobs.db`) for local demos and tests.

Tables:

    report_jobs      - one row per background job (report renders, AI parse, ...)
    report_schedules - recurring report schedules (the "on a schedule" stretch)
    job_alerts       - failure/retry notifications, so *someone finds out*

Jobs never carry big payloads around: a finished report job only stores the
*path* to the artifact on disk (`artifact_name`) and links to it via the
download endpoint. Small JSON results (e.g. a parsed receipt) live in
`result`. Retries are tracked per job (`attempts` / `max_attempts`), and
`next_retry_at` gates when a retried job becomes claimable again. A caller can
send the same `idempotency_key` and safely get the *same* job back.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


def iso_now() -> str:
    """UTC ISO-8601 timestamp, seconds precision, +00:00 offset.

    Every stored timestamp uses this exact format so lexicographic
    comparisons (`due_schedules`, `next_retry_at`) stay correct across
    backends.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_minutes(value: str, minutes: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


def add_seconds(value: str, seconds: float) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS report_jobs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    result          TEXT,
    artifact_name   TEXT,
    artifact_bytes  INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_retry_at   TEXT,
    idempotency_key TEXT
)
"""

JOBS_IDEM_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
    ON report_jobs (kind, idempotency_key)
    WHERE idempotency_key IS NOT NULL
"""

NEW_JOBS_COLUMNS = {
    "result": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "next_retry_at": "TEXT",
    "idempotency_key": "TEXT",
}

SCHEDULES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS report_schedules (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    next_run_at      TEXT NOT NULL,
    last_run_at      TEXT,
    last_job_id      TEXT,
    created_at       TEXT NOT NULL
)
"""

ALERTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_alerts (
    id         TEXT PRIMARY KEY,
    job_id     TEXT,
    kind       TEXT NOT NULL,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked      INTEGER NOT NULL DEFAULT 0
)
"""


def job_from_row(row) -> dict:
    result = row["result"]
    return {
        "job_id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "payload": json.loads(row["payload"]) if row["payload"] else {},
        "result": json.loads(result) if result else None,
        "artifact_name": row["artifact_name"],
        "artifact_bytes": row["artifact_bytes"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "next_retry_at": row["next_retry_at"],
        "idempotency_key": row["idempotency_key"],
    }


def alert_from_row(row) -> dict:
    return {
        "alert_id": row["id"],
        "job_id": row["job_id"],
        "kind": row["kind"],
        "level": row["level"],
        "message": row["message"],
        "created_at": row["created_at"],
        "acked": bool(row["acked"]),
    }


def schedule_from_row(row) -> dict:
    return {
        "schedule_id": row["id"],
        "name": row["name"],
        "interval_minutes": row["interval_minutes"],
        "status": row["status"],
        "next_run_at": row["next_run_at"],
        "last_run_at": row["last_run_at"],
        "last_job_id": row["last_job_id"],
        "created_at": row["created_at"],
    }


def retry_delay_seconds(attempts: int) -> int:
    """Exponential backoff capped at 30s: failure #1 -> 2s, #2 -> 4s, #3 -> 8s..."""
    return min(30, 2 ** max(attempts, 1))


class JobRepository(ABC):
    @abstractmethod
    def init_db(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_job(
        self,
        kind: str,
        payload: dict | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    def find_by_idempotency_key(self, kind: str, idempotency_key: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def claim_next_pending(self) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def mark_running(self, job_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_done(
        self,
        job_id: str,
        artifact_name: str | None = None,
        artifact_bytes: int = 0,
        result: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_failed(self, job_id: str, error: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def handle_failure(
        self,
        job_id: str,
        attempts_so_far: int,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> dict:
        """Record a failed attempt; retry now -> pending (scheduled) or failed.

        Returns {"retried": bool, "attempts": int, "alert": dict}.
        Retried jobs are re-claimable only after next_retry_at.
        """
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def add_alert(
        self, kind: str, job_id: str, message: str, level: str = "critical"
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    def list_alerts(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def ack_alerts(self, alert_ids: list[str]) -> int:
        raise NotImplementedError

    @abstractmethod
    def create_schedule(self, name: str, interval_minutes: int) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_schedule(self, schedule_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def list_schedules(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def delete_schedule(self, schedule_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def due_schedules(self, now_iso: str) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def update_schedule_ran(
        self, schedule_id: str, job_id: str, last_run_at: str, next_run_at: str
    ) -> None:
        raise NotImplementedError


class SqliteJobRepository(JobRepository):
    def __init__(self, db_path: str | Path = "jobs.db") -> None:
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(JOBS_TABLE_SQL)
            connection.execute(SCHEDULES_TABLE_SQL)
            connection.execute(ALERTS_TABLE_SQL)
            self._ensure_columns(connection, "report_jobs", NEW_JOBS_COLUMNS)
            connection.execute(JOBS_IDEM_INDEX_SQL)

    @staticmethod
    def _ensure_columns(connection, table: str, columns: dict) -> None:
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def create_job(
        self,
        kind: str,
        payload: dict | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self.find_by_idempotency_key(kind, idempotency_key)
            if existing is not None:
                return existing
        job_id = uuid.uuid4().hex
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO report_jobs (id, kind, status, created_at, payload,"
                    " max_attempts, idempotency_key) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        job_id,
                        kind,
                        iso_now(),
                        json.dumps(payload or {}),
                        max_attempts,
                        idempotency_key,
                    ),
                )
        except sqlite3.IntegrityError:
            if idempotency_key:
                return self.find_by_idempotency_key(kind, idempotency_key)
            raise
        return self.get_job(job_id)

    def find_by_idempotency_key(self, kind: str, idempotency_key: str) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM report_jobs WHERE kind = ? AND idempotency_key = ?",
                (kind, idempotency_key),
            ).fetchone()
        return job_from_row(row) if row is not None else None

    def claim_next_pending(self) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM report_jobs WHERE status = 'pending'"
                " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                " ORDER BY created_at, id LIMIT 1",
                (iso_now(),),
            ).fetchone()
        return job_from_row(row) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (iso_now(), job_id),
            )

    def mark_done(
        self,
        job_id: str,
        artifact_name: str | None = None,
        artifact_bytes: int = 0,
        result: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        attempts_clause = ", attempts = ?" if attempts is not None else ""
        params: list = [
            iso_now(),
            artifact_name,
            artifact_bytes,
            json.dumps(result) if result is not None else None,
        ]
        if attempts is not None:
            params.append(attempts)
        params.append(job_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'done', finished_at = ?,"
                " artifact_name = ?, artifact_bytes = ?,"
                " result = ?, error = NULL, next_retry_at = NULL"
                f"{attempts_clause} WHERE id = ?",
                params,
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'failed', finished_at = ?, error = ?"
                " WHERE id = ?",
                (iso_now(), error[:500], job_id),
            )

    def handle_failure(
        self,
        job_id: str,
        attempts_so_far: int,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> dict:
        attempts = attempts_so_far + 1
        now = iso_now()
        if not retryable or attempts >= max_attempts:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE report_jobs SET status = 'failed', finished_at = ?,"
                    " attempts = ?, error = ? WHERE id = ?",
                    (now, attempts, error[:500], job_id),
                )
            alert = self.add_alert(
                "job",
                job_id,
                f"{self.get_job(job_id)['kind']} job {job_id} failed after "
                f"{attempts} attempt(s): {error[:300]}",
                level="critical",
            )
            return {"retried": False, "attempts": attempts, "alert": alert}
        delay = retry_delay_seconds(attempts)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'pending', attempts = ?,"
                " error = ?, next_retry_at = ?, started_at = NULL WHERE id = ?",
                (attempts, error[:500], add_seconds(now, delay), job_id),
            )
        alert = self.add_alert(
            "job",
            job_id,
            f"attempt {attempts} of {max_attempts} failed for {self.get_job(job_id)['kind']} "
            f"job {job_id}; retrying in {delay}s: {error[:300]}",
            level="warning",
        )
        return {"retried": True, "attempts": attempts, "alert": alert}

    def get_job(self, job_id: str) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM report_jobs WHERE id = ?", (job_id,)).fetchone()
        return job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM report_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [job_from_row(row) for row in rows]

    def add_alert(
        self, kind: str, job_id: str, message: str, level: str = "critical"
    ) -> dict:
        alert_id = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO job_alerts (id, job_id, kind, level, message, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (alert_id, job_id, kind, level, message, iso_now()),
            )
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM job_alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        return alert_from_row(row)

    def list_alerts(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM job_alerts ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [alert_from_row(row) for row in rows]

    def ack_alerts(self, alert_ids: list[str]) -> int:
        if not alert_ids:
            return 0
        placeholders = ", ".join("?" for _ in alert_ids)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE job_alerts SET acked = 1 WHERE id IN ({placeholders})",
                alert_ids,
            )
            return cursor.rowcount

    def create_schedule(self, name: str, interval_minutes: int) -> dict:
        schedule_id = uuid.uuid4().hex
        now = iso_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO report_schedules (id, name, interval_minutes, status,"
                " next_run_at, created_at) VALUES (?, ?, ?, 'active', ?, ?)",
                (schedule_id, name, interval_minutes, add_minutes(now, interval_minutes), now),
            )
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM report_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return schedule_from_row(row) if row is not None else None

    def list_schedules(self) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT * FROM report_schedules ORDER BY created_at DESC").fetchall()
        return [schedule_from_row(row) for row in rows]

    def delete_schedule(self, schedule_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM report_schedules WHERE id = ?", (schedule_id,)
            )
            return cursor.rowcount > 0

    def due_schedules(self, now_iso: str) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM report_schedules WHERE status = 'active' AND next_run_at <= ?",
                (now_iso,),
            ).fetchall()
        return [schedule_from_row(row) for row in rows]

    def update_schedule_ran(
        self, schedule_id: str, job_id: str, last_run_at: str, next_run_at: str
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_schedules SET last_run_at = ?, last_job_id = ?,"
                " next_run_at = ? WHERE id = ?",
                (last_run_at, job_id, next_run_at, schedule_id),
            )


class PostgresJobRepository(JobRepository):
    def __init__(self) -> None:
        load_dotenv()
        self.database_url = os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set")

    def get_db_connection(self):
        return psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)

    def _ensure_columns(self, cursor, table: str, columns: dict) -> None:
        for name, ddl in columns.items():
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")

    def init_db(self) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(JOBS_TABLE_SQL)
                cursor.execute(SCHEDULES_TABLE_SQL)
                cursor.execute(ALERTS_TABLE_SQL)
                self._ensure_columns(cursor, "report_jobs", NEW_JOBS_COLUMNS)
                cursor.execute(JOBS_IDEM_INDEX_SQL)

    def create_job(
        self,
        kind: str,
        payload: dict | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self.find_by_idempotency_key(kind, idempotency_key)
            if existing is not None:
                return existing
        job_id = uuid.uuid4().hex
        try:
            with self.get_db_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO report_jobs (id, kind, status, created_at, payload,"
                        " max_attempts, idempotency_key) VALUES (%s, %s, 'pending', %s, %s, %s, %s)",
                        (
                            job_id,
                            kind,
                            iso_now(),
                            json.dumps(payload or {}),
                            max_attempts,
                            idempotency_key,
                        ),
                    )
        except psycopg2.errors.UniqueViolation:
            if idempotency_key:
                return self.find_by_idempotency_key(kind, idempotency_key)
            raise
        return self.get_job(job_id)

    def find_by_idempotency_key(self, kind: str, idempotency_key: str) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_jobs WHERE kind = %s AND idempotency_key = %s",
                    (kind, idempotency_key),
                )
                row = cursor.fetchone()
        return job_from_row(row) if row is not None else None

    def claim_next_pending(self) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_jobs WHERE status = 'pending'"
                    " AND (next_retry_at IS NULL OR next_retry_at <= %s)"
                    " ORDER BY created_at LIMIT 1",
                    (iso_now(),),
                )
                row = cursor.fetchone()
        return job_from_row(row) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'running', started_at = %s WHERE id = %s",
                    (iso_now(), job_id),
                )

    def mark_done(
        self,
        job_id: str,
        artifact_name: str | None = None,
        artifact_bytes: int = 0,
        result: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        attempts_clause = ", attempts = %s" if attempts is not None else ""
        params: list = [
            iso_now(),
            artifact_name,
            artifact_bytes,
            json.dumps(result) if result is not None else None,
        ]
        if attempts is not None:
            params.append(attempts)
        params.append(job_id)
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'done', finished_at = %s,"
                    " artifact_name = %s, artifact_bytes = %s,"
                    " result = %s, error = NULL, next_retry_at = NULL"
                    f"{attempts_clause} WHERE id = %s",
                    params,
                )

    def mark_failed(self, job_id: str, error: str) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'failed', finished_at = %s,"
                    " error = %s WHERE id = %s",
                    (iso_now(), error[:500], job_id),
                )

    def handle_failure(
        self,
        job_id: str,
        attempts_so_far: int,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> dict:
        attempts = attempts_so_far + 1
        now = iso_now()
        job = self.get_job(job_id)
        if not retryable or attempts >= max_attempts:
            with self.get_db_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE report_jobs SET status = 'failed', finished_at = %s,"
                        " attempts = %s, error = %s WHERE id = %s",
                        (now, attempts, error[:500], job_id),
                    )
            alert = self.add_alert(
                "job",
                job_id,
                f"{job['kind']} job {job_id} failed after {attempts} attempt(s): {error[:300]}",
                level="critical",
            )
            return {"retried": False, "attempts": attempts, "alert": alert}
        delay = retry_delay_seconds(attempts)
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'pending', attempts = %s,"
                    " error = %s, next_retry_at = %s, started_at = NULL WHERE id = %s",
                    (attempts, error[:500], add_seconds(now, delay), job_id),
                )
        alert = self.add_alert(
            "job",
            job_id,
            f"attempt {attempts} of {max_attempts} failed for {job['kind']} "
            f"job {job_id}; retrying in {delay}s: {error[:300]}",
            level="warning",
        )
        return {"retried": True, "attempts": attempts, "alert": alert}

    def get_job(self, job_id: str) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM report_jobs WHERE id = %s", (job_id,))
                row = cursor.fetchone()
        return job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_jobs ORDER BY created_at DESC, id DESC LIMIT %s",
                    (limit,),
                )
                rows = cursor.fetchall()
        return [job_from_row(row) for row in rows]

    def add_alert(
        self, kind: str, job_id: str, message: str, level: str = "critical"
    ) -> dict:
        alert_id = uuid.uuid4().hex
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO job_alerts (id, job_id, kind, level, message, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (alert_id, job_id, kind, level, message, iso_now()),
                )
                cursor.execute("SELECT * FROM job_alerts WHERE id = %s", (alert_id,))
                row = cursor.fetchone()
        return alert_from_row(row)

    def list_alerts(self, limit: int = 50) -> list[dict]:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM job_alerts ORDER BY created_at DESC, id DESC LIMIT %s",
                    (limit,),
                )
                rows = cursor.fetchall()
        return [alert_from_row(row) for row in rows]

    def ack_alerts(self, alert_ids: list[str]) -> int:
        if not alert_ids:
            return 0
        placeholders = ", ".join("%s" for _ in alert_ids)
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE job_alerts SET acked = 1 WHERE id IN ({placeholders})",
                    alert_ids,
                )
                return cursor.rowcount

    def create_schedule(self, name: str, interval_minutes: int) -> dict:
        schedule_id = uuid.uuid4().hex
        now = iso_now()
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO report_schedules (id, name, interval_minutes, status,"
                    " next_run_at, created_at) VALUES (%s, %s, %s, 'active', %s, %s)",
                    (
                        schedule_id,
                        name,
                        interval_minutes,
                        add_minutes(now, interval_minutes),
                        now,
                    ),
                )
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_schedules WHERE id = %s", (schedule_id,)
                )
                row = cursor.fetchone()
        return schedule_from_row(row) if row is not None else None

    def list_schedules(self) -> list[dict]:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM report_schedules ORDER BY created_at DESC")
                rows = cursor.fetchall()
        return [schedule_from_row(row) for row in rows]

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM report_schedules WHERE id = %s", (schedule_id,))
                return cursor.rowcount > 0

    def due_schedules(self, now_iso: str) -> list[dict]:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_schedules WHERE status = 'active'"
                    " AND next_run_at <= %s",
                    (now_iso,),
                )
                rows = cursor.fetchall()
        return [schedule_from_row(row) for row in rows]

    def update_schedule_ran(
        self, schedule_id: str, job_id: str, last_run_at: str, next_run_at: str
    ) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_schedules SET last_run_at = %s, last_job_id = %s,"
                    " next_run_at = %s WHERE id = %s",
                    (last_run_at, job_id, next_run_at, schedule_id),
                )