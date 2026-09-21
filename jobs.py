"""Background job store for the report pipeline (the A7 job pattern).

Mirrors the repo's storage pattern: a small repository interface with two
concrete backends, so jobs run against PostgreSQL in production and fall back
to a zero-config SQLite file (`jobs.db`) for local demos and tests.

Two tables:

    report_jobs      - one row per report generation run (on-demand or scheduled)
    report_schedules - recurring report schedules (the "on a schedule" stretch)

Jobs never carry the report payload around: a finished job only stores the
*path* to the artifact on disk (`artifact_name`) and links to it via the
download endpoint.
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
    comparisons (`due_schedules`) stay correct across backends.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_minutes(value: str, minutes: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS report_jobs (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    error          TEXT,
    payload        TEXT NOT NULL DEFAULT '{}',
    artifact_name  TEXT,
    artifact_bytes INTEGER
)
"""

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


def job_from_row(row) -> dict:
    return {
        "job_id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "artifact_name": row["artifact_name"],
        "artifact_bytes": row["artifact_bytes"],
        "payload": json.loads(row["payload"]) if row["payload"] else {},
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


class JobRepository(ABC):
    @abstractmethod
    def init_db(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_job(self, kind: str, payload: dict | None = None) -> dict:
        raise NotImplementedError

    @abstractmethod
    def claim_next_pending(self) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def mark_running(self, job_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_done(self, job_id: str, artifact_name: str, artifact_bytes: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_failed(self, job_id: str, error: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(self, limit: int = 50) -> list[dict]:
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

    def create_job(self, kind: str, payload: dict | None = None) -> dict:
        job_id = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO report_jobs (id, kind, status, created_at, payload)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (job_id, kind, iso_now(), json.dumps(payload or {})),
            )
        return self.get_job(job_id)

    def claim_next_pending(self) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM report_jobs WHERE status = 'pending'"
                " ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return job_from_row(row) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (iso_now(), job_id),
            )

    def mark_done(self, job_id: str, artifact_name: str, artifact_bytes: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'done', finished_at = ?,"
                " artifact_name = ?, artifact_bytes = ? WHERE id = ?",
                (iso_now(), artifact_name, artifact_bytes, job_id),
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE report_jobs SET status = 'failed', finished_at = ?, error = ?"
                " WHERE id = ?",
                (iso_now(), error[:500], job_id),
            )

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

    def init_db(self) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(JOBS_TABLE_SQL)
                cursor.execute(SCHEDULES_TABLE_SQL)

    def create_job(self, kind: str, payload: dict | None = None) -> dict:
        job_id = uuid.uuid4().hex
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO report_jobs (id, kind, status, created_at, payload)"
                    " VALUES (%s, %s, 'pending', %s, %s)",
                    (job_id, kind, iso_now(), json.dumps(payload or {})),
                )
        return self.get_job(job_id)

    def claim_next_pending(self) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM report_jobs WHERE status = 'pending'"
                    " ORDER BY created_at LIMIT 1"
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

    def mark_done(self, job_id: str, artifact_name: str, artifact_bytes: int) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'done', finished_at = %s,"
                    " artifact_name = %s, artifact_bytes = %s WHERE id = %s",
                    (iso_now(), artifact_name, artifact_bytes, job_id),
                )

    def mark_failed(self, job_id: str, error: str) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE report_jobs SET status = 'failed', finished_at = %s,"
                    " error = %s WHERE id = %s",
                    (iso_now(), error[:500], job_id),
                )

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