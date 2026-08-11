import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from repository import TaskRepository


class PostgresTaskRepository(TaskRepository):
    def __init__(self) -> None:
        load_dotenv()
        self.database_url = os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set")

    def get_db_connection(self):
        return psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)

    def row_to_task(self, row) -> dict:
        return {"id": row["id"], "title": row["title"], "done": bool(row["done"]) }

    def init_db(self) -> None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Create the tasks table if it does not already exist.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        done BOOLEAN NOT NULL DEFAULT FALSE
                    )
                    """
                )

                # Check whether the table already has data so example rows are only inserted once.
                cursor.execute("SELECT COUNT(*) AS count FROM tasks")
                if cursor.fetchone()["count"] == 0:
                    # Insert the initial example tasks only when the table is empty.
                    cursor.executemany(
                        "INSERT INTO tasks (title, done) VALUES (%s, %s)",
                        [
                            ("Buy groceries", False),
                            ("Walk the dog", False),
                            ("Read a book", False),
                        ],
                    )

    def list_tasks(self) -> list[dict]:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Select every task from the database in ID order to preserve list behavior.
                cursor.execute("SELECT id, title, done FROM tasks ORDER BY id")
                return [self.row_to_task(row) for row in cursor.fetchall()]

    def get_task(self, task_id: int) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Select the matching task by primary key.
                cursor.execute(
                    "SELECT id, title, done FROM tasks WHERE id = %s",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return self.row_to_task(row)

    def create_task(self, title: str) -> dict:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Insert a new task row and use the generated primary key as the task ID.
                cursor.execute(
                    "INSERT INTO tasks (title, done) VALUES (%s, %s) RETURNING id, title, done",
                    (title, False),
                )
                # Read the newly inserted task back so the response matches the existing shape.
                result = cursor.fetchone()
                return self.row_to_task(result)

    def update_task(self, task_id: int, title: str | None, done: bool | None) -> dict | None:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Select the current task so we can preserve existing values for omitted fields.
                cursor.execute(
                    "SELECT id, title, done FROM tasks WHERE id = %s",
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
                cursor.execute(
                    "UPDATE tasks SET title = %s, done = %s WHERE id = %s",
                    (updated_title, updated_done, task_id),
                )

                # Read the updated task back so the response stays identical to the old API.
                cursor.execute(
                    "SELECT id, title, done FROM tasks WHERE id = %s",
                    (task_id,),
                )
                updated = cursor.fetchone()
                return self.row_to_task(updated)

    def delete_task(self, task_id: int) -> bool:
        with self.get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Delete the task row that matches the requested primary key.
                cursor.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
                return cursor.rowcount > 0