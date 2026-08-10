# Task API — CRUD with SQLite

A simple RESTful API for managing tasks, built with FastAPI and backed by a SQLite database for persistent storage.

## Why SQLite?

SQLite was chosen because:
- It requires no separate database server — the entire database lives in a single file.
- It's built into Python's standard library (sqlite3), so no extra installation is needed.
- It's perfect for small projects and learning purposes, while still using real SQL.
- Data survives server restarts, unlike the in-memory list used in the previous version of this project.

## Where the database is stored

The database file is created automatically at "tasks.db" in the project root directory, the first time the application runs. If the file doesn't exist, it is created automatically along with the tasks table. If the table is empty, three example tasks are inserted automatically.

## Project Structure

assignment2/
├── main.py           # FastAPI app + SQLite integration
├── requirements.txt  # Python dependencies
├── tasks.db           # SQLite database (auto-generated)
└── README.md

## How to run the project

1. Clone the repository:
   git clone <your-repo-url>
   cd assignment2

2. Create and activate a virtual environment:
   python -m venv .venv
   .venv\Scripts\activate

3. Install dependencies:
   pip install -r requirements.txt

4. Run the server:
   uvicorn main:app --reload

5. Open the interactive API docs:
   http://127.0.0.1:8000/docs

On first run, tasks.db is created automatically with the tasks table and 3 example tasks.

## API Endpoints

| Method | Endpoint         | Description             |
|--------|------------------|-------------------------|
| GET    | /tasks           | List all tasks          |
| GET    | /tasks/{task_id} | Get a single task by ID |
| POST   | /tasks           | Create a new task       |
| PUT    | /tasks/{task_id} | Update an existing task |
| DELETE | /tasks/{task_id} | Delete a task           |

Unknown IDs return { "error": "Task not found" } with status code 404.

## Database Schema

Table: tasks

| Column | Type    | Description                 |
|--------|---------|-----------------------------|
| id     | INTEGER | Primary key (auto-increment) |
| title  | TEXT    | The task's title            |
| done   | BOOLEAN | Whether the task is completed |

## Exploring the database manually

The database can be inspected using DB Browser for SQLite (https://sqlitebrowser.org/dl/).

### Example SQL query used

UPDATE tasks SET done = 1 WHERE id = 1;
SELECT * FROM tasks;

This updates the first task to "done" directly in the database, and the change is immediately reflected through the API — proving that the API and the database are truly connected.

Screenshot of the database viewer:

![Database screenshot](./db-screenshot.png)

## What changed from the in-memory version

- Tasks are now stored in a real SQLite database (tasks.db) instead of an in-memory Python list.
- Data now persists across server restarts.
- All CRUD operations (GET, POST, PUT, DELETE) now execute real SQL queries (SELECT, INSERT, UPDATE, DELETE) instead of manipulating a list in memory.
- The API's routes, request bodies, and response shapes were not changed — only the storage layer underneath.
