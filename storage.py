import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DATA_DIRECTORY = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATA_DIRECTORY / "project_u.sqlite3"


class RecordNotFound(Exception):
    pass


# The single accepted due_at format: a calendar date, or a UTC timestamp with a
# space separator and a trailing Z. Shared by the API, tool schemas, and the UI.
DUE_AT_PATTERN = r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2}(\.\d{1,6})?Z)?$"
DUE_AT_FORMAT_DESCRIPTION = (
    "YYYY-MM-DD for a calendar date, or 'YYYY-MM-DD HH:MM:SSZ' for a UTC time"
)
_DUE_AT = re.compile(DUE_AT_PATTERN)
_UNSET = object()


def validate_due_at(value: str | None) -> str | None:
    """Accept a calendar date or UTC timestamp in the task API/tool format."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("due_at must be a date string or null.")
    try:
        if not _DUE_AT.fullmatch(value):
            raise ValueError
        if len(value) == 10:
            datetime.strptime(value, "%Y-%m-%d")
        else:
            timestamp = value[:-1]
            fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in timestamp else "%Y-%m-%d %H:%M:%S"
            datetime.strptime(timestamp, fmt)
    except ValueError as error:
        raise ValueError(f"due_at must be {DUE_AT_FORMAT_DESCRIPTION}.") from error
    return value


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
                completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
                due_at TEXT
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
        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "due_at" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")


def list_tasks(database_path: str | Path | None = None) -> list[dict]:
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, description, created_at, completed, due_at "
            "FROM tasks ORDER BY completed ASC, created_at DESC, id DESC"
        ).fetchall()
    return [{**dict(row), "completed": bool(row["completed"]), "due_at": row["due_at"]} for row in rows]


def create_task(
    title: str,
    description: str = "",
    database_path: str | Path | None = None,
    *,
    due_at: str | None = None,
) -> dict:
    due_at = validate_due_at(due_at)
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (title, description, created_at, due_at) VALUES (?, ?, ?, ?)",
            (title, description, _timestamp(), due_at),
        )
        row = connection.execute(
            "SELECT id, title, description, created_at, completed, due_at FROM tasks WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return {**dict(row), "completed": bool(row["completed"]), "due_at": row["due_at"]}


def update_task(
    task_id: int,
    database_path: str | Path | None = None,
    *,
    completed: object = _UNSET,
    due_at: object = _UNSET,
) -> dict:
    """Update only the fields that are passed; due_at=None clears the due date."""
    assignments = []
    values: list[object] = []
    if completed is not _UNSET:
        if not isinstance(completed, bool):
            raise ValueError("completed must be a boolean.")
        assignments.append("completed = ?")
        values.append(int(completed))
    if due_at is not _UNSET:
        assignments.append("due_at = ?")
        values.append(validate_due_at(due_at))
    with _connection(database_path) as connection:
        if assignments:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", (*values, task_id)
            )
            if cursor.rowcount == 0:
                raise RecordNotFound("Task not found.")
        row = connection.execute(
            "SELECT id, title, description, created_at, completed, due_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise RecordNotFound("Task not found.")
    return {**dict(row), "completed": bool(row["completed"]), "due_at": row["due_at"]}


def set_task_completed(
    task_id: int,
    completed: bool,
    database_path: str | Path | None = None,
    *,
    due_at: object = _UNSET,
) -> dict:
    return update_task(task_id, database_path, completed=completed, due_at=due_at)


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
