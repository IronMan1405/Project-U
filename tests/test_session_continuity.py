"""Session continuity (session_state.py): the recent session survives a reload or a restart, holds only
metadata and IDs, expires, and never turns an old approval into a new one. Temporary databases only;
a "restart" is a second runtime or app built on the same database."""

import json
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import automation
import session_state
import storage
import web_app
from automation import AutomationRun, Step
from developer import DeveloperCompanion
from google_accounts import GoogleAccounts
from session_state import SessionError
from timers import TimerService
from tool_registry import ToolRegistry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automation import Planner, plan, step  # noqa: E402
from test_google_accounts import FakeProvider  # noqa: E402


def reply(text):
    return {"role": "assistant", "content": text}


def tool_call(name, arguments=None, call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments or {}}}]}


def roundtrip(state):
    return json.loads(json.dumps(state))  # exactly what the database holds


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "s.sqlite3"
        storage.initialize_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, model=None, planner=None, google=None):
        registry = ToolRegistry(self.database, google=google, timers=TimerService(self.database))
        return assistant_runtime.AssistantRuntime(
            "http://local", "m", database_path=self.database, tool_registry=registry,
            model_client=model or Mock(side_effect=AssertionError("no model call expected")),
            planner_client=planner or Planner())


class RuntimeContinuityTests(Case):
    def test_turns_and_working_context_carry_into_the_next_request_after_a_restart(self):
        first = self.runtime(Mock(side_effect=[tool_call("create_task", {"title": "Buy servos"}), reply("Added Buy servos.")]))
        first.run_turn("Add a task to buy servos")
        state = roundtrip(first.session_state())

        model = Mock(return_value=reply("It has no due date yet."))
        second = self.runtime(model)
        second.restore_session(state)
        second.run_turn("When is it due?")
        messages = model.call_args.args[2]
        conversation = [m for m in messages if m["role"] != "system"]
        self.assertEqual(conversation[:2], [{"role": "user", "content": "Add a task to buy servos"},
                                            {"role": "assistant", "content": "Added Buy servos."}])
        note = next(m["content"] for m in messages if m["content"].startswith("Working context"))
        self.assertIn('task 1: "Buy servos", created', note)  # the item, by its ID, from the restored context
        self.assertEqual(len(storage.list_tasks(self.database)), 1)  # restoring ran nothing

    def test_a_proposal_survives_a_restart_and_do_it_resumes_it_with_approval(self):
        def helping(planner):
            original = planner.__call__
            return lambda messages, schema: ({"mode": "help", "multi_step": True} if schema is automation.ROUTE_SCHEMA
                                             else original(messages, schema))

        first = self.runtime(planner=helping(Planner(
            plan(step("list_tasks", purpose="Check tasks"),
                 step("start_timer", {"kind": "focus", "minutes": 45}, "Start a 45-minute focus session")), route=True)))
        proposed = first.run_turn("Help me plan my afternoon: check my tasks and then a focus session")
        self.assertIn("I can do this next", proposed.response)
        state = roundtrip(first.session_state())
        self.assertEqual(state["run"]["steps"][1]["arguments"], {"kind": "focus", "minutes": 45})

        second = self.runtime(planner=Planner(
            {"action": "run", "tool": "start_timer", "arguments": {"kind": "focus", "minutes": 45}}))
        second.restore_session(state)
        self.assertEqual(second.registry.timers.active(), [])
        result = second.run_turn("Do it")
        self.assertEqual(result.pending_confirmation["tool"], "start_automation")  # the plan is approved again
        self.assertEqual(second.registry.timers.active(), [])
        second.resolve_confirmation(result.pending_confirmation["confirmation_id"], True)
        self.assertEqual(len(second.registry.timers.active()), 1)

    def test_a_restored_consequential_step_needs_its_confirmation_and_old_approvals_are_dropped(self):
        provider = FakeProvider("me@x.org")
        first = self.runtime(google=GoogleAccounts.single(provider))
        first.session_turns.append(session_state.turn_record("Help me reply to the professor", "I can send it.",
                                                             datetime.now().astimezone(), False))
        first.runs.append(AutomationRun("r1", "Send the professor a reply saying Monday works", status=automation.PAUSED,
                                        propose_only=True, finished_at=datetime.now().astimezone().isoformat(), steps=[
            Step("search_gmail", {"query": "from:prof@x.org"}, "Find the email", status="success", message="Found 1 message."),
            Step("send_email", {"to": "prof@x.org", "subject": "Re: SOP", "body": "SECRET BODY Monday works"},
                 "Send the reply", depends_on=[0], approved=True, approved_summary="Send email to prof@x.org: Re: SOP")]))
        state = roundtrip(first.session_state())
        self.assertNotIn("SECRET BODY", json.dumps(state))
        self.assertIsNone(state["run"]["steps"][1]["arguments"])  # the body is never stored
        # Even a tampered state cannot carry an approval or a body in.
        state["run"]["steps"][1].update(approved=True, approved_summary="x",
                                        arguments={"to": "prof@x.org", "subject": "Re", "body": "Injected"})

        decision = {"action": "run", "tool": "send_email",
                    "arguments": {"to": "prof@x.org", "subject": "Re: SOP", "body": "Monday works."}}
        second = self.runtime(planner=Planner(decision, decision), google=GoogleAccounts.single(provider))
        second.restore_session(state)
        restored = second.runs[0]
        self.assertEqual([(s.approved, s.approved_summary, s.arguments) for s in restored.steps][1], (False, None, None))
        result = second.run_turn("Do it")
        pending = result.pending_confirmation
        # "Do it" alone sends nothing: the plan with the send needs a new explicit approval in this session.
        # (Approving a plan whose send is already concrete counts as that send's approval; that is the
        # automation engine's existing rule and is unchanged here.)
        self.assertEqual(pending["tool"], "start_automation")
        self.assertIn(["Step 2", "Send the reply — needs approval"], pending["preview"]["sections"][0]["rows"])
        self.assertNotIn("send_message", provider.calls)
        second.resolve_confirmation(pending["confirmation_id"], False)  # declined: still nothing sent
        self.assertNotIn("send_message", provider.calls)
        self.assertNotIn("Injected", json.dumps(provider.calls))

    def test_restoring_runs_nothing_and_an_unusable_run_is_refused(self):
        first = self.runtime()
        first.session_turns.append(session_state.turn_record("Hi", "Hello.", datetime.now().astimezone(), False))
        first.runs.append(AutomationRun("r1", "x", status=automation.PAUSED, steps=[Step("list_tasks", {}, "Check")]))
        state = roundtrip(first.session_state())
        state["run"]["steps"][0]["tool"] = "run_shell_command"  # not a tool K.R.I.S. has
        second = self.runtime()
        second.registry.execute = Mock(side_effect=AssertionError("nothing may run on restore"))
        with self.assertRaises(SessionError):
            second.restore_session(state)
        state["run"]["status"] = automation.EXECUTING  # a run caught mid-way is never resumed
        state["run"]["steps"][0]["tool"] = "list_tasks"
        with self.assertRaises(SessionError):
            self.runtime().restore_session(state)


class ExpiryTests(Case):
    def test_an_old_session_is_refused_and_old_items_still_expire(self):
        runtime = self.runtime(Mock(side_effect=[tool_call("create_task", {"title": "Buy servos"}), reply("Added.")]))
        runtime.run_turn("Add a task to buy servos")
        state = roundtrip(runtime.session_state())

        old = dict(state, saved_at=(datetime.now().astimezone() - timedelta(minutes=session_state.SESSION_MAX_AGE_MINUTES + 1)).isoformat())
        with self.assertRaisesRegex(SessionError, "expired"):
            self.runtime().restore_session(old)

        # A recent session keeps its items' own times: an item last seen too long ago is dropped.
        stale = (datetime.now().astimezone() - timedelta(minutes=130)).isoformat(timespec="seconds")
        state["context"]["items"].append({"kind": "email", "key": "m9", "label": '"Old" from x', "status": "found",
                                          "source": "search_gmail", "turn": state["context"]["turn"], "at": stale,
                                          "account": None})
        restored = self.runtime()
        restored.restore_session(state)
        self.assertIn(("task", "1"), restored.context.items)
        self.assertNotIn(("email", "m9"), restored.context.items)

    def test_the_store_keeps_only_recent_sessions(self):
        storage.save_session("a", "{}", self.database)
        with storage._connection(self.database) as connection:
            connection.execute("UPDATE sessions SET updated_at = ?",
                               ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),))
        storage.save_session("b", '{"x": 1}', self.database)
        self.assertEqual(storage.latest_session(7200, self.database), ("b", '{"x": 1}'))
        with storage._connection(self.database) as connection:
            self.assertEqual([r[0] for r in connection.execute("SELECT conversation_id FROM sessions")], ["b"])  # "a" deleted


class PrivacyTests(Case):
    def test_email_and_note_bodies_and_tool_data_are_never_stored(self):
        provider = FakeProvider("me@x.org")
        provider.read_message = lambda message_id, include_thread=False: {"message": {
            "id": message_id, "from": "Prof <prof@x.org>", "subject": "SOP review", "body": "SECRET-EMAIL-BODY"}}
        model = Mock(side_effect=[
            tool_call("read_gmail_message", {"message_id": "msg1"}), reply("He wrote: SECRET-EMAIL-BODY"),
            tool_call("create_note", {"title": "Camera idea", "content": "SECRET-NOTE-BODY"}, "c2"), reply("Saved: SECRET-NOTE-BODY"),
            tool_call("create_note", {"title": "Other", "content": "SECRET-NOTE-BODY again"}, "c3"),
            reply("Paris."),
        ])
        runtime = self.runtime(model, google=GoogleAccounts.single(provider))
        runtime.registry._seen_message_ids.add((runtime.registry.google.default, "msg1"))
        runtime.run_turn("Read the professor's email")
        saved = runtime.run_turn("Save a note called Camera idea: stereo depth")
        if saved.pending_confirmation:
            runtime.resolve_confirmation(saved.pending_confirmation["confirmation_id"], True)
        declined = runtime.run_turn("Also save a note called Other")  # waiting for approval, then declined
        if declined.pending_confirmation:
            runtime.resolve_confirmation(declined.pending_confirmation["confirmation_id"], False)
        runtime.run_turn("Capital of France?")
        text = json.dumps(runtime.session_state())
        for secret in ("SECRET-EMAIL-BODY", "SECRET-NOTE-BODY", "access_token", "refresh_token", "audio"):
            self.assertNotIn(secret, text)
        state = json.loads(text)
        self.assertEqual(set(state), {"version", "saved_at", "turns", "context", "run"})
        self.assertEqual([t["reply"] for t in state["turns"]], [None, None, None, "Paris."])  # replies that could quote content are dropped
        self.assertEqual({key for turn in state["turns"] for key in turn}, {"user", "reply", "at"})
        labels = [item["label"] for item in state["context"]["items"]]
        self.assertIn('"SOP review" from Prof <prof@x.org>', labels)  # the email by ID and subject only
        self.assertIn('"Camera idea"', labels)


class WebContinuityTests(Case):
    def app(self, model):
        with patch.object(assistant_runtime, "ask_ollama", model):
            return web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion())

    def test_reload_restart_and_new_conversation(self):
        model = Mock(side_effect=[reply("Canberra."), reply("About 460,000 people."), reply("Fresh start.")])
        with patch.object(assistant_runtime, "ask_ollama", model):
            client = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion()).test_client()
            started = client.post("/api/conversations", json={"restore": True}).get_json()
            self.assertNotIn("restored", started)  # nothing to continue yet
            conversation = started["conversation_id"]
            client.post("/api/chat", json={"conversation_id": conversation, "message": "What's the capital of Australia?"})

            reloaded = client.post("/api/conversations", json={"restore": True}).get_json()  # a page reload
            self.assertEqual(reloaded["conversation_id"], conversation)
            self.assertEqual(reloaded["restored"]["turns"][0]["reply"], "Canberra.")

            restarted = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion()).test_client()
            again = restarted.post("/api/conversations", json={"restore": True}).get_json()  # after a restart
            self.assertEqual(again["conversation_id"], conversation)
            answer = restarted.post("/api/chat", json={"conversation_id": conversation, "message": "How many people live there?"})
            self.assertEqual(answer.get_json()["response"], "About 460,000 people.")
            sent = [m for m in model.call_args.args[2] if m["role"] != "system"]
            self.assertEqual(sent[:2], [{"role": "user", "content": "What's the capital of Australia?"},
                                        {"role": "assistant", "content": "Canberra."}])

            fresh = restarted.post("/api/conversations", json={"previous_id": conversation}).get_json()  # New conversation
            self.assertNotEqual(fresh["conversation_id"], conversation)
            self.assertNotIn("restored", restarted.post("/api/conversations", json={"restore": True}).get_json())

    def test_a_waiting_approval_is_shown_again_after_a_reload_but_not_approved(self):
        task = storage.create_task("Old thing", database_path=self.database)
        model = Mock(side_effect=[tool_call("delete_task", {"task_id": task["id"]})])
        with patch.object(assistant_runtime, "ask_ollama", model):
            client = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion()).test_client()
            conversation = client.post("/api/conversations", json={}).get_json()["conversation_id"]
            # A turn must have finished for there to be a session: a short exchange first.
            model.side_effect = [reply("Hello."), tool_call("delete_task", {"task_id": task["id"]})]
            client.post("/api/chat", json={"conversation_id": conversation, "message": "Hi"})
            pending = client.post("/api/chat", json={"conversation_id": conversation, "message": "Delete the task Old thing"})
            self.assertEqual(pending.get_json()["pending_confirmation"]["tool"], "delete_task")
            reloaded = client.post("/api/conversations", json={"restore": True}).get_json()
        self.assertEqual(reloaded["restored"]["pending_confirmation"]["tool"], "delete_task")
        self.assertEqual(len(storage.list_tasks(self.database)), 1)  # still waiting for Confirm

    def test_corrupt_or_foreign_saved_state_starts_fresh_without_crashing(self):
        for raw in ("not json", json.dumps({"version": 99}), json.dumps({"version": 1, "saved_at": "nonsense"}),
                    json.dumps({"version": 1, "saved_at": datetime.now().astimezone().isoformat(), "turns": [],
                                "context": {"items": "wrong"}})):
            with self.subTest(raw=raw[:30]):
                broken = str(uuid.uuid4())
                storage.save_session(broken, raw, self.database)
                client = self.app(Mock()).test_client()
                response = client.post("/api/conversations", json={"restore": True})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("restored", response.get_json())
                self.assertNotEqual(response.get_json()["conversation_id"], broken)
                self.assertIsNone(storage.latest_session(7200, self.database))  # the broken row is gone


if __name__ == "__main__":
    unittest.main()
