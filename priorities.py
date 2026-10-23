"""What deserves attention: a short, explainable focus built from the briefing's sources.

Input is what briefing.build_briefing already gathered (calendar, tasks, reminders, timers,
recent mail) plus the conversation's working context. Nothing is fetched twice and nothing is
invented: every item states the evidence it rests on (its due time, "overdue since", Gmail's own
Important/Starred marker, an unfinished request), and items without such evidence are not
called urgent. The rules, in order:

  1. next commitment   the next timed calendar event that has not started (all-day events are
                       listed separately), with the minutes until it starts
  2. needs attention   overdue tasks and reminders; then items due before the next commitment
                       or later today; then unread mail Gmail marks Important or Starred;
                       then an unfinished request from this conversation
  3. suggestion        one next step with its reason: get ready if the next commitment starts
                       within PREPARE_MINUTES; otherwise the oldest overdue item; otherwise what
                       is due soonest; otherwise important mail; otherwise the unfinished request;
                       otherwise, with a free stretch, an undated task (said to have no deadline)

A suggestion is a proposal for the user, never an action: nothing here creates, moves, sends or
deletes anything. Sources that could not be read are listed, so a partial picture is never
presented as complete.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

PREPARE_MINUTES = 30
FREE_STRETCH_MINUTES = 45
MAX_ATTENTION = 5


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def _key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (title or "").casefold()).split())


def _events(calendar: dict) -> list[dict]:
    if calendar.get("status") != "ok":
        return []
    if calendar.get("accounts"):
        return [{**event, "account": group["account"]} for group in calendar["accounts"] for event in group["events"]]
    return [dict(event, account=calendar.get("account")) for event in calendar.get("events") or []]


def focus(briefing: dict, now: datetime, context: dict | None = None, window: str = "day") -> dict:
    """The focus for the briefing's day. window "before_next" keeps only what is overdue or due
    before the next commitment."""
    now = now.astimezone()
    sources = briefing.get("sources", {})
    result = {"as_of": now.strftime("%Y-%m-%d %H:%M"), "day": briefing.get("day"), "next_commitment": None,
              "all_day": [], "needs_attention": [], "suggestion": None,
              "unavailable": list(briefing.get("unavailable", []))}

    # 1. The next commitment (timed events only; all-day ones are context, not a start time).
    upcoming = []
    for event in _events(sources.get("calendar", {})):
        if event.get("all_day"):
            result["all_day"].append(event.get("title"))
            continue
        start = _parse(event.get("start"))
        if start is not None and start > now:
            upcoming.append((start, event))
    upcoming.sort(key=lambda pair: pair[0])
    result["calendar_checked"] = sources.get("calendar", {}).get("status") == "ok"
    scope = sources.get("calendar", {}).get("scope")
    if scope:
        result["calendar_scope"] = scope
    next_start = None
    if upcoming:
        next_start, event = upcoming[0]
        result["next_commitment"] = {
            "title": event.get("title"), "start": next_start.strftime("%H:%M"), "date": next_start.date().isoformat(),
            "minutes_until": int((next_start - now).total_seconds() // 60), "location": event.get("location") or None,
            "account": event.get("account"), "why": "next event in your calendar",
        }

    # 2. What needs attention, each with its evidence; the same title is listed once.
    attention, seen = [], set()

    def add(title: str, why: str, source: str, rank: int, due: str | None = None, extra: dict | None = None) -> None:
        if not title or _key(title) in seen:
            return
        seen.add(_key(title))
        attention.append({"title": title, "why": why, "source": source, "due": due, "rank": rank, **(extra or {})})

    tasks = sources.get("tasks", {}) if sources.get("tasks", {}).get("status") == "ok" else {}
    reminders = sources.get("reminders", {}) if sources.get("reminders", {}).get("status") == "ok" else {}
    for item in (tasks.get("overdue") or {}).get("items", []):
        add(item["title"], f"overdue (was due {item['due']})", "K.R.I.S. tasks", 0, item["due"], {"task_id": item.get("task_id")})
    for item in (reminders.get("overdue") or {}).get("items", []):
        add(item["title"], f"overdue (was due {item['due']})", "Apple Reminders", 0, item["due"])
    for source, block, ident in (("K.R.I.S. tasks", tasks.get("due"), "task_id"), ("Apple Reminders", reminders.get("due"), None)):
        for item in (block or {}).get("items", []):
            timed = re.search(r"\b(\d{2}):(\d{2})$", item.get("due") or "")
            before_next = False
            if timed and next_start is not None:
                due_at = now.replace(hour=int(timed.group(1)), minute=int(timed.group(2)), second=0, microsecond=0)
                before_next = due_at <= next_start
            why = (f"due {item['due']}, before your next commitment" if before_next
                   else f"due {item['due']}" if timed else "due today (no time set)" if briefing.get("day") == "today"
                   else f"due {item['due']}")
            add(item["title"], why, source, 1 if before_next or timed else 2, item.get("due"),
                {"task_id": item.get(ident)} if ident else None)
    mail = sources.get("email", {})
    if mail.get("status") == "ok":
        for message in mail.get("messages", []):
            if not message.get("unread") or not (message.get("important") or message.get("starred")):
                continue
            marker = "starred" if message.get("starred") else "marked Important by Gmail"
            add(f"Email: {message.get('subject')}", f"unread, {marker}, from {message.get('from')}", "Gmail", 3,
                message.get("date"), {"message_id": message.get("id"), "account": message.get("account")})
    objective = (context or {}).get("objective")
    if objective and objective.get("status") in ("CANCELLED", "FAILED", "PAUSED") and objective.get("remaining"):
        add(f"Unfinished: {objective['text']}", f"stopped ({objective['status'].lower()}); not done: "
            + "; ".join(objective["remaining"][:3]), "this conversation", 4)
    if window == "before_next":
        attention = [item for item in attention if item["rank"] in (0, 1) or item["source"] in ("Gmail", "this conversation")]
    attention.sort(key=lambda item: item["rank"])
    result["needs_attention"] = [{k: v for k, v in item.items() if k != "rank"} for item in attention[:MAX_ATTENTION]]
    result["more_attention"] = max(0, len(attention) - MAX_ATTENTION)

    # 3. One suggested next step, with its reason (a proposal, never an action).
    nxt = result["next_commitment"]
    suggestion = None
    if nxt and nxt["minutes_until"] <= PREPARE_MINUTES:
        suggestion = (f"Get ready for {nxt['title']}", f"it starts at {nxt['start']}, in {nxt['minutes_until']} minutes")
    elif attention:
        first = attention[0]
        verb = "Deal with" if first["rank"] == 0 else "Read" if first["source"] == "Gmail" else "Continue" if first["rank"] == 4 else "Work on"
        reason = first["why"]
        if nxt and first["rank"] > 0:
            reason += f"; you have {nxt['minutes_until']} minutes before {nxt['title']}"
        suggestion = (f"{verb} {first['title']}", reason)
    else:
        undated = (tasks.get("no_due_date") or {}).get("items", [])
        free = nxt["minutes_until"] if nxt else None
        if undated and (free is None or free >= FREE_STRETCH_MINUTES):
            stretch = f"{free} free minutes before {nxt['title']}" if nxt else "nothing is due"
            suggestion = (f"Pick up {undated[0]['title']}", f"it has no deadline and {stretch}")
    if suggestion:
        result["suggestion"] = {"text": suggestion[0], "reason": suggestion[1],
                                "note": "A suggestion only: nothing is done unless you ask."}
    return result


def summary_text(focus_view: dict) -> str:
    """The focus as plain sentences, stated from the data (for the tool result message)."""
    parts = []
    nxt = focus_view.get("next_commitment")
    parts.append(f"Next commitment: {nxt['title']} at {nxt['start']} on {nxt['date']} (in {nxt['minutes_until']} min)."
                 if nxt else "Next commitment: none found for this day." if focus_view.get("calendar_checked", True)
                 else "Next commitment: unknown, because the calendar could not be read.")
    if focus_view.get("calendar_scope"):
        parts.append(focus_view["calendar_scope"] + ".")
    if focus_view.get("needs_attention"):
        parts.append("Needs attention: " + "; ".join(f"{i['title']} ({i['why']})" for i in focus_view["needs_attention"]) + ".")
    else:
        parts.append("Needs attention: nothing overdue, due soon or flagged.")
    if focus_view.get("suggestion"):
        parts.append(f"Suggested next step: {focus_view['suggestion']['text']}, because {focus_view['suggestion']['reason']}.")
    if focus_view.get("unavailable"):
        parts.append("Not available: " + "; ".join(focus_view["unavailable"]) + ".")
    return " ".join(parts)


def dashboard(briefing: dict, context: dict | None, automation: dict | None) -> dict:
    """What the Focus panel shows: the focus, what is being worked on, source freshness and state."""
    focus_view = briefing["focus"]
    sources = {name: {"status": source.get("status"), "reason": source.get("reason")}
               for name, source in briefing.get("sources", {}).items()}
    working = None
    if context:
        if context.get("objective"):
            working = {"kind": "objective", "text": context["objective"]["text"], "status": context["objective"]["status"],
                       "remaining": context["objective"].get("remaining") or []}
        elif context.get("project"):
            working = {"kind": "project", "text": context["project"]["name"], "status": context["project"]["source"]}
    active = automation if automation and automation.get("status") in ("EXECUTING", "AWAITING_APPROVAL", "PAUSED") else None
    return {"as_of": focus_view["as_of"], "next_commitment": focus_view["next_commitment"],
            "needs_attention": focus_view["needs_attention"][:3], "more_attention": focus_view.get("more_attention", 0)
            + max(0, len(focus_view["needs_attention"]) - 3), "suggestion": focus_view["suggestion"],
            "all_day": focus_view["all_day"], "working_on": working, "automation": active, "sources": sources,
            "calendar_scope": focus_view.get("calendar_scope"), "calendar_checked": focus_view.get("calendar_checked", True),
            "unavailable": focus_view["unavailable"]}
