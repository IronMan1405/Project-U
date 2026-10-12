import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DATA_DIRECTORY = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATA_DIRECTORY / "project_u.sqlite3"


class RecordNotFound(Exception):
    pass


@contextmanager
def _connection(database_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(database_path) if database_path is not None else DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def initialize_database(database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def list_tasks(database_path: str | Path | None = None) -> list[dict]:
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, description, created_at, completed "
            "FROM tasks ORDER BY completed ASC, created_at DESC, id DESC"
        ).fetchall()
    return [{**dict(row), "completed": bool(row["completed"])} for row in rows]


def create_task(
    title: str,
    description: str = "",
    database_path: str | Path | None = None,
) -> dict:
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (title, description, created_at) VALUES (?, ?, ?)",
            (title, description, _timestamp()),
        )
        row = connection.execute(
            "SELECT id, title, description, created_at, completed FROM tasks WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return {**dict(row), "completed": bool(row["completed"])}


def set_task_completed(
    task_id: int,
    completed: bool,
    database_path: str | Path | None = None,
) -> dict:
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "UPDATE tasks SET completed = ? WHERE id = ?", (int(completed), task_id)
        )
        if cursor.rowcount == 0:
            raise RecordNotFound("Task not found.")
        row = connection.execute(
            "SELECT id, title, description, created_at, completed FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return {**dict(row), "completed": bool(row["completed"])}


def delete_task(task_id: int, database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise RecordNotFound("Task not found.")


def list_notes(database_path: str | Path | None = None) -> list[dict]:
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, created_at, updated_at, substr(content, 1, 180) AS preview "
            "FROM notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def create_note(
    title: str,
    content: str,
    database_path: str | Path | None = None,
) -> dict:
    now = _timestamp()
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO notes (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, content, now, now),
        )
        row = connection.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


def get_note(note_id: int, database_path: str | Path | None = None) -> dict:
    with _connection(database_path) as connection:
        row = connection.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    if row is None:
        raise RecordNotFound("Note not found.")
    return dict(row)


def update_note(
    note_id: int,
    title: str,
    content: str,
    database_path: str | Path | None = None,
) -> dict:
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
            (title, content, _timestamp(), note_id),
        )
        if cursor.rowcount == 0:
            raise RecordNotFound("Note not found.")
        row = connection.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    return dict(row)


def delete_note(note_id: int, database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if cursor.rowcount == 0:
            raise RecordNotFound("Note not found.")