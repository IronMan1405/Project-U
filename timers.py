"""K.R.I.S.-managed timers, focus sessions and breaks.

Apple's Clock app has no scripting interface (no AppleScript dictionary), so K.R.I.S. cannot
set or verify Clock timers; it runs its own instead. Each timer is a row in SQLite (the
`timers` table), so it survives a restart:

    queued  -> running -> done        (it ran to the end while K.R.I.S. was running)
                       -> missed      (it ended while K.R.I.S. was not running)
            -> cancelled              (cancelled before it ended)

A queued timer ("a 10-minute break after that") starts the moment the timer it follows
ends. Timers only alert while K.R.I.S. is running: `TimerService.process_due` marks finished
timers and sends a macOS notification; it runs every second on a background thread (started
with the server) and also before every read, so state is always current. On startup
`recover` marks timers that ended while K.R.I.S. was closed as missed, never as done.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import storage

logger = logging.getLogger(__name__)

KINDS = ("timer", "focus", "break")
KIND_NAMES = {"timer": "timer", "focus": "focus session", "break": "break"}
MIN_SECONDS = 10
MAX_SECONDS = 12 * 60 * 60
MAX_ACTIVE = 5
MAX_LABEL_CHARS = 80
RECENT_SECONDS = 15 * 60  # finished timers stay visible this long
POLL_SECONDS = 1.0


class TimerError(ValueError):
    """A request that cannot be carried out; the message is safe to show."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def describe_duration(seconds: int) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def timer_name(row: dict) -> str:
    """'25-minute focus session "Lab report"' style name for messages and notifications."""
    seconds = row["duration_seconds"]
    if seconds < 60:
        length = f"{seconds}-second"
    elif seconds % 60 == 0:
        length = f"{seconds // 60}-minute"
    else:
        length = describe_duration(seconds)
    name = f"{length} {KIND_NAMES[row['kind']]}"
    return f'{name} "{row["label"]}"' if row["label"] else name


class TimerService:
    def __init__(
        self,
        database_path: str | Path | None = None,
        notifier: Callable[[str, str], bool] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        reminder_closer: Callable[[str], object] | None = None,
    ):
        self.database_path = database_path
        self.notifier = notifier
        # Completes a timer's Apple Reminder (reminders.AppleReminders.complete); raises on failure.
        self.reminder_closer = reminder_closer
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = False  # the table is created on first use, so an unavailable store never blocks startup

    # --- reading -----------------------------------------------------------------------
    def _ensure_table(self) -> None:
        if not self._ready:
            storage.initialize_database(self.database_path)
            self._ready = True

    def _rows(self, where: str, parameters: tuple = ()) -> list[dict]:
        self._ensure_table()
        with storage._connection(self.database_path) as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM timers WHERE {where} ORDER BY id", parameters)]

    def view(self, row: dict, now: datetime | None = None) -> dict:
        """What the model and the page see: no internal columns, times as local and remaining seconds."""
        now = now or self.clock()
        ends = _parse(row["ends_at"])
        result = {
            "timer_id": row["id"], "kind": row["kind"], "label": row["label"], "status": row["status"],
            "name": timer_name(row), "duration_seconds": row["duration_seconds"],
        }
        if row["status"] == "running" and ends:
            remaining = max(0, int((ends - now).total_seconds() + 0.999))
            result.update(remaining_seconds=remaining, remaining=describe_duration(remaining),
                          ends_at_local=ends.astimezone().strftime("%H:%M:%S"))
        if row["status"] == "queued":
            result["starts_after_timer_id"] = row["after_timer_id"]
        if row["finished_at"]:
            result["finished_at_local"] = _parse(row["finished_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if row.get("reminder_id"):
            result["reminder"] = {"due_local": _parse(row["reminder_due"]).astimezone().strftime("%Y-%m-%d %H:%M"),
                                  "state": row["reminder_state"]}
        return result

    # --- the optional Apple Reminder at a timer's end ------------------------------------
    def expected_end(self, timer_id: int) -> datetime:
        """When a running timer ends, or a queued one will (its chain's ends plus its own length)."""
        rows = self._rows("id = ?", (timer_id,))
        if not rows or rows[0]["status"] not in ("running", "queued"):
            raise TimerError("That timer is not running or queued.")
        row = rows[0]
        if row["status"] == "running":
            return _parse(row["ends_at"])
        return self.expected_end(row["after_timer_id"]) + timedelta(seconds=row["duration_seconds"])

    def attach_reminder(self, timer_id: int, reminder_id: str, due: datetime) -> None:
        with storage._connection(self.database_path) as connection:
            connection.execute("UPDATE timers SET reminder_id = ?, reminder_due = ?, reminder_state = 'open' WHERE id = ?",
                               (reminder_id, _stamp(due), timer_id))

    def reminder_id(self, timer_id: int) -> str:
        return self._rows("id = ?", (timer_id,))[0]["reminder_id"]

    def mark_reminder(self, timer_id: int, state: str) -> None:
        with storage._connection(self.database_path) as connection:
            connection.execute("UPDATE timers SET reminder_state = ? WHERE id = ?", (state, timer_id))

    def close_finished_reminders(self, grace_seconds: int = 120) -> list[int]:
        """Complete the reminders of timers that ended (done or missed) once the reminder has
        had time to alert, so finished timers do not pile up as overdue reminders. A reminder
        that cannot be completed is marked 'failed' and left for the user."""
        if self.reminder_closer is None:
            return []
        cutoff = _stamp(self.clock() - timedelta(seconds=grace_seconds))
        closed = []
        for row in self._rows("status IN ('done', 'missed') AND reminder_state = 'open' AND reminder_due <= ?", (cutoff,)):
            try:
                self.reminder_closer(row["reminder_id"])
            except Exception:
                logger.warning("Could not complete the reminder of timer %s.", row["id"])
                self.mark_reminder(row["id"], "failed")
                continue
            self.mark_reminder(row["id"], "completed")
            closed.append(row["id"])
        return closed

    def active(self) -> list[dict]:
        self.process_due()
        now = self.clock()
        return [self.view(row, now) for row in self._rows("status IN ('running', 'queued')")]

    def recent(self) -> list[dict]:
        """Timers that finished (done or missed) in the last RECENT_SECONDS."""
        self.process_due()
        now = self.clock()
        since = _stamp(now - timedelta(seconds=RECENT_SECONDS))
        rows = self._rows("status IN ('done', 'missed') AND finished_at >= ?", (since,))
        return [self.view(row, now) for row in rows]

    # --- changing ----------------------------------------------------------------------
    def start(self, kind: str, seconds: float, label: str | None = None, after_timer_id: int | None = None) -> dict:
        if kind not in KINDS:
            raise TimerError("The timer kind must be timer, focus or break.")
        seconds = int(round(seconds))
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise TimerError("A timer must be between 10 seconds and 12 hours long.")
        label = " ".join((label or "").split())[:MAX_LABEL_CHARS]
        with self._lock:
            self.process_due()
            if len(self._rows("status IN ('running', 'queued')")) >= MAX_ACTIVE:
                raise TimerError(f"{MAX_ACTIVE} timers are already running or queued; cancel one first.")
            now = self.clock()
            status, starts_at, ends_at = "running", _stamp(now), _stamp(now + timedelta(seconds=seconds))
            if after_timer_id is not None:
                previous = self._rows("id = ?", (after_timer_id,))
                if not previous or previous[0]["status"] not in ("running", "queued"):
                    raise TimerError("That timer is not running or queued, so nothing can start after it.")
                status, starts_at, ends_at = "queued", None, None
            with storage._connection(self.database_path) as connection:
                cursor = connection.execute(
                    "INSERT INTO timers (kind, label, duration_seconds, status, created_at, starts_at, ends_at, after_timer_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind, label, seconds, status, _stamp(now), starts_at, ends_at, after_timer_id),
                )
                timer_id = cursor.lastrowid
            return self.view(self._rows("id = ?", (timer_id,))[0])

    def cancel(self, timer_id: int | None = None) -> list[dict]:
        """Cancel one running or queued timer (and anything queued after it). Without an id,
        cancels the only active timer; with several active, refuses and lists them."""
        with self._lock:
            self.process_due()
            active = self._rows("status IN ('running', 'queued')")
            if timer_id is None:
                if not active:
                    raise TimerError("No timer is running.")
                heads = [row for row in active if row["status"] == "running"] or active
                if len(heads) > 1:
                    names = "; ".join(f"timer {row['id']}: {timer_name(row)}" for row in heads)
                    raise TimerError(f"Several timers are running ({names}). Ask which one to cancel.")
                timer_id = heads[0]["id"]
            target = [row for row in active if row["id"] == timer_id]
            if not target:
                raise TimerError("That timer is not running or queued.")
            cancelled, queue = [], [timer_id]
            now = _stamp(self.clock())
            with storage._connection(self.database_path) as connection:
                while queue:
                    current = queue.pop()
                    connection.execute(
                        "UPDATE timers SET status = 'cancelled', finished_at = ? WHERE id = ? AND status IN ('running', 'queued')",
                        (now, current),
                    )
                    cancelled.append(current)
                    queue.extend(row["id"] for row in connection.execute(
                        "SELECT id FROM timers WHERE after_timer_id = ? AND status = 'queued'", (current,)))
            return [self.view(row) for row in self._rows(f"id IN ({','.join('?' * len(cancelled))})", tuple(cancelled))]

    def process_due(self, recovering: bool = False) -> list[dict]:
        """Finish every running timer whose end has passed and start what was queued after it.
        Timers found already ended during startup recovery are 'missed', not 'done'."""
        finished = []
        with self._lock:
            now = self.clock()
            while True:
                due = self._rows("status = 'running' AND ends_at <= ?", (_stamp(now),))
                if not due:
                    break
                with storage._connection(self.database_path) as connection:
                    for row in due:
                        status = "missed" if recovering else "done"
                        connection.execute("UPDATE timers SET status = ?, finished_at = ends_at WHERE id = ?",
                                           (status, row["id"]))
                        for follower in connection.execute(
                                "SELECT id, duration_seconds FROM timers WHERE after_timer_id = ? AND status = 'queued'",
                                (row["id"],)).fetchall():
                            start = _parse(row["ends_at"])
                            connection.execute(
                                "UPDATE timers SET status = 'running', starts_at = ?, ends_at = ? WHERE id = ?",
                                (_stamp(start), _stamp(start + timedelta(seconds=follower["duration_seconds"])), follower["id"]),
                            )
                finished += [self.view({**row, "status": "missed" if recovering else "done", "finished_at": row["ends_at"]}, now)
                             for row in due]
        for timer in finished:
            self._notify(timer, recovering)
        return finished

    def recover(self) -> list[dict]:
        """At startup: timers that ended while K.R.I.S. was closed become 'missed'."""
        return self.process_due(recovering=True)

    def _notify(self, timer: dict, missed: bool) -> None:
        if self.notifier is None:
            return
        if missed:
            message = f"Your {timer['name']} ended at {timer.get('finished_at_local', '')[11:16]} while K.R.I.S. was not running."
        else:
            message = f"Your {timer['name']} is done."
        try:
            self.notifier("K.R.I.S.", message)
        except Exception:  # a notification failure never affects the timer itself
            logger.warning("Timer notification failed.")

    # --- background --------------------------------------------------------------------
    def start_background(self) -> None:
        """Recover, then check every second on a daemon thread (the server only; tests call process_due)."""
        if self._thread is not None:
            return
        self.recover()
        self._thread = threading.Thread(target=self._run, name="kris-timers", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.process_due()
                self.close_finished_reminders()
            except Exception:
                logger.exception("Timer check failed")

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
