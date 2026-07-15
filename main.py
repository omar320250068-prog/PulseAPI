from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Task API", version="1.0")

class TaskIn(BaseModel):
    title: str

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    done: Optional[bool] = None

tasks = [
    {"id": 1, "title": "Buy groceries", "done": False},
    {"id": 2, "title": "Walk the dog", "done": False},
    {"id": 3, "title": "Read a book", "done": False},
]


def next_id() -> int:
    return max((task["id"] for task in tasks), default=0) + 1


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


@app.get("/tasks", summary="List tasks")
def list_tasks():
    """Return all tasks stored in memory."""
    return tasks


@app.get("/tasks/{task_id}", summary="Get task by ID")
def get_task(task_id: int):
    """Return a single task by its ID."""
    task = next((item for item in tasks if item["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


@app.post("/tasks", status_code=201, summary="Create task")
def create_task(payload: TaskIn):
    """Create a new task with an auto-generated ID."""
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title must be a non-empty string")
    task = {"id": next_id(), "title": title, "done": False}
    tasks.append(task)
    return task


@app.put("/tasks/{task_id}", summary="Update task")
def update_task(task_id: int, payload: TaskUpdate):
    """Update a task's title and/or done status."""
    task = next((item for item in tasks if item["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title must be a non-empty string")
        task["title"] = title
    if payload.done is not None:
        task["done"] = payload.done
    return task


@app.delete("/tasks/{task_id}", status_code=204, summary="Delete task")
def delete_task(task_id: int):
    """Delete a task from memory."""
    for index, task in enumerate(tasks):
        if task["id"] == task_id:
            tasks.pop(index)
            return
    raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
