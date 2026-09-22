"""Background job execution (A6/A7 job pattern).

    JobWorker       - claims a pending `report_jobs` row, runs its registered
                      handler, and records the outcome. Handlers that raise
                      RetryableJobError are retried with exponential backoff
                      (see jobs.handle_failure); everything else is a permanent
                      failure. Every failure or retry writes a `job_alerts` row
                      and is reported to the console and, if configured, the
                      ALERT_WEBHOOK_URL endpoint.
    ScheduleRunner  - wakes due `report_schedules` and enqueues a fresh job,
                      then moves `next_run_at` forward by the interval.
"""

from __future__ import annotations

import os
import threading

import httpx

from jobs import add_minutes, iso_now
from llm import (
    InvalidModelOutputError,
    LLMClient,
    LLMUnavailableError,
    Receipt,
)
from reports import REPORT_KIND, run_task_report_job


class RetryableJobError(Exception):
    """Raised by a handler to tell the worker to retry this job (transient)."""


class JobExecutionError(Exception):
    """Raised by a handler to tell the worker the job can never succeed."""


def make_report_handlers(repo, artifact_dir, books_path=None) -> dict:
    """Map job kinds to the functions that turn them into artifacts."""

    def run_task_report(job: dict) -> dict:
        return run_task_report_job(repo, artifact_dir, books_path, job["job_id"])

    return {REPORT_KIND: run_task_report}


def make_receipt_handlers(client: LLMClient | None = None) -> dict:
    """Handle `receipt_parse` jobs: run the A5 AI call in the background.

    The slow LLM judgement moves off the HTTP request and onto a worker. The
    parsed, schema-validated Receipt is stored as the job's small JSON result.
    """
    if client is None:
        client = LLMClient()

    def parse_receipt(job: dict) -> dict:
        text = (job["payload"] or {}).get("text", "").strip()
        if not text:
            raise JobExecutionError("no text to parse in job payload")
        try:
            receipt = client.judge_text(text, Receipt)
        except (LLMUnavailableError, InvalidModelOutputError) as exc:
            raise RetryableJobError(f"{type(exc).__name__}: {exc}") from exc
        return receipt.model_dump()

    return {"receipt_parse": parse_receipt}


def notify_alert(alert: dict, event: str) -> None:
    """Best-effort alert delivery: console always, webhook if configured."""
    print(f"[alert:{event}] {alert['level']} {alert['message']}")
    webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        httpx.post(
            webhook,
            json={"event": event, "alert": alert},
            timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        print(f"[alert] webhook delivery to {webhook} failed (best-effort, ignored)")


class JobWorker(threading.Thread):
    def __init__(self, store, handlers: dict, poll_interval: float = 1.0) -> None:
        super().__init__(name="report-job-worker", daemon=True)
        self.store = store
        self.handlers = handlers
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim_next_pending()
            except Exception:  # noqa: BLE001 -- keep the worker alive
                self._stop.wait(self.poll_interval)
                continue
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                self._process(job)
            except Exception:  # noqa: BLE001
                self._stop.wait(self.poll_interval)

    def _process(self, job: dict) -> None:
        if job.get("status") in ("done", "failed"):
            return  # terminal jobs are never re-run
        self.store.mark_running(job["job_id"])
        error = None
        data = None
        try:
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise JobExecutionError(f"no handler registered for job kind {job['kind']!r}")
            data = handler(job)
        except RetryableJobError as exc:
            self._finish_failure(job, f"{type(exc).__name__}: {exc}"[:500], retryable=True)
            return
        except Exception as exc:  # noqa: BLE001
            self._finish_failure(job, f"{type(exc).__name__}: {exc}"[:500], retryable=False)
            return

        if isinstance(data, dict) and "artifact_name" in data and "artifact_bytes" in data:
            self.store.mark_done(
                job["job_id"],
                data["artifact_name"],
                data["artifact_bytes"],
                attempts=job["attempts"] + 1,
            )
            print(f"[worker] job {job['job_id']} done -> artifact {data['artifact_name']}")
        else:
            self.store.mark_done(
                job["job_id"], result=data or {"ok": True}, attempts=job["attempts"] + 1
            )
            print(f"[worker] job {job['job_id']} done -> result")

    def _finish_failure(self, job: dict, error: str, retryable: bool) -> None:
        outcome = self.store.handle_failure(
            job["job_id"],
            job["attempts"],
            error,
            retryable,
            job["max_attempts"],
        )
        notify_alert(outcome["alert"], event="retry" if outcome["retried"] else "job-failed")


class ScheduleRunner(threading.Thread):
    def __init__(self, store, job_kind: str, poll_interval: float = 5.0) -> None:
        super().__init__(name="report-scheduler", daemon=True)
        self.store = store
        self.job_kind = job_kind
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            self._stop.wait(self.poll_interval)

    def _run_once(self) -> None:
        try:
            due = self.store.due_schedules(iso_now())
        except Exception:  # noqa: BLE001
            return
        for schedule in due:
            try:
                job = self.store.create_job(self.job_kind, {"schedule_id": schedule["schedule_id"]})
                now = iso_now()
                self.store.update_schedule_ran(
                    schedule["schedule_id"],
                    job["job_id"],
                    now,
                    add_minutes(now, schedule["interval_minutes"]),
                )
            except Exception:  # noqa: BLE001
                continue