"""v1.1 assistant intelligence: working context, priorities and briefings, intent modes,
propose-then-do and resume, task/note links, project awareness, the partial-match guard, the
Focus panel and draft editing. Fakes and temporary databases only; no model, Google or Mac.
"""
import base64
import dataclasses
import io
import json
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import automation
import briefing
import config
import google_workspace
import priorities
import storage
import web_app
from developer import DeveloperCompanion, DeveloperSettings
from google_accounts import GoogleAccounts
from google_workspace import GoogleError
from reminders import AppleReminders
from timers import TimerService
from tool_registry import ToolError, ToolOutcome, ToolRegistry
from working_context import WorkingContext

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automation import Planner, plan, step  # noqa: E402
from test_google_accounts import FakeProvider  # noqa: E402
from test_google_workspace import ALL_SCOPES, ProviderTestCase  # noqa: E402
from test_planning import FakeOsascript, record  # noqa: E402

NOW = datetime.now().astimezone()


def at(minutes):
    return (NOW + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


def timed_event(title, minutes, event_id, all_day=False):
    start = at(minutes)
    return {"event_id": event_id, "title": title, "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
            "all_day": all_day}


class MailProvider(FakeProvider):
    def __init__(self, email, events=(), messages=(), **kwargs):
        super().__init__(email, events, **kwargs)
        self.messages = list(messages)

    def search_messages(self, query, limit):
        self._call("search_messages")
        return {"messages": list(self.messages)[:limit], "more_available": False}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "i.sqlite3"
        storage.initialize_database(self.database)

    def tearDown(self):
        self.temp.cleanup()


# --- Working context -----------------------------------------------------------------------

class WorkingContextTests(Case):
    def test_tool_results_become_dated_items_and_the_newest_comes_first(self):
        context = WorkingContext()
        context.new_turn("Find the SOP email")
        context.observe("search_gmail", {}, True, "Found 1 message(s).",
                        {"messages": [{"id": "m1", "subject": "SOP", "from": "Prof X"}], "account": "personal"})
        context.new_turn("Draft a reply")
        context.observe("create_gmail_draft", {"to": ["prof@x.edu"], "subject": "Re: SOP", "account": "personal"}, True,
                        "Gmail draft created.", {"draft_id": "d1", "account": "personal"})
        note = context.render()
        self.assertIn("draft d1 [personal]: to prof@x.edu: \"Re: SOP\", created (not sent)", note)
        self.assertLess(note.index("draft d1"), note.index("email m1"))  # newest first
        self.assertIn("If a reference could mean more than one listed item, ask", note)
        self.assertIn("Read live items again before changing", note)

    def test_two_recent_drafts_are_both_shown_so_it_can_be_asked_about(self):
        context = WorkingContext()
        for draft in ("d1", "d2"):
            context.new_turn("draft")
            context.observe("create_gmail_draft", {"to": ["a@x.org"], "subject": draft}, True, "ok", {"draft_id": draft})
        note = context.render()
        self.assertIn("draft d1", note)
        self.assertIn("draft d2", note)

    def test_decisions_projects_and_unfinished_objectives_are_kept(self):
        context = WorkingContext(project_names=lambda: ["Project-U"])
        context.new_turn("Let's work on Project U")
        self.assertEqual(context.project["name"], "Project-U")
        self.assertFalse(context.project["verified"])  # named, not yet checked
        context.observe("git_status", {"project": "Project-U"}, True, "clean", {"project": "Project-U"})
        self.assertTrue(context.project["verified"])
        context.pending("send_email", {}, "Send email to prof@x.edu: Re: SOP")
        context.resolved("send_email", "Send email to prof@x.edu: Re: SOP", approved=False)
        context.automation({"objective": "Prepare lab", "status": "CANCELLED", "steps": [
            {"purpose": "Open project", "status": "success"}, {"purpose": "Start focus", "status": "cancelled"}]}, "")
        note = context.render()
        self.assertIn("Decision: You declined: Send email to prof@x.edu: Re: SOP", note)
        self.assertIn("Current objective: Prepare lab (cancelled", note)
        self.assertIn("not done: Start focus", note)
        self.assertNotIn("waiting for your approval", note)

    def test_stale_items_expire_and_an_explicit_change_of_subject_clears(self):
        clock = Mock(return_value=NOW)
        context = WorkingContext(clock=clock)
        context.new_turn("x")
        context.observe("create_task", {}, True, "Task created.", {"id": 1, "title": "Order servos"})
        clock.return_value = NOW + timedelta(hours=3)
        context.new_turn("y")
        self.assertNotIn("Order servos", context.render() or "")
        context.observe("create_task", {}, True, "Task created.", {"id": 2, "title": "Buy wire"})
        context.new_turn("New topic: what's the weather like?")
        self.assertIsNone(context.render())

    def test_the_runtime_gives_the_model_the_note_but_never_stores_it(self):
        storage.create_task("Order servos", database_path=self.database)
        model = Mock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "create_task", "arguments": {"title": "Buy wire"}}}]},
            {"role": "assistant", "content": "Added."},
            {"role": "assistant", "content": "It is task 2."},
        ])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database, model_client=model,
                                                     tool_registry=ToolRegistry(self.database), planner_client=Planner())
        runtime.run_turn("Add buy wire to my tasks")
        runtime.run_turn("Which task was that?")
        last_request = model.call_args_list[-1].args[2]
        notes = [m["content"] for m in last_request if m["role"] == "system" and "Working context" in m["content"]]
        self.assertTrue(notes and "task 2" in notes[0] and "\"Buy wire\"" in notes[0])
        self.assertNotIn("Working context", json.dumps(runtime.history))


# --- Draft editing (continuity: "make it shorter") --------------------------------------------

def raw_message(to, subject, body, in_reply_to=None):
    headers = [{"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    return {"id": "d1", "message": {"threadId": "t1", "payload": {"mimeType": "text/plain", "headers": headers,
                                                                   "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}}


class DraftUpdateTests(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.token_endpoint()
        self.current = raw_message("prof@x.edu", "Re: SOP", "Dear Professor, I will meet you on Monday at ten as agreed.", "<m1@x>")
        self.puts = []
        self.google.on("GET", f"{google_workspace.GMAIL_API}/drafts/d1", lambda request: self.current)
        self.google.on("PUT", f"{google_workspace.GMAIL_API}/drafts/d1",
                       lambda request: (self.puts.append(json.loads(request.data.decode())), {"id": "d1"})[1])

    def test_an_edit_changes_only_the_given_fields_after_approval_and_keeps_the_thread(self):
        provider = self.connected(domain=None)
        prepared = provider.prepare_draft_update("d1", body="Dear Professor, see you Monday.")
        self.assertEqual(prepared["before"]["body"], "Dear Professor, I will meet you on Monday at ten as agreed.")
        self.assertEqual((prepared["draft"]["to"], prepared["draft"]["subject"]), (["prof@x.edu"], "Re: SOP"))
        self.assertEqual(self.puts, [])  # nothing changes before approval
        provider.update_draft(prepared)
        sent = self.puts[0]
        raw = base64.urlsafe_b64decode(sent["message"]["raw"]).decode()
        self.assertIn("see you Monday", raw)
        self.assertIn("In-Reply-To: <m1@x>", raw)
        self.assertEqual(sent["message"]["threadId"], "t1")

    def test_a_draft_changed_in_gmail_meanwhile_is_not_overwritten(self):
        provider = self.connected(domain=None)
        prepared = provider.prepare_draft_update("d1", body="Shorter.")
        self.current = raw_message("prof@x.edu", "Re: SOP", "Edited in Gmail by hand.")
        with self.assertRaises(GoogleError) as raised:
            provider.update_draft(prepared)
        self.assertEqual(raised.exception.kind, "stale")
        self.assertEqual(self.puts, [])

    def test_the_tool_needs_a_draft_from_this_conversation_and_always_asks(self):
        registry = ToolRegistry(Path(self.temp_dir.name) / "r.sqlite3", google=self.connected(domain=None))
        with self.assertRaisesRegex(ToolError, "only drafts found in this conversation"):
            registry.prepare("update_gmail_draft", {"draft_id": "d1", "body": "Shorter."}, "Make it shorter")
        registry._seen_draft_ids.add((registry.google.default, "d1"))
        prepared = registry.prepare("update_gmail_draft", {"draft_id": "d1", "body": "Shorter."}, "Make it shorter")
        self.assertTrue(prepared.always_confirm)
        self.assertEqual([s["heading"] for s in prepared.preview["sections"]], ["Draft now", "After the change"])
        self.assertIn("update_gmail_draft", google_workspace.CONSEQUENTIAL_OPERATIONS)

    def test_draft_then_edit_in_place_then_a_declined_send_leaves_one_unsent_draft(self):
        # The whole conversation through the runtime and the real Gmail provider (fake HTTP).
        created, sends = [], []

        def create(request):
            raw = json.loads(request.data.decode())["message"]["raw"]
            created.append(base64.urlsafe_b64decode(raw).decode())
            self.current = raw_message("prof@x.edu", "Re: SOP", "Dear Professor, I will meet you on Monday at ten as agreed.")
            return {"id": "d1", "message": {"threadId": "t1"}}

        def put(request):
            self.puts.append(json.loads(request.data.decode()))
            self.current = raw_message("prof@x.edu", "Re: SOP", "Dear Professor, see you Monday at ten.")
            return {"id": "d1"}

        self.google.on("POST", f"{google_workspace.GMAIL_API}/drafts", create)
        self.google.on("POST", f"{google_workspace.GMAIL_API}/drafts/send", lambda request: sends.append(request) or {})
        self.google.on("POST", f"{google_workspace.GMAIL_API}/messages/send", lambda request: sends.append(request) or {})
        self.google.on("PUT", f"{google_workspace.GMAIL_API}/drafts/d1", put)
        call = lambda name, **arguments: {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c-{name}", "function": {"name": name, "arguments": arguments}}]}
        model = Mock(side_effect=[
            call("create_gmail_draft", to="prof@x.edu", subject="Re: SOP",
                 body="Dear Professor, I will meet you on Monday at ten as agreed."),
            {"role": "assistant", "content": "The draft is saved."},
            call("update_gmail_draft", draft_id="d1", body="Dear Professor, see you Monday at ten."),
            {"role": "assistant", "content": "I shortened the draft."},
            call("send_gmail_draft", draft_id="d1"),
            {"role": "assistant", "content": "Not sent."},
        ])
        database = Path(self.temp_dir.name) / "r.sqlite3"
        runtime = assistant_runtime.AssistantRuntime(
            "http://local", "m", database_path=database, model_client=model,
            tool_registry=ToolRegistry(database, google=self.connected(domain=None)))

        first = runtime.run_turn("Draft a reply to the professor saying I'll meet him Monday at ten.")
        self.assertEqual(first.pending_confirmation["tool"], "create_gmail_draft")
        runtime.resolve_confirmation(first.pending_confirmation["confirmation_id"], True)

        edit = runtime.run_turn("Make it shorter.")
        pending = edit.pending_confirmation
        self.assertEqual(pending["tool"], "update_gmail_draft")
        sections = {section["heading"]: dict(section["rows"]) for section in pending["preview"]["sections"]}
        self.assertEqual(sections["Draft now"]["Body"], "Dear Professor, I will meet you on Monday at ten as agreed.")
        self.assertEqual(sections["After the change"]["Body"], "Dear Professor, see you Monday at ten.")
        self.assertEqual(self.puts, [])  # nothing changes before approval
        runtime.resolve_confirmation(pending["confirmation_id"], True)

        send = runtime.run_turn("Send it.")
        self.assertEqual(send.pending_confirmation["tool"], "send_gmail_draft")
        self.assertIn("see you Monday at ten", json.dumps(send.pending_confirmation["preview"]))  # the edited text
        runtime.resolve_confirmation(send.pending_confirmation["confirmation_id"], False)

        self.assertEqual(len(created), 1)  # one draft, edited in place
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(sends, [])  # declining left it unsent
        self.assertIn("You declined", runtime.context.render())


# --- Priorities and briefings ------------------------------------------------------------------

class PriorityTests(Case):
    def briefing(self, events=(), messages=(), reminders_open=(), reminders_fail=None, context=None, window="day", now=NOW):
        accounts = GoogleAccounts.single(MailProvider("me@x.org", events, messages))
        osascript = FakeOsascript()
        osascript.open = list(reminders_open)
        osascript.fail = reminders_fail
        return briefing.build_briefing(now, "today", database_path=self.database, google=accounts,
                                       reminders=AppleReminders(runner=osascript), timers=TimerService(self.database),
                                       include_email=True, context=context, window=window)

    def test_a_realistic_day_is_ordered_with_reasons_and_no_invented_urgency(self):
        yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        storage.create_task("Submit lab report", due_at=storage.normalize_due_at(f"{yesterday} 18:00"), database_path=self.database)
        storage.create_task("Read paper", database_path=self.database)  # no due date: not urgent
        later = at(90)
        storage.create_task("Email TA", due_at=storage.normalize_due_at(later.strftime("%Y-%m-%d %H:%M")) if later.date() == NOW.date() else None,
                            database_path=self.database)
        events = [timed_event("Electronics lab", 120, "labevt01"), timed_event("Holiday", 0, "holevt01", all_day=True)]
        messages = [{"id": "e1", "subject": "SOP feedback", "from": "Prof X", "unread": True, "important": True, "starred": False},
                    {"id": "e2", "subject": "Newsletter", "from": "News", "unread": True, "important": False, "starred": False}]
        result = self.briefing(events=events, messages=messages)
        focus = result["focus"]
        self.assertEqual(focus["next_commitment"]["title"], "Electronics lab")
        self.assertEqual(focus["all_day"], ["Holiday"])
        titles = [item["title"] for item in focus["needs_attention"]]
        self.assertEqual(titles[0], "Submit lab report")
        self.assertIn("overdue (was due", focus["needs_attention"][0]["why"])
        self.assertIn("Email: SOP feedback", titles)
        self.assertNotIn("Email: Newsletter", titles)  # not flagged by Gmail: not called important
        self.assertNotIn("Read paper", titles)
        self.assertEqual(focus["suggestion"]["text"], "Deal with Submit lab report")
        self.assertIn("nothing is done unless you ask", focus["suggestion"]["note"])

    def test_the_same_item_is_listed_once(self):
        today = NOW.strftime("%Y-%m-%d")
        storage.create_task("Pay fees", due_at=today, database_path=self.database)
        result = self.briefing(reminders_open=[record(title="Pay fees", due=f"{today}T00:00:00", all_day=f"{today}T00:00:00")])
        self.assertEqual([i["title"] for i in result["focus"]["needs_attention"]].count("Pay fees"), 1)

    def test_an_imminent_commitment_comes_first_and_before_next_narrows(self):
        storage.create_task("Read paper", database_path=self.database)
        result = self.briefing(events=[timed_event("Viva", 20, "vivaevt1")])
        self.assertEqual(result["focus"]["suggestion"]["text"], "Get ready for Viva")
        self.assertRegex(result["focus"]["suggestion"]["reason"], r"in (19|20) minutes")
        self.assertEqual(result["focus"]["needs_attention"], [])

    def test_unavailable_sources_are_named_and_the_rest_still_works(self):
        storage.create_task("Overdue thing", due_at=(NOW - timedelta(days=2)).strftime("%Y-%m-%d"), database_path=self.database)
        result = self.briefing(reminders_fail="(-1743)")
        self.assertTrue(any(u.startswith("reminders:") for u in result["focus"]["unavailable"]))
        self.assertEqual(result["focus"]["needs_attention"][0]["title"], "Overdue thing")

    def test_an_unreadable_calendar_is_never_reported_as_an_empty_day(self):
        result = briefing.build_briefing(NOW, "today", database_path=self.database,
                                         google=GoogleAccounts.single(FakeProvider("me@x.org", fail="offline")),
                                         timers=TimerService(self.database))
        self.assertFalse(result["focus"]["calendar_checked"])
        text = priorities.summary_text(result["focus"])
        self.assertIn("unknown, because the calendar could not be read", text)
        self.assertNotIn("none found", text)
        self.assertFalse(priorities.dashboard(result, None, None)["calendar_checked"])

    def test_unfinished_work_from_the_conversation_is_surfaced(self):
        context = {"objective": {"text": "Prepare lab", "status": "CANCELLED", "remaining": ["Start focus"]}}
        focus = self.briefing(context=context)["focus"]
        self.assertEqual(focus["needs_attention"][0]["title"], "Unfinished: Prepare lab")
        self.assertEqual(focus["suggestion"]["text"], "Continue Unfinished: Prepare lab")

    def test_with_nothing_due_an_undated_task_is_offered_as_having_no_deadline(self):
        storage.create_task("Read paper", database_path=self.database)
        focus = self.briefing()["focus"]
        self.assertEqual(focus["needs_attention"], [])
        self.assertIn("no deadline", focus["suggestion"]["reason"])

    def test_the_focus_is_stated_as_text_and_names_the_calendar_it_covered(self):
        from google_workspace import GoogleSettings
        storage.create_task("Overdue thing", due_at=(NOW - timedelta(days=2)).strftime("%Y-%m-%d"), database_path=self.database)
        path = Path(self.temp.name) / "accounts.json"
        path.write_text(json.dumps({"version": 1, "default": "personal", "accounts": [
            {"name": "personal", "item": "oauth", "email": "me@x.org"}, {"name": "BITS", "item": "oauth:bits", "email": "d@bits.edu"}]}))
        providers = {"oauth": MailProvider("me@x.org"), "oauth:bits": MailProvider("d@bits.edu", [timed_event("Lecture", 60, "lecevt01")])}
        accounts = GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x")), registry_path=path,
                                  provider_factory=lambda item: providers[item])
        registry = ToolRegistry(self.database, google=accounts, timers=TimerService(self.database))
        outcome = registry.execute(registry.prepare("get_briefing", {"day": "today"}, "What's important today?"))
        self.assertIn("Needs attention: Overdue thing (overdue (was due", outcome.message)
        self.assertIn("Next commitment: Lecture at", outcome.message)  # from BITS: every calendar is included
        self.assertEqual(list(outcome.data)[0], "focus")  # the focus comes first

    def test_the_model_gets_a_compact_briefing_with_the_focus(self):
        events = [timed_event(f"Event {i}", 60 + i * 10, f"evtx{i:04d}") for i in range(30)]
        compact = briefing.for_model(self.briefing(events=events, messages=[{"id": "e1", "subject": "S", "unread": True}]))
        self.assertIn("focus", compact)
        self.assertEqual(len(compact["sources"]["calendar"]["events"]), briefing.MAX_EVENTS_FOR_MODEL)
        self.assertEqual(compact["sources"]["email"]["unread_recent"], 1)
        self.assertLess(len(json.dumps(compact)), 8000)


# --- Intent modes, propose-then-do, resume -------------------------------------------------------

class ModeTests(Case):
    def runtime(self, planner, model=None):
        registry = ToolRegistry(self.database, timers=TimerService(self.database))
        return assistant_runtime.AssistantRuntime(
            "http://local", "m", database_path=self.database, tool_registry=registry, planner_client=planner,
            model_client=model or Mock(side_effect=AssertionError("not needed")))

    def test_route_modes(self):
        ask = lambda mode, multi: (lambda messages, schema: {"mode": mode, "multi_step": multi})
        message = "Check my tasks and then start a focus session"
        self.assertIsNone(automation.route(message, "", ask("tell", True)))  # information stays informational
        self.assertEqual(automation.route(message, "", ask("help", True)), "help")
        self.assertEqual(automation.route(message, "", ask("do", True)), "do")
        self.assertIsNone(automation.route(message, "", ask("do", False)))  # a single action: ordinary path
        for text in ("do it", "Go ahead.", "OK, continue", "resume", "yes, do it"):
            self.assertTrue(automation.is_resume_reply(text), text)
        for text in ("do it later, not now", "continue the story about dragons", "confirm"):
            self.assertFalse(automation.is_resume_reply(text), text)

    def test_help_mode_proposes_changes_and_do_it_carries_them_out_with_approval(self):
        planner = Planner(plan(step("list_tasks", purpose="Check tasks"),
                               step("start_timer", {"kind": "focus", "minutes": 45}, "Start a 45-minute focus session")),
                          # on "do it" the model re-checks before continuing (earlier results may be stale)
                          {"action": "run", "tool": "start_timer", "arguments": {"kind": "focus", "minutes": 45}},
                          route=True)
        planner.route_mode = "help"
        planner.__call__  # noqa: B018
        original = planner.__call__

        def ask(messages, schema):
            if schema is automation.ROUTE_SCHEMA:
                return {"mode": "help", "multi_step": True}
            return original(messages, schema)

        runtime = self.runtime(ask)
        result = runtime.run_turn("Help me plan my afternoon: check my tasks and then a focus session")
        self.assertIn("I can do this next:\n2. Start a 45-minute focus session", result.response)
        self.assertEqual(runtime.registry.timers.active(), [])  # proposed, not done
        result = runtime.run_turn("Do it")
        self.assertIsNotNone(result.pending_confirmation)  # the change is approved as usual
        self.assertIn("So far: List tasks (success)", result.pending_confirmation["summary"])
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(len(runtime.registry.timers.active()), 1)
        self.assertEqual([s["status"] for s in runtime.automation_status()["steps"]], ["success", "success"])

    def test_an_interrupted_run_resumes_without_redoing_finished_steps(self):
        registry_calls = []
        planner = Planner(
            plan(step("capture_note", {"text": "Order servos"}, "Save it"), step("list_tasks", purpose="Check tasks"),
                 step("start_timer", {"kind": "focus", "minutes": 30}, "Start focus")),
            {"action": "run", "tool": "start_timer", "arguments": {"kind": "focus", "minutes": 30}}, route=True)
        runtime = self.runtime(planner)
        original = runtime.registry.execute

        def execute(prepared):
            registry_calls.append(prepared.name)
            if prepared.name == "list_tasks" and registry_calls.count("list_tasks") == 1:
                runtime.cancel_automation()
            return original(prepared)

        runtime.registry.execute = execute
        result = runtime.run_turn("Remember to order servos, check my tasks and start a focus session")
        if result.pending_confirmation:
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(runtime.automation_status()["status"], "CANCELLED")
        result = runtime.run_turn("continue")
        if result.pending_confirmation:
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(registry_calls.count("capture_note"), 1)  # not redone
        self.assertIn("paused and resumed", json.dumps(planner.requests[-1]))  # the model was told results may be stale
        self.assertEqual(len(runtime.registry.timers.active()), 1)

    def test_a_plain_question_is_answered_not_automated(self):
        for text in ("What needs my attention before my next class?", "Anything urgent?", "What does tomorrow look like?"):
            self.assertTrue(automation.is_plain_question(text), text)
        for text in ("What's on my calendar tomorrow, and get the relevant project open.", "Open GitHub",
                     "Check my calendar and then open the project?"):
            self.assertFalse(automation.is_plain_question(text), text)
        model = Mock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "start_automation", "arguments": {"objective": "What needs my attention?"}}}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "list_tasks", "arguments": {}}}]},
            {"role": "assistant", "content": "Nothing urgent."},
        ])
        planner = Planner()
        runtime = self.runtime(planner, model=model)
        self.assertEqual(runtime.run_turn("What needs my attention before my next class?").response, "Nothing urgent.")
        self.assertEqual(planner.requests, [])  # no plan was made
        self.assertIsNone(runtime.automation_status())
        # Nor is the routing question asked: a question with a clause is still just a question.
        ask = Mock(return_value={"mode": "do", "multi_step": True})
        self.assertIsNone(automation.route("What's due tomorrow, before my lab starts?", "", ask))
        ask.assert_not_called()

    def test_resume_words_with_nothing_to_resume_take_the_ordinary_path(self):
        model = Mock(return_value={"role": "assistant", "content": "Continue with what?"})
        runtime = self.runtime(Planner(route=False), model=model)
        self.assertEqual(runtime.run_turn("continue").response, "Continue with what?")


# --- Loop closure, projects, partial matches --------------------------------------------------

class ProjectTests(Case):
    def setUp(self):
        super().setUp()
        self.home = Path(self.temp.name).resolve()
        for folder in ("Projects/SOLVE", "Projects/SOLVE-old/arm", "Projects/robotics", "Desktop/robotics", "Desktop/kris"):
            (self.home / folder).mkdir(parents=True, exist_ok=True)
        from computer_utilities import UtilitySettings
        self.utilities = UtilitySettings({"Projects": self.home / "Projects", "Desktop": self.home / "Desktop"}, ())
        self.developer = DeveloperCompanion(DeveloperSettings({"Project-U": self.home / "Desktop" / "kris"}, {}, "Visual Studio Code"))

    def registry(self):
        return ToolRegistry(self.database, utilities=self.utilities, developer=self.developer)

    def test_a_task_from_a_note_keeps_a_link_to_it(self):
        registry = self.registry()
        note = storage.create_note("Camera", "Use an eye-in-hand camera", self.database)
        outcome = registry.execute(registry.prepare("create_task", {"title": "Mount the camera", "note_id": note["id"]},
                                                    "Turn that into a task"))
        self.assertEqual(outcome.data["source_note_id"], note["id"])
        self.assertEqual(storage.list_tasks(self.database)[0]["source_note_id"], note["id"])

    def test_configured_projects_are_found_by_name_with_checked_facts_and_name_mentions(self):
        storage.create_task("Fix Project U wake word", database_path=self.database)
        storage.create_task("Buy milk", database_path=self.database)
        registry = self.registry()
        outcome = registry.execute(registry.prepare("project_overview", {"name": "project u"}, "Let's work on Project U"))
        self.assertTrue(outcome.data["configured"])
        self.assertIn("unavailable", outcome.data["git"])  # not a Git repository here: said, not invented
        self.assertEqual([t["title"] for t in outcome.data["mentioning_the_name"]["open_tasks"]], ["Fix Project U wake word"])
        self.assertIn("may be unrelated", outcome.data["how_to_read"])

    def test_unconfigured_projects_use_discovery_and_ask_when_unsure(self):
        registry = self.registry()
        solve = registry.execute(registry.prepare("project_overview", {"name": "SOLVE"}, "Let's work on SOLVE"))
        self.assertEqual(solve.data["project"], "Projects/SOLVE")
        self.assertFalse(solve.data["configured"])
        both = registry.execute(registry.prepare("project_overview", {"name": "robotics"}, "Let's work on robotics"))
        self.assertIn("Several folders are named", both.message)
        self.assertEqual(sorted(c["path"] for c in both.data["candidates"]), ["Desktop/robotics", "Projects/robotics"])
        none = registry.execute(registry.prepare("project_overview", {"name": "Hydra"}, "Let's work on Hydra"))
        self.assertIn("Ask the user where it is", none.message)

    def test_a_merely_similar_folder_is_never_opened_without_asking(self):
        registry = self.registry()
        registry.execute(registry.prepare("find_folders", {"query": "SOLVE old"}, "find SOLVE old"))
        registry.execute(registry.prepare("find_folders", {"query": "arm"}, "find arm"))
        self.assertIn("Projects/SOLVE-old/arm", registry._seen_paths)
        registry._partial_paths.add("Projects/SOLVE-old/arm")
        with self.assertRaisesRegex(ToolError, "partial name match"):
            registry.prepare("open_in_editor", {"path": "Projects/SOLVE-old/arm"}, "Open my robot project")
        self.assertEqual(automation.classify("Only a partial name match was found (x); ask the user whether this is the one", None),
                         "ambiguous")


# --- Focus panel -----------------------------------------------------------------------------

class FocusEndpointTests(Case):
    def test_the_focus_view_is_read_only_compact_and_marks_unavailable_sources(self):
        storage.create_task("Overdue thing", due_at=(NOW - timedelta(days=2)).strftime("%Y-%m-%d"), database_path=self.database)
        osascript = FakeOsascript()
        osascript.fail = "(-1743)"
        app = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion(),
                                 reminders=AppleReminders(runner=osascript),
                                 google=GoogleAccounts.single(MailProvider("me@x.org", [timed_event("Lab", 60, "labevt99")])))
        client = app.test_client()
        view = client.get("/api/focus").get_json()
        self.assertEqual(view["next_commitment"]["title"], "Lab")
        self.assertEqual(view["needs_attention"][0]["title"], "Overdue thing")
        self.assertTrue(any(u.startswith("reminders:") for u in view["unavailable"]))
        self.assertEqual(view["sources"]["reminders"]["status"], "unavailable")
        self.assertIn("as_of", view)
        self.assertEqual(storage.list_tasks(self.database)[0]["completed"], False)  # nothing was changed


# --- Responsiveness ---------------------------------------------------------------------------

class ResponsivenessTests(Case):
    def test_the_warm_up_sends_the_same_fixed_prompt_a_turn_starts_with(self):
        sent = []

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"message": {"role": "assistant", "content": "Hi."}}).encode())

        registry = ToolRegistry(self.database, timers=TimerService(self.database))
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=registry, think=False)
        with patch.object(assistant_runtime.urllib.request, "urlopen", urlopen):
            self.assertTrue(runtime.warm_up())
            runtime.run_turn("Hello")
        warm, turn = sent
        self.assertEqual(warm["options"], {"num_predict": 1})  # one token: only the prompt is processed
        self.assertEqual(warm["messages"][:2], turn["messages"][:2])  # profile and rules
        self.assertEqual(warm["tools"], turn["tools"])
        self.assertEqual((warm["think"], turn["think"]), (False, False))
        self.assertEqual(runtime.history[0], {"role": "user", "content": "Hello"})  # the warm-up left no trace
        self.assertEqual(len(runtime.history), 2)

    def test_only_the_running_server_warms_the_model(self):
        app = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion())
        self.assertFalse(app.config["WARM_MODEL"])
        with patch.object(assistant_runtime.AssistantRuntime, "warm_up") as warm_up:
            app.test_client().post("/api/conversations", json={})
            warm_up.assert_not_called()
            app.config["WARM_MODEL"] = True
            app.test_client().post("/api/conversations", json={})
            for _ in range(50):
                if warm_up.called:
                    break
                time.sleep(0.01)
            warm_up.assert_called_once()

    def test_after_an_automation_the_prompt_is_warmed_again_only_when_asked(self):
        for keep_warm, expected in ((False, 0), (True, 1)):
            planner = Planner(plan(step("list_tasks")), route=True)
            runtime = ModeTests.runtime(self, planner)
            runtime.keep_warm = keep_warm
            with patch.object(runtime, "warm_up") as warm_up:
                runtime.run_turn("Check my tasks and then tell me what is open")
                for _ in range(50):
                    if warm_up.call_count >= expected:
                        break
                    time.sleep(0.01)
                self.assertEqual(warm_up.call_count, expected)

    def test_a_timeout_says_what_happened(self):
        app = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion())
        client = app.test_client()
        conversation = client.post("/api/conversations", json={}).get_json()["conversation_id"]
        with patch.object(assistant_runtime.urllib.request, "urlopen", Mock(side_effect=TimeoutError("timed out"))):
            response = client.post("/api/chat", json={"conversation_id": conversation, "message": "Hello"})
        self.assertEqual(response.status_code, 504)
        self.assertIn("did not answer within 120 seconds", response.get_json()["error"])
        self.assertIn("nothing was changed", response.get_json()["error"])

    def test_think_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            for text, expected in (("", None), ("think = off\n", False), ("think = on\n", True), ("think = maybe\n", None)):
                path.write_text(f"[ollama]\nendpoint = http://x\nmodel = m\n{text}")
                with patch("builtins.print"):
                    self.assertIs(config.load_think_setting(path), expected)



# --- Direct answers (no second model call for a complete read result) ---------------------------

def tool_call(name, arguments=None, call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments or {}}}]}


class DirectAnswerTests(Case):
    def runtime(self, *replies, registry=None, google=None, reminders_fail=None):
        osascript = FakeOsascript()
        osascript.fail = reminders_fail
        registry = registry or ToolRegistry(self.database, google=google, reminders=AppleReminders(runner=osascript),
                                            timers=TimerService(self.database))
        self.model = Mock(side_effect=list(replies))  # one reply per allowed model call; another call fails the test
        return assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database, tool_registry=registry,
                                                  model_client=self.model, planner_client=Planner())

    def test_whats_important_is_answered_from_the_briefing_without_a_second_call(self):
        storage.create_task("Submit lab report", due_at=(NOW - timedelta(days=1)).strftime("%Y-%m-%d"), database_path=self.database)
        google = GoogleAccounts.single(MailProvider("me@x.org", [timed_event("Robotics lab", 90, "labevt01"),
                                                                 timed_event("Control lecture", 240, "lecevt01")]))
        runtime = self.runtime(tool_call("get_briefing", {"day": "today"}), google=google, reminders_fail="(-1743)")
        result = runtime.run_turn("What's important today?")
        self.assertEqual(self.model.call_count, 1)
        text = result.response
        self.assertRegex(text, r"^Next: Robotics lab at \d\d:\d\d (?:tomorrow, )?in 1 hour (?:29|30) minutes\.")
        self.assertIn("Later: ", text)
        self.assertIn("Control lecture", text)
        self.assertIn("- Submit lab report: overdue (was due", text)
        self.assertIn("Suggestion: Deal with Submit lab report, because overdue", text)
        self.assertIn("Not available: reminders:", text)  # the unreadable source is still named
        self.assertNotIn("Answer from this focus", text)  # no model instructions or raw data
        self.assertNotIn("{", text)
        self.assertEqual([a["tool"] for a in result.activities], ["get_briefing"])
        # History stays complete: the call, its result and the reply, so a follow-up can use them.
        self.assertEqual([m["role"] for m in runtime.history], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(runtime.history[-1]["content"], text)

    def test_the_calendar_coverage_note_and_an_unreadable_calendar_are_kept(self):
        from google_workspace import GoogleSettings
        path = Path(self.temp.name) / "accounts.json"
        path.write_text(json.dumps({"version": 1, "default": "personal", "accounts": [
            {"name": "personal", "item": "oauth", "email": "me@x.org"}, {"name": "BITS", "item": "oauth:bits", "email": "d@bits.edu"}]}))
        providers = {"oauth": MailProvider("me@x.org"), "oauth:bits": MailProvider("d@bits.edu", [timed_event("Lecture", 60, "l1")])}
        accounts = GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x")), registry_path=path,
                                  provider_factory=lambda item: providers[item])
        text = self.runtime(tool_call("get_briefing"), google=accounts).run_turn("What's important today?").response
        self.assertRegex(text, r"^Next: Lecture \(BITS\) at \d\d:\d\d,? (?:tomorrow, )?in (?:59 minutes|1 hour)\.")  # every calendar
        self.assertNotIn("Only the", text)
        text = self.runtime(tool_call("get_briefing"), google=accounts).run_turn("What's on my personal calendar today?").response
        self.assertIn("Nothing else is scheduled today.", text)
        self.assertIn("Only the personal calendar, as asked; not included: BITS.", text)
        offline = GoogleAccounts.single(FakeProvider("me@x.org", fail="offline"))
        text = self.runtime(tool_call("get_briefing"), google=offline).run_turn("Brief me").response
        self.assertIn("I couldn't read your calendar", text)
        self.assertNotIn("Nothing else is scheduled", text)
        self.assertIn("Not available: calendar", text)

    def test_timer_status_and_a_plain_task_list_are_answered_directly(self):
        runtime = self.runtime(tool_call("list_timers"), tool_call("list_tasks", call_id="c2"))
        runtime.registry.timers.start("focus", 25 * 60, "SOP edits", None)
        text = runtime.run_turn("How much time is left on my timer?").response
        self.assertRegex(text, r"^25-minute focus session \"SOP edits\": (?:25 minutes|24 minutes \d+ seconds) left, ends at \d\d:\d\d\.$")
        storage.create_task("Submit lab report", due_at=(NOW - timedelta(days=1)).strftime("%Y-%m-%d"), database_path=self.database)
        storage.create_task("Email the TA", due_at=(NOW + timedelta(days=2)).strftime("%Y-%m-%d"), database_path=self.database)
        done = storage.create_task("Old thing", database_path=self.database)
        storage.update_task(done["id"], self.database, completed=True)
        text = runtime.run_turn("What tasks do I have?").response
        self.assertEqual(self.model.call_count, 2)  # one per turn
        lines = text.splitlines()
        self.assertEqual(lines[0], "You have 2 open tasks:")
        self.assertIn("- Email the TA (due ", text)
        self.assertRegex(text, r"- Submit lab report \(overdue, was due \w{3} \d{4}-\d\d-\d\d\)")
        self.assertEqual(lines[-1], "1 completed task is not listed.")

    def test_requests_that_need_more_than_the_result_keep_the_model_path(self):
        answer = {"role": "assistant", "content": "Phrased by the model."}
        cases = [
            ("What tasks are due this week?", [tool_call("list_tasks")]),  # a filter needs interpretation
            ("Check my timer and then start a break", [tool_call("list_timers")]),  # more to do
            ("What notes do I have?", [tool_call("list_notes")]),  # not allowlisted
            ("What's important today?", [{"role": "assistant", "content": "", "tool_calls": [  # two calls at once
                {"id": "a", "function": {"name": "get_briefing", "arguments": {}}},
                {"id": "b", "function": {"name": "list_tasks", "arguments": {}}}]}]),
            ("What tasks do I have?", [tool_call("list_notes"), tool_call("list_tasks", call_id="c2")]),  # not the first step
        ]
        for message, steps in cases:
            with self.subTest(message=message, steps=len(steps)):
                runtime = self.runtime(*steps, answer)
                self.assertEqual(runtime.run_turn(message).response, "Phrased by the model.")
                self.assertEqual(self.model.call_count, len(steps) + 1)

    def test_a_failed_read_or_a_selected_document_keeps_the_model_path(self):
        registry = ToolRegistry(self.database, timers=TimerService(self.database))
        registry._definitions["list_timers"] = dataclasses.replace(
            registry._definitions["list_timers"],
            execute=lambda arguments: ToolOutcome(False, "The timer store is unavailable.", answer="unused"))
        runtime = self.runtime(tool_call("list_timers"), {"role": "assistant", "content": "I couldn't read your timers."},
                               registry=registry)
        self.assertEqual(runtime.run_turn("How much time is left?").response, "I couldn't read your timers.")
        runtime = self.runtime(tool_call("list_tasks"), {"role": "assistant", "content": "From the document: ..."})
        result = runtime.run_turn("What tasks do I have?", additional_context=[{"role": "system", "content": "Document excerpt"}])
        self.assertEqual(result.response, "From the document: ...")

    def test_approvals_writes_and_automation_are_unchanged(self):
        task = storage.create_task("Old thing", database_path=self.database)
        runtime = self.runtime(tool_call("delete_task", {"task_id": task["id"]}))
        pending = runtime.run_turn("Delete the task Old thing").pending_confirmation
        self.assertEqual(pending["tool"], "delete_task")  # still asks first
        self.assertEqual(len(storage.list_tasks(self.database)), 1)
        runtime = self.runtime(tool_call("create_task", {"title": "Buy servos"}), {"role": "assistant", "content": "Added."})
        self.assertEqual(runtime.run_turn("Add a task to buy servos").response, "Added.")  # writes are phrased by the model
        planner = Planner(plan(step("list_tasks")), route=True)
        automated = ModeTests.runtime(self, planner)
        result = automated.run_turn("Check my tasks and then tell me what is open")
        self.assertEqual(automated.automation_status()["status"], "COMPLETED")
        self.assertNotIn("You have", result.response)  # the automation reports; its read step does not end the turn



# --- Every connected calendar (P0.4) ------------------------------------------------------------

class AllCalendarsTests(Case):
    def accounts(self, personal_events=(), bits_events=(), bits_fail=None):
        from google_workspace import GoogleSettings
        path = Path(self.temp.name) / "accounts.json"
        path.write_text(json.dumps({"version": 1, "default": "personal", "accounts": [
            {"name": "personal", "item": "oauth", "email": "me@x.org"}, {"name": "BITS", "item": "oauth:bits", "email": "d@bits.edu"}]}))
        providers = {"oauth": MailProvider("me@x.org", list(personal_events)),
                     "oauth:bits": MailProvider("d@bits.edu", list(bits_events), fail=bits_fail)}
        return GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x")), registry_path=path,
                              provider_factory=lambda item: providers[item])

    def answer(self, accounts, message="What's important today?"):
        runtime = DirectAnswerTests.runtime(self, tool_call("get_briefing", {"day": "today"}), google=accounts)
        return runtime.run_turn(message).response

    def test_one_timeline_in_local_time_with_all_day_events_from_every_calendar(self):
        later, sooner = at(150), at(90)
        utc_event = {"event_id": "bitsevt1", "ical_uid": "b1@google.com", "title": "Lab viva", "all_day": False,
                     "start": sooner.astimezone(timezone.utc).isoformat(), "end": (sooner + timedelta(hours=1)).astimezone(timezone.utc).isoformat()}
        holiday = {"event_id": "bitsevt2", "title": "Founders' Day", "start": NOW.date().isoformat(),
                   "end": (NOW.date() + timedelta(days=1)).isoformat(), "all_day": True}
        text = self.answer(self.accounts([timed_event("Group meeting", 150, "pevt0001")], [holiday, utc_event]))
        # The BITS event, stored in UTC, is shown at its local time and comes first; then the personal one.
        self.assertIn(f"Next: Lab viva (BITS) at {sooner:%H:%M}", text)
        if later.date() == sooner.date():
            self.assertIn(f"Later: {later:%H:%M} Group meeting (personal).", text)
        self.assertIn("All day: Founders' Day.", text)

    def test_an_empty_day_in_every_calendar_is_said_plainly(self):
        text = self.answer(self.accounts())
        self.assertIn("Nothing else is scheduled today.", text)
        self.assertNotIn("Not available", text)

    def test_a_calendar_that_cannot_be_read_makes_the_day_partial_not_empty(self):
        accounts = self.accounts(bits_fail="Google Calendar is unavailable.")
        text = self.answer(accounts)
        self.assertIn("Nothing else is scheduled today in the calendars I could check; I couldn't check BITS.", text)
        self.assertIn("Not available: calendar (BITS): Google Calendar is unavailable.", text)
        result = briefing.build_briefing(NOW, "today", database_path=self.database, google=accounts, timers=TimerService(self.database))
        self.assertEqual(priorities.dashboard(result, None, None)["calendar_missing"], ["BITS"])
        self.assertIn("none found in the calendars that could be read (not BITS)", priorities.summary_text(result["focus"]))


if __name__ == "__main__":
    unittest.main()
