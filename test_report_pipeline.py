"""Offline tests for the report pipeline (Week 6) -- no network, no Docker.

Two SQLite-backed pieces are exercised deterministically:
  - the task aggregation that the PDF report is built from
  - the background job machinery: queue -> run -> artifact, failure handling,
    and the schedule runner that turns "due" schedules into fresh jobs.

Run with:  python test_report_pipeline.py
"""

import json
import tempfile
from pathlib import Path

from jobs import SqliteJobRepository, iso_now
from jobs import add_minutes
from report_worker import JobWorker, ScheduleRunner, make_report_handlers
from reports import aggregate_books, build_task_report_pdf, run_task_report_job
from sqlite_repository import SQLiteTaskRepository


def make_tasks_repo(tmp: Path) -> SQLiteTaskRepository:
    repo = SQLiteTaskRepository()
    repo.base_dir = tmp
    repo.db_path = tmp / "tasks.db"
    repo.init_db()
    return repo


def make_books_file(tmp: Path) -> Path:
    books = [
        {"title": "Alpha", "price": 10.0, "currency": "GBP", "rating": 5},
        {"title": "Beta", "price": 20.0, "currency": "GBP", "rating": 4},
        {"title": "Gamma", "price": 30.0, "currency": "GBP", "rating": 5},
    ]
    path = tmp / "books.json"
    path.write_text(json.dumps(books), encoding="utf-8")
    return path


def test_aggregate_tasks_sqlite():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = make_tasks_repo(Path(tmpdir))
        repo.update_task(1, None, True)

        agg = repo.aggregate_tasks()
        assert agg["total"] == 3
        assert agg["done"] == 1
        assert agg["open"] == 2
        assert abs(agg["completion_rate"] - 33.3) < 0.5


def test_job_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SqliteJobRepository(Path(tmpdir) / "jobs.db")
        store.init_db()

        job = store.create_job("task_report", {"schedule_id": "abc"})
        assert job["status"] == "pending"
        assert job["payload"] == {"schedule_id": "abc"}

        claimed = store.claim_next_pending()
        assert claimed["job_id"] == job["job_id"]

        store.mark_running(job["job_id"])
        store.mark_done(job["job_id"], "report.pdf", 1024)

        done = store.get_job(job["job_id"])
        assert done["status"] == "done"
        assert done["artifact_name"] == "report.pdf"
        assert done["artifact_bytes"] == 1024
        assert len(store.list_jobs()) == 1


def test_failed_job_records_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SqliteJobRepository(Path(tmpdir) / "jobs.db")
        store.init_db()
        job = store.create_job("task_report")
        store.mark_failed(job["job_id"], "ValueError: Boom")
        failed = store.get_job(job["job_id"])
        assert failed["status"] == "failed"
        assert "Boom" in failed["error"]
        assert failed["artifact_name"] is None


def test_schedule_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SqliteJobRepository(Path(tmpdir) / "jobs.db")
        store.init_db()
        sched = store.create_schedule("nightly", 60)
        assert sched["status"] == "active"
        assert sched["interval_minutes"] == 60
        assert sched["next_run_at"] > iso_now()

        assert len(store.list_schedules()) == 1
        assert store.delete_schedule(sched["schedule_id"]) is True
        assert store.get_schedule(sched["schedule_id"]) is None
        assert store.delete_schedule(sched["schedule_id"]) is False


def test_scheduler_enqueues_due_schedule():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SqliteJobRepository(Path(tmpdir) / "jobs.db")
        store.init_db()
        sched = store.create_schedule("hourly", 60)

        # Force the next run into the past so the runner sees it as due.
        past = "2000-01-01T00:00:00+00:00"
        store.update_schedule_ran(sched["schedule_id"], "", past, past)

        runner = ScheduleRunner(store, job_kind="task_report", poll_interval=999)
        runner._run_once()

        jobs = store.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "pending"
        assert jobs[0]["payload"].get("schedule_id") == sched["schedule_id"]

        updated = store.get_schedule(sched["schedule_id"])
        assert updated["last_job_id"] == jobs[0]["job_id"]
        assert updated["next_run_at"] > iso_now()


def test_report_worker_completes_and_writes_pdf():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = make_tasks_repo(tmp)
        store = SqliteJobRepository(tmp / "jobs.db")
        store.init_db()
        books = make_books_file(tmp)

        handlers = make_report_handlers(repo, tmp / "artifacts", books)
        job = store.create_job("task_report")

        worker = JobWorker(store, handlers)
        worker._process(job)

        done = store.get_job(job["job_id"])
        assert done["status"] == "done"
        assert done["artifact_name"].startswith("report-")
        assert done["artifact_bytes"] > 0

        artifact = tmp / "artifacts" / done["artifact_name"]
        assert artifact.is_file()
        data = artifact.read_bytes()
        assert data[:5] == b"%PDF-"
        assert b"Task Activity Report" in data


def test_report_worker_marks_failed_on_handler_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = make_tasks_repo(tmp)
        store = SqliteJobRepository(tmp / "jobs.db")
        store.init_db()

        def boom(_job):
            raise RuntimeError("handler Boom")

        job = store.create_job("task_report")
        worker = JobWorker(store, {"task_report": boom})
        worker._process(job)

        failed = store.get_job(job["job_id"])
        assert failed["status"] == "failed"
        assert "Boom" in failed["error"]


def test_unknown_job_kind_is_failed():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SqliteJobRepository(Path(tmpdir) / "jobs.db")
        store.init_db()
        job = store.create_job("no_such_kind")
        worker = JobWorker(store, {})
        worker._process(job)
        assert store.get_job(job["job_id"])["status"] == "failed"


def test_aggregate_books_handlers():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        books = make_books_file(tmp)
        agg = aggregate_books(books)
        assert agg["count"] == 3
        assert agg["avg_price"] == 20.0
        assert agg["most_expensive"] == "Gamma"
        assert agg["best_rated"][0] == "Gamma"

        assert aggregate_books(tmp / "missing.json") is None
        assert aggregate_books(None) is None
        broken = tmp / "bad.json"
        broken.write_text("not json", encoding="utf-8")
        assert aggregate_books(broken) is None


def test_build_pdf_directly():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        aggregate = {"total": 3, "done": 1, "open": 2, "completion_rate": 33.3}
        tasks = [
            {"id": 1, "title": "Buy groceries", "done": True},
            {"id": 2, "title": "Walk the dog", "done": False},
        ]
        books = {"count": 3, "avg_price": 20.0, "currency": "GBP",
                 "most_expensive": "Gamma", "best_rated": ["Alpha", "Gamma"]}
        destination = tmp / "out.pdf"
        build_task_report_pdf(destination, tasks, aggregate, books=books)
        data = destination.read_bytes()
        assert data[:5] == b"%PDF-"
        assert b"Task Activity Report" in data


def test_job_run_via_helper():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = make_tasks_repo(tmp)
        meta = run_task_report_job(repo, tmp / "artifacts", None, "deadbeef")
        assert meta["artifact_name"] == "report-deadbeef.pdf"
        assert meta["artifact_bytes"] > 0
        assert (tmp / "artifacts" / meta["artifact_name"]).is_file()


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} report pipeline tests passed")


if __name__ == "__main__":
    main()