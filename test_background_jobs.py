"""Offline tests for background jobs (A6) -- no network, no Docker.

Covers the three non-negotiables:
  - idempotency: the same `client_request_id` never creates a second job
  - retries: a flaky (transient) failure is retried with backoff and the job
    only becomes claimable again after `next_retry_at`
  - alerts: every retry and every permanent failure writes a `job_alerts`
    row that can be listed and acknowledged

Run with:  python test_background_jobs.py
"""

import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from jobs import SqliteJobRepository
from llm import LLMNotConfiguredError, LLMUnavailableError, Receipt
from report_worker import JobWorker, make_receipt_handlers


def make_store(tmp: Path) -> SqliteJobRepository:
    store = SqliteJobRepository(tmp / "jobs.db")
    store.init_db()
    return store


def force_retry_due(tmp: Path, job_id: str) -> None:
    """Poke next_retry_at into the past so the retry can be claimed now."""
    with closing(
        sqlite3.connect(tmp / "jobs.db")
    ) as connection, connection:
        connection.execute(
            "UPDATE report_jobs SET next_retry_at = '2000-01-01T00:00:00+00:00'"
            " WHERE id = ?",
            (job_id,),
        )


class FlakyClient:
    """judge_text that fails (transient LLMUnavailableError) then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def judge_text(self, text: str, schema):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMUnavailableError("flaky provider timeout")
        return schema(merchant="Corner Cafe", total=9.74, currency="GBP", line_items=[])


class NeverConfiguredClient:
    def judge_text(self, text, schema):
        raise LLMNotConfiguredError("no API key configured")


def test_job_retry_defaults():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(Path(tmpdir))
        job = store.create_job("receipt_parse", {"text": "hi"})
        assert job["attempts"] == 0
        assert job["max_attempts"] == 3
        assert job["next_retry_at"] is None
        assert job["idempotency_key"] is None
        assert job["result"] is None


def test_idempotency_dedupe():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(Path(tmpdir))
        first = store.create_job("receipt_parse", {"text": "same"}, idempotency_key="req-001")
        second = store.create_job("receipt_parse", {"text": "same"}, idempotency_key="req-001")
        other = store.create_job("receipt_parse", {"text": "same"}, idempotency_key="req-002")

        assert first["job_id"] == second["job_id"]
        assert other["job_id"] != first["job_id"]
        assert len(store.list_jobs()) == 2
        assert store.find_by_idempotency_key("receipt_parse", "req-001")["job_id"] == first["job_id"]
        assert store.find_by_idempotency_key("receipt_parse", "missing") is None


def test_retry_is_gated_by_next_retry_at():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = make_store(tmp)
        job = store.create_job("receipt_parse", {"text": "hi"}, max_attempts=5)

        out = store.handle_failure(job["job_id"], job["attempts"], "boom", retryable=True, max_attempts=5)
        assert out["retried"] is True
        assert out["attempts"] == 1

        retried = store.get_job(job["job_id"])
        assert retried["status"] == "pending"
        assert retried["next_retry_at"] > retried["created_at"]
        assert store.claim_next_pending() is None  # not claimable yet

        force_retry_due(tmp, job["job_id"])
        claimed = store.claim_next_pending()
        assert claimed is not None and claimed["job_id"] == job["job_id"]
        assert claimed["attempts"] == 1


def test_worker_retries_flaky_handler_then_succeeds():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = make_store(tmp)
        client = FlakyClient(fail_times=2)
        handlers = make_receipt_handlers(client=client)
        job = store.create_job("receipt_parse", {"text": "Corner Cafe / flat white 9.74"}, max_attempts=3)

        for _ in range(3):  # attempts 1..3
            force_retry_due(tmp, job["job_id"]) if _ in (1, 2) else None
            current = store.claim_next_pending()
            if current is None:
                assert _ == 0  # first attempt is always claimable immediately
            JobWorker(store, handlers)._process(current or job)

        done = store.get_job(job["job_id"])
        assert done["status"] == "done"
        assert done["attempts"] == 3
        assert done["result"]["merchant"] == "Corner Cafe"
        assert done["result"]["total"] == 9.74
        assert done["artifact_name"] is None
        # two retry alerts (warning) were raised while it recovered
        alerts = store.list_alerts()
        assert len(alerts) == 2
        assert all(a["level"] == "warning" for a in alerts)


def test_worker_fails_permanently_after_max_attempts():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = make_store(tmp)
        handlers = make_receipt_handlers(client=FlakyClient(fail_times=999))
        job = store.create_job("receipt_parse", {"text": "hi"}, max_attempts=2)

        worker = JobWorker(store, handlers)
        worker._process(job)
        assert store.get_job(job["job_id"])["status"] == "pending"  # retrying

        current = store.get_job(job["job_id"])
        force_retry_due(tmp, current["job_id"])
        force_retry_due(tmp, current["job_id"])
        worker._process(current)
        worker._process(store.get_job(current["job_id"]))

        failed = store.get_job(job["job_id"])
        assert failed["status"] == "failed"
        assert failed["attempts"] == 2
        assert "LLMUnavailableError" in failed["error"]

        alerts = store.list_alerts()
        assert any(a["level"] == "warning" for a in alerts)
        assert any(a["level"] == "critical" and a["acked"] is False for a in alerts)


def test_non_retryable_feature_error_fails_immediately():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(Path(tmpdir))
        handlers = make_receipt_handlers(client=NeverConfiguredClient())
        job = store.create_job("receipt_parse", {"text": "hi"}, max_attempts=5)

        JobWorker(store, handlers)._process(job)

        failed = store.get_job(job["job_id"])
        assert failed["status"] == "failed"
        assert failed["attempts"] == 1  # no retry wasted on a fatal config error
        critical = [a for a in store.list_alerts() if a["level"] == "critical"]
        assert len(critical) == 1


def test_alerts_ack():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(Path(tmpdir))
        first = store.add_alert("job", "j1", "never mind", level="warning")
        second = store.add_alert("job", "j2", "real problem", level="critical")

        assert len(store.list_alerts()) == 2
        acked = store.ack_alerts([second["alert_id"]])
        assert acked == 1

        alerts = store.list_alerts()
        unacked = [a for a in alerts if not a["acked"]]
        acked_list = [a for a in alerts if a["acked"]]
        assert len(unacked) == 1 and unacked[0]["alert_id"] == first["alert_id"]
        assert len(acked_list) == 1 and acked_list[0]["alert_id"] == second["alert_id"]
        assert store.ack_alerts([]) == 0


def test_mark_done_stores_result():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(Path(tmpdir))
        job = store.create_job("receipt_parse", {"text": "hi"})
        store.mark_done(job["job_id"], result={"merchant": "Corner Cafe", "total": 9.74})
        done = store.get_job(job["job_id"])
        assert done["result"] == {"merchant": "Corner Cafe", "total": 9.74}
        assert done["artifact_name"] is None
        assert done["status"] == "done"


def test_endpoint_202_then_polls_to_failed_with_alert_e2e():
    """Offline e2e driving the actual serving app + worker thread (queue,
    status endpoint, idempotent replay, alert feed + ack), with the AI handler
    replaced by a deterministic stub so no live credentials are needed."""
    import main

    main.make_receipt_handlers = lambda *args, **kwargs: make_receipt_handlers(
        client=NeverConfiguredClient()
    )
    job_repo = main.job_repo

    with TestClient(main.app) as client:
        def wait_job(job_id: str, timeout: float = 8.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                job = client.get(f"/ai/jobs/{job_id}").json()["job"]
                if job["status"] in ("done", "failed"):
                    return job
                time.sleep(0.25)
            raise AssertionError("job never finished")

        text = "Corner Cafe\nFlat white 3.50\nToast 4.24\nTotal 9.74"
        req_id = f"e2e-{uuid.uuid4().hex}"
        first = client.post("/ai/jobs", json={"text": text, "client_request_id": req_id})
        assert first.status_code == 202
        body = first.json()
        assert body["idempotent_replay"] is False
        job_id = body["job_id"]

        replay = client.post("/ai/jobs", json={"text": text, "client_request_id": req_id})
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job_id
        assert replay.json()["idempotent_replay"] is True

        only_one = [j for j in job_repo.list_jobs() if j["idempotency_key"] == req_id]
        assert len(only_one) == 1

        final = wait_job(job_id)
        assert final["status"] == "failed"
        assert "LLMNotConfiguredError" in final["error"]

        feed = client.get("/alerts").json()
        assert feed["open"] >= 1
        assert any(a["job_id"] == job_id for a in feed["alerts"])

        alert_ids = [a["alert_id"] for a in feed["alerts"] if a["job_id"] == job_id]
        ack = client.post("/alerts/ack", json={"alert_ids": alert_ids})
        assert ack.status_code == 200
        assert ack.json()["acked"] == len(alert_ids)
        after = client.get("/alerts").json()
        remaining = [a for a in after["alerts"] if a["job_id"] == job_id]
        assert all(a["acked"] is True for a in remaining)


def test_endpoint_rejects_empty_text():
    from main import app

    with TestClient(app) as client:
        response = client.post("/ai/jobs", json={"text": "   "})
        assert response.status_code == 422


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} background-job tests passed")


if __name__ == "__main__":
    main()