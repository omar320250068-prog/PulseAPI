from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import check_supabase_connection, supabase
from postgres_repository import PostgresTaskRepository
from supabase_auth.errors import AuthApiError

app = FastAPI(title="Task API", version="1.0")
repo = PostgresTaskRepository()

class TaskIn(BaseModel):
    title: str

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    done: Optional[bool] = None

class AuthCredentials(BaseModel):
    email: str
    password: str


@app.on_event("startup")
def startup_event() -> None:
    repo.init_db()
    ok, message = check_supabase_connection()
    print(message)
    if not ok:
        print("WARNING: " + message)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "Invalid request body"})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.get("/", summary="API metadata")
def root():
    """Return API metadata and supported endpoints."""
    return {"name": "Task API", "version": "1.0", "endpoints": ["/tasks"]}


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