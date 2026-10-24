"""Session continuity: the recent working session, kept so a page reload or a server restart does not
lose what the user was just doing. This is not long-term memory: a session older than
SESSION_MAX_AGE_MINUTES (the working context's own limit) is deleted, never restored.

What is kept is the minimum needed to continue, and only metadata and IDs:
  turns     the last MAX_TURNS exchanges: the user's words and K.R.I.S.'s reply. A reply from a turn
            that read or wrote email, draft, note or document content is not kept (a placeholder says
            so), because it may quote that content.
  context   the working context (items with IDs, titles, subjects and senders; the objective, project,
            decisions), with the times they were last seen, so its expiry rules keep applying.
  run       the latest automation run with work left: objective, status and each step's tool, purpose
            and status. No tool results, and arguments only when they hold no email/note/draft text;
            approvals are never kept.
Never kept: tool result data, email/draft/note/document bodies, pending confirmations, the
"seen in this conversation" ID guards (live objects are found and read again), credentials, audio.

Restoring never runs anything. A restored run is resumed only by the user ("do it"), through
AutomationRunner.resume(), which asks for every approval again and re-checks earlier results.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime

import automation
from automation import AutomationRun, Step
from working_context import MAX_AGE_MINUTES, Item, WorkingContext

VERSION = 1
SESSION_MAX_AGE_MINUTES = MAX_AGE_MINUTES
MAX_TURNS = 6
MAX_USER_CHARS = 1000
MAX_REPLY_CHARS = 2000
# Tools whose results (or arguments) carry email, draft, note or event-description text: a reply in a
# turn that used one may quote it, so that reply is not kept, and neither are their step arguments.
CONTENT_TOOLS = frozenset({
    "read_gmail_message", "list_gmail_drafts", "create_gmail_draft", "update_gmail_draft", "send_email",
    "reply_to_email", "forward_email", "send_gmail_draft", "read_calendar_event", "get_note", "list_notes",
    "search_notes", "create_note", "update_note", "capture_note",
})
REPLY_NOT_KEPT = ("(This reply is not kept after a reload because it may quote email, note or document content. "
                  "Read the item again if it is needed.)")
_TEXT_ARGUMENTS = frozenset({"body", "content", "description", "text", "draft", "message"})
_RUN_STATES = {automation.PAUSED, automation.CANCELLED, automation.FAILED}
_STEP_STATES = {"pending", "success", "uncertain", "failed", "skipped", "cancelled"}


class SessionError(ValueError):
    """The persisted session is unusable (wrong version, malformed or expired): start fresh."""


def turn_record(user: str, reply: str | None, at: datetime, saw_content: bool) -> dict:
    return {"user": (user or "")[:MAX_USER_CHARS], "reply": None if saw_content else (reply or "")[:MAX_REPLY_CHARS],
            "at": at.isoformat(timespec="seconds")}


def _time(value) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


def _parse(value) -> datetime:
    if not isinstance(value, str):
        raise SessionError("a time is missing")
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.astimezone()


def _dated(value: dict | None, keys: tuple[str, ...]) -> dict | None:
    if value is None:
        return None
    return {**{key: value.get(key) for key in keys}, "turn": int(value["turn"]), "at": _time(value["at"])}


def _label(item: Item) -> str:
    """An approval that waited for a content tool is labelled by its summary, which can hold the email
    or note text: only what kind of action it was is kept."""
    if item.key.startswith("pending:") and item.source in CONTENT_TOOLS:
        return f"{item.source.replace('_', ' ')} (details not kept)"
    return item.label


def _decision(decision: dict) -> dict:
    exported = _dated(decision, ("text",))
    if decision.get("tool") in CONTENT_TOOLS:  # "You declined: Create note: ... (content: ...)"
        exported["text"] = f"{decision['text'].split(':', 1)[0]}: {decision['tool'].replace('_', ' ')} (details not kept)"
    return exported


def export_context(context: WorkingContext) -> dict:
    return {
        "turn": context.turn,
        "items": [{"kind": i.kind, "key": i.key, "label": _label(i), "status": i.status, "source": i.source,
                   "turn": i.turn, "at": _time(i.at), "account": i.account} for i in context.items.values()],
        "objective": _dated(context.objective, ("text", "status", "remaining")),
        "project": _dated(context.project, ("name", "where", "source", "verified")),
        "last_action": _dated(context.last_action, ("text", "ok", "tool")),
        "last_search": _dated(context.last_search, ("tool", "summary")),
        "decisions": [_decision(d) for d in context.decisions],
        "open_question": context.open_question,
    }


def load_context(context: WorkingContext, data: dict) -> None:
    """Fill the conversation's (empty) working context; its usual expiry applies at once."""
    def dated(value):
        return None if value is None else {**value, "turn": int(value["turn"]), "at": _parse(value["at"])}
    items = OrderedDict()
    for raw in data["items"]:
        item = Item(str(raw["kind"]), str(raw["key"]), str(raw["label"])[:140], str(raw["status"]), str(raw["source"]),
                    int(raw["turn"]), _parse(raw["at"]), raw.get("account"))
        items[(item.kind, item.key)] = item
    context.turn = int(data["turn"])
    context.items = items
    context.objective = dated(data.get("objective"))
    if context.objective is not None:
        context.objective["remaining"] = [str(text) for text in context.objective.get("remaining") or []][:5]
    context.project = dated(data.get("project"))
    context.last_action = dated(data.get("last_action"))
    context.last_search = dated(data.get("last_search"))
    context.decisions = deque((dated(d) for d in data.get("decisions") or []), maxlen=6)
    context.open_question = data.get("open_question")
    context._prune()  # anything past MAX_AGE_TURNS / MAX_AGE_MINUTES goes, exactly as in a live session


def _safe_arguments(tool: str, arguments) -> dict | None:
    """Arguments with no email/note/draft text, or None (the step is then filled in again)."""
    if not isinstance(arguments, dict) or tool in CONTENT_TOOLS or _TEXT_ARGUMENTS & set(arguments):
        return None
    return arguments


def export_run(run: AutomationRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id, "objective": run.objective, "status": run.status, "propose_only": run.propose_only,
        "replans": run.replans, "created_at": run.created_at, "finished_at": run.finished_at,
        "steps": [{"tool": step.tool, "purpose": step.purpose, "depends_on": list(step.depends_on), "status": step.status,
                   "arguments": _safe_arguments(step.tool, step.arguments) if step.status == "pending" else None,
                   "message": "" if step.tool in CONTENT_TOOLS else step.message[:300],
                   "summary": step.summary[:200], "verification": step.verification[:200], "changes": step.changes}
                  for step in run.steps],
    }


def load_run(data: dict | None, tools: set[str]) -> AutomationRun | None:
    """A run to offer for resuming. Nothing is approved, nothing has result data; a step with a tool
    that no longer exists makes the whole run unusable."""
    if data is None:
        return None
    if data["status"] not in _RUN_STATES:
        raise SessionError("only unfinished runs are kept")
    steps = []
    for raw in data["steps"]:
        if raw["tool"] not in tools or raw["status"] not in _STEP_STATES:
            raise SessionError("a step is not usable any more")
        steps.append(Step(raw["tool"], _safe_arguments(raw["tool"], raw.get("arguments")), str(raw["purpose"])[:200],
                          depends_on=[int(i) for i in raw.get("depends_on") or []], status=raw["status"],
                          message=str(raw.get("message") or ""), verification=str(raw.get("verification") or ""),
                          summary=str(raw.get("summary") or ""), changes=bool(raw.get("changes"))))
    return AutomationRun(str(data["run_id"]), str(data["objective"])[:600], status=data["status"], steps=steps,
                         replans=int(data.get("replans") or 0), created_at=str(data["created_at"]),
                         finished_at=data.get("finished_at"), propose_only=bool(data.get("propose_only")))


def build(turns: list[dict], context: WorkingContext, run: AutomationRun | None, now: datetime) -> dict:
    return {"version": VERSION, "saved_at": now.isoformat(timespec="seconds"), "turns": list(turns)[-MAX_TURNS:],
            "context": export_context(context), "run": export_run(run)}


def check(state: object, now: datetime) -> dict:
    """The state if it can be restored; SessionError otherwise (the caller starts fresh)."""
    if not isinstance(state, dict) or state.get("version") != VERSION:
        raise SessionError("unknown session format")
    if (now - _parse(state.get("saved_at"))).total_seconds() > SESSION_MAX_AGE_MINUTES * 60:
        raise SessionError("the session has expired")
    if not isinstance(state.get("turns"), list) or not isinstance(state.get("context"), dict):
        raise SessionError("the session is incomplete")
    return state
