"""Email sends are approved only from their exact preview: never by a plan approval, a resumed or restored
run, or a conversational "yes". Fake Gmail and temporary databases; nothing is ever really sent."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import assistant_runtime
import automation
import storage
from google_accounts import GoogleAccounts
from timers import TimerService
from tool_registry import ToolRegistry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automation import Planner, plan, step  # noqa: E402
from test_google_accounts import FakeProvider  # noqa: E402

REPLY = {"to": "prof@x.org", "subject": "Re: SOP", "body": "Monday at 3 works for me."}
SENT = {**REPLY, "to": ["prof@x.org"]}  # as the provider receives it


class Mailbox(FakeProvider):
    """Records exactly what would have been sent."""

    def __init__(self):
        super().__init__("me@x.org")
        self.sent = []

    def send_message(self, message):
        self.sent.append({key: message[key] for key in ("to", "subject", "body")})
        return super().send_message(message)


def rows(pending):
    return dict(pending["preview"]["sections"][0]["rows"])


class SendApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "send.sqlite3"
        storage.initialize_database(self.database)
        self.mail = Mailbox()

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, planner=None, model=None):
        registry = ToolRegistry(self.database, google=GoogleAccounts.single(self.mail), timers=TimerService(self.database))
        return assistant_runtime.AssistantRuntime(
            "http://local", "m", database_path=self.database, tool_registry=registry, planner_client=planner or Planner(),
            model_client=model or Mock(side_effect=AssertionError("no model call expected")))

    def plan_with_send(self, *more):
        # The send is complete in the plan, and a focus session the user did not explicitly start needs approval.
        return Planner(plan(step("send_email", dict(REPLY), "Send the reply"),
                            step("start_timer", {"kind": "focus", "minutes": 25}, "A focus session")), *more, route=True)

    def test_approving_the_plan_does_not_send_the_exact_preview_does(self):
        runtime = self.runtime(self.plan_with_send())
        result = runtime.run_turn("Reply to the professor that Monday at 3 works and then a focus session")
        plan_approval = result.pending_confirmation
        self.assertEqual(plan_approval["tool"], "start_automation")
        self.assertIn("1. Send the reply (you will confirm the exact email before it is sent)", plan_approval["summary"])
        self.assertIn(["Step 1", "Send the reply — exact email confirmed separately"],
                      plan_approval["preview"]["sections"][0]["rows"])

        result = runtime.resolve_confirmation(plan_approval["confirmation_id"], True)
        self.assertEqual(self.mail.sent, [])  # the plan approval sent nothing
        send = result.pending_confirmation
        self.assertEqual(send["tool"], "send_email")
        self.assertEqual((rows(send)["To"], rows(send)["Subject"], rows(send)["Body"]),
                         (REPLY["to"], REPLY["subject"], REPLY["body"]))
        self.assertIn("Pressing Confirm sends this email immediately. It cannot be unsent.", send["preview"]["notes"])

        # Whatever the run's step says now, only the payload that was shown can be sent.
        runtime._run.steps[0].arguments["body"] = "CHANGED AFTER THE PREVIEW"
        runtime.resolve_confirmation(send["confirmation_id"], True)
        self.assertEqual(self.mail.sent, [SENT])

    def test_declining_the_send_leaves_it_unsent(self):
        runtime = self.runtime(self.plan_with_send())
        result = runtime.run_turn("Reply to the professor that Monday at 3 works and then a focus session")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], False)
        self.assertEqual(self.mail.sent, [])
        self.assertNotIn("send_message", self.mail.calls)

    def test_a_plan_whose_only_change_is_a_send_goes_straight_to_the_exact_preview(self):
        runtime = self.runtime(Planner(plan(step("list_tasks", purpose="Check tasks"),
                                            step("send_email", dict(REPLY), "Send the reply")),
                                       # (no tasks found, so the model is asked again before the send)
                                       {"action": "run", "tool": "send_email", "arguments": dict(REPLY)}, route=True))
        result = runtime.run_turn("Check my tasks and then reply to the professor that Monday at 3 works")
        self.assertEqual(result.pending_confirmation["tool"], "send_email")  # one approval, and it is the email
        self.assertEqual(rows(result.pending_confirmation)["Body"], REPLY["body"])
        runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(self.mail.sent, [SENT])

    def test_a_second_send_with_the_same_summary_but_another_body_is_confirmed_again(self):
        changed = {**REPLY, "body": "Actually, Tuesday."}
        runtime = self.runtime(Planner(plan(step("send_email", dict(REPLY), "Send the reply")),
                                       {"action": "run", "tool": "send_email", "arguments": changed, "purpose": "Send again"},
                                       route=True))
        result = runtime.run_turn("Reply to the professor that Monday at 3 works and then follow up")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(self.mail.sent, [SENT])
        self.assertEqual(result.pending_confirmation["tool"], "send_email")  # the earlier approval does not carry over
        self.assertEqual(rows(result.pending_confirmation)["Body"], "Actually, Tuesday.")
        runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], False)
        self.assertEqual(self.mail.sent, [SENT])

    def test_a_resumed_run_still_confirms_its_send(self):
        planner = Planner(plan(step("list_tasks", purpose="Check tasks"), step("send_email", dict(REPLY), "Send the reply")),
                          {"action": "run", "tool": "send_email", "arguments": dict(REPLY)}, route=True)
        runtime = self.runtime(planner)
        original = runtime.registry.execute

        def execute(prepared):
            if prepared.name == "list_tasks":
                runtime.cancel_automation()  # "stop" while the first step runs
            return original(prepared)

        runtime.registry.execute = execute
        runtime.run_turn("Check my tasks and then reply to the professor that Monday at 3 works")
        self.assertEqual(runtime.automation_status()["status"], automation.CANCELLED)
        result = runtime.run_turn("continue")
        self.assertEqual(result.pending_confirmation["tool"], "send_email")
        self.assertEqual(rows(result.pending_confirmation)["Body"], REPLY["body"])
        self.assertEqual(self.mail.sent, [])

    def test_a_send_the_model_picks_unexpectedly_still_needs_its_preview(self):
        call = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "send_email", "arguments": dict(REPLY)}}]}
        direct = self.runtime(model=Mock(side_effect=[call]))
        pending = direct.run_turn("Draft a reply to the professor saying Monday at 3 works").pending_confirmation
        self.assertEqual((pending["tool"], rows(pending)["Body"]), ("send_email", REPLY["body"]))
        self.assertEqual(self.mail.sent, [])

        # In an objective, the model swaps the planned draft for a send: still the exact preview first.
        swapped = self.runtime(Planner(plan(step("create_gmail_draft", purpose="Draft the reply", later=True)),
                                       {"action": "run", "tool": "send_email", "arguments": dict(REPLY)}, route=True))
        result = swapped.run_turn("Find nothing and then draft a reply to the professor saying Monday at 3 works")
        while result.pending_confirmation and result.pending_confirmation["tool"] == "start_automation":
            result = swapped.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(result.pending_confirmation["tool"], "send_email")
        self.assertEqual(self.mail.sent, [])

    def test_a_direct_send_is_confirmed_and_sends_exactly_the_preview(self):
        call = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "send_email", "arguments": dict(REPLY)}}]}
        runtime = self.runtime(model=Mock(side_effect=[call, {"role": "assistant", "content": "Sent."}]))
        pending = runtime.run_turn("Send the professor an email saying Monday at 3 works").pending_confirmation
        self.assertEqual(self.mail.sent, [])
        runtime.resolve_confirmation(pending["confirmation_id"], True)
        self.assertEqual(self.mail.sent, [SENT])

    def test_a_send_that_times_out_is_not_retried(self):
        from google_workspace import GoogleError
        attempts = []

        def timed_out(message):
            attempts.append(message)
            raise GoogleError("timeout", "Gmail did not respond. Try again.")

        self.mail.send_message = timed_out
        runtime = self.runtime(Planner(plan(step("send_email", dict(REPLY), "Send the reply")), route=True))
        result = runtime.run_turn("Reply to the professor that Monday at 3 works and then tell me")
        result = runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(len(attempts), 1)  # it may have gone out: never sent a second time by itself
        self.assertIsNone(result.pending_confirmation)

    def test_other_consequential_steps_keep_the_plan_approval_rule(self):
        runtime = self.runtime(Planner(plan(step("create_calendar_event", {"title": "SOP meeting", "start": "2026-10-26T15:00"},
                                                 "Add the meeting")), route=True))
        result = runtime.run_turn("Add the SOP meeting on Monday at 3 and then check it")
        self.assertEqual(result.pending_confirmation["tool"], "start_automation")  # the calendar change is in the plan
        runtime.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertIn("create_event", self.mail.calls)  # approved with the plan, as before


if __name__ == "__main__":
    unittest.main()
