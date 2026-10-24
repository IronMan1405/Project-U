"""Automation runs (automation.py) through the real runtime and registry, with a scripted planner
instead of the model. Reminders, Google, opening apps and Accessibility are fakes; notes and tasks
use a temporary database. Nothing here needs the local model.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import automation
import computer_utilities
import storage
import tool_registry
import voice
import web_app
from automation import AutomationLimits, AutomationRunner, classify, report
from google_accounts import LEGACY_ITEM, GoogleAccounts
from google_workspace import GoogleSettings
from reminders import AppleReminders
from timers import TimerService
from tool_registry import ToolOutcome, ToolRegistry
from ui_control import UIController, UIControlSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capture_calendar_ui import FakeBackend  # noqa: E402
from test_google_accounts import FakeProvider, event  # noqa: E402
from test_planning import FakeOsascript  # noqa: E402

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


class Planner:
    """Returns the scripted plan/decisions in order and records what it was asked."""

    def __init__(self, *responses, route=False):
        self.responses = list(responses)
        self.requests = []
        self.route = route  # the answer to the routing question (is this an objective?)
        self.routed = []

    def __call__(self, messages, schema):
        if schema is automation.ROUTE_SCHEMA:
            self.routed.append(messages[-1]["content"])
            return {"multi_step": self.route}
        self.requests.append(messages)
        if not self.responses:
            return {"action": "finish", "summary": ""}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def step(tool, arguments=None, purpose=None, later=False, depends_on=()):
    return {"tool": tool, "arguments": arguments or {}, "fill_later": later, "purpose": purpose or tool,
            "depends_on": list(depends_on)}


def plan(*steps, unsupported=None, question=None):
    return {"steps": list(steps), "unsupported": unsupported, "question": question}


def call(objective="Get me ready for tomorrow", call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "function": {"name": "start_automation", "arguments": {"objective": objective}}}]}


class AutomationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "a.sqlite3"
        storage.initialize_database(self.database)
        self.osascript = FakeOsascript()
        self.opener = Mock(return_value=True)
        self.activity = []

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, **extra):
        return ToolRegistry(self.database, reminders=AppleReminders(runner=self.osascript),
                            timers=TimerService(self.database), open_runner=self.opener, **extra)

    def runtime(self, planner, registry=None, limits=None, replies=("unused",), objective="Get me ready for tomorrow"):
        model = Mock(side_effect=[call(objective)] + [{"role": "assistant", "content": text} for text in replies])
        self.model = model
        return assistant_runtime.AssistantRuntime(
            "http://local", "m", database_path=self.database, tool_registry=registry or self.registry(),
            model_client=model, planner_client=planner, automation_limits=limits,
            activity_callback=self.activity.append)

    def run_status(self, runtime):
        return runtime.automation_status()


class PlanningTests(AutomationCase):
    def test_a_read_only_plan_runs_without_approval_and_reports_the_answer(self):
        storage.create_task("Lab report", database_path=self.database)
        planner = Planner(plan(step("list_tasks", purpose="Check open tasks")),
                          {"action": "finish", "summary": "You have one open task: Lab report."})
        runtime = self.runtime(planner)
        result = runtime.run_turn("Go through my open tasks for me")
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual(result.response, "You have one open task: Lab report.")
        status = self.run_status(runtime)
        self.assertEqual((status["status"], status["steps"][0]["status"]), ("COMPLETED", "success"))
        self.assertTrue(any(a.get("status") == "automation" for a in self.activity))  # observable
        self.assertEqual(self.model.call_count, 1)  # the chat model is not called again: the run reports

    def test_unknown_tools_shell_and_nested_automation_are_refused_at_planning(self):
        for bad in ("run_command", "book_flight", "start_automation"):
            with self.subTest(tool=bad):
                runtime = self.runtime(Planner(plan(step(bad))))
                result = runtime.run_turn("Do it")
                self.assertIn("could not make a usable plan", result.response)
                self.assertEqual(self.run_status(runtime)["status"], "FAILED")
                self.assertEqual(self.opener.call_count, 0)

    def test_an_unsupported_objective_is_reported_honestly(self):
        runtime = self.runtime(Planner(plan(unsupported="booking flights")))
        result = runtime.run_turn("Book me a flight to Delhi")
        self.assertIn("I can't do this: booking flights.", result.response)
        self.assertEqual(self.run_status(runtime)["steps"], [])

    def test_a_vague_objective_gets_one_question(self):
        runtime = self.runtime(Planner(plan(question="Which morning: tomorrow's?")))
        result = runtime.run_turn("Clean up my morning")
        self.assertEqual(result.response, "Which morning: tomorrow's?")
        self.assertEqual(self.run_status(runtime)["status"], "PAUSED")

    def test_too_many_steps_are_refused(self):
        limits = AutomationLimits(max_steps=2)
        runtime = self.runtime(Planner(plan(*[step("list_tasks")] * 3)), limits=limits)
        self.assertIn("more than 2 steps", runtime.run_turn("Do everything").response)

    def test_a_broken_planner_answer_is_a_clear_failure(self):
        runtime = self.runtime(Planner(ValueError("not json")))
        self.assertIn("could not make a plan", runtime.run_turn("Do it").response)

    def test_the_model_sees_a_compact_catalogue_without_the_automation_tool(self):
        planner = Planner(plan(step("list_tasks")))
        self.runtime(planner).run_turn("x")
        system = planner.requests[0][0]["content"]
        self.assertIn("- list_tasks() [reads]", system)
        self.assertIn("[needs approval]", system)
        self.assertNotIn("start_automation(", system)
        self.assertNotIn("run_command", system)


class DependencyTests(AutomationCase):
    def test_later_steps_are_filled_from_earlier_results(self):
        note = storage.create_note("SOLVE camera", "Use an eye-in-hand camera", self.database)
        planner = Planner(
            plan(step("search_notes", {"query": "camera"}, "Find the camera idea"),
                 step("create_task", later=True, purpose="Make it a task", depends_on=[0])),
            {"action": "run", "tool": "create_task", "arguments": {"title": "Test eye-in-hand camera",
                                                                   "note_id": note["id"]}},
            {"action": "finish", "summary": "Found the camera idea."},
        )
        runtime = self.runtime(planner, objective="Turn my camera idea into a task")
        result = runtime.run_turn("Turn my camera idea into a task")
        # The task was not explicitly asked for in the objective's words: the plan asked first.
        self.assertIsNotNone(result.pending_confirmation)
        self.assertIn("2. Make it a task", result.pending_confirmation["summary"])
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        decision_input = json.loads(planner.requests[1][-1]["content"])
        self.assertIn("Use an eye-in-hand camera", decision_input["steps"][0]["result"]["data"])  # result passed on
        self.assertIn("Done: Create task: Test eye-in-hand camera", result.response)
        self.assertEqual(storage.list_tasks(self.database)[0]["title"], "Test eye-in-hand camera")
        self.assertEqual(self.run_status(runtime)["steps"][1]["verification"], "found in tasks")

    def test_an_invalid_next_step_gets_one_chance_to_be_corrected(self):
        for decisions, corrected in (([{"kind": "nap", "minutes": 25}, {"kind": "focus", "minutes": 25}], True),
                                     ([{"kind": "nap", "minutes": 25}, {"kind": "snooze", "minutes": 25}], False)):
            planner = Planner(
                plan(step("search_notes", {"query": "camera"}, "Find the camera idea"),
                     step("start_timer", later=True, purpose="Start a focus session", depends_on=[0])),
                *[{"action": "run", "tool": "start_timer", "arguments": arguments} for arguments in decisions],
                {"action": "finish", "summary": "Started."},
            )
            runtime = self.runtime(planner, objective="Find my camera idea and start a focus session")
            before = len(runtime.registry.timers.active())
            result = runtime.run_turn("Find my camera idea and start a focus session")
            while result.pending_confirmation:
                result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
            retry = json.loads(planner.requests[2][-1]["content"])
            self.assertIn("kind must be one of", retry["rejected_next_step"])  # the model was told what was wrong
            if corrected:
                self.assertEqual(len(runtime.registry.timers.active()), before + 1)
                self.assertEqual(self.run_status(runtime)["status"], "COMPLETED")
            else:  # bounded: a second unusable answer stops the run
                self.assertEqual(len(runtime.registry.timers.active()), before)
                self.assertIn("the next step was not usable", result.response)

    def test_an_empty_result_makes_the_model_replan(self):
        planner = Planner(
            plan(step("search_notes", {"query": "lidar"}, "Find lidar notes"),
                 step("create_task", {"title": "Read lidar notes"}, "Task to read them", depends_on=[0])),
            {"action": "replan", "steps": [step("search_notes", {"query": "laser"}, "Look for laser notes instead")]},
            {"action": "finish", "summary": "No lidar or laser notes were saved."},
        )
        runtime = self.runtime(planner, objective="Find my lidar notes and add a task to read them")
        result = runtime.run_turn("Find my lidar notes and add a task to read them")
        if result.pending_confirmation:  # the task step needed approval under the existing policy
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        status = self.run_status(runtime)
        self.assertEqual([s["purpose"] for s in status["steps"]], ["Find lidar notes", "Look for laser notes instead"])
        self.assertEqual(storage.list_tasks(self.database), [])  # the planned task was not blindly created
        self.assertIn("No lidar or laser notes", result.response)

    def test_the_model_can_add_the_next_action_after_the_plan_with_approval(self):
        planner = Planner(
            plan(step("list_tasks", purpose="Check tasks")),
            {"action": "run", "tool": "start_timer", "arguments": {"kind": "focus", "minutes": 25},
             "purpose": "Start a focus session"},
            {"action": "finish", "summary": "Ready."},
        )
        runtime = self.runtime(planner, objective="Get me working on my tasks")
        result = runtime.run_turn("Get me working on my tasks")
        # Beyond the plan: a new step that needs approval is shown before it runs.
        self.assertIn("Start a focus session", result.pending_confirmation["summary"])
        self.assertEqual(runtime.registry.timers.active(), [])
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(len(runtime.registry.timers.active()), 1)
        self.assertIn("Done: Start a 25-minute focus session", result.response)


class ApprovalTests(AutomationCase):
    def test_one_approval_covers_exactly_the_shown_low_impact_steps(self):
        planner = Planner(plan(
            step("capture_note", {"text": "Test the Gemini camera tomorrow"}, "Save the camera test"),
            step("create_reminder", {"title": "Test the Gemini camera", "due_at": f"{TOMORROW} 10:00"}, "Remind me at 10"),
        ), {"action": "finish", "summary": "Saved and set."})
        runtime = self.runtime(planner, objective="Remember that I need to test the Gemini camera tomorrow and remind me at 10")
        result = runtime.run_turn("Remember that I need to test the Gemini camera tomorrow and remind me at 10")
        if result.pending_confirmation:  # only if the existing policy wanted approval for either step
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertIsNone(result.pending_confirmation)
        steps = self.run_status(runtime)["steps"]
        self.assertEqual([(s["status"], s["verification"]) for s in steps],
                         [("success", "found in notes"), ("success", "read back from Reminders")])
        self.assertEqual([c[0] for c in self.osascript.calls if c[0] != "LIST"], ["CREATE", "GET"])

    def test_a_consequential_step_shown_without_details_gets_its_own_confirmation(self):
        accounts = GoogleAccounts.single(FakeProvider("me@gmail.example"))
        planner = Planner(
            plan(step("search_gmail", {"query": "from:prof SOP"}, "Find the SOP email"),
                 step("create_gmail_draft", later=True, purpose="Draft a reply", depends_on=[0])),
            {"action": "run", "tool": "create_gmail_draft", "arguments": {"body": "I'll meet Monday.",
                                                                          "reply_to_message_id": "me1"}},
        )
        runtime = self.runtime(planner, registry=self.registry(google=accounts),
                               objective="Find the email from my professor about the SOP and draft a reply")
        result = runtime.run_turn("Find the email from my professor about the SOP and draft a reply saying I'll meet Monday")
        self.assertIn("you will see the exact details before it happens", result.pending_confirmation["summary"])
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        # The draft itself: the usual confirmation with the full preview, not covered by the plan approval.
        self.assertEqual(result.pending_confirmation["tool"], "create_gmail_draft")
        self.assertEqual(result.pending_confirmation["preview"]["body"], "I'll meet Monday.")
        self.assertEqual(result.pending_confirmation["automation"]["kind"], "step")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], False)
        self.assertIn("Cancelled, not done", result.response)
        self.assertNotIn("create_draft", accounts.provider().calls)
        self.assertEqual(self.run_status(runtime)["status"], "CANCELLED")

    def test_a_changed_action_after_approval_needs_new_approval(self):
        planner = Planner(
            plan(step("list_tasks", purpose="Check tasks"),
                 step("start_timer", later=True, purpose="Start a focus session", depends_on=[0])),
            {"action": "run", "tool": "create_reminder", "arguments": {"title": "Focus", "due_at": f"{TOMORROW} 09:00"},
             "purpose": "Remind me to focus"},
        )
        runtime = self.runtime(planner, objective="Plan my focus time")
        result = runtime.run_turn("Plan my focus time")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)  # the shown plan
        # The model swapped the timer for a reminder: that was not approved, so it is asked again.
        self.assertIsNotNone(result.pending_confirmation)
        self.assertIn("Remind me to focus", result.pending_confirmation["summary"])
        self.assertFalse([c for c in self.osascript.calls if c[0] == "CREATE"])

    def test_declining_the_plan_runs_nothing_and_a_new_message_cancels_it(self):
        planner = Planner(plan(step("start_timer", {"kind": "focus", "minutes": 90}, "Start a 90-minute focus session")))
        runtime = self.runtime(planner, objective="Get me into deep work")
        result = runtime.run_turn("Get me into deep work")
        declined = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], False)
        self.assertIn("Cancelled, not done: Start a 90-minute focus session", declined.response)
        self.assertEqual(runtime.registry.timers.active(), [])
        planner2 = Planner(plan(step("start_timer", {"kind": "focus", "minutes": 90}, "Start focus")))
        runtime2 = self.runtime(planner2, objective="Get me into deep work", replies=("Okay.",))
        pending = runtime2.run_turn("Get me into deep work").pending_confirmation
        later = runtime2.run_turn("Actually, never mind")
        self.assertIn("Cancelled", later.notice)
        self.assertEqual(runtime2.registry.timers.active(), [])
        with self.assertRaises(assistant_runtime.RuntimeBusyError):
            runtime2.resolve_confirmation(pending["confirmation_id"], True)  # stale approval


class BoundsTests(AutomationCase):
    def test_transient_failures_are_retried_a_bounded_number_of_times(self):
        registry = self.registry()
        flaky = Mock(side_effect=[ToolOutcome(False, "Reminders did not respond in time. Try again in a moment.", {"error": "timeout"})] * 5)
        registry._definitions["list_reminders"] = tool_registry.ToolDefinition("list_reminders", "x", {"type": "object", "properties": {}}, flaky)
        runtime = self.runtime(Planner(plan(step("list_reminders")), {"action": "finish", "summary": "Could not read reminders."}),
                               registry=registry, limits=AutomationLimits(max_retries=2))
        runtime.run_turn("Check reminders")
        self.assertEqual(flaky.call_count, 3)  # first try plus two retries, no more
        self.assertEqual(self.run_status(runtime)["failures"][0]["kind"], "transient")

    def test_plan_changes_are_bounded(self):
        replan = {"action": "replan", "steps": [step("search_notes", {"query": "x"})]}
        runtime = self.runtime(Planner(plan(step("search_notes", {"query": "x"})), *[replan] * 10),
                               limits=AutomationLimits(max_replans=2))
        result = runtime.run_turn("Find it")
        self.assertIn("more than 2 times", result.response)
        self.assertEqual(self.run_status(runtime)["status"], "FAILED")

    def test_steps_are_bounded_even_when_the_model_keeps_adding(self):
        more = {"action": "run", "tool": "list_tasks", "arguments": {}}
        runtime = self.runtime(Planner(plan(step("list_tasks")), *[more] * 10),
                               limits=AutomationLimits(max_steps=3, max_replans=6))
        self.assertIn("limit of 3 steps", runtime.run_turn("Keep checking").response)

    def test_execution_time_is_bounded(self):
        times = iter([0, 0, 1000, 1000, 1000])
        registry = self.registry()
        runner_limits = AutomationLimits(timeout_seconds=30)
        runtime = self.runtime(Planner(plan(step("list_tasks"), step("list_notes"))), registry=registry, limits=runner_limits)
        runtime.runner.clock = lambda: next(times, 1000)
        result = runtime.run_turn("Check things")
        self.assertIn("longer than 30 seconds", result.response)

    def test_cancellation_stops_before_the_next_step_and_keeps_finished_ones(self):
        registry = self.registry()
        holder = {}

        def list_and_cancel(arguments):
            holder["runtime"].cancel_automation()  # as if "stop" arrived while this step ran
            return ToolOutcome(True, "Found 0 task(s).", {"tasks": []})

        registry._definitions["list_tasks"] = tool_registry.ToolDefinition("list_tasks", "x", {"type": "object", "properties": {}}, list_and_cancel)
        planner = Planner(plan(step("capture_note", {"text": "Order servos"}, "Save it"), step("list_tasks", purpose="Check tasks"),
                               step("start_timer", {"kind": "focus", "minutes": 30}, "Start focus")))
        runtime = self.runtime(planner, registry=registry, objective="Remember to order servos, check my tasks and start focus")
        holder["runtime"] = runtime
        result = runtime.run_turn("Remember to order servos, check my tasks and start focus")
        if result.pending_confirmation:
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        status = self.run_status(runtime)
        self.assertEqual(status["status"], "CANCELLED")
        self.assertEqual([s["status"] for s in status["steps"]], ["success", "success", "cancelled"])
        self.assertEqual(runtime.registry.timers.active(), [])
        self.assertIn("Cancelled, not done: Start focus", result.response)
        self.assertFalse(runtime.cancel_automation())  # nothing left to cancel

    def test_one_automation_at_a_time_and_disabled_automation(self):
        runtime = self.runtime(Planner(plan(step("start_timer", {"kind": "focus", "minutes": 30}, "Focus"))),
                               objective="Focus please")
        runtime.run_turn("Focus please")  # waiting for approval
        runtime._turn.seen_call_ids.clear()
        second = runtime._start_automation("c2", {"objective": "Another"}, [])
        self.assertIn("already running", second.response)
        off = self.runtime(Planner(), limits=AutomationLimits(enabled=False))
        self.assertIn("switched off", off.run_turn("Do it").response)


class FailureTests(AutomationCase):
    def test_failure_kinds(self):
        self.assertEqual(classify("Reminders did not respond in time. Try again in a moment.", {"error": "timeout"}), "transient")
        self.assertEqual(classify("K.R.I.S. does not have Accessibility permission.", {"error": "permission"}), "permission")
        self.assertEqual(classify("No Accessibility permission. Turn it on, then try again.", None), "permission")
        self.assertEqual(classify("Several matches fit this request (a; b).", None), "ambiguous")
        self.assertEqual(classify("That item does not exist in the approved folder.", None), "missing")
        self.assertEqual(classify("kind must be one of meeting, exam.", None), "invalid")

    def test_ambiguous_targets_become_one_question(self):
        storage.create_task("A", database_path=self.database)
        registry = self.registry()
        registry.prepare = Mock(side_effect=tool_registry.ToolError("Several matches fit this request (Desktop/robotics; Documents/Robotics). Ask the user which."))
        runtime = self.runtime(Planner(plan(step("list_tasks"))), registry=registry)
        result = runtime.run_turn("Open my robotics folder")
        self.assertIn("Several matches", result.response)
        self.assertEqual(self.run_status(runtime)["status"], "PAUSED")

    def test_permission_failures_stop_with_the_permission_needed(self):
        ui = UIController(UIControlSettings(("Finder",)), FakeBackend(trusted=False))
        planner = Planner(plan(step("ui_action", {"app": "Finder", "role": "button", "name": "Back", "action": "press"}, "Press Back")))
        runtime = self.runtime(planner, registry=self.registry(ui=ui), objective="Go back in Finder")
        result = runtime.run_turn("Go back in Finder")
        self.assertIn("Accessibility permission", result.response)
        self.assertEqual(self.run_status(runtime)["failures"][0]["kind"], "permission")

    def test_ui_steps_need_their_exact_approval_and_disallowed_apps_never_run(self):
        backend = FakeBackend()
        ui = UIController(UIControlSettings(("Finder",)), backend)
        planner = Planner(plan(step("ui_action", {"app": "Finder", "role": "button", "name": "Back", "action": "press"}, "Press Back")),
                          {"action": "finish", "summary": "Went back."})
        runtime = self.runtime(planner, registry=self.registry(ui=ui), objective="Press Back in Finder")
        result = runtime.run_turn("Press Back in Finder")
        self.assertEqual(backend.performed, [])  # nothing before approval
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(backend.performed, [((0, 0, 1), "press", None)])
        self.assertIn("Not confirmed: Press the button", result.response)  # a press cannot be read back
        planner2 = Planner(plan(step("ui_action", {"app": "Safari", "role": "button", "name": "Send", "action": "press"})),
                           {"action": "run", "tool": "ui_action", "arguments": {"app": "Safari", "role": "button", "name": "Send", "action": "press"}})
        runtime2 = self.runtime(planner2, registry=self.registry(ui=UIController(UIControlSettings(("Finder",)), backend)))
        self.assertIn("app must be one of: Finder", runtime2.run_turn("Send it in Safari").response)
        self.assertEqual(len(backend.performed), 1)


class AccountTests(AutomationCase):
    def test_google_steps_keep_their_account_and_reminders_stay_apple(self):
        personal = FakeProvider("me@gmail.example")
        bits = FakeProvider("daks@bits.example", [event("Electronics lab", 10, "labevt01")])
        registry_path = Path(self.temp.name) / "accounts.json"
        registry_path.write_text(json.dumps({"version": 1, "default": "personal", "accounts": [
            {"name": "personal", "item": LEGACY_ITEM, "email": personal.email},
            {"name": "BITS", "item": "oauth:bits", "email": bits.email}]}))
        items = {LEGACY_ITEM: personal, "oauth:bits": bits}
        accounts = GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x")), registry_path=registry_path,
                                  provider_factory=lambda item: items[item])
        planner = Planner(
            plan(step("search_calendar_events", {"start": TOMORROW, "end": TOMORROW, "account": "BITS"}, "Find tomorrow's BITS events"),
                 step("create_reminder", later=True, purpose="Remind me before it", depends_on=[0])),
            {"action": "run", "tool": "create_reminder", "arguments": {"title": "Electronics lab", "due_at": f"{TOMORROW} 09:00"}},
            {"action": "finish", "summary": "Tomorrow: Electronics lab at 10:00 (BITS)."},
        )
        runtime = self.runtime(planner, registry=self.registry(google=accounts),
                               objective="Check my BITS calendar for tomorrow and create a reminder")
        result = runtime.run_turn("Check my BITS calendar for tomorrow and create a reminder")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual((bits.calls, personal.calls), (["search_events"], []))
        self.assertIn("Done: Create reminder: Electronics lab", result.response)
        self.assertTrue([c for c in self.osascript.calls if c[0] == "CREATE"])

    def test_a_step_cannot_switch_an_item_to_another_account(self):
        personal = FakeProvider("me@gmail.example")
        bits = FakeProvider("daks@bits.example")
        registry_path = Path(self.temp.name) / "accounts.json"
        registry_path.write_text(json.dumps({"version": 1, "default": "personal", "accounts": [
            {"name": "personal", "item": LEGACY_ITEM, "email": personal.email},
            {"name": "BITS", "item": "oauth:bits", "email": bits.email}]}))
        items = {LEGACY_ITEM: personal, "oauth:bits": bits}
        accounts = GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x")), registry_path=registry_path,
                                  provider_factory=lambda item: items[item])
        planner = Planner(plan(step("search_gmail", {"query": "SOP", "account": "BITS"}, "Find the SOP email"),
                               step("read_gmail_message", later=True, depends_on=[0])),
                          {"action": "run", "tool": "read_gmail_message", "arguments": {"message_id": "daks1", "account": "personal"}})
        runtime = self.runtime(planner, registry=self.registry(google=accounts), objective="Read the SOP email in BITS")
        result = runtime.run_turn("Read the SOP email in BITS")
        self.assertIn("asked for the BITS account, not personal", result.response)
        self.assertEqual(personal.calls, [])


class VerificationTests(AutomationCase):
    def test_opening_is_checked_against_the_running_app_or_marked_uncertain(self):
        from developer import DeveloperCompanion, DeveloperSettings
        project = (Path(self.temp.name) / "proj").resolve()
        project.mkdir()
        dev = DeveloperCompanion(DeveloperSettings({"Proj": project}, {}, "Visual Studio Code"))
        planner = Planner(plan(step("open_in_editor", {"path": "Proj"}, "Open Proj")), {"action": "finish", "summary": "Opened."})
        runtime = self.runtime(planner, registry=self.registry(developer=dev), objective="Open Proj in VS Code")
        with patch.object(computer_utilities, "open_supported", return_value=True), \
                patch.object(computer_utilities, "find_application", return_value=Path("/Applications/Visual Studio Code.app")):
            result = runtime.run_turn("Open Proj in VS Code")
        self.assertIn("Not confirmed", result.response)  # no Mac control here: what is on screen cannot be checked
        self.assertEqual(self.run_status(runtime)["steps"][0]["status"], "uncertain")

    def test_findings_are_stated_from_results_when_the_answer_omits_them(self):
        read = automation.Step("search_calendar_events", {}, "Find events", status="success",
                               data={"events": [{"title": "BITS F385 - L1", "start": "2026-10-23T14:00:00+05:30"}]})
        run = automation.AutomationRun("r", "x", status=automation.COMPLETED, steps=[read], answer="Events were found.")
        self.assertEqual(report(run), "Events were found. Found: BITS F385 - L1 (2026-10-23 14:00).")
        run.answer = "Tomorrow you have BITS F385 - L1 at 14:00."
        self.assertEqual(report(run), "Tomorrow you have BITS F385 - L1 at 14:00.")

    def test_a_plan_that_only_reports_something_unsupported_is_valid(self):
        runtime = self.runtime(Planner({"steps": None, "unsupported": "booking flights"}))
        self.assertEqual(runtime.run_turn("Book me a flight").response, "I can't do this: booking flights.")

    def test_the_report_comes_from_step_records(self):
        run = automation.AutomationRun("r", "x", status=automation.COMPLETED, steps=[
            automation.Step("create_task", {}, "t", status="success", summary="Create task: A", changes=True),
            automation.Step("create_reminder", {}, "r", status="failed", summary="Create reminder: B", message="Reminders refused"),
            automation.Step("open_file", {}, "o", status="uncertain", summary="Open file C", verification="cannot be checked"),
            automation.Step("start_timer", {}, "Start focus", status="skipped"),
        ], answer="Here is what I did.")
        text = report(run)
        self.assertEqual(text, "Here is what I did. Done: Create task: A. Not confirmed: Open file C (cannot be checked). "
                               "Failed: Create reminder: B (Reminders refused). Not done: Start focus.")


class DirectPathAndVoiceTests(AutomationCase):
    def test_single_actions_still_run_directly(self):
        model = Mock(side_effect=[{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_tasks", "arguments": {}}}]}, {"role": "assistant", "content": "None."}])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=self.registry(), model_client=model,
                                                     planner_client=Mock(side_effect=AssertionError("no planning")))
        self.assertEqual(runtime.run_turn("Which tasks are due this week?").response, "None.")
        self.assertIsNone(runtime.automation_status())

    def test_the_automation_tool_is_offered_once_and_only_runs_in_the_runtime(self):
        registry = self.registry()
        names = [d["function"]["name"] for d in registry.definitions()]
        self.assertEqual(names.count("start_automation"), 1)
        self.assertFalse(registry.execute(registry.prepare("start_automation", {"objective": "abc"}, "abc")).ok)

    def test_stop_is_recognised_in_short_clips_only_during_automation(self):
        for text in ("Stop.", "Kris, stop!", "cancel", "Hey Kris, cancel it", "Stop it."):
            with self.subTest(text=text):
                self.assertTrue(voice.is_stop_phrase(text))
        for text in ("Stop the music in Music app", "Kris", "don't stop", "Stopwatch"):
            with self.subTest(text=text):
                self.assertFalse(voice.is_stop_phrase(text))
        settings = voice.VoiceSettings()
        with patch.object(voice, "transcribe", return_value="Kris, stop."):
            self.assertEqual(voice.check_clip(settings, b"x", stop=True), {"wake": False, "stop": True})
            self.assertEqual(voice.check_clip(settings, b"x", stop=False), {"wake": False, "stop": False})

    def test_the_cancel_endpoint_sets_the_stop_flag_without_waiting(self):
        app = web_app.create_app(self.database, ui_settings={})
        client = app.test_client()
        conversation = client.post("/api/conversations", json={}).get_json()["conversation_id"]
        response = client.post("/api/automation/cancel", json={"conversation_id": conversation})
        self.assertEqual(response.get_json(), {"cancelling": False})  # nothing running
        self.assertEqual(client.get(f"/api/conversations/{conversation}/automation").get_json(), {"automation": None})
        self.assertEqual(client.post("/api/automation/cancel", json={"conversation_id": "bad"}).status_code, 400)


class RecoveryTests(AutomationCase):
    def test_a_next_step_missing_details_gets_one_correction(self):
        planner = Planner(
            plan(step("list_tasks", purpose="Check tasks"), step("create_task", later=True, purpose="Add a task", depends_on=[0])),
            {"action": "run", "tool": "create_task", "arguments": {"due_at": TOMORROW}},  # no title
            {"action": "run", "tool": "create_task", "arguments": {"title": "Review lidar notes", "due_at": TOMORROW}},
            {"action": "finish", "summary": "Added."},
        )
        runtime = self.runtime(planner, objective="Check my tasks and add a task to review lidar notes tomorrow")
        result = runtime.run_turn("Check my tasks and add a task to review lidar notes tomorrow")
        if result.pending_confirmation:
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        correction = json.loads(planner.requests[2][-1]["content"])
        self.assertIn("Needs:", json.dumps(correction))  # the model was told what was missing
        self.assertEqual(storage.list_tasks(self.database)[0]["title"], "Review lidar notes")

    def test_finish_written_as_a_step_ends_the_run_and_progress_is_shown_mid_run(self):
        planner = Planner(plan(step("search_notes", {"query": "nothing"}, "Look for notes"),
                               step("create_task", {"title": "x"}, "Add a task", depends_on=[0])),
                          {"action": "replan", "steps": [{"tool": "finish", "purpose": "done"}]})
        runtime = self.runtime(planner, objective="Find notes and add a task")
        result = runtime.run_turn("Find notes and add a task")
        if result.pending_confirmation:
            result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(self.run_status(runtime)["status"], "COMPLETED")
        self.assertEqual(storage.list_tasks(self.database), [])
        run = automation.AutomationRun("r", "o", steps=[automation.Step("list_tasks", {}, "Check tasks", status="success",
                                                                        summary="List tasks")])
        self.assertEqual(automation.progress(run), "So far: List tasks (success).")


class RoutingTests(AutomationCase):
    def test_the_model_routes_an_objective_straight_into_a_run(self):
        planner = Planner(plan(step("list_tasks", purpose="Check tasks")), {"action": "finish", "summary": "Nothing open."},
                          route=True)
        model = Mock(side_effect=AssertionError("the chat model is not needed"))
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=self.registry(), model_client=model, planner_client=planner)
        result = runtime.run_turn("Check my tasks and get me ready for tomorrow")
        self.assertEqual(result.response, "Nothing open.")
        self.assertEqual(runtime.history[1]["tool_calls"][0]["function"]["name"], "start_automation")
        self.assertIn("Check my tasks and get me ready for tomorrow", planner.routed[0])

    def test_short_or_single_clause_messages_are_not_routed_and_failures_fall_back(self):
        planner = Planner(route=True)
        self.assertFalse(automation.wants_automation("What time is it?", "", planner))
        self.assertFalse(automation.wants_automation("Open GitHub", "", planner))
        self.assertEqual(planner.routed, [])
        broken = Mock(side_effect=ValueError("no json"))
        self.assertFalse(automation.wants_automation("Find the email and draft a reply to it", "", broken))
        model = Mock(side_effect=[{"role": "assistant", "content": "Hello!"}])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=self.registry(), model_client=model,
                                                     planner_client=Planner(route=False))
        self.assertEqual(runtime.run_turn("Hi Kris, how are you and what can you do?").response, "Hello!")


class JsonPlannerTests(unittest.TestCase):
    def test_structured_output_falls_back_to_parsing_when_the_model_cannot_do_it(self):
        assistant_runtime._STRUCTURED_UNAVAILABLE.discard("m")
        replies = [urllib.error.HTTPError("http://x", 501, "Not Implemented", {}, None),
                   {"message": {"content": 'Sure:\n```json\n{"steps": [], "question": "Which?"}\n```'}}]

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                return json.dumps(self.body).encode()

        def opener(request, timeout=None):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return Response(reply)

        with patch.object(assistant_runtime.urllib.request, "urlopen", opener):
            self.assertEqual(assistant_runtime.ask_ollama_json("http://x", "m", [], {}), {"steps": [], "question": "Which?"})
        self.assertIn("m", assistant_runtime._STRUCTURED_UNAVAILABLE)  # not retried every time
        with self.assertRaises(ValueError):
            assistant_runtime._json_object("no json here")


if __name__ == "__main__":
    unittest.main()
