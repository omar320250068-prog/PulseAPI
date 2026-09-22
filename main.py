from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from auth import check_supabase_connection, get_current_user, logout_user, supabase
from jobs import PostgresJobRepository, SqliteJobRepository
from llm import (
    InvalidModelOutputError,
    LLMClient,
    LLMNotConfiguredError,
    LLMUnavailableError,
    Receipt,
    ReceiptRequest,
)
from postgres_repository import PostgresTaskRepository
from report_worker import (
    JobWorker,
    ScheduleRunner,
    make_receipt_handlers,
    make_report_handlers,
)
from reports import REPORT_KIND
from sqlite_repository import SQLiteTaskRepository
from supabase_auth.errors import AuthApiError

app = FastAPI(title="Task & Auth API", version="2.0")
repo = PostgresTaskRepository()

BASE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
JOBS_DB = BASE_DIR / "jobs.db"
BOOKS_JSON = BASE_DIR / "books.json"

RECEIPT_JOB_KIND = "receipt_parse"

report_worker: JobWorker | None = None
schedule_runner: ScheduleRunner | None = None


def build_job_repository():
    """Jobs live in PostgreSQL in production, SQLite (jobs.db) otherwise."""
    try:
        store = PostgresJobRepository()
        store.init_db()
        print("Report job store: PostgreSQL")
        return store
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: PostgreSQL job store unavailable ({exc}); using SQLite jobs.db")
        store = SqliteJobRepository(JOBS_DB)
        store.init_db()
        return store


job_repo = build_job_repository()

class TaskIn(BaseModel):
    title: str

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    done: Optional[bool] = None

class AuthCredentials(BaseModel):
    email: str
    password: str

class ReportScheduleIn(BaseModel):
    name: str
    interval_minutes: int = Field(60, ge=1, le=1440)


class AIJobRequest(BaseModel):
    text: str
    client_request_id: str | None = None

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be empty")
        return stripped


class AlertsAckIn(BaseModel):
    alert_ids: list[str]


@app.on_event("startup")
def startup_event() -> None:
    global repo, report_worker, schedule_runner
    try:
        repo.init_db()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: PostgreSQL task DB unavailable ({exc}); falling back to SQLite tasks.db")
        repo = SQLiteTaskRepository()
        repo.init_db()
    ok, message = check_supabase_connection()
    print(message)
    if not ok:
        print("WARNING: " + message)

    report_worker = JobWorker(
        job_repo,
        {**make_report_handlers(repo, ARTIFACTS_DIR, BOOKS_JSON), **make_receipt_handlers()},
    )
    schedule_runner = ScheduleRunner(job_repo, REPORT_KIND, poll_interval=5.0)
    report_worker.start()
    schedule_runner.start()
    print("Report worker + scheduler started")


@app.on_event("shutdown")
def shutdown_event() -> None:
    if report_worker is not None:
        report_worker.stop()
    if schedule_runner is not None:
        schedule_runner.stop()


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path == "/ai/parse-receipt":
        return JSONResponse(status_code=400, content={"error": "Invalid request body"})
    return JSONResponse(status_code=422, content={"error": "Invalid request body"})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.get("/", summary="API metadata")
def root():
    """Return API metadata and supported endpoints."""
    return {
        "name": "Task & Auth API",
        "version": "2.0",
        "endpoints": [
            "/tasks",
            "/auth/signup",
            "/auth/login",
            "/auth/logout",
            "/public/info",
            "/protected/profile",
            "/protected/dashboard",
            "/ai/parse-receipt",
            "/ai/jobs",
            "/ai/jobs/{job_id}",
            "/alerts",
            "/alerts/ack",
            "/reports",
            "/reports/{job_id}",
            "/reports/{job_id}/download",
            "/reports/schedules",
        ],
    }


@app.get("/health", summary="Health check")
def health():
    """Return a simple readiness status."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth: open routes
# ---------------------------------------------------------------------------

@app.post("/auth/signup", status_code=201, summary="Create a new user account")
def signup(payload: AuthCredentials):
    """Register a new user with Supabase. Returns the created user object."""
    email = payload.email.strip()
    password = payload.password
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    try:
        response = supabase.auth.sign_up({"email": email, "password": password})
    except AuthApiError as exc:
        raise HTTPException(status_code=400, detail=exc.message or "Sign up failed")
    except Exception:
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    if response.user is None:
        raise HTTPException(status_code=400, detail="Unable to create user")
    return response.user.model_dump(mode="json")


@app.post("/auth/login", summary="Authenticate a user and return JWTs")
def login(payload: AuthCredentials):
    """Log a user in via Supabase. Returns access token, refresh token and user."""
    email = payload.email.strip()
    password = payload.password
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except AuthApiError:
        raise HTTPException(status_code=401, detail="Invalid login credentials")
    except Exception:
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    if response.session is None:
        raise HTTPException(status_code=401, detail="Invalid login credentials")
    session = response.session
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "user": session.user.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Public + protected routes
# ---------------------------------------------------------------------------

@app.get("/public/info", summary="Public, unprotected info")
def public_info():
    """Read-only endpoint that requires no authentication."""
    return {"message": "Welcome stranger! This info is public."}


@app.post("/auth/logout", status_code=204, summary="Log out the current user")
def logout(current: dict = Depends(get_current_user)):
    """Revoke the user's Supabase session (protected route)."""
    logout_user(current["token"])
    return Response(status_code=204)


@app.get("/protected/profile", summary="Read private profile data")
def profile(current: dict = Depends(get_current_user)):
    """Return the verified user's secure metadata."""
    user = current["user"]
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "created_at": user.get("created_at"),
    }


@app.get("/protected/dashboard", summary="Second protected route")
def dashboard(current: dict = Depends(get_current_user)):
    """Prove the shared guard protects every route it is attached to."""
    user = current["user"]
    return {
        "message": "Welcome to your dashboard",
        "user_id": user.get("id"),
        "email": user.get("email"),
    }


# ---------------------------------------------------------------------------
# AI judgement: one workflow step, only trusted answers
# ---------------------------------------------------------------------------

@app.post("/ai/parse-receipt", summary="Extract structured fields from receipt text")
def parse_receipt(payload: ReceiptRequest):
    """Ask an LLM to pull structured fields out of messy receipt text.

    The answer is returned only after it passes a Pydantic schema, a hard
    timeout and bounded retries. Errors map to clear status codes.
    """
    client = LLMClient()
    try:
        receipt = client.judge_text(payload.text, Receipt)
    except LLMNotConfiguredError:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured")
    except LLMUnavailableError:
        raise HTTPException(status_code=503, detail="LLM service unavailable")
    except InvalidModelOutputError:
        raise HTTPException(status_code=502, detail="Model output could not be validated")
    return receipt.model_dump()


@app.get("/tasks", summary="List tasks")
def list_tasks():
    """Return all tasks stored in SQLite."""
    return repo.list_tasks()


@app.get("/tasks/{task_id}", summary="Get task by ID")
def get_task(task_id: int):
    """Return a single task by its ID."""
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks", status_code=201, summary="Create task")
def create_task(payload: TaskIn):
    """Create a new task with an auto-generated ID."""
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title must be a non-empty string")
    return repo.create_task(title)


@app.put("/tasks/{task_id}", summary="Update task")
def update_task(task_id: int, payload: TaskUpdate):
    """Update a task's title and/or done status."""
    title = None
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title must be a non-empty string")

    updated = repo.update_task(task_id, title, payload.done)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return updated


@app.delete("/tasks/{task_id}", status_code=204, summary="Delete task")
def delete_task(task_id: int):
    """Delete a task from SQLite."""
    deleted = repo.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")


# ---------------------------------------------------------------------------
# Reports: background job -> PDF artifact (query, render, store and link)
# ---------------------------------------------------------------------------

def artifact_view(job: dict) -> dict:
    """Attach the artifact link to a finished job (store-and-link, no bytes)."""
    view = dict(job)
    if job["status"] == "done" and job.get("artifact_name"):
        view["artifact"] = {
            "name": job["artifact_name"],
            "bytes": job["artifact_bytes"],
            "content_type": "application/pdf",
            "url": f"/reports/{job['job_id']}/download",
        }
    else:
        view["artifact"] = None
    return view


@app.post("/reports", status_code=202, summary="Generate a task report in the background")
def create_report():
    """Enqueue a report job. Returns a job id + status URL immediately."""
    job = job_repo.create_job(REPORT_KIND)
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "status_url": f"/reports/{job['job_id']}",
    }


@app.get("/reports", summary="List report jobs")
def list_reports():
    """Recent report job summaries, newest first."""
    return {"reports": [artifact_view(job) for job in job_repo.list_jobs(limit=50)]}


@app.post("/reports/schedules", status_code=201, summary="Schedule a recurring report")
def create_schedule(payload: ReportScheduleIn):
    """Create a schedule that enqueues a report job every `interval_minutes`."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Schedule name must be a non-empty string")
    schedule = job_repo.create_schedule(name, payload.interval_minutes)
    return {"schedule": schedule}


@app.get("/reports/schedules", summary="List report schedules")
def list_schedules():
    """All recurring report schedules."""
    return {"schedules": job_repo.list_schedules()}


@app.delete("/reports/schedules/{schedule_id}", status_code=204, summary="Delete a schedule")
def delete_schedule(schedule_id: str):
    """Remove a recurring report schedule."""
    deleted = job_repo.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")


@app.get("/reports/{job_id}", summary="Get report job status")
def get_report(job_id: str):
    """Poll a report job. A done job carries a link to its PDF artifact."""
    job = job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    return {"report": artifact_view(job)}


@app.get("/reports/{job_id}/download", summary="Download the generated PDF artifact")
def download_report(job_id: str):
    """Serve the stored PDF from disk. Never passed through job memory."""
    job = job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    if job["status"] in ("pending", "running"):
        raise HTTPException(status_code=409, detail="Report is still being generated")
    if not job.get("artifact_name") or not job.get("artifact_bytes"):
        raise HTTPException(status_code=404, detail="Report produced no artifact")
    artifact_path = ARTIFACTS_DIR / Path(job["artifact_name"]).name
    if not artifact_path.is_file():
        raise HTTPException(status_code=410, detail="Artifact file is missing from disk")
    return FileResponse(
        artifact_path,
        media_type="application/pdf",
        filename=job["artifact_name"],
    )


# ---------------------------------------------------------------------------
# Background AI jobs: the slow LLM call answers with 202, a worker does the
# work, and a status endpoint reports the result (idempotent by
# client_request_id, bounded retries, alerts when a job really fails).
# ---------------------------------------------------------------------------

@app.post("/ai/jobs", status_code=202, summary="Enqueue a background AI receipt parse")
def create_ai_job(payload: AIJobRequest):
    """Accept fast (202), run the LLM in the background.

    Send the same `client_request_id` again and no second job is created -
    the existing one is returned (idempotency).
    """
    idem_key = payload.client_request_id or None
    replay = None
    if idem_key:
        replay = job_repo.find_by_idempotency_key(RECEIPT_JOB_KIND, idem_key)
    job = job_repo.create_job(
        RECEIPT_JOB_KIND,
        {"text": payload.text},
        max_attempts=3,
        idempotency_key=idem_key,
    )
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "status_url": f"/ai/jobs/{job['job_id']}",
        "idempotent_replay": replay is not None,
    }


@app.get("/ai/jobs/{job_id}", summary="Get a background AI job's status and result")
def get_ai_job(job_id: str):
    """Poll an AI job. A done job carries its parsed receipt in `result`."""
    job = job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="AI job not found")
    return {"job": job}


@app.get("/ai/jobs", summary="List background AI jobs")
def list_ai_jobs():
    """Recent AI job summaries, newest first."""
    return {"jobs": job_repo.list_jobs(limit=50)}


@app.get("/alerts", summary="List job alerts (the 'someone must find out' feed)")
def list_alerts():
    """Alerts raised by failed/retried jobs. `open` counts un-"acked" ones."""
    alerts = job_repo.list_alerts(limit=50)
    return {"alerts": alerts, "open": sum(1 for alert in alerts if not alert["acked"])}


@app.post("/alerts/ack", summary="Acknowledge alert(s)")
def ack_alerts(payload: AlertsAckIn):
    """Mark alerts as handled so the feed's `open` count drops."""
    return {"acked": job_repo.ack_alerts(payload.alert_ids)}