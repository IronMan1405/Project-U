"""The daily briefing: one verified picture of a day from K.R.I.S.'s sources.

Sources: Google Calendar (the existing provider), K.R.I.S. tasks (SQLite), Apple Reminders,
and K.R.I.S. timers. Each source reports its own status ("ok", "not_connected" or
"unavailable" with the reason), so a briefing never presents missing data as an empty day.
Nothing is inferred: tasks have no completion time, so "completed today" is not reported.
A task and a reminder with the same title due the same day are shown once.
"""

from datetime import date, datetime, time, timedelta, timezone

import google_workspace
import storage
from reminders import AppleReminders, RemindersError, normalize_title

MAX_ITEMS = 15


def _local_now(now: datetime) -> datetime:
    return now.astimezone() if now.tzinfo else now.astimezone()


def _task_due(task: dict) -> tuple[date, datetime | None] | None:
    """(local due date, local due time or None for a date-only task)."""
    due = task.get("due_at")
    if not due:
        return None
    if len(due) == 10:
        return date.fromisoformat(due), None
    stamp = due[:-1]
    moment = datetime.fromisoformat(stamp.replace(" ", "T")).replace(tzinfo=timezone.utc).astimezone()
    return moment.date(), moment


def _reminder_due(item: dict) -> tuple[date, datetime | None] | None:
    if not item.get("due"):
        return None
    if item["all_day"]:
        return date.fromisoformat(item["due"]), None
    moment = datetime.fromisoformat(item["due"].replace(" ", "T")).astimezone()
    return moment.date(), moment


def _overdue(due: tuple[date, datetime | None], now: datetime) -> bool:
    day, moment = due
    return moment < now if moment is not None else day < now.date()


def _when(due: tuple[date, datetime | None] | None) -> str:
    if due is None:
        return ""
    day, moment = due
    return f"{moment:%a %d %b %H:%M}" if moment is not None else f"{day:%a %d %b} (all day)"


def _order(due: tuple[date, datetime | None] | None) -> str:
    """Sort key: by day, with a date-only item after the timed ones that day."""
    if due is None:
        return "9999"
    day, moment = due
    return f"{day.isoformat()} {moment:%H:%M}" if moment is not None else f"{day.isoformat()} 99:99"


def _cap(items: list[dict]) -> dict:
    return {"items": items[:MAX_ITEMS], "more": max(0, len(items) - MAX_ITEMS)}


def build_briefing(
    now: datetime,
    day: str = "today",
    *,
    database_path=None,
    google: "google_workspace.GoogleWorkspaceProvider | None" = None,
    reminders: AppleReminders | None = None,
    timers=None,
) -> dict:
    now = _local_now(now)
    target = now.date() + timedelta(days=1 if day == "tomorrow" else 0)
    day_start = datetime.combine(target, time.min).astimezone()
    day_end = day_start + timedelta(days=1)
    offset = now.strftime("%z")
    briefing = {
        "day": day, "date": target.isoformat(), "weekday": f"{target:%A}",
        "now_local": f"{now:%Y-%m-%d %H:%M}", "timezone": f"{now.tzname()} (UTC{offset[:3]}:{offset[3:]})",
        "sources": {},
    }

    # Calendar: Google, through the existing provider (never invented when unavailable).
    if google is None or not google.settings.enabled:
        briefing["sources"]["calendar"] = {"status": "not_connected", "reason": "Google Calendar is not connected."}
    else:
        try:
            found = google.search_events(day_start.isoformat(), day_end.isoformat(), None, google_workspace.MAX_EVENT_RESULTS)
            events = found["events"]
            if day == "today":
                for event in events:
                    end = event.get("end") or ""
                    event["finished"] = bool(not event["all_day"] and end and datetime.fromisoformat(end) <= now)
            briefing["sources"]["calendar"] = {"status": "ok", "events": events, "more": found["more_available"]}
        except google_workspace.GoogleError as error:
            briefing["sources"]["calendar"] = {"status": "unavailable", "reason": error.message}

    # Tasks: K.R.I.S.'s own list.
    tasks = [task for task in storage.list_tasks(database_path) if not task["completed"]]
    overdue, due_today, undated, later = [], [], [], []
    for task in tasks:
        due = _task_due(task)
        entry = {"task_id": task["id"], "title": task["title"], "due": _when(due), "source": "K.R.I.S. tasks",
                 "_order": _order(due)}
        if due is None:
            undated.append(entry)
        elif _overdue(due, now) and due[0] <= target:
            overdue.append(entry)
        elif due[0] == target:
            due_today.append(entry)
        else:
            later.append(entry)
    for bucket in (overdue, due_today, later):
        bucket.sort(key=lambda entry: entry["_order"])
    for entry in overdue + due_today + undated + later:
        entry.pop("_order")
    briefing["sources"]["tasks"] = {
        "status": "ok", "overdue": _cap(overdue), "due": _cap(due_today), "no_due_date": _cap(undated),
        "later_count": len(later), "note": "K.R.I.S. does not record when tasks were completed.",
    }

    # Reminders: Apple Reminders.
    seen_reminder_ids = []
    if reminders is None:
        briefing["sources"]["reminders"] = {"status": "not_connected", "reason": "Apple Reminders is not available."}
    else:
        try:
            open_reminders = reminders.list_open()
            r_overdue, r_due = [], []
            task_keys = {(normalize_title(t["title"]), (_task_due(t) or (None,))[0]) for t in tasks}
            for item in open_reminders:
                due = _reminder_due(item)
                if due is None:
                    continue
                is_overdue = _overdue(due, now)
                if not is_overdue and due[0] != target:
                    continue
                if (normalize_title(item["title"]), due[0]) in task_keys:
                    # The same item in both places: shown once, under tasks, marked as in both.
                    for bucket in (overdue, due_today):
                        for entry in bucket:
                            if normalize_title(entry["title"]) == normalize_title(item["title"]):
                                entry["also_in"] = "Apple Reminders"
                    continue
                entry = {"reminder_id": item["reminder_id"], "title": item["title"], "list": item["list"],
                         "due": _when(due), "source": "Apple Reminders", "_order": _order(due)}
                (r_overdue if is_overdue else r_due).append(entry)
                seen_reminder_ids.append(item["reminder_id"])
            for bucket in (r_overdue, r_due):
                bucket.sort(key=lambda entry: entry["_order"])
                for entry in bucket:
                    entry.pop("_order")
            briefing["sources"]["reminders"] = {"status": "ok", "overdue": _cap(r_overdue), "due": _cap(r_due)}
        except RemindersError as error:
            briefing["sources"]["reminders"] = {"status": "unavailable", "reason": error.message}

    # Timers and focus sessions.
    if timers is not None:
        briefing["sources"]["timers"] = {"status": "ok", "active": timers.active(),
                                         "recently_finished": timers.recent()}
    briefing["unavailable"] = [
        f"{name}: {source['reason']}" for name, source in briefing["sources"].items() if source["status"] != "ok"
    ]
    briefing["_reminder_ids"] = seen_reminder_ids
    return briefing
