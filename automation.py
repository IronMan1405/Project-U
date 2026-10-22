"""Automation runs: a user objective carried out as a short, checked plan over existing tools.

The model decides what should happen; this module decides whether and how it may happen.

  plan     One model call turns the objective into structured steps (tool, arguments, purpose,
           dependencies) chosen only from the tools the registry offers. Arguments that depend on
           an earlier result are left to be filled in later. Unsupported parts and a needed
           clarification are reported, never invented.
  approve  Steps that the existing policy would confirm are shown together as one plan; one
           approval covers exactly those steps. A consequential step (a send, a Calendar change,
           a draft, a UI press...) is covered only if its exact action was shown; otherwise it
           gets the usual per-action confirmation with the full preview when it is reached. A
           step that changes after approval (another tool, or a new step needing approval) needs
           a new approval.
  execute  Each step goes through ToolRegistry.prepare (validation, account selection, seen-ID
           checks, policy) and execute, then is verified with a read-back where one exists
           (success, uncertain or failed). The model is consulted again only when a step depends
           on earlier results or something did not go as planned: it may fill in arguments,
           change the remaining steps, ask the user one question, or finish.
  bounds   At most max_steps executed steps, max_replans plan changes, max_retries retries of a
           transient failure per step, and timeout_seconds of execution; cancellation (a new
           message, Cancel, or the stop request) stops before the next step. A run cannot start
           another run, and there is no shell, script or code step: only registered tools.

Runs live in memory with their conversation (AssistantRuntime); nothing is saved as memory.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from tool_registry import PreparedToolCall, ToolError, ToolOutcome, validate_arguments

AUTOMATION_TOOL = "start_automation"
PLANNING, AWAITING_APPROVAL, EXECUTING, PAUSED, FAILED, COMPLETED, CANCELLED = (
    "PLANNING", "AWAITING_APPROVAL", "EXECUTING", "PAUSED", "FAILED", "COMPLETED", "CANCELLED")
READ, LOW_IMPACT, CONSEQUENTIAL = "read", "low_impact", "consequential"
MAX_RESULT_CHARS_FOR_MODEL = 1800
MAX_PURPOSE_CHARS = 120
# Treated as consequential inside automation: an approval covers only the exact action shown.
ALWAYS_EXACT = frozenset({"ui_action"})
EMPTY_KEYS = ("events", "messages", "results", "notes", "reminders", "drafts", "elements", "accounts", "tasks")


@dataclass(frozen=True)
class AutomationLimits:
    enabled: bool = True
    max_steps: int = 8
    max_replans: int = 3
    max_retries: int = 1
    timeout_seconds: int = 300


def build_automation_limits(values: dict[str, str]) -> AutomationLimits:
    """[automation] settings, clamped to sensible bounds."""
    defaults = AutomationLimits()

    def number(name: str, low: int, high: int) -> int:
        try:
            return min(max(int(values.get(name, getattr(defaults, name))), low), high)
        except (TypeError, ValueError):
            return getattr(defaults, name)

    enabled = str(values.get("enabled", "on")).strip().casefold() not in ("0", "off", "false", "no")
    return AutomationLimits(enabled, number("max_steps", 1, 15), number("max_replans", 0, 6),
                            number("max_retries", 0, 3), number("timeout_seconds", 30, 1800))


@dataclass
class Step:
    tool: str
    arguments: dict | None  # None: filled in from earlier results when the step is reached
    purpose: str
    depends_on: list[int] = field(default_factory=list)
    status: str = "pending"  # pending, success, uncertain, failed, skipped, cancelled
    message: str = ""
    verification: str = ""
    approved: bool = False
    approved_summary: str | None = None  # the exact action shown, for a consequential step
    attempts: int = 0
    summary: str = ""  # what was actually run
    changes: bool = False  # the tool changes something (not a read)
    decided: bool = False  # written by the model from the results: run as decided, without asking again
    data: object = None  # the tool's result data, kept for later steps

    def view(self, index: int) -> dict:
        view = {"step": index + 1, "tool": self.tool, "purpose": self.purpose, "status": self.status}
        for key in ("summary", "message", "verification"):
            if getattr(self, key):
                view[key] = getattr(self, key)
        return view


@dataclass
class AutomationRun:
    run_id: str
    objective: str
    status: str = PLANNING
    steps: list[Step] = field(default_factory=list)
    current: int | None = None
    replans: int = 0
    executed: int = 0
    question: str | None = None
    unsupported: str | None = None
    answer: str | None = None  # what the model concluded for the user (findings, not claims of actions)
    failures: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    finished_at: str | None = None
    deadline: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def view(self) -> dict:
        return {"run_id": self.run_id, "objective": self.objective, "status": self.status,
                "steps": [step.view(i) for i, step in enumerate(self.steps)],
                "current_step": None if self.current is None else self.current + 1,
                "question": self.question, "unsupported": self.unsupported, "failures": self.failures,
                "created_at": self.created_at, "finished_at": self.finished_at}


@dataclass
class Outcome:
    """What the runtime must do next: show an approval, confirm one action, ask, or report."""
    kind: str  # "approve_plan", "confirm_step", "question", "done"
    run: AutomationRun
    prepared: PreparedToolCall | None = None
    text: str = ""


class PlanError(ValueError):
    pass


ROUTE_SCHEMA = {"type": "object", "properties": {"multi_step": {"type": "boolean"}}, "required": ["multi_step"]}
ROUTER_PROMPT = """Decide how K.R.I.S. should handle the user's latest message. Answer JSON {"multi_step": true|false}.
true: it asks K.R.I.S. to carry out an objective that needs two or more different actions or tools, e.g. find
something and then act on it, do several things, or get something ready (prepare a project, set up for an event).
false: a question, a single action, a reply to K.R.I.S.'s last question, small talk, or anything unclear."""
# Cheap structural signs that a message may combine actions; only then is the model asked (one short call).
_MAYBE_OBJECTIVE = re.compile(
    r"\b(?:and|then|also|after|before|plus)\b|,\s*\w+|\b(?:ready|prepare|set up|setup|take care|handle|organi[sz]e|"
    r"clean up|sort out|get (?:me|everything)|workspace)\b", re.IGNORECASE)


def wants_automation(message: str, last_reply: str, ask_json) -> bool:
    """Whether a message is an objective for an automation run. The model decides; this only skips
    the question for short or single-clause messages."""
    if len(message.split()) < 5 or not _MAYBE_OBJECTIVE.search(message):
        return False
    try:
        answer = ask_json([{"role": "system", "content": ROUTER_PROMPT},
                           {"role": "user", "content": (f"K.R.I.S. last said: {last_reply[:300]}\n" if last_reply else "")
                                                       + f"User: {message[:600]}"}], ROUTE_SCHEMA)
    except Exception:
        return False  # no answer: the ordinary path handles it
    return answer.get("multi_step") is True


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
            "fill_later": {"type": "boolean"},
            "purpose": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
        }, "required": ["tool", "purpose"]}},
        "unsupported": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["steps"],
}
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["run", "replan", "ask", "finish", "stop"]},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "purpose": {"type": "string"},
        "steps": PLAN_SCHEMA["properties"]["steps"],
        "question": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["action"],
}

PLANNER_PROMPT = """You plan short automations for K.R.I.S., a personal assistant on the user's Mac.
Turn the user's objective into the fewest steps that achieve it, using ONLY the tools listed below.
Answer with JSON: {"steps": [{"tool", "arguments", "fill_later", "purpose", "depends_on"}], "unsupported", "question"}.
- tool: an exact tool name from the list. Never invent a tool or a capability.
- arguments: the tool's arguments, with exact values. If a value can only come from an earlier step's
  result (an ID, a path, a time, a title), set "fill_later": true and leave arguments empty; it is filled in
  when that step is reached.
- purpose: what the step does, for the user, at most 12 words.
- depends_on: indexes (from 0) of earlier steps whose results this step needs.
- Use only the sources the objective needs; do not search everything just because a tool exists.
- Google tools: use the account the user named (account argument); otherwise leave it out.
- Dates and times: machine format (YYYY-MM-DD, YYYY-MM-DDTHH:MM, or 'YYYY-MM-DD HH:MM' for due_at),
  from the current date below.
- Draft emails, never send them, unless the user explicitly asked to send.
- Save a note (capture_note) only if the user asked you to remember something.
- Prefer a modest, sensible plan over asking: the user sees the plan and approves anything that changes
  something. "Ready", "set up" or "prepare" for a project usually means opening it in the editor
  (open_in_editor) and, if useful, checking its state; for an event, finding it and opening what it needs.
  "Remember X and remind me" means saving X (capture_note) and creating a reminder.
- Opening a project or code folder for work uses open_in_editor, not Finder.
- Ask only when no reasonable plan exists (nothing concrete is named and the tools cannot find out): put
  ONE short clarifying question in "question" and no steps.
- If the objective (or part of it) needs something no tool can do, such as booking, buying, paying,
  calling, or acting on a website, describe it in "unsupported" instead of asking for its details, and
  plan the rest only if it is still useful on its own.
- A single simple action is one step."""

DECIDER_PROMPT = """You are carrying out a K.R.I.S. automation, one step at a time. Using ONLY the tools listed,
decide what happens next. Answer with JSON: {"action", ...}.
- "run": run the next step now, with "tool", concrete "arguments" (exact IDs, paths, titles and times taken
  from the results so far) and "purpose". Normally this is the planned next step, filled in.
- "replan": the remaining plan no longer fits the results (nothing found, a different place to look):
  give the new remaining "steps".
- "ask": the results are ambiguous or something the user must decide is missing for THIS objective: one short
  "question". Do not ask about optional further work; mention it in the summary instead.
- "finish": the objective is achieved, or nothing more is useful: "summary" answers the user from the
  results, with the concrete findings (for events: titles and times; for searches: what matched). Do not
  claim actions; K.R.I.S. reports those itself.
- Opening a project or code folder for work uses open_in_editor, not open_file (Finder).
- "stop": continuing would be wrong or impossible: "summary" says why.
Never repeat a step that already succeeded. Never pick one of several plausible matches yourself: ask.
When all planned steps are done, either run what the objective still needs or finish."""


class AutomationRunner:
    def __init__(self, registry, ask_json: Callable[[list[dict], dict], dict], *,
                 limits: AutomationLimits | None = None, notify: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, context: Callable[[], str] | None = None):
        self.registry = registry
        self.ask_json = ask_json
        self.limits = limits or AutomationLimits()
        self.notify = notify or (lambda activity: None)
        self.clock = clock
        self.context = context or (lambda: "")

    # --- Tools the planner may use -------------------------------------------------------

    def tools(self) -> dict:
        return {name: definition for name, definition in self.registry._definitions.items() if name != AUTOMATION_TOOL}

    def tier(self, tool: str) -> str:
        definition = self.registry._definitions[tool]
        if definition.always_confirm or tool in ALWAYS_EXACT:
            return CONSEQUENTIAL
        return LOW_IMPACT if definition.direct_write else READ

    def catalog(self) -> str:
        lines = []
        for name, definition in self.tools().items():
            properties = definition.parameters.get("properties", {})
            required = set(definition.parameters.get("required", []))
            params = ", ".join(f"{key}{'' if key in required else '?'}" + (
                f"={'|'.join(map(str, spec['enum']))}" if isinstance(spec, dict) and spec.get("enum") and len(spec["enum"]) <= 8 else "")
                for key, spec in properties.items())
            kind = {READ: "reads", LOW_IMPACT: "small change", CONSEQUENTIAL: "needs approval"}[self.tier(name)]
            lines.append(f"- {name}({params}) [{kind}]: {_first_sentence(definition.description)}")
        return "\n".join(lines)

    # --- Planning ------------------------------------------------------------------------

    def plan(self, objective: str, conversation: str = "") -> AutomationRun:
        run = AutomationRun(run_id=secrets.token_hex(6), objective=" ".join(objective.split())[:500])
        self._event(run, PLANNING, f"Planning: {run.objective}")
        messages = [
            {"role": "system", "content": f"{PLANNER_PROMPT}\n\n{self.context()}\n\nTools:\n{self.catalog()}"},
            {"role": "user", "content": (f"Recent conversation (for references like 'that'):\n{conversation}\n\n"
                                         if conversation else "") + f"Objective: {run.objective}"},
        ]
        try:
            raw = self.ask_json(messages, PLAN_SCHEMA)
            run.unsupported = _text(raw.get("unsupported"), 300)
            run.question = _text(raw.get("question"), 300)
            steps = raw.get("steps")
            if steps is None and (run.unsupported or run.question):
                steps = []  # nothing to run: only something to report or ask
            run.steps = self._steps(steps)
        except PlanError as error:
            run.status, run.answer = FAILED, f"I could not make a usable plan: {error}"
            return run
        except Exception:
            run.status, run.answer = FAILED, "I could not make a plan (the local model did not answer properly)."
            return run
        if run.question and not run.steps:
            run.status = PAUSED
        return run

    def _steps(self, raw, offset: int = 0) -> list[Step]:
        if not isinstance(raw, list):
            raise PlanError("the plan has no steps")
        if offset + len(raw) > self.limits.max_steps:
            raise PlanError(f"it needs more than {self.limits.max_steps} steps; narrow the request")
        tools, steps = self.tools(), []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise PlanError("a step is malformed")
            tool = item.get("tool")
            if tool == AUTOMATION_TOOL:
                raise PlanError("an automation cannot start another automation")
            if tool not in tools:
                raise PlanError(f"it uses a tool K.R.I.S. does not have ({str(tool)[:60]})")
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            later = bool(item.get("fill_later"))
            problem = ""
            if not later:
                try:  # check concrete arguments against the tool's schema now (no side effects)
                    arguments = validate_arguments(tools[tool].parameters, arguments)
                except ToolError as error:
                    if "must be one of" in str(error):  # e.g. an app that is not allowed: a refusal, not a gap
                        raise PlanError(f"{tool}: {error}") from None
                    later = True  # incomplete: filled in when the step is reached
                    problem = str(error)
            depends = [d for d in item.get("depends_on") or [] if isinstance(d, int) and 0 <= d < offset + index]
            steps.append(Step(tool, None if later else arguments, _text(item.get("purpose"), MAX_PURPOSE_CHARS) or tool,
                              depends, message=problem))
        return steps

    def needs_approval(self, run: AutomationRun) -> list[int]:
        """Planned steps that the existing policy would not run directly, given the objective."""
        pending = []
        for index, step in enumerate(run.steps):
            if step.status != "pending" or step.approved:
                continue
            tier = self.tier(step.tool)
            if tier == READ:
                continue
            if tier == CONSEQUENTIAL or step.arguments is None:
                pending.append(index)
                continue
            try:
                prepared = self.registry.prepare(step.tool, step.arguments, run.objective)
            except ToolError:
                continue  # it will fail (and be reported) when reached
            if prepared.always_confirm:
                pending.append(index)
        return pending

    def plan_text(self, run: AutomationRun, indexes: list[int] | None = None) -> str:
        lines = []
        for index, step in enumerate(run.steps):
            if step.status != "pending":
                continue
            mark = ""
            if indexes is not None and index in indexes:
                mark = (" (you will see the exact details before it happens)" if self.tier(step.tool) == CONSEQUENTIAL
                        and not self._concrete_summary(run, step) else "")
            lines.append(f"{index + 1}. {step.purpose}{mark}")
        return "\n".join(lines)

    def approval_preview(self, run: AutomationRun, indexes: list[int]) -> dict:
        rows = []
        for index, step in enumerate(run.steps):
            if step.status != "pending":
                continue
            needs = "needs approval" if index in indexes else {READ: "read only", LOW_IMPACT: "small change",
                                                              CONSEQUENTIAL: "approved"}[self.tier(step.tool)]
            rows.append([f"Step {index + 1}", f"{step.purpose} — {needs}"])
        notes = ["Approving runs these steps only. Anything different that needs approval is asked for again.",
                 "Say \"stop\" or press Stop to cancel; finished steps are kept."]
        if run.unsupported:
            notes.insert(0, f"Not possible: {run.unsupported}")
        return {"kind": "sections", "sections": [{"heading": f"Plan: {run.objective}", "rows": rows}], "notes": notes}

    def _concrete_summary(self, run: AutomationRun, step: Step) -> str | None:
        if step.arguments is None:
            return None
        try:
            return self.registry.prepare(step.tool, step.arguments, run.objective).summary
        except ToolError:
            return None

    # --- Approval and execution ----------------------------------------------------------

    def start(self, run: AutomationRun) -> Outcome:
        if run.status in (FAILED, PAUSED):
            return self._finish(run)
        if not run.steps:
            run.status = FAILED if run.unsupported else COMPLETED
            return self._finish(run)
        # Steps already known in full are checked first, so nothing that cannot run is put up for approval.
        for index, step in enumerate(run.steps):
            if step.arguments is None or self.tier(step.tool) == READ:
                continue
            try:
                self.registry.prepare(step.tool, step.arguments, run.objective)
            except ToolError as error:
                kind = classify(str(error), None)
                if kind in ("permission", "unsupported", "ambiguous", "missing"):
                    return self._step_failed(run, index, str(error), kind)
        approval = self.needs_approval(run)
        if approval:
            run.status = AWAITING_APPROVAL
            self._event(run, AWAITING_APPROVAL, "Waiting for approval of the plan")
            return Outcome("approve_plan", run, text=self.plan_text(run, approval))
        return self.execute(run)

    def approve(self, run: AutomationRun) -> Outcome:
        """The user approved the plan as shown: exactly its pending steps."""
        for step in run.steps:
            if step.status == "pending" and not step.approved:
                step.approved = True
                if self.tier(step.tool) == CONSEQUENTIAL:
                    step.approved_summary = self._concrete_summary(run, step)
        return self.execute(run)

    def cancel(self, run: AutomationRun, reason: str = "cancelled") -> Outcome:
        run.cancel_event.set()
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        run.status = CANCELLED
        self._event(run, CANCELLED, f"Automation {reason}")
        return self._finish(run)

    def execute(self, run: AutomationRun) -> Outcome:
        run.status = EXECUTING
        # The time limit counts execution, not time spent waiting for the user's approval.
        run.deadline = self.clock() + self.limits.timeout_seconds
        repaired: set[int] = set()
        while True:
            if run.cancel_event.is_set():
                return self.cancel(run)
            if self.clock() > run.deadline:
                return self._stop(run, f"it ran longer than {self.limits.timeout_seconds} seconds")
            index = next((i for i, step in enumerate(run.steps) if step.status == "pending"), None)
            if index is None:
                return self._conclude(run)
            if run.executed >= self.limits.max_steps:
                return self._stop(run, f"it reached the limit of {self.limits.max_steps} steps")
            step = run.steps[index]
            run.current = index
            if self._needs_decision(run, index):
                decision = self._decide(run, index)
                if isinstance(decision, Outcome):
                    return decision
                if run.cancel_event.is_set():
                    return self.cancel(run)
                step = run.steps[index]
            try:
                prepared = self.registry.prepare(step.tool, step.arguments, run.objective)
            except ToolError as error:
                kind = classify(str(error), None)
                if kind in ("invalid", "missing") and index not in repaired and run.replans < self.limits.max_replans:
                    repaired.add(index)  # let the model correct the arguments once
                    step.message = str(error)
                    step.arguments = None
                    continue
                return self._step_failed(run, index, str(error), kind)
            gate = self._gate(run, index, prepared)
            if gate is not None:
                return gate
            self._run_step(run, index, prepared)
            if step.status == "failed" and self.tier(step.tool) != READ:
                return self._stop(run, f"step {index + 1} failed: {step.message}")

    def execute_confirmed(self, run: AutomationRun, prepared: PreparedToolCall) -> Outcome:
        """The user approved this one action (its exact preview): run it and carry on."""
        index = run.current
        run.steps[index].approved, run.steps[index].approved_summary = True, prepared.summary
        if run.cancel_event.is_set():
            return self.cancel(run)
        self._run_step(run, index, prepared)
        if run.steps[index].status == "failed":
            return self._stop(run, f"step {index + 1} failed: {run.steps[index].message}")
        return self.execute(run)

    def _gate(self, run: AutomationRun, index: int, prepared: PreparedToolCall) -> Outcome | None:
        """Whether this exact action may run now under the existing policy and the approval given."""
        step = run.steps[index]
        if not prepared.always_confirm:
            return None
        tier = self.tier(step.tool)
        if step.approved and (tier != CONSEQUENTIAL or step.approved_summary == prepared.summary):
            return None
        if tier == CONSEQUENTIAL or step.approved:
            # Its exact form was not shown (or changed): the usual confirmation with the full preview.
            run.status = AWAITING_APPROVAL
            self._event(run, AWAITING_APPROVAL, f"Waiting for confirmation: {prepared.summary}")
            return Outcome("confirm_step", run, prepared=prepared)
        # A new or changed small step: the remaining plan is shown again for approval.
        run.status = AWAITING_APPROVAL
        indexes = self.needs_approval(run)
        return Outcome("approve_plan", run, text=self.plan_text(run, indexes))

    def _run_step(self, run: AutomationRun, index: int, prepared: PreparedToolCall) -> None:
        step = run.steps[index]
        step.summary = prepared.summary
        step.changes = self.tier(prepared.name) != READ
        position = f"Step {index + 1} of {len(run.steps)}"
        while True:
            step.attempts += 1
            self._event(run, EXECUTING, f"{position}: {prepared.summary}", tool=prepared.name)
            outcome = self.registry.execute(prepared)
            run.executed += 1
            if outcome.ok:
                break
            kind = classify(outcome.message, outcome.data)
            if kind == "transient" and step.attempts <= self.limits.max_retries and not run.cancel_event.is_set():
                continue
            step.status, step.message = "failed", outcome.message
            run.failures.append({"step": index + 1, "kind": kind, "message": outcome.message})
            run.activities.append({"tool": prepared.name, "status": "failed", "summary": outcome.message})
            return
        step.data, step.message = outcome.data, outcome.message
        status, note = verify(self.registry, prepared, outcome)
        step.status, step.verification = status, note
        run.activities.append({"tool": prepared.name, "status": "success" if status != "failed" else "failed",
                               "summary": outcome.message + (f" ({note})" if note else "")})

    def _needs_decision(self, run: AutomationRun, index: int) -> bool:
        step = run.steps[index]
        if step.arguments is None:
            return True
        if step.decided:
            return False
        for dependency in step.depends_on:
            earlier = run.steps[dependency]
            if earlier.status not in ("success", "uncertain") or _empty(earlier.data):
                return True
        previous = [s for s in run.steps[:index] if s.status in ("success", "uncertain", "failed")]
        return bool(previous) and (previous[-1].status == "failed" or _empty(previous[-1].data))

    def _decide(self, run: AutomationRun, index: int) -> Outcome | None:
        """Ask the model for the next action given the results so far, and apply it."""
        try:
            raw = self.ask_json(self._decision_messages(run, index), DECISION_SCHEMA)
        except Exception:
            return self._stop(run, "the local model did not answer while deciding the next step")
        action = raw.get("action")
        if action == "replan" and any(isinstance(s, dict) and s.get("tool") in ("finish", "stop", "ask")
                                      for s in raw.get("steps") or []):
            action = "finish"  # the model wrote the end of the run as if it were a step
        at_end = index >= len(run.steps)
        if at_end and action == "ask" and not any(step.status == "failed" for step in run.steps):
            action = "finish"  # the objective is done: optional further work is not a question to stop on
        if at_end and action in ("run", "replan") and run.replans >= self.limits.max_replans:
            return self._stop(run, f"the plan had to change more than {self.limits.max_replans} times")
        if action == "ask":
            run.question = _text(raw.get("question"), 300) or "How should I continue?"
            run.status = PAUSED
            self._event(run, PAUSED, "Needs your decision")
            return self._finish(run)
        if action in ("finish", "stop"):
            for step in run.steps:
                if step.status == "pending":
                    step.status = "skipped"
            run.answer = _text(raw.get("summary"), 1500)
            if action == "stop":
                return self._stop(run, run.answer or "continuing would not be right")
            return self._conclude(run, decided=True)
        if action == "replan":
            if run.replans >= self.limits.max_replans:
                return self._stop(run, f"the plan had to change more than {self.limits.max_replans} times")
            try:
                new = self._steps(raw.get("steps"), offset=index)
            except PlanError as error:
                return self._stop(run, f"the revised plan was not usable ({error})")
            if not new:
                return self._conclude(run, decided=True)
            run.replans += 1
            run.steps = run.steps[:index] + new
            self._event(run, EXECUTING, "Plan changed: " + "; ".join(s.purpose for s in new))
            return None  # the loop fills in any step that still needs arguments
        if action != "run":
            return self._stop(run, "the local model gave an unusable next step")
        planned = run.steps[index] if not at_end else None
        tool = raw.get("tool") or (planned.tool if planned else None)
        try:
            [step] = self._steps([{"tool": tool, "arguments": raw.get("arguments") or {},
                                   "purpose": raw.get("purpose") or (planned.purpose if planned else tool),
                                   "depends_on": planned.depends_on if planned else list(range(index))}], offset=index)
        except PlanError as error:
            return self._stop(run, f"the next step was not usable ({error})")
        if step.arguments is None:
            target = planned or step
            if target.attempts == 0 and not target.message.startswith("Needs:"):
                # One chance to correct it: the model is told exactly what was wrong.
                target.message = f"Needs: {step.message or 'complete arguments'}"
                if planned is None:
                    run.steps.append(target)
                return self._decide(run, index)
            return self._stop(run, f"the next step ({tool}) is missing information ({step.message or 'arguments'})")
        step.decided = True
        if planned is None:
            run.replans += 1  # an action beyond the plan the user saw: approval is asked again if needed
            run.steps.append(step)
            self._event(run, EXECUTING, f"Next: {step.purpose}")
            return None
        if tool != planned.tool:
            if run.replans >= self.limits.max_replans:
                return self._stop(run, f"the plan had to change more than {self.limits.max_replans} times")
            run.replans += 1  # a different action: earlier approval of this step does not carry over
        else:
            step.approved, step.approved_summary = planned.approved, planned.approved_summary
        run.steps[index] = step
        return None

    def _decision_messages(self, run: AutomationRun, index: int) -> list[dict]:
        state = {"objective": run.objective, "steps": []}
        if index >= len(run.steps):
            state["all_planned_steps_done"] = ("If the objective still needs an action the results now make possible "
                                               "(for example opening the project or file you identified), choose run; "
                                               "otherwise finish with a summary answering the user from the results.")
        else:
            state["next_step"] = index + 1
        for i, step in enumerate(run.steps):
            entry = {"step": i + 1, "tool": step.tool, "purpose": step.purpose, "status": step.status}
            if step.arguments is not None:
                entry["arguments"] = step.arguments
            if step.status in ("success", "uncertain", "failed"):
                entry["result"] = _compact(step.message, step.data)
            elif step.message:
                entry["problem"] = step.message
            state["steps"].append(entry)
        return [{"role": "system", "content": f"{DECIDER_PROMPT}\n\n{self.context()}\n\nTools:\n{self.catalog()}"},
                {"role": "user", "content": json.dumps(state, ensure_ascii=False)}]

    def _step_failed(self, run: AutomationRun, index: int, message: str, kind: str) -> Outcome:
        step = run.steps[index]
        step.status, step.message = "failed", message
        run.failures.append({"step": index + 1, "kind": kind, "message": message})
        if kind == "ambiguous":
            run.question = message
            run.status = PAUSED
            return self._finish(run)
        return self._stop(run, f"step {index + 1} could not run: {message}")

    def _stop(self, run: AutomationRun, reason: str) -> Outcome:
        for step in run.steps:
            if step.status == "pending":
                step.status = "skipped"
        run.status = FAILED
        run.answer = (run.answer + " " if run.answer and reason not in run.answer else "") + f"Stopped: {reason}."
        self._event(run, FAILED, f"Automation stopped: {reason}")
        return self._finish(run)

    def _conclude(self, run: AutomationRun, decided: bool = False) -> Outcome:
        """The planned steps are done. The model may add what the results now make possible (open the
        project it identified, say) within the same limits, or finish with an answer from the results."""
        if not decided and any(step.status in ("success", "uncertain") for step in run.steps):
            if run.executed >= self.limits.max_steps:
                return self._stop(run, f"it reached the limit of {self.limits.max_steps} steps")
            decision = self._decide(run, len(run.steps))
            if isinstance(decision, Outcome):
                return decision
            if any(step.status == "pending" for step in run.steps):
                return self.execute(run)
        run.status = COMPLETED
        self._event(run, COMPLETED, "Automation finished")
        return self._finish(run)

    def _finish(self, run: AutomationRun) -> Outcome:
        run.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        run.current = None
        kind = "question" if run.status == PAUSED else "done"
        return Outcome(kind, run, text=report(run))

    def _event(self, run: AutomationRun, status: str, text: str, tool: str | None = None) -> None:
        self.notify({"status": "automation", "tool": tool or AUTOMATION_TOOL, "summary": text,
                     "run": {"run_id": run.run_id, "status": run.status}})


# --- Verification ---------------------------------------------------------------------------

def verify(registry, prepared: PreparedToolCall, outcome: ToolOutcome) -> tuple[str, str]:
    """(success | uncertain | failed, note): checked with a separate read where one exists."""
    name, data = prepared.name, outcome.data if isinstance(outcome.data, dict) else {}
    definition = registry._definitions.get(name)
    if definition is not None and not definition.always_confirm and not definition.direct_write:
        return "success", ""  # a read: its result is the evidence
    try:
        check = _VERIFIERS.get(name)
        if check is None:
            if name in _SELF_CHECKING:
                return "success", "read back by the tool"
            return "uncertain", "the tool reported success; there is no separate check for it"
        return check(registry, prepared, data)
    except Exception:
        return "uncertain", "the check after it could not be completed"


def _account_provider(registry, prepared):
    return registry.google.check(prepared.arguments.get("account") or registry.google.default)


def _verify_task(registry, prepared, data):
    import storage
    found = any(task["id"] == data.get("id") for task in storage.list_tasks(registry.database_path))
    return ("success", "found in tasks") if found else ("failed", "not found in tasks afterwards")


def _verify_note(registry, prepared, data):
    import storage
    storage.get_note(data["note_id"], registry.database_path)
    return "success", "found in notes"


def _verify_reminder(registry, prepared, data):
    item = registry.reminders.get(data["reminder_id"])
    return ("success", "read back from Reminders") if item and not item.get("completed") else ("failed", "not in Reminders afterwards")


def _verify_timer(registry, prepared, data):
    active = {timer["timer_id"] for timer in registry.timers.active()}
    return ("success", "running") if data.get("timer_id") in active else ("uncertain", "not among running timers")


def _verify_draft(registry, prepared, data):
    drafts = _account_provider(registry, prepared).list_drafts(10)["drafts"]
    found = any(draft["draft_id"] == data.get("draft_id") for draft in drafts)
    return ("success", "found in Gmail drafts") if found else ("uncertain", "not among the newest drafts")


def _verify_event(registry, prepared, data):
    provider = _account_provider(registry, prepared)
    if prepared.arguments.get("operation") == "delete":
        try:
            provider.read_event(data.get("event_id") or prepared.arguments.get("event_id"))
        except Exception:
            return "success", "no longer in the calendar"
        return "failed", "still in the calendar"
    provider.read_event(data.get("event_id"))
    return "success", "read back from Google Calendar"


def _verify_opened(registry, prepared, data):
    app = data.get("application") or data.get("name") or ("Finder" if "Finder" in prepared.summary else None)
    if app is None or registry.mac is None:
        return "uncertain", "macOS accepted the request; what is on screen cannot be checked"
    if registry.mac.is_running(app):
        return "success", f"{app} is running (the window itself is not checked)"
    return "uncertain", f"{app} is not running yet"


def _verify_ui(registry, prepared, data):
    verified = str(data.get("verified") or "")
    if verified in ("focused", "text set and read back"):
        return "success", verified
    return "uncertain", verified or "the result cannot be read back"


_VERIFIERS = {
    "create_task": _verify_task, "capture_note": _verify_note, "create_reminder": _verify_reminder,
    "start_timer": _verify_timer, "create_gmail_draft": _verify_draft,
    "create_calendar_event": _verify_event, "update_calendar_event": _verify_event, "delete_calendar_event": _verify_event,
    "open_file": _verify_opened, "open_in_editor": _verify_opened, "open_application": _verify_opened,
    "ui_action": _verify_ui,
}
# Tools whose own result is already a read-back of the change.
_SELF_CHECKING = frozenset({"complete_reminder", "reschedule_reminder", "set_volume", "set_muted", "media_control",
                            "play_music", "cancel_timer", "set_default_google_account", "mark_email_read",
                            "mark_email_unread", "star_email", "unstar_email", "send_email", "reply_to_email",
                            "forward_email", "send_gmail_draft", "run_project_tests", "set_task_completed",
                            "set_task_due_date"})


# --- Helpers ----------------------------------------------------------------------------------

def classify(message: str, data) -> str:
    """transient, permission, missing, ambiguous, unsupported, invalid or failed."""
    kind = data.get("error") if isinstance(data, dict) else None
    text = (message or "").casefold()
    if kind in ("denied", "permission", "labels_not_granted", "not_granted", "not_allowed") or any(
            word in text for word in ("permission", "privacy & security", "not allowed", "cannot interact")):
        return "permission"  # checked first: permission messages often end with "then try again"
    if kind in ("timeout", "unavailable") or "try again" in text or "did not respond" in text:
        return "transient"
    if any(word in text for word in ("several", "which one", "ask which", "ask the user which")):
        return "ambiguous"
    if any(word in text for word in ("not available", "not supported", "not configured", "no tool")):
        return "unsupported"
    if any(word in text for word in ("not found", "no such", "does not exist", "find the", "first;")):
        return "missing"
    if any(word in text for word in ("must", "invalid", "format", "required", "is not a")):
        return "invalid"
    return "failed"


def report(run: AutomationRun) -> str:
    """What happened, from the step records only (never from the model's account of it)."""
    if run.status == PAUSED and run.question:
        done = [s for s in run.steps if s.status in ("success", "uncertain")]
        prefix = ("So far: " + "; ".join(s.summary or s.purpose for s in done) + ". ") if done else ""
        return f"{prefix}{run.question}"
    if run.status == FAILED and not run.steps:
        parts = [run.answer] if run.answer else ([] if run.unsupported else ["I could not plan that."])
        if run.unsupported:
            parts.append(f"I can't do this: {_clean(run.unsupported)}.")
        return " ".join(parts)
    done = [s for s in run.steps if s.status == "success" and _changes(s)]
    unsure = [s for s in run.steps if s.status == "uncertain"]
    failed = [s for s in run.steps if s.status == "failed"]
    left = [s for s in run.steps if s.status in ("skipped", "cancelled")]
    parts = []
    if run.answer:
        parts.append(run.answer)
    found = findings(run)
    if found and not (run.answer and found[0][0] in run.answer):
        parts.append("Found: " + "; ".join(text for _, text in found) + ".")
    if done:
        parts.append("Done: " + "; ".join(_clean(s.summary or s.purpose) for s in done) + ".")
    if unsure:
        parts.append("Not confirmed: " + "; ".join(f"{s.summary or s.purpose} ({s.verification})" for s in unsure) + ".")
    if failed:
        parts.append("Failed: " + "; ".join(f"{s.summary or s.purpose} ({s.message})" for s in failed) + ".")
    if left:
        verb = "Cancelled, not done" if run.status == CANCELLED else "Not done"
        parts.append(f"{verb}: " + "; ".join(_clean(s.purpose) for s in left) + ".")
    if run.unsupported:
        parts.append(f"I can't do this part: {_clean(run.unsupported)}.")
    if run.status == FAILED and run.answer is None:
        parts.append("The automation stopped.")
    if not parts:
        parts.append("Nothing needed doing.")
    return " ".join(parts)


def findings(run: AutomationRun, limit: int = 6) -> list[tuple[str, str]]:
    """Concrete items the read steps returned ((name, description) pairs): event titles and times,
    email subjects, note titles, file paths, tasks. Stated from the results, never from the model."""
    items = []
    for step in run.steps:
        if step.status != "success" or step.changes or not isinstance(step.data, dict):
            continue
        data = step.data
        groups = [data] + [group for group in data.get("accounts") or [] if isinstance(group, dict)]
        for group in groups:
            for event in group.get("events") or []:
                when = str(event.get("start") or "").replace("T", " ")[:16]
                items.append((str(event.get("title")), f"{event.get('title')} ({when})"))
            for message in group.get("messages") or []:
                items.append((str(message.get("subject")), f"\"{message.get('subject')}\" from {message.get('from')}"))
        for note in data.get("notes") or []:
            items.append((str(note.get("title")), f"note \"{note.get('title')}\""))
        for result in data.get("results") or []:
            if isinstance(result, dict) and result.get("path"):
                items.append((str(result["path"]), str(result.get("location") or result["path"])))
        for task in data.get("tasks") or []:
            if isinstance(task, dict) and not task.get("completed"):
                items.append((str(task.get("title")), f"task \"{task.get('title')}\""))
    return items[:limit]


def _clean(text: str) -> str:
    return (text or "").rstrip(" .")


def progress(run: AutomationRun) -> str:
    """Steps already run, for an approval asked part-way through."""
    ran = [s for s in run.steps if s.status in ("success", "uncertain", "failed")]
    if not ran:
        return ""
    return "So far: " + "; ".join(f"{_clean(s.summary or s.purpose)} ({s.status})" for s in ran) + "."


def _changes(step: Step) -> bool:
    return step.status == "success" and step.changes


def _empty(data) -> bool:
    if not isinstance(data, dict):
        return False
    for key in EMPTY_KEYS:
        if key in data and isinstance(data[key], list):
            return not data[key]
    return False


def _compact(message: str, data) -> dict:
    text = json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""
    if len(text) > MAX_RESULT_CHARS_FOR_MODEL:
        text = text[:MAX_RESULT_CHARS_FOR_MODEL] + "…(truncated)"
    return {"message": message, "data": text}


def _first_sentence(text: str, limit: int = 140) -> str:
    """A tool's description cut to its first sentence, to keep planning prompts short."""
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence[:limit] + ("…" if len(sentence) > limit else "")


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None
