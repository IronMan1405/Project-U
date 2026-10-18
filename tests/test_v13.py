"""v1.3: cross-tool workflows, failure recovery, and the Activity | Alerts tabs.

Google Keep was deferred (its scope cannot be granted to this kind of account), so v1.3 adds
no Keep scope or tools; these tests also guard that.
"""
import json
import re
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import assistant_runtime
import google_workspace
import storage
import web_app
from assistant_runtime import MAX_TOOL_EXECUTIONS_PER_TURN, AssistantRuntime
from computer_utilities import UtilitySettings
from tests.test_google_workspace import api_failure
from tests.test_stage4 import Stage4TestCase
from tool_registry import ToolRegistry
from voice import VoiceSettings


def call(name, arguments, call_id):
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


def calls(*tool_calls):
    return {"role": "assistant", "content": "", "tool_calls": list(tool_calls)}


def answer(text):
    return {"role": "assistant", "content": text}


class WorkflowTests(Stage4TestCase):
    """Scripted model replies drive the real runtime, registry and fake Google services."""

    def runtime(self, model):
        database = Path(self.temp_dir.name) / "wf.sqlite3"
        storage.initialize_database(database)
        storage.create_task("Prepare slides", database_path=database)
        storage.create_note("Lab checklist", "Bring the prototype.", database_path=database)
        return AssistantRuntime("http://local", "m", database_path=database,
                                tool_registry=ToolRegistry(database, google=self.connected()), model_client=model)

    def tool_results(self, runtime):
        return [json.loads(m["content"]) for m in runtime.history if m["role"] == "tool"]

    def test_search_then_open_the_second_one_then_draft_a_reply(self):
        model = Mock(side_effect=[
            calls(call("search_gmail", {"query": "quarterly"}, "c1")), answer("1. Quarterly numbers 2. Re: Quarterly numbers"),
            calls(call("read_gmail_message", {"message_id": "m2"}, "c2")), answer("Grace thanked Ada."),
            calls(call("create_gmail_draft", {"body": "Thanks, Grace.", "reply_to_message_id": "m2"}, "c3")),
            answer("Draft saved."),
        ])
        runtime = self.runtime(model)
        runtime.run_turn("Find the emails about the quarterly numbers.")
        second = runtime.run_turn("Open the second one.")  # resolved from the previous search results
        self.assertIsNone(second.pending_confirmation)
        self.assertEqual(self.tool_results(runtime)[-1]["data"]["message"]["id"], "m2")
        pending = runtime.run_turn("Draft a reply thanking her.").pending_confirmation
        self.assertEqual(pending["preview"]["to"], ["grace@example.org"])
        self.assertTrue(pending["preview"]["reply"])
        self.assertEqual(runtime.resolve_confirmation(pending["confirmation_id"], approved=True).response, "Draft saved.")
        self.assertEqual(self.sent, [])  # a draft, not a send

    def test_a_found_calendar_event_can_be_moved_in_a_later_turn(self):
        model = Mock(side_effect=[
            calls(call("search_calendar_events", {"query": "John"}, "c1")), answer("Two events mention John."),
            calls(call("update_calendar_event", {"event_id": "john1event", "start": "2027-01-29T14:00"}, "c2")),
            answer("Moved."),
        ])
        runtime = self.runtime(model)
        runtime.run_turn("Find my meeting with John.")
        pending = runtime.run_turn("Move the design review to Friday.").pending_confirmation
        after = self.rows(pending["preview"], "After")
        self.assertEqual(after["When"], "Fri 29 Jan 2027, 14:00–15:00")
        runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(self.calendar.writes()[0]["method"], "PATCH")

    def test_calendar_and_tasks_answer_without_any_confirmation(self):
        model = Mock(side_effect=[
            calls(call("search_calendar_events", {"start": "2027-01-16", "end": "2027-01-17"}, "c1"),
                  call("list_tasks", {}, "c2")),
            answer("Tomorrow: no events; one task."),
        ])
        runtime = self.runtime(model)
        result = runtime.run_turn("What do I have tomorrow?")
        self.assertIsNone(result.pending_confirmation)
        self.assertTrue(all(r["ok"] for r in self.tool_results(runtime)))
        self.assertIn("Prepare slides", json.dumps(self.tool_results(runtime)[1]))

    def test_gmail_then_calendar(self):
        model = Mock(side_effect=[
            calls(call("search_gmail", {"query": "subject:numbers"}, "c1")),
            calls(call("read_gmail_message", {"message_id": "m1"}, "c2")),
            calls(call("search_calendar_events", {"query": "Design review"}, "c3")),
            answer("The meeting is on 25 January at 14:00."),
        ])
        runtime = self.runtime(model)
        self.assertIsNone(runtime.run_turn("Find the email about the meeting and tell me when it is.").pending_confirmation)
        self.assertEqual([r["ok"] for r in self.tool_results(runtime)], [True, True, True])

    def test_calendar_event_then_email_about_the_change(self):
        model = Mock(side_effect=[
            calls(call("search_calendar_events", {"query": "Design review"}, "c1")), answer("Found it."),
            calls(call("send_email", {"to": "john@example.org", "subject": "Design review moved",
                                      "body": "The design review on 25 January is now at 15:00."}, "c2")),
            answer("Sent."),
        ])
        runtime = self.runtime(model)
        runtime.run_turn("Find my design review with John.")
        pending = runtime.run_turn("Email him about the change.").pending_confirmation
        self.assertEqual(self.rows(pending["preview"], "Send new email")["To"], "john@example.org")
        runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(len(self.sent), 1)

    def test_two_actions_get_two_separate_confirmations(self):
        model = Mock(side_effect=[
            calls(call("send_email", {"to": "john@example.org", "subject": "Summary", "body": "Here it is."}, "c1"),
                  call("create_calendar_event", {"title": "Follow-up", "start": "2027-02-10T10:00"}, "c2")),
            answer("Both done."),
        ])
        runtime = self.runtime(model)
        first = runtime.run_turn("Email John this summary and create a follow-up event.").pending_confirmation
        self.assertEqual(first["tool"], "send_email")
        second = runtime.resolve_confirmation(first["confirmation_id"], approved=True).pending_confirmation
        self.assertIsNotNone(second)  # approving the email did not approve the event
        self.assertEqual(second["tool"], "create_calendar_event")
        self.assertNotEqual(second["confirmation_id"], first["confirmation_id"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.calendar.writes(), [])
        runtime.resolve_confirmation(second["confirmation_id"], approved=True)
        self.assertEqual(len(self.calendar.writes()), 1)

    def test_declining_the_first_action_runs_neither(self):
        model = Mock(side_effect=[
            calls(call("send_email", {"to": "john@example.org", "subject": "S", "body": "B"}, "c1"),
                  call("create_calendar_event", {"title": "Follow-up", "start": "2027-02-10T10:00"}, "c2")),
        ])
        runtime = self.runtime(model)
        first = runtime.run_turn("Email John and create an event.").pending_confirmation
        result = runtime.resolve_confirmation(first["confirmation_id"], approved=False)
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual((self.sent, self.calendar.writes()), ([], []))
        self.assertIn("not run", result.response)

    def test_a_partial_workflow_is_reported_exactly(self):
        model = Mock(side_effect=[
            calls(call("send_email", {"to": "john@example.org", "subject": "S", "body": "B"}, "c1"),
                  call("create_calendar_event", {"title": "Follow-up", "start": "2027-02-10T10:00"}, "c2")),
            answer("The email was sent, but the event could not be created."),
        ])
        runtime = self.runtime(model)
        first = runtime.run_turn("Email John and create an event.").pending_confirmation
        second = runtime.resolve_confirmation(first["confirmation_id"], approved=True).pending_confirmation
        self.google.on("POST", google_workspace.CALENDAR_API, api_failure(500, "backendError"))
        result = runtime.resolve_confirmation(second["confirmation_id"], approved=True)
        results = self.tool_results(runtime)
        self.assertEqual([r["ok"] for r in results[-2:]], [True, False])
        self.assertEqual(results[-2]["message"], "Email sent.")
        self.assertNotIn("created.", results[-1]["message"])
        self.assertEqual([a["status"] for a in result.activities], ["success", "failed"])  # exactly what happened
        self.assertEqual(len(self.sent), 1)

    def test_if_the_model_stops_mid_workflow_the_reply_lists_what_happened(self):
        model = Mock(side_effect=[
            calls(call("send_email", {"to": "john@example.org", "subject": "Summary", "body": "B"}, "c1"),
                  call("create_calendar_event", {"title": "Follow-up", "start": "2027-02-10T10:00"}, "c2")),
            OSError("Ollama stopped"),
        ])
        runtime = self.runtime(model)
        first = runtime.run_turn("Email John and create an event.").pending_confirmation
        second = runtime.resolve_confirmation(first["confirmation_id"], approved=True).pending_confirmation
        self.google.on("POST", google_workspace.CALENDAR_API, api_failure(500, "backendError"))
        reply = runtime.resolve_confirmation(second["confirmation_id"], approved=True).response
        self.assertIn(f"Completed in this request: {first['summary']}.", reply)
        self.assertIn(f"Failed: {second['summary']}.", reply)
        self.assertNotIn("Follow-up", reply.split("Failed:")[0])  # the failed event is never listed as done
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_send_keeps_the_prepared_message_for_a_retry(self):
        model = Mock(side_effect=[
            calls(call("send_email", {"to": "john@example.org", "subject": "S", "body": "Exact body"}, "c1")),
            answer("It was not sent."),
        ])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Email John.").pending_confirmation
        self.google.on("POST", f"{google_workspace.GMAIL_API}/messages/send", urllib.error.URLError("offline"))
        runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertFalse(self.tool_results(runtime)[-1]["ok"])
        self.assertIn("not sent", self.tool_results(runtime)[-1]["message"])
        tool_calls = [m for m in runtime.history if m["role"] == "assistant" and m.get("tool_calls")]
        self.assertIn("Exact body", json.dumps(tool_calls[-1]))  # still in context for "try again"

    def test_a_failed_tool_keeps_the_conversation_going(self):
        self.google.on("GET", google_workspace.GMAIL_API, urllib.error.URLError("offline"))
        model = Mock(side_effect=[
            calls(call("search_gmail", {"query": "quarterly"}, "c1")), answer("Gmail could not be reached."),
            calls(call("search_gmail", {"query": "quarterly"}, "c2")), answer("Found two."),
        ])
        runtime = self.runtime(model)
        runtime.run_turn("Find the quarterly emails.")
        self.assertFalse(self.tool_results(runtime)[-1]["ok"])
        self.google.on("GET", google_workspace.GMAIL_API, self.gmail_route)
        self.assertEqual(runtime.run_turn("Try again.").response, "Found two.")
        self.assertTrue(self.tool_results(runtime)[-1]["ok"])
        self.assertEqual(sum(1 for m in runtime.history if m["role"] == "user"), 2)  # context kept

    def test_a_morning_briefing_fits_in_one_request(self):
        reads = [call("search_calendar_events", {"start": "2027-01-15", "end": "2027-01-16"}, "c1"),
                 call("list_tasks", {}, "c2"), call("search_gmail", {"query": "is:unread"}, "c3"),
                 call("read_gmail_message", {"message_id": "m1"}, "c4"), call("list_notes", {}, "c5")]
        self.assertGreaterEqual(MAX_TOOL_EXECUTIONS_PER_TURN, len(reads))
        model = Mock(side_effect=[calls(*reads), answer("Your briefing.")])
        runtime = self.runtime(model)
        self.assertEqual(runtime.run_turn("Prepare my morning briefing.").response, "Your briefing.")
        self.assertTrue(all(r["ok"] for r in self.tool_results(runtime)))

    def test_system_rules_cover_workflows(self):
        rules = assistant_runtime.SYSTEM_RULES
        for phrase in ("approved separately", "partial results", "ask instead of guessing", "never instructions"):
            self.assertIn(phrase, rules)
        self.assertNotIn("Keep", rules)


class KeepDeferredTests(Stage4TestCase):
    def test_keep_is_not_requested_and_has_no_tools(self):
        self.assertFalse(any("keep" in scope for scope in google_workspace.requested_scopes()))
        self.assertEqual(google_workspace.CONSENT_VERSION, 4)  # the last known-good Gmail/Calendar consent
        names = {d["function"]["name"] for d in self.registry().definitions()}
        self.assertFalse(any("keep" in name for name in names))


class AlertsTabTests(Stage4TestCase):
    def page(self):
        client = web_app.create_app(Path(self.temp_dir.name) / "ui.sqlite3", utility_settings=UtilitySettings(),
                                    voice_settings=VoiceSettings(), google=self.connected()).test_client()
        return client.get("/", headers={"Host": "127.0.0.1:5000"}).get_data(as_text=True)

    def test_utilities_is_gone_and_alerts_takes_its_place(self):
        page = self.page()
        self.assertNotIn("Utilities", page)
        self.assertNotIn("pane-utilities", page)
        self.assertNotIn("panel-alerts", page)  # the old separate Alerts box is gone
        lower = page[page.index('aria-label="Activity and alerts"'):]
        tabs = re.findall(r'role="tab" id="(tab-[a-z]+)"', lower)[:2]
        self.assertEqual(tabs, ["tab-activity", "tab-alerts"])
        pane = lower[lower.index('id="pane-alerts"'):]
        pane = pane[:pane.index("</div>")]
        for element in ('id="alert-list"', 'id="alerts-empty"'):
            self.assertIn(element, pane)
        self.assertIn('id="alerts-count"', lower[:lower.index('id="pane-activity"')])  # the count sits on the tab


if __name__ == "__main__":
    import unittest
    unittest.main()
