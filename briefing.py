"""The daily briefing: one verified picture of a day from K.R.I.S.'s sources.

Sources: Google Calendar (the default Google account, or with calendar_accounts="all" every
connected account, grouped by account), K.R.I.S. tasks (SQLite), Apple Reminders, and K.R.I.S.
timers. Each source reports its own status ("ok", "not_connected" or
"unavailable" with the reason), so a briefing never presents missing data as an empty day.
Nothing is inferred: tasks have no completion time, so "completed today" is not reported.
A task and a reminder with the same title due the same day are shown once.
"""

from datetime import date, datetime, time, timedelta, timezone

import google_workspace
import priorities
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


def _all_calendars(accounts, day_events) -> dict:
    """Every connected account's events, grouped by account. An event that appears in several
    accounts (an invitation both received) is listed once, in the first, with also_in naming the
    others. One account failing is reported for that account; the others still count."""
    groups, unavailable, seen = [], [], {}
    for name in accounts.names:
        status = accounts.account_status(name)
        if not status["connected"]:
            unavailable.append({"account": name, "reason": status["message"] or "Not connected."})
            continue
        try:
            found = day_events(accounts.check(name))
        except google_workspace.GoogleError as error:
            unavailable.append({"account": name, "reason": error.message})
            continue
        events = []
        for event in found["events"]:
            key = (event.get("title"), event.get("start"), event.get("end"))
            if key in seen:
                seen[key].setdefault("also_in", []).append(name)
                continue
            seen[key] = event
            events.append(event)
        groups.append({"account": name, "email": status["email"], "events": events, "more": found["more_available"]})
    if not groups:
        return {"status": "unavailable", "reason": "; ".join(f"{u['account']}: {u['reason']}" for u in unavailable)
                or "No Google account is connected."}
    result = {"status": "ok", "accounts": groups,
              "note": "Events are grouped by account; always say which account each event is in."}
    if unavailable:
        result["unavailable_accounts"] = unavailable
    return result


def build_briefing(
    now: datetime,
    day: str = "today",
    *,
    database_path=None,
    google: "google_workspace.GoogleWorkspaceProvider | None" = None,
    reminders: AppleReminders | None = None,
    timers=None,
    calendar_accounts: str | None = None,
    include_email: bool = False,
    context: dict | None = None,
    window: str = "day",
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

    # Calendar: Google (never invented when unavailable). The default account unless all were asked for.
    def day_events(provider) -> dict:
        found = provider.search_events(day_start.isoformat(), day_end.isoformat(), None, google_workspace.MAX_EVENT_RESULTS)
        if day == "today":
            for event in found["events"]:
                end = event.get("end") or ""
                event["finished"] = bool(not event["all_day"] and end and datetime.fromisoformat(end) <= now)
        return found

    accounts = google if hasattr(google, "provider") else None  # several accounts (google_accounts.py)
    if google is None or not google.settings.enabled:
        briefing["sources"]["calendar"] = {"status": "not_connected", "reason": "Google Calendar is not connected."}
    elif calendar_accounts == "all" and accounts is not None and accounts.multiple:
        briefing["sources"]["calendar"] = _all_calendars(accounts, day_events)
    else:
        try:
            found = day_events(accounts.check(accounts.default) if accounts is not None else google)
            briefing["sources"]["calendar"] = {"status": "ok", "events": found["events"], "more": found["more_available"]}
            if accounts is not None and accounts.multiple:
                briefing["sources"]["calendar"]["account"] = accounts.default
                others = [name for name in accounts.names if name != accounts.default]
                briefing["sources"]["calendar"]["scope"] = (
                    f"Only the {accounts.default} calendar; not included: {', '.join(others)} (ask for all calendars)")
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
    # Recent mail: unread inbox messages from the last three days (headers only), for "needs attention".
    if include_email and google is not None and google.settings.enabled:
        briefing["sources"]["email"] = _recent_mail(google, calendar_accounts)

    briefing["unavailable"] = [
        f"{name}: {source['reason']}" for name, source in briefing["sources"].items() if source["status"] != "ok"
    ]
    briefing["_reminder_ids"] = seen_reminder_ids
    briefing["focus"] = priorities.focus(briefing, now, context, window)
    return briefing


MAIL_QUERY = "is:unread in:inbox newer_than:3d"
MAIL_LIMIT = 8


def _recent_mail(google, calendar_accounts: str | None) -> dict:
    accounts = google if hasattr(google, "provider") else None
    names = (accounts.connected() if calendar_accounts == "all" and accounts is not None and accounts.multiple
             else [accounts.default] if accounts is not None else [None])
    messages, problems = [], []
    for name in names:
        try:
            provider = accounts.check(name) if accounts is not None else google
            found = provider.search_messages(MAIL_QUERY, MAIL_LIMIT)
            messages += [{**message, "account": name} for message in found["messages"]]
        except google_workspace.GoogleError as error:
            problems.append(f"{name or 'Gmail'}: {error.message}")
    if problems and not messages:
        return {"status": "unavailable", "reason": "; ".join(problems)}
    result = {"status": "ok", "query": MAIL_QUERY, "messages": messages,
              "note": "Unread inbox mail from the last three days, headers only."}
    if problems:
        result["partly_unavailable"] = problems
    return result


EVENT_FIELDS = ("event_id", "title", "start", "end", "all_day", "finished")
MAX_EVENTS_FOR_MODEL = 12


def for_model(briefing: dict) -> dict:
    """The briefing trimmed for the model: the focus in full; events reduced to what identifies them;
    mail as a count (the messages that matter are already in the focus)."""
    result = {"focus": briefing["focus"], **{key: value for key, value in briefing.items() if key not in ("sources", "focus")}}
    sources = {}
    for name, source in briefing.get("sources", {}).items():
        source = dict(source)
        if name == "calendar" and source.get("status") == "ok":
            trim = lambda events: [{k: e[k] for k in EVENT_FIELDS if k in e} for e in events[:MAX_EVENTS_FOR_MODEL]]
            if source.get("accounts"):
                source["accounts"] = [{**group, "events": trim(group["events"]),
                                       "more": group.get("more") or len(group["events"]) > MAX_EVENTS_FOR_MODEL}
                                      for group in source["accounts"]]
            else:
                source["more"] = source.get("more") or len(source.get("events") or []) > MAX_EVENTS_FOR_MODEL
                source["events"] = trim(source.get("events") or [])
        if name == "email" and source.get("status") == "ok":
            source = {"status": "ok", "unread_recent": len(source.get("messages") or []), "note":
                      "Unread inbox mail from the last three days; those Gmail marks Important or Starred are in focus."}
        sources[name] = source
    result["sources"] = sources
    return result
