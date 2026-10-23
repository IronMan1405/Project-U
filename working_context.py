"""Working context: what this conversation is currently about, kept small, explicit and dated.

Built only from what actually happened here: tool results, approvals and declines, automation
runs and the user's own words. Never from the model's guesses, and never saved beyond the
conversation (this is not long-term memory). It answers "which draft?", "the project we're
working on", "what did we decide?" for the model, which receives it as one short note per request.

Each item records where it came from and when K.R.I.S. last saw it. Items older than
MAX_AGE_TURNS user messages or MAX_AGE_MINUTES are dropped; everything shown is labelled with
its time and status, and the note tells the model that live objects must be read again before a
consequential action (the tools also re-check). When two items of the same kind are recent, both
are shown, so an ambiguous "it" can be asked about rather than guessed.
"""

from __future__ import annotations

import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

MAX_AGE_TURNS = 12
MAX_AGE_MINUTES = 120
MAX_ITEMS = 8
MAX_PER_KIND = 3
MAX_NOTE_CHARS = 1800
RESET = re.compile(r"\b(?:new topic|change of subject|forget (?:that|all that|everything)|start over|never ?mind all that)\b",
                   re.IGNORECASE)


@dataclass
class Item:
    kind: str  # email, draft, event, task, note, reminder, timer, file, folder
    key: str
    label: str
    status: str  # found, read, pending approval, created, changed, sent, opened, declined, cancelled...
    source: str  # the tool or event that produced it
    turn: int
    at: datetime
    account: str | None = None
    extra: dict = field(default_factory=dict)


class WorkingContext:
    def __init__(self, clock: Callable[[], datetime] | None = None, project_names: Callable[[], list[str]] | None = None):
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.project_names = project_names or (lambda: [])
        self.turn = 0
        self.items: OrderedDict[tuple[str, str], Item] = OrderedDict()
        self.objective: dict | None = None  # {"text", "status", "remaining", "turn", "at"}
        self.project: dict | None = None  # {"name", "where", "source", "verified", "turn", "at"}
        self.last_action: dict | None = None
        self.last_search: dict | None = None
        self.decisions: deque[dict] = deque(maxlen=6)
        self.open_question: str | None = None

    # --- Lifecycle ------------------------------------------------------------------------

    def new_turn(self, message: str) -> None:
        self.turn += 1
        if RESET.search(message or ""):
            self.clear()
            return
        self._prune()
        self.open_question = None
        mentioned = [name for name in self.project_names() if _mentions(message, name)]
        if len(mentioned) == 1 and (self.project is None or self.project["name"] != mentioned[0]):
            # Named by the user: an explicit choice, but not yet a verified folder or repository.
            self.project = {"name": mentioned[0], "where": None, "source": "you named it", "verified": False,
                            "turn": self.turn, "at": self.clock()}

    def clear(self) -> None:
        self.items.clear()
        self.objective = self.project = self.last_action = self.last_search = None
        self.decisions.clear()
        self.open_question = None

    def _prune(self) -> None:
        now = self.clock()
        for key, item in list(self.items.items()):
            if self.turn - item.turn > MAX_AGE_TURNS or (now - item.at).total_seconds() > MAX_AGE_MINUTES * 60:
                del self.items[key]
        for name in ("project", "objective", "last_search"):
            value = getattr(self, name)
            if value and (self.turn - value["turn"] > MAX_AGE_TURNS
                          or (now - value["at"]).total_seconds() > MAX_AGE_MINUTES * 60):
                setattr(self, name, None)

    # --- Recording ------------------------------------------------------------------------

    def _put(self, kind: str, key, label: str, status: str, source: str, account=None, **extra) -> None:
        if key is None or key == "":
            return
        key = (kind, str(key))
        self.items.pop(key, None)
        self.items[key] = Item(kind, str(key[1]), str(label)[:140], status, source, self.turn, self.clock(), account, extra)
        same = [k for k in self.items if k[0] == kind]
        for old in same[:-MAX_PER_KIND]:
            del self.items[old]
        while len(self.items) > MAX_ITEMS * 2:
            self.items.popitem(last=False)

    def observe(self, name: str, arguments: dict, ok: bool, message: str, data) -> None:
        """Record what a tool did (called for every executed tool, direct or in an automation run)."""
        data = data if isinstance(data, dict) else {}
        account = data.get("account") or arguments.get("account")
        self.last_action = {"text": message[:200], "ok": ok, "tool": name, "turn": self.turn, "at": self.clock()}
        if not ok:
            return
        if name in ("search_gmail", "search_calendar_events", "find_files", "find_folders", "search_notes",
                    "list_reminders", "list_tasks", "find_music"):
            self.last_search = {"tool": name, "summary": message[:160], "turn": self.turn, "at": self.clock()}
        handler = getattr(self, f"_on_{name}", None)
        if handler is not None:
            handler(arguments, data, account)

    def pending(self, name: str, arguments: dict, summary: str) -> None:
        """An action waiting for the user's approval (e.g. a draft or a send shown in full)."""
        kind = _KINDS.get(name)
        if kind:
            self._put(kind, f"pending:{name}", summary, "waiting for your approval", name, arguments.get("account"))

    def resolved(self, name: str, summary: str, approved: bool) -> None:
        self.items.pop((_KINDS.get(name, ""), f"pending:{name}"), None)
        self.decisions.append({"text": f"You {'approved' if approved else 'declined'}: {summary[:160]}",
                               "turn": self.turn, "at": self.clock()})

    def automation(self, run_view: dict, report: str) -> None:
        remaining = [step["purpose"] for step in run_view.get("steps", []) if step["status"] in ("pending", "skipped", "cancelled")]
        status = run_view.get("status")
        self.objective = {"text": run_view.get("objective"), "status": status, "remaining": remaining[:5],
                          "turn": self.turn, "at": self.clock()}
        if run_view.get("question"):
            self.open_question = run_view["question"]

    # --- Per tool -------------------------------------------------------------------------

    def _on_search_gmail(self, arguments, data, account):
        for group in data.get("accounts") or [data]:
            for message in (group.get("messages") or [])[:2]:
                self._put("email", message.get("id"), f"\"{message.get('subject')}\" from {message.get('from')}", "found",
                          "search_gmail", group.get("account") or account)

    def _on_read_gmail_message(self, arguments, data, account):
        message = data.get("message") or {}
        self._put("email", arguments.get("message_id"), f"\"{message.get('subject')}\" from {message.get('from')}", "read",
                  "read_gmail_message", account)

    def _on_create_gmail_draft(self, arguments, data, account):
        to = ", ".join(arguments.get("to") or [])
        self._put("draft", data.get("draft_id"), f"to {to}: \"{arguments.get('subject')}\"", "created (not sent)",
                  "create_gmail_draft", account)

    def _on_update_gmail_draft(self, arguments, data, account):
        draft = arguments.get("draft") or {}
        to = ", ".join(draft.get("to") or [])
        self._put("draft", data.get("draft_id"), f"to {to}: \"{draft.get('subject')}\"", "changed (not sent)",
                  "update_gmail_draft", account)

    def _on_send(self, arguments, data, account):
        to = ", ".join(arguments.get("to") or [])
        self._put("email", data.get("message_id"), f"to {to}: \"{arguments.get('subject')}\"", "sent", "send", account)
        if arguments.get("draft_id"):
            self.items.pop(("draft", str(arguments["draft_id"])), None)

    _on_send_email = _on_reply_to_email = _on_forward_email = _on_send_gmail_draft = _on_send

    def _on_search_calendar_events(self, arguments, data, account):
        for group in data.get("accounts") or [data]:
            for event in (group.get("events") or [])[:2]:
                self._put("event", event.get("event_id"), f"\"{event.get('title')}\" at {str(event.get('start'))[:16]}",
                          "found", "search_calendar_events", group.get("account") or account)

    def _on_read_calendar_event(self, arguments, data, account):
        self._put("event", data.get("event_id"), f"\"{data.get('title')}\" at {str(data.get('start'))[:16]}", "read",
                  "read_calendar_event", account)

    def _on_calendar_change(self, arguments, data, account):
        event = arguments.get("event") or arguments.get("after") or {}
        status = {"create": "created", "update": "changed", "delete": "cancelled"}.get(arguments.get("operation"), "changed")
        self._put("event", data.get("event_id") or arguments.get("event_id"),
                  f"\"{event.get('title')}\" at {str(event.get('start'))[:16]}", status, "calendar", account)

    _on_create_calendar_event = _on_update_calendar_event = _on_delete_calendar_event = _on_calendar_change

    def _on_create_task(self, arguments, data, account):
        link = f" (from note {data['source_note_id']})" if data.get("source_note_id") else ""
        status = "already existed" if data.get("already_existed") else "created"
        self._put("task", data.get("id"), f"\"{data.get('title')}\"{link}" + (f", due {data['due_at']}" if data.get("due_at") else ""),
                  status, "create_task")

    def _on_set_task_completed(self, arguments, data, account):
        self._put("task", data.get("id"), f"\"{data.get('title')}\"", "completed" if data.get("completed") else "reopened",
                  "set_task_completed")

    def _on_set_task_due_date(self, arguments, data, account):
        self._put("task", data.get("id"), f"\"{data.get('title')}\", due {data.get('due_at')}", "changed", "set_task_due_date")

    def _on_capture_note(self, arguments, data, account):
        self._put("note", data.get("note_id"), f"\"{data.get('title')}\"", "already saved" if data.get("already_saved") else "saved",
                  "capture_note")

    def _on_create_note(self, arguments, data, account):
        self._put("note", data.get("id"), f"\"{data.get('title')}\"", "saved", "create_note")

    def _on_get_note(self, arguments, data, account):
        self._put("note", data.get("id"), f"\"{data.get('title')}\"", "read", "get_note")

    def _on_search_notes(self, arguments, data, account):
        for note in (data.get("notes") or [])[:2]:
            self._put("note", note.get("note_id"), f"\"{note.get('title')}\"", "found", "search_notes")

    def _on_create_reminder(self, arguments, data, account):
        self._put("reminder", data.get("reminder_id"), f"\"{data.get('title')}\" due {data.get('due')}", "created",
                  "create_reminder")

    def _on_start_timer(self, arguments, data, account):
        self._put("timer", data.get("timer_id"), str(data.get("name")), "running", "start_timer")

    def _on_find_folders(self, arguments, data, account):
        for result in (data.get("results") or [])[:2]:
            self._put("folder", result.get("path"), f"{result.get('path')} ({result.get('location')})", "found", "find_folders")

    def _on_find_files(self, arguments, data, account):
        for result in (data.get("results") or [])[:2]:
            self._put("file", result.get("path"), str(result.get("path")), "found", "find_files")

    def _opened(self, arguments, data, account):
        path = data.get("path") or arguments.get("path")
        app = data.get("application") or arguments.get("application")
        self._put("file", path, f"{path}" + (f" in {app}" if app else ""), "opened", "open")
        root = str(path or "").split("/")[0]
        if root in self.project_names():
            self.project = {"name": root, "where": path, "source": f"opened {path}", "verified": True,
                            "turn": self.turn, "at": self.clock()}

    _on_open_file = _on_open_in_editor = _opened

    def _project(self, arguments, data, account):
        name = data.get("project") or arguments.get("project")
        if name:
            self.project = {"name": name, "where": data.get("folder"), "source": "checked with the project tools",
                            "verified": True, "turn": self.turn, "at": self.clock()}

    _on_git_status = _on_git_diff_summary = _on_git_recent_commits = _on_run_project_tests = _on_project_overview = _project

    # --- Output ---------------------------------------------------------------------------

    def render(self) -> str | None:
        """The note for the model, or None when nothing is known."""
        lines = []
        when = lambda value: value["at"].strftime("%H:%M")
        if self.objective:
            line = f"Current objective: {self.objective['text']} ({self.objective['status'].lower()}, {when(self.objective)})"
            if self.objective["remaining"]:
                line += "; not done: " + "; ".join(self.objective["remaining"])
            lines.append(line)
        if self.project:
            how = "verified" if self.project["verified"] else "not yet checked"
            lines.append(f"Project being worked on: {self.project['name']} ({self.project['source']}, {how}, "
                         f"{when(self.project)})")
        if self.items:
            lines.append("Recent items, newest first (kind id [account]: what, status, when last seen):")
            for item in reversed(list(self.items.values())[-MAX_ITEMS:]):
                account = f" [{item.account}]" if item.account else ""
                key = "" if item.key.startswith("pending:") else f" {item.key}"
                lines.append(f"- {item.kind}{key}{account}: {item.label}, {item.status}, {item.at:%H:%M}")
        for decision in self.decisions:
            lines.append(f"Decision: {decision['text']} ({decision['at']:%H:%M})")
        if self.last_action:
            outcome = "done" if self.last_action["ok"] else "failed"
            lines.append(f"Last action: {self.last_action['text']} ({outcome}, {self.last_action['at']:%H:%M})")
        if self.open_question:
            lines.append(f"Open question to the user: {self.open_question}")
        if not lines:
            return None
        text = ("Working context from this conversation (facts from tool results and the user's decisions, with the "
                "time K.R.I.S. last saw them; they may have changed since). Use it to resolve 'it', 'that draft', 'the "
                "project'. If a reference could mean more than one listed item, ask. Read live items again before "
                "changing, sending or deleting anything.\n" + "\n".join(lines))
        return text[:MAX_NOTE_CHARS]

    def snapshot(self) -> dict:
        """For the dashboard: the objective, the project and the newest items."""
        def plain(value):
            return {k: (v.isoformat(timespec="minutes") if isinstance(v, datetime) else v) for k, v in value.items()} if value else None
        return {
            "objective": plain(self.objective), "project": plain(self.project),
            "items": [{"kind": i.kind, "label": i.label, "status": i.status, "account": i.account,
                       "at": i.at.isoformat(timespec="minutes")} for i in reversed(list(self.items.values())[-4:])
                      if not i.key.startswith("pending:")],
            "last_action": plain(self.last_action), "open_question": self.open_question,
        }


_KINDS = {"create_gmail_draft": "draft", "update_gmail_draft": "draft", "send_email": "email", "reply_to_email": "email",
          "forward_email": "email", "send_gmail_draft": "email", "create_calendar_event": "event",
          "update_calendar_event": "event", "delete_calendar_event": "event", "start_automation": "objective"}


def _mentions(message: str, name: str) -> bool:
    words = lambda text: " ".join(re.findall(r"[a-z0-9]+", (text or "").casefold()))
    return bool(name) and f" {words(name)} " in f" {words(message)} "
