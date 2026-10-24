import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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


# Assistant-tool input for due dates. The model writes what the user said: a date, or a
# local wall-clock time; an explicit offset or Z only when the user names another zone.
# normalize_due_at converts these to the canonical storage format above in Python, so the
# model never does timezone arithmetic. "Local" means this Mac's system time zone.
TOOL_DUE_AT_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}"
    r"( \d{2}:\d{2}:\d{2}(\.\d{1,6})?Z|[ T]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?$"
)
TOOL_DUE_AT_FORMAT_DESCRIPTION = (
    "'YYYY-MM-DD' for a day, or 'YYYY-MM-DD HH:MM' (24-hour) for a time in the user's "
    "local time zone; add '+HH:MM', '-HH:MM' or 'Z' only for a time in another zone"
)
_TOOL_DUE_TIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:\d{2})?$"
)


def _local_wall_time(moment: datetime) -> datetime:
    """Attach this Mac's time zone to a naive local time, refusing DST gaps and overlaps."""
    first = moment.replace(fold=0).astimezone()
    second = moment.replace(fold=1).astimezone()
    if first.utcoffset() != second.utcoffset() or first.replace(tzinfo=None) != moment:
        raise ValueError(
            f"{moment:%Y-%m-%d %H:%M} is skipped or repeated by a daylight-saving change in "
            "the local time zone; give the time with an explicit UTC offset."
        )
    return first


def normalize_due_at(value: str | None) -> str | None:
    """Convert an assistant-tool due date to canonical storage form (date or UTC ...Z).

    Dates and canonical UTC values are kept exactly. Local times are converted with the
    system time zone's rules; explicit offsets are honoured. Anything else is rejected.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("due_at must be a date string or null.")
    try:
        return validate_due_at(value)
    except ValueError:
        pass
    match = _TOOL_DUE_TIME.fullmatch(value)
    if not match:
        raise ValueError(f"due_at must be {TOOL_DUE_AT_FORMAT_DESCRIPTION}.")
    year, month, day, hour, minute, second, zone = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError as error:
        raise ValueError(f"due_at {value!r} is not a real date and time.") from error
    if zone is None:
        aware = _local_wall_time(moment)
    elif zone == "Z":
        aware = moment.replace(tzinfo=timezone.utc)
    else:
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 14 or minutes > 59:
            raise ValueError(f"due_at has an invalid UTC offset {zone}.")
        offset = timedelta(hours=hours, minutes=minutes)
        aware = moment.replace(tzinfo=timezone(offset if zone[0] == "+" else -offset))
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def describe_due_at(due_at: str | None) -> str:
    """Human-readable local form of a stored due_at, e.g. 'Fri 2026-10-16 05:00 IST, UTC+05:30'."""
    if not due_at:
        return ""
    if len(due_at) == 10:
        return f"{datetime.strptime(due_at, '%Y-%m-%d'):%a} {due_at}"
    stamp = due_at[:-1]
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in stamp else "%Y-%m-%d %H:%M:%S"
    local = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).astimezone()
    offset = local.strftime("%z")
    utc_offset = f"UTC{offset[:3]}:{offset[3:]}"
    zone = utc_offset if local.tzname() in (None, utc_offset) else f"{local.tzname()}, {utc_offset}"
    return f"{local:%a %Y-%m-%d %H:%M} {zone}"


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
            -- K.R.I.S.-managed timers and focus sessions (timers.py). Times are UTC ISO strings.
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('timer', 'focus', 'break')),
                label TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'cancelled', 'missed')),
                created_at TEXT NOT NULL,
                starts_at TEXT,
                ends_at TEXT,
                after_timer_id INTEGER REFERENCES timers(id),
                finished_at TEXT
            );
            -- The recent working session (session_state.py), so a reload or restart can continue it.
            -- Compact JSON of metadata and IDs only; rows older than the session limit are deleted.
            CREATE TABLE IF NOT EXISTS sessions (
                conversation_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL
            );
            """
        )
        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "due_at" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        # A task made from a saved note keeps a link to it (quick capture -> task).
        if "source_note_id" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN source_note_id INTEGER")
        # A timer's optional Apple Reminder at its end time (timers.py): ID, due (UTC) and
        # state (open, completed, or failed when Reminders refused to complete it).
        timer_columns = {row["name"] for row in connection.execute("PRAGMA table_info(timers)").fetchall()}
        for column in ("reminder_id", "reminder_due", "reminder_state"):
            if column not in timer_columns:
                connection.execute(f"ALTER TABLE timers ADD COLUMN {column} TEXT")
        # Quick capture ("remember this ..."): optional tags (comma-separated, lower case) and
        # where a note came from ("" for notes written in the Notes tab).
        note_columns = {row["name"] for row in connection.execute("PRAGMA table_info(notes)").fetchall()}
        for column in ("tags", "source"):
            if column not in note_columns:
                connection.execute(f"ALTER TABLE notes ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def list_tasks(database_path: str | Path | None = None) -> list[dict]:
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, description, created_at, completed, due_at, source_note_id "
            "FROM tasks ORDER BY completed ASC, created_at DESC, id DESC"
        ).fetchall()
    return [{**dict(row), "completed": bool(row["completed"]), "due_at": row["due_at"]} for row in rows]


def create_task(
    title: str,
    description: str = "",
    database_path: str | Path | None = None,
    *,
    due_at: str | None = None,
    source_note_id: int | None = None,
) -> dict:
    due_at = validate_due_at(due_at)
    with _connection(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (title, description, created_at, due_at, source_note_id) VALUES (?, ?, ?, ?, ?)",
            (title, description, _timestamp(), due_at, source_note_id),
        )
        row = connection.execute(
            "SELECT id, title, description, created_at, completed, due_at, source_note_id FROM tasks WHERE id = ?",
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
            "SELECT id, title, description, created_at, completed, due_at, source_note_id FROM tasks WHERE id = ?",
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


MAX_NOTE_TAGS = 8
MAX_SEARCH_RESULTS = 10
_TAG = re.compile(r"^[\w][\w .+#-]{0,29}$")


def normalize_text(text: str) -> str:
    """Case, punctuation and spacing ignored: for spotting the same note or task twice."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").casefold()).split())


def normalize_tags(tags) -> str:
    if tags is None:
        return ""
    if not isinstance(tags, (list, tuple)) or len(tags) > MAX_NOTE_TAGS:
        raise ValueError(f"tags must be a list of at most {MAX_NOTE_TAGS} short words.")
    cleaned = []
    for tag in tags:
        tag = " ".join(str(tag).split()).casefold()
        if not _TAG.fullmatch(tag):
            raise ValueError("Each tag must be 1-30 letters, digits, spaces or . + # -.")
        if tag not in cleaned:
            cleaned.append(tag)
    return ",".join(cleaned)


def capture_note(title: str, content: str, *, tags=None, source: str = "",
                 database_path: str | Path | None = None) -> tuple[dict, bool]:
    """Save a captured note; returns (note, created). The same text already saved (case,
    punctuation and spacing ignored) is not saved twice: the existing note is returned."""
    tag_text = normalize_tags(tags)
    wanted = normalize_text(content)
    with _connection(database_path) as connection:
        for row in connection.execute("SELECT id, content FROM notes").fetchall():
            if normalize_text(row["content"]) == wanted:
                return _note(connection, row["id"]), False
        now = _timestamp()
        cursor = connection.execute(
            "INSERT INTO notes (title, content, created_at, updated_at, tags, source) VALUES (?, ?, ?, ?, ?, ?)",
            (title, content, now, now, tag_text, source[:100]),
        )
        return _note(connection, cursor.lastrowid), True


def _note(connection, note_id: int) -> dict:
    row = connection.execute(
        "SELECT id, title, content, created_at, updated_at, tags, source FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    note = dict(row)
    note["tags"] = [tag for tag in note["tags"].split(",") if tag]
    return note


def search_notes(words: list[str] | None = None, *, tag: str | None = None, created_after: str | None = None,
                 created_before: str | None = None, limit: int = MAX_SEARCH_RESULTS,
                 database_path: str | Path | None = None) -> dict:
    """Keyword search (not semantic): notes whose title, text or tags contain every word, newest
    first, optionally with a tag and within a creation-time range (UTC ISO bounds)."""
    clauses, values = [], []
    for word in words or []:
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
        values += [pattern, pattern, pattern]
    if tag:
        clauses.append("(',' || tags || ',') LIKE ?")
        values.append(f"%,{normalize_tags([tag])},%")
    if created_after:
        clauses.append("created_at >= ?")
        values.append(created_after)
    if created_before:
        clauses.append("created_at < ?")
        values.append(created_before)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"SELECT id FROM notes {where} ORDER BY created_at DESC, id DESC LIMIT ?", (*values, limit + 1)
        ).fetchall()
        notes = [_note(connection, row["id"]) for row in rows[:limit]]
    return {"notes": notes, "more": len(rows) > limit}


def find_open_task(title: str, due_at: str | None, database_path: str | Path | None = None) -> dict | None:
    """An open task with the same title (case, punctuation and spacing ignored) and due date."""
    wanted = normalize_text(title)
    for task in list_tasks(database_path):
        if not task["completed"] and normalize_text(task["title"]) == wanted and task["due_at"] == due_at:
            return task
    return None


def save_session(conversation_id: str, state: str, database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        connection.execute(
            "INSERT INTO sessions (conversation_id, updated_at, state) VALUES (?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET updated_at = excluded.updated_at, state = excluded.state",
            (conversation_id, _timestamp(), state),
        )


def latest_session(max_age_seconds: float, database_path: str | Path | None = None) -> tuple[str, str] | None:
    """The most recently saved session younger than max_age_seconds. Older ones are deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    with _connection(database_path) as connection:
        connection.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        row = connection.execute(
            "SELECT conversation_id, state FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return (row["conversation_id"], row["state"]) if row else None


def delete_session(conversation_id: str | None = None, database_path: str | Path | None = None) -> None:
    """Forget one saved session (or, with no ID, every saved session)."""
    with _connection(database_path) as connection:
        if conversation_id is None:
            connection.execute("DELETE FROM sessions")
        else:
            connection.execute("DELETE FROM sessions WHERE conversation_id = ?", (conversation_id,))


def delete_note(note_id: int, database_path: str | Path | None = None) -> None:
    with _connection(database_path) as connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if cursor.rowcount == 0:
            raise RecordNotFound("Note not found.")
