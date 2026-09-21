import sqlite3
from contextlib import closing
from pathlib import Path

from repository import TaskRepository


class SQLiteTaskRepository(TaskRepository):
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parent
        self.db_path = self.base_dir / "tasks.db"

    def get_db_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def row_to_task(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "title": row["title"], "done": bool(row["done"]) }

    def init_db(self) -> None:
        with closing(self.get_db_connection()) as connection, connection:
            # Create the tasks table if it does not already exist.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            # Check whether the table already has data so example rows are only inserted once.
            cursor = connection.execute("SELECT COUNT(*) AS count FROM tasks")
            if cursor.fetchone()["count"] == 0:
                # Insert the initial example tasks only when the table is empty.
                connection.executemany(
                    "INSERT INTO tasks (title, done) VALUES (?, ?)",
                    [
                        ("Buy groceries", 0),
                        ("Walk the dog", 0),
                        ("Read a book", 0),
                    ],
                )

    def list_tasks(self) -> list[dict]:
        with closing(self.get_db_connection()) as connection, connection:
            # Select every task from the database in ID order to preserve list behavior.
            cursor = connection.execute("SELECT id, title, done FROM tasks ORDER BY id")
            return [self.row_to_task(row) for row in cursor.fetchall()]

    def get_task(self, task_id: int) -> dict | None:
        with closing(self.get_db_connection()) as connection, connection:
            # Select the matching task by primary key.
            cursor = connection.execute(
                "SELECT id, title, done FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self.row_to_task(row)

    def create_task(self, title: str) -> dict:
        with closing(self.get_db_connection()) as connection, connection:
            # Insert a new task row and use the generated primary key as the task ID.
            cursor = connection.execute(
                "INSERT INTO tasks (title, done) VALUES (?, ?)",
                (title, 0),
            )
            # Read the newly inserted task back so the response matches the existing shape.
            result = connection.execute(
                "SELECT id, title, done FROM tasks WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self.row_to_task(result)

    def update_task(self, task_id: int, title: str | None, done: bool | None) -> dict | None:
        with closing(self.get_db_connection()) as connection, connection:
            # Select the current task so we can preserve existing values for omitted fields.
            cursor = connection.execute(
                "SELECT id, title, done FROM tasks WHERE id = ?",
                (task_id,),
            )
            existing = cursor.fetchone()
            if existing is None:
                return None

            updated_title = existing["title"]
            if title is not None:
                updated_title = title

            updated_done = bool(existing["done"])
            if done is not None:
                updated_done = done

            # Update the task row with the merged title and done values.
            connection.execute(
                "UPDATE tasks SET title = ?, done = ? WHERE id = ?",
                (updated_title, int(updated_done), task_id),
            )

            # Read the updated task back so the response stays identical to the old API.
            updated = connection.execute(
                "SELECT id, title, done FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self.row_to_task(updated)

    def delete_task(self, task_id: int) -> bool:
        with closing(self.get_db_connection()) as connection, connection:
            # Delete the task row that matches the requested primary key.
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def aggregate_tasks(self) -> dict:
        """One SQL pass over the tasks table: count and completion stats."""
        with closing(self.get_db_connection()) as connection, connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN done THEN 1 ELSE 0 END), 0) AS done,
                    COALESCE(SUM(CASE WHEN done THEN 0 ELSE 1 END), 0) AS open,
                    COALESCE(AVG(CASE WHEN done THEN 100.0 ELSE 0 END), 0) AS completion_rate
                FROM tasks
                """
            ).fetchone()
        return {
            "total": int(row["total"]),
            "done": int(row["done"]),
            "open": int(row["open"]),
            "completion_rate": round(float(row["completion_rate"]), 1),
        }