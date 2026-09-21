"""Background job execution for the report pipeline.

Two daemon threads run inside the FastAPI process (fine for a single worker
process; scale out to a real queue in production):

    JobWorker       - picks a pending `report_jobs` row off the queue and runs
                      its registered handler, then marks the job done/failed.
    ScheduleRunner  - wakes due `report_schedules` and enqueues a fresh job,
                      then moves `next_run_at` forward by the interval.
"""

from __future__ import annotations

import threading

from jobs import add_minutes, iso_now
from reports import REPORT_KIND, run_task_report_job


def make_report_handlers(repo, artifact_dir, books_path=None) -> dict:
    """Map job kinds to the functions that turn them into artifacts."""

    def run_task_report(job: dict) -> dict:
        return run_task_report_job(repo, artifact_dir, books_path, job["job_id"])

    return {REPORT_KIND: run_task_report}


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
        self.store.mark_running(job["job_id"])
        try:
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise ValueError(f"no handler registered for job kind {job['kind']!r}")
            artifact = handler(job)
            self.store.mark_done(
                job["job_id"], artifact["artifact_name"], artifact["artifact_bytes"]
            )
        except Exception as exc:  # noqa: BLE001
            self.store.mark_failed(job["job_id"], f"{type(exc).__name__}: {exc}"[:500])


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