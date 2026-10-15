import json
import tempfile
import io
import socket
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock

import assistant_runtime
import main
import storage
import tool_registry
import web_app
from assistant_runtime import AssistantRuntime, RuntimeBusyError
from tool_registry import MAX_NOTE_CONTENT_RESULT, ToolError, ToolRegistry, validate_http_url


def assistant_tool_call(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {"index": 0, "name": name, "arguments": arguments},
            }
        ],
    }


class AssistantRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "runtime.sqlite3"
        storage.initialize_database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def runtime(self, model_client):
        return AssistantRuntime(
            "http://local",
            "test-model",
            database_path=self.database_path,
            model_client=model_client,
        )

    def test_ordinary_chat_uses_runtime_without_tool_calls(self):
        model = Mock(return_value={"role": "assistant", "content": "Hello."})
        runtime = self.runtime(model)

        result = runtime.run_turn("Hi U")

        self.assertEqual(result.response, "Hello.")
        self.assertEqual(result.activities, [])
        self.assertTrue(model.call_args.args[3])
        self.assertEqual(runtime.history[-2:], [
            {"role": "user", "content": "Hi U"},
            {"role": "assistant", "content": "Hello."},
        ])

    def test_valid_task_tool_executes_once_and_returns_native_tool_message(self):
        model = Mock(
            side_effect=[
                assistant_tool_call(
                    "create_task", {"title": "Water plants", "description": ""}
                ),
                {"role": "assistant", "content": "Created task 1."},
            ]
        )
        events = []
        runtime = AssistantRuntime(
            "http://local",
            "test-model",
            database_path=self.database_path,
            model_client=model,
            activity_callback=events.append,
        )

        result = runtime.run_turn("Please create a task to water the plants.")

        self.assertEqual(result.response, "Created task 1.")
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)
        self.assertEqual(storage.list_tasks(self.database_path)[0]["title"], "Water plants")
        second_messages = model.call_args_list[1].args[2]
        assistant_call = next(message for message in second_messages if message.get("tool_calls"))
        tool_result = next(message for message in second_messages if message["role"] == "tool")
        self.assertEqual(assistant_call["tool_calls"][0]["function"]["name"], "create_task")
        self.assertEqual(tool_result["tool_name"], "create_task")
        self.assertTrue(json.loads(tool_result["content"])["ok"])
        self.assertIn({"status": "running", "tool": "create_task", "summary": "Create task: Water plants"}, events)
        self.assertEqual(events[-1]["status"], "idle")

    def test_duplicate_call_id_does_not_repeat_a_write(self):
        first_response = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "same", "function": {"name": "create_task", "arguments": {"title": "Once"}}},
                {"id": "same", "function": {"name": "create_task", "arguments": {"title": "Twice"}}},
            ],
        }
        model = Mock(side_effect=[first_response, {"role": "assistant", "content": "One task was added."}])
        runtime = self.runtime(model)

        result = runtime.run_turn("Create a task called Once.")

        self.assertEqual(result.response, "One task was added.")
        tasks = storage.list_tasks(self.database_path)
        self.assertEqual([task["title"] for task in tasks], ["Once"])
        self.assertEqual(result.activities[1]["status"], "rejected")

    def test_multiple_tool_calls_are_returned_before_final_answer(self):
        first_response = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "one", "function": {"name": "list_tasks", "arguments": {}}},
                {"id": "two", "function": {"name": "list_notes", "arguments": {}}},
            ],
        }
        model = Mock(
            side_effect=[first_response, {"role": "assistant", "content": "There are no tasks or notes."}]
        )
        result = self.runtime(model).run_turn("Check my tasks and notes.")

        self.assertEqual(result.response, "There are no tasks or notes.")
        self.assertEqual(len(result.activities), 2)
        second_messages = model.call_args_list[1].args[2]
        tool_results = [message for message in second_messages if message["role"] == "tool"]
        self.assertEqual([message["tool_name"] for message in tool_results], ["list_tasks", "list_notes"])

    def test_unknown_tools_and_invalid_arguments_are_rejected(self):
        first_response = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "unknown", "function": {"name": "run_shell", "arguments": {}}},
                {"id": "bad", "function": {"name": "create_task", "arguments": {"title": ""}}},
            ],
        }
        model = Mock(side_effect=[first_response, {"role": "assistant", "content": "No changes made."}])
        runtime = self.runtime(model)

        result = runtime.run_turn("What is saved?")

        self.assertEqual(result.response, "No changes made.")
        self.assertEqual(result.activities[0]["status"], "rejected")
        self.assertEqual(result.activities[1]["status"], "rejected")
        self.assertEqual(storage.list_tasks(self.database_path), [])

    def test_tool_failure_is_returned_to_model_without_crashing(self):
        model = Mock(
            side_effect=[
                assistant_tool_call("get_note", {"note_id": 999}),
                {"role": "assistant", "content": "There is no note with that ID."},
            ]
        )
        runtime = self.runtime(model)

        result = runtime.run_turn("Read note 999.")

        self.assertEqual(result.response, "There is no note with that ID.")
        self.assertEqual(result.activities[0]["status"], "failed")
        tool_result = next(message for message in model.call_args_list[1].args[2] if message["role"] == "tool")
        self.assertFalse(json.loads(tool_result["content"])["ok"])

    def test_execution_limit_is_four(self):
        count = 0

        def model_client(endpoint, model, messages, tools):
            nonlocal count
            if tools is None:
                return {"role": "assistant", "content": "Stopped after the tool limit."}
            count += 1
            return assistant_tool_call("list_tasks", {}, f"call-{count}")

        runtime = self.runtime(model_client)
        result = runtime.run_turn("Keep checking my tasks.")

        self.assertEqual(result.response, "Stopped after the tool limit.")
        self.assertEqual(len(result.activities), assistant_runtime.MAX_TOOL_EXECUTIONS_PER_TURN)
        self.assertEqual(count, assistant_runtime.MAX_TOOL_EXECUTIONS_PER_TURN)

    def test_delete_requires_confirmation_and_cancel_does_not_execute(self):
        task = storage.create_task("Keep me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": task["id"]}),
                {"role": "assistant", "content": "Deletion was cancelled."},
            ]
        )
        runtime = self.runtime(model)

        pending = runtime.run_turn(f"Delete task {task['id']}.")
        self.assertIsNone(pending.response)
        self.assertEqual(pending.pending_confirmation["tool"], "delete_task")
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

        result = runtime.resolve_confirmation(pending.pending_confirmation["confirmation_id"], False)
        self.assertEqual(result.response, f"Cancelled: Delete task {task['id']}. It was not run.")
        self.assertEqual(model.call_count, 1)  # the reply is fixed text, not a model interpretation
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(pending.pending_confirmation["confirmation_id"], True)

    def multi_call(self, *calls):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"call-{index}", "function": {"name": name, "arguments": arguments}}
                for index, (name, arguments) in enumerate(calls)
            ],
        }

    def assert_every_tool_call_has_a_result(self, history):
        calls = sum(len(m.get("tool_calls") or []) for m in history if m["role"] == "assistant")
        self.assertEqual(calls, sum(m["role"] == "tool" for m in history))

    def test_cancel_skips_queued_actions_and_replies_deterministically(self):
        keep = storage.create_task("Keep me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                self.multi_call(
                    ("delete_task", {"task_id": keep["id"]}),
                    ("create_task", {"title": "Queued write"}),
                    ("set_task_completed", {"task_id": keep["id"], "completed": True}),
                ),
                {"role": "assistant", "content": "Next turn answer."},
            ]
        )
        runtime = self.runtime(model)
        pending = runtime.run_turn("Please create a task and delete task 1.")
        result = runtime.resolve_confirmation(pending.pending_confirmation["confirmation_id"], False)

        self.assertEqual(
            result.response,
            f"Cancelled: Delete task {keep['id']}. It was not run. 2 other requested actions were not run either.",
        )
        self.assertEqual(model.call_count, 1)
        tasks = storage.list_tasks(self.database_path)
        self.assertEqual([(t["title"], t["completed"]) for t in tasks], [("Keep me", False)])
        self.assertEqual(
            [(a["tool"], a["status"]) for a in result.activities],
            [("delete_task", "cancelled"), ("create_task", "skipped"), ("set_task_completed", "skipped")],
        )
        self.assert_every_tool_call_has_a_result(runtime.history)
        self.assertEqual(runtime.history[-1], {"role": "assistant", "content": result.response})

        follow_up = runtime.run_turn("What now?")
        self.assertEqual(follow_up.response, "Next turn answer.")

    def test_cancel_after_an_earlier_action_ran_says_so(self):
        target = storage.create_task("Old task", database_path=self.database_path)
        model = Mock(
            side_effect=[
                self.multi_call(
                    ("create_task", {"title": "New task"}),
                    ("delete_task", {"task_id": target["id"]}),
                ),
            ]
        )
        runtime = self.runtime(model)
        pending = runtime.run_turn("Please create a task called New task and delete the old one.")
        self.assertEqual(len(storage.list_tasks(self.database_path)), 2)
        result = runtime.resolve_confirmation(pending.pending_confirmation["confirmation_id"], False)
        self.assertEqual(
            result.response,
            f"Cancelled: Delete task {target['id']}. It was not run. "
            "Actions that already completed in this request are listed in the activity above.",
        )
        self.assertEqual(sorted(t["title"] for t in storage.list_tasks(self.database_path)), ["New task", "Old task"])
        self.assertEqual([a["status"] for a in result.activities], ["success", "cancelled"])
        self.assert_every_tool_call_has_a_result(runtime.history)

    def test_terminal_decline_skips_queued_actions(self):
        keep = storage.create_task("Keep me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                self.multi_call(
                    ("delete_task", {"task_id": keep["id"]}),
                    ("create_task", {"title": "Queued write"}),
                ),
            ]
        )
        runtime = self.runtime(model)
        prompts = []
        result = runtime.run_turn(
            "Please create a task and delete task 1.",
            confirm_callback=lambda summary: prompts.append(summary) or False,
        )
        self.assertEqual(prompts, [f"Delete task {keep['id']}"])
        self.assertEqual(
            result.response,
            f"Cancelled: Delete task {keep['id']}. It was not run. 1 other requested action was not run either.",
        )
        self.assertEqual([t["title"] for t in storage.list_tasks(self.database_path)], ["Keep me"])
        self.assertEqual(model.call_count, 1)
        self.assert_every_tool_call_has_a_result(runtime.history)

    def test_hidden_thinking_is_not_stored_but_tool_calls_are_kept_exactly(self):
        call = assistant_tool_call("list_tasks", {})
        call["thinking"] = "SECRET-REASONING " * 50
        call["content"] = None
        model = Mock(side_effect=[call, {"role": "assistant", "content": "None yet.", "thinking": "more"}])
        runtime = self.runtime(model)
        runtime.run_turn("What tasks do I have?")

        stored_call = runtime.history[1]
        self.assertEqual(stored_call, {"role": "assistant", "content": "", "tool_calls": call["tool_calls"]})
        self.assertNotIn("SECRET-REASONING", json.dumps(runtime.history))
        second_request = model.call_args_list[1].args[2]
        self.assertNotIn("SECRET-REASONING", json.dumps(second_request))
        self.assertEqual(runtime.history[-1], {"role": "assistant", "content": "None yet."})

    def test_history_is_trimmed_by_whole_turns_and_keeps_the_newest(self):
        big_result = "x" * 900

        def model(endpoint, model_name, messages, tools):
            last = messages[-1]
            if last["role"] == "user":
                return assistant_tool_call("get_system_info", {}, call_id=f"call-{len(messages)}")
            return {"role": "assistant", "content": f"Answer {len(messages)} " + big_result}

        runtime = self.runtime(model)
        with unittest.mock.patch.object(assistant_runtime, "MAX_HISTORY_CHARS", 6000), unittest.mock.patch.object(
            assistant_runtime, "HISTORY_TRIM_TARGET_CHARS", 3600
        ):
            for index in range(12):
                runtime.run_turn(f"Question {index}")
                history = runtime.history
                size = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
                # Trimming happens before each new turn, so at most one turn exceeds the bound.
                self.assertEqual(history[0]["role"], "user")
                self.assert_every_tool_call_has_a_result(history)
                for position, message in enumerate(history):
                    if message["role"] == "tool":
                        self.assertIn(history[position - 1]["role"], ("assistant", "tool"))
                self.assertEqual(history[-4]["content"], f"Question {index}")
                self.assertLess(size, 6000 + 4000)
            kept_questions = [m["content"] for m in runtime.history if m["role"] == "user"]
            self.assertLess(len(kept_questions), 12)
            self.assertEqual(kept_questions[-1], "Question 11")
            self.assertEqual(kept_questions, [f"Question {i}" for i in range(12 - len(kept_questions), 12)])

    def test_a_single_oversized_turn_is_never_split_or_dropped(self):
        runtime = self.runtime(Mock(return_value={"role": "assistant", "content": "ok"}))
        huge = "y" * 5000
        runtime.history.extend(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "list_tasks", "arguments": {}}}]},
                {"role": "tool", "tool_name": "list_tasks", "content": huge},
                {"role": "assistant", "content": huge},
            ]
        )
        with unittest.mock.patch.object(assistant_runtime, "MAX_HISTORY_CHARS", 1000), unittest.mock.patch.object(
            assistant_runtime, "HISTORY_TRIM_TARGET_CHARS", 600
        ):
            runtime.run_turn("second")
            # The only earlier turn is the newest completed one at trim time: it is kept whole.
            self.assertEqual([m["content"] for m in runtime.history if m["role"] == "user"], ["first", "second"])
            self.assert_every_tool_call_has_a_result(runtime.history)
            runtime.run_turn("third")
            # Now "first" is older than the newest completed turn and is dropped whole.
            self.assertEqual([m["content"] for m in runtime.history if m["role"] == "user"], ["second", "third"])
            self.assertNotIn(huge, json.dumps(runtime.history))

    def test_new_message_cancels_pending_action_and_queued_calls(self):
        keep = storage.create_task("Keep me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                self.multi_call(
                    ("delete_task", {"task_id": keep["id"]}),
                    ("create_task", {"title": "Queued write"}),
                ),
                {"role": "assistant", "content": "Here is the March invoice instead."},
            ]
        )
        runtime = self.runtime(model)
        pending = runtime.run_turn("Please create a task and delete task 1.")
        token = pending.pending_confirmation["confirmation_id"]

        result = runtime.run_turn("No, never mind that. Show me the March invoice.")

        self.assertEqual(
            result.notice,
            f"Cancelled: Delete task {keep['id']}. It was not run because you sent a new message. "
            "1 other requested action was not run either.",
        )
        self.assertEqual(result.response, "Here is the March invoice instead.")
        self.assertEqual(
            [(a["tool"], a["status"]) for a in result.activities],
            [("delete_task", "cancelled"), ("create_task", "skipped")],
        )
        self.assertEqual([t["title"] for t in storage.list_tasks(self.database_path)], ["Keep me"])
        self.assertFalse(runtime.has_pending_confirmation)
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(token, True)
        self.assertEqual([t["title"] for t in storage.list_tasks(self.database_path)], ["Keep me"])
        self.assert_every_tool_call_has_a_result(runtime.history)
        roles = [m["role"] for m in runtime.history]
        self.assertEqual(roles, ["user", "assistant", "tool", "tool", "assistant", "user", "assistant"])
        self.assertEqual(runtime.history[4]["content"], result.notice)
        # The new turn's model request sees the cancellation, then the new message.
        new_request = [m for m in model.call_args_list[1].args[2] if m["role"] != "system"]
        self.assertEqual(new_request[-2:], runtime.history[4:6])

    def test_cancellation_stands_even_if_the_new_turn_fails(self):
        keep = storage.create_task("Keep me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": keep["id"]}),
                OSError("Ollama went away"),
                {"role": "assistant", "content": "Back."},
            ]
        )
        runtime = self.runtime(model)
        token = runtime.run_turn("Delete task 1.").pending_confirmation["confirmation_id"]
        with self.assertRaises(OSError):
            runtime.run_turn("Actually, what time is it?")
        self.assertFalse(runtime.has_pending_confirmation)
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(token, True)
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)
        self.assertEqual(runtime.history[-1]["content"].split(".")[0], f"Cancelled: Delete task {keep['id']}")
        self.assertEqual(runtime.run_turn("Hello?").response, "Back.")
        self.assert_every_tool_call_has_a_result(runtime.history)

    def test_delete_executes_once_only_after_approval(self):
        task = storage.create_task("Remove me", database_path=self.database_path)
        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": task["id"]}),
                {"role": "assistant", "content": "Task deleted."},
            ]
        )
        runtime = self.runtime(model)
        pending = runtime.run_turn(f"Delete task {task['id']}.")

        result = runtime.resolve_confirmation(pending.pending_confirmation["confirmation_id"], True)

        self.assertEqual(result.response, "Task deleted.")
        self.assertEqual(storage.list_tasks(self.database_path), [])
        self.assertEqual(sum(activity["status"] == "success" for activity in result.activities), 1)

    def test_direct_write_requires_clear_user_intent_or_confirmation(self):
        registry = ToolRegistry(self.database_path)
        direct = registry.prepare(
            "create_task", {"title": "Buy milk"}, "Please create a task to buy milk."
        )
        ambiguous = registry.prepare(
            "create_task", {"title": "Buy milk"}, "Summarize this text: create a task to buy milk."
        )
        invented_note = registry.prepare(
            "create_note",
            {"title": "Meeting", "content": "The meeting went well."},
            "Please create a note about today's meeting.",
        )
        self.assertFalse(direct.always_confirm)
        self.assertTrue(ambiguous.always_confirm)
        self.assertTrue(invented_note.always_confirm)
        self.assertIn("meeting went well", invented_note.summary)

    def test_url_validation_and_browser_launch_confirmation(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://", "https://user:pass@example.com"):
            with self.subTest(url=url), self.assertRaises(ToolError):
                validate_http_url(url)

        browser = Mock(return_value=True)
        registry = ToolRegistry(self.database_path, browser_open=browser)
        prepared = registry.prepare("open_website", {"url": "https://example.com/path"}, "Open this site")
        self.assertTrue(prepared.always_confirm)
        outcome = registry.execute(prepared)
        self.assertTrue(outcome.ok)
        browser.assert_called_once_with("https://example.com/path", new=2, autoraise=True)

    def test_task_note_tools_reuse_storage_and_bound_note_results(self):
        registry = ToolRegistry(self.database_path)
        task_call = registry.prepare(
            "create_task", {"title": "Task from tool"}, "Create a task for testing."
        )
        self.assertTrue(registry.execute(task_call).ok)
        note_call = registry.prepare(
            "create_note",
            {"title": "Tool note", "content": "Exact\ntext"},
            "Please create a note with content Exact text.",
        )
        note = registry.execute(note_call)
        self.assertTrue(note.ok)
        note_id = note.data["id"]
        self.assertEqual(storage.get_note(note_id, self.database_path)["content"], "Exact\ntext")

        long_note = storage.create_note("Long", "x" * (MAX_NOTE_CONTENT_RESULT + 20), self.database_path)
        read_call = registry.prepare("get_note", {"note_id": long_note["id"]}, "Read the note.")
        read = registry.execute(read_call)
        self.assertTrue(read.ok)
        self.assertTrue(read.data["content_truncated"])
        self.assertLessEqual(len(read.data["content"]), MAX_NOTE_CONTENT_RESULT)

    def test_task_completion_and_note_edit_use_existing_storage(self):
        task = storage.create_task("Review proposal", database_path=self.database_path)
        note = storage.create_note("Draft", "Original text", self.database_path)
        registry = ToolRegistry(self.database_path)

        complete = registry.prepare(
            "set_task_completed",
            {"task_id": task["id"], "completed": True},
            "Please complete task 1.",
        )
        self.assertFalse(complete.always_confirm)
        self.assertTrue(registry.execute(complete).ok)
        self.assertTrue(storage.list_tasks(self.database_path)[0]["completed"])

        edit = registry.prepare(
            "update_note",
            {"note_id": note["id"], "title": "Updated draft", "content": "Revised text"},
            "Please edit note 1 to say Revised text.",
        )
        self.assertFalse(edit.always_confirm)
        self.assertTrue(registry.execute(edit).ok)
        self.assertEqual(storage.get_note(note["id"], self.database_path)["content"], "Revised text")

    def test_due_date_tool_schemas_describe_and_enforce_format(self):
        registry = ToolRegistry(self.database_path)
        definitions = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in registry.definitions()
        }
        create_due = definitions["create_task"]["properties"]["due_at"]
        update_due = definitions["set_task_due_date"]["properties"]["due_at"]
        for schema in (create_due, update_due):
            self.assertEqual(schema["pattern"], storage.TOOL_DUE_AT_PATTERN)
            self.assertIn("YYYY-MM-DD HH:MM", schema["description"])
        self.assertEqual(update_due["type"], ["string", "null"])
        self.assertEqual(definitions["set_task_due_date"]["required"], ["task_id", "due_at"])

        pattern_schema = {
            "type": "object",
            "properties": {"code": {"type": "string", "pattern": r"^[a-z]{3}$"}},
        }
        self.assertEqual(
            tool_registry.validate_arguments(pattern_schema, {"code": "abc"}), {"code": "abc"}
        )
        with self.assertRaises(ToolError):
            tool_registry.validate_arguments(pattern_schema, {"code": "abcd"})

        for name, arguments in (
            ("create_task", {"title": "Bad", "due_at": "2026-02-30"}),
            ("create_task", {"title": "Bad", "due_at": "Friday"}),
            ("create_task", {"title": "Bad", "due_at": None}),
            ("create_task", {"title": "Bad", "due_at": "2026-11-03 5:00"}),
            ("set_task_due_date", {"task_id": 1, "due_at": "2026-11-03 10:00 PM"}),
            ("set_task_due_date", {"task_id": 1, "due_at": "2026-04-31"}),
            ("set_task_due_date", {"task_id": 1}),
            ("set_task_due_date", {"task_id": 1, "due_at": 3}),
        ):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolError):
                registry.prepare(name, arguments, "Please set the due date of task 1.")

    def test_task_due_date_tools_set_change_clear_and_preserve(self):
        registry = ToolRegistry(self.database_path)
        create = registry.prepare(
            "create_task",
            {"title": "File taxes", "due_at": "2026-11-03"},
            "Please create a task to file taxes due 2026-11-03.",
        )
        self.assertFalse(create.always_confirm)
        self.assertEqual(create.summary, "Create task: File taxes (due Tue 2026-11-03)")
        task_id = registry.execute(create).data["id"]
        self.assertEqual(storage.list_tasks(self.database_path)[0]["due_at"], "2026-11-03")

        change = registry.prepare(
            "set_task_due_date",
            {"task_id": task_id, "due_at": "2026-11-04 17:00:00Z"},
            "Please change the due date of task 1 to Nov 4 at 5pm UTC.",
        )
        self.assertFalse(change.always_confirm)
        local = storage.describe_due_at("2026-11-04 17:00:00Z")
        self.assertEqual(change.summary, f"Set due date of task {task_id} to {local}")
        changed = registry.execute(change)
        self.assertTrue(changed.ok)
        self.assertEqual(changed.data["due_at"], "2026-11-04 17:00:00Z")
        self.assertEqual(changed.data["due_local"], local)
        self.assertEqual(changed.message, f"Task due date set to {local}.")

        complete = registry.prepare(
            "set_task_completed", {"task_id": task_id, "completed": True}, "Please complete task 1."
        )
        self.assertEqual(registry.execute(complete).data["due_at"], "2026-11-04 17:00:00Z")
        reopen = registry.prepare(
            "set_task_completed", {"task_id": task_id, "completed": False}, "Please reopen task 1."
        )
        self.assertEqual(registry.execute(reopen).data["due_at"], "2026-11-04 17:00:00Z")

        clear = registry.prepare(
            "set_task_due_date",
            {"task_id": task_id, "due_at": None},
            "Please remove the due date from task 1.",
        )
        self.assertFalse(clear.always_confirm)
        self.assertEqual(clear.summary, f"Clear due date of task {task_id}")
        cleared = registry.execute(clear)
        self.assertEqual(cleared.message, "Task due date cleared.")
        self.assertIsNone(storage.list_tasks(self.database_path)[0]["due_at"])
        self.assertIs(storage.list_tasks(self.database_path)[0]["completed"], False)

        implied = registry.prepare(
            "set_task_due_date",
            {"task_id": task_id, "due_at": "2026-12-01"},
            "What do I have going on in December?",
        )
        self.assertTrue(implied.always_confirm)
        missing = registry.execute(
            registry.prepare(
                "set_task_due_date",
                {"task_id": 999, "due_at": None},
                "Please clear the due date on task 999.",
            )
        )
        self.assertFalse(missing.ok)
        self.assertEqual(missing.message, "Task not found.")

    def test_runtime_gives_model_current_date_for_relative_due_dates(self):
        from datetime import datetime, timedelta, timezone

        fixed_now = datetime(2026, 10, 15, 14, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        model = Mock(
            side_effect=[
                assistant_tool_call(
                    "create_task", {"title": "Call bank", "due_at": "2026-10-16"}
                ),
                {"role": "assistant", "content": "Added for tomorrow."},
            ]
        )
        runtime = AssistantRuntime(
            "http://local",
            "test-model",
            database_path=self.database_path,
            model_client=model,
            clock=lambda: fixed_now,
        )

        result = runtime.run_turn("Please add a task to call the bank tomorrow.")

        self.assertEqual(result.response, "Added for tomorrow.")
        first_messages = model.call_args_list[0].args[2]
        date_context = next(
            message["content"]
            for message in first_messages
            if message["role"] == "system" and "Current local date" in message["content"]
        )
        self.assertIn("2026-10-15", date_context)
        self.assertIn("Thursday", date_context)
        self.assertIn("UTC+05:30", date_context)
        self.assertIn("'tomorrow at 5 AM' -> 2026-10-16 05:00", date_context)
        self.assertEqual(storage.list_tasks(self.database_path)[0]["due_at"], "2026-10-16")

    def test_date_context_gives_local_due_at_examples_for_any_offset(self):
        from datetime import datetime, timedelta, timezone

        cases = (
            # (local now, offset, tomorrow, tomorrow 5 AM local, today 6 PM local)
            (datetime(2026, 10, 15, 14, 5), timedelta(hours=5, minutes=30),
             "2026-10-16", "2026-10-16 05:00", "2026-10-15 18:00"),
            (datetime(2026, 10, 15, 22, 0), timedelta(hours=-7),
             "2026-10-16", "2026-10-16 05:00", "2026-10-15 18:00"),
            (datetime(2026, 12, 31, 9, 0), timedelta(0),
             "2027-01-01", "2027-01-01 05:00", "2026-12-31 18:00"),
        )
        for local_now, offset, tomorrow, tomorrow_5am, today_6pm in cases:
            now = local_now.replace(tzinfo=timezone(offset))
            with self.subTest(now=now.isoformat()):
                context = assistant_runtime.date_context(now)
                self.assertIn(f"{local_now:%A %Y-%m-%d %H:%M}", context)
                self.assertIn(f"'tomorrow' -> {tomorrow};", context)
                self.assertIn(f"'tomorrow at 5 AM' -> {tomorrow_5am};", context)
                self.assertIn(f"'today at 6 PM' -> {today_6pm}.", context)
                self.assertNotIn("Z;", context)
                for example in (tomorrow, tomorrow_5am, today_6pm):
                    self.assertTrue(storage.normalize_due_at(example))

        context = assistant_runtime.date_context(cases[0][0].replace(tzinfo=timezone(cases[0][1])))
        for instruction in (
            "due_at is a machine-format value, never natural language",
            "Specific times are supported",
            "never drop a time the user gave",
            "'YYYY-MM-DD HH:MM' in 24-hour local time",
            "Do not convert to UTC or apply the offset",
            "Relative days and clock times are not ambiguous",
            "use the local time the tool result gives",
        ):
            self.assertIn(instruction, context)

    def test_due_at_tool_descriptions_ask_for_local_time_not_utc(self):
        registry = ToolRegistry(self.database_path)
        functions = {item["function"]["name"]: item["function"] for item in registry.definitions()}
        for name in ("create_task", "set_task_due_date"):
            with self.subTest(tool=name):
                self.assertIn("specific time", functions[name]["description"])
                description = functions[name]["parameters"]["properties"]["due_at"]["description"]
                self.assertIn("Specific times are supported", description)
                self.assertIn("'YYYY-MM-DD HH:MM' (24-hour)", description)
                self.assertIn("do not convert to UTC", description)
                self.assertIn("never natural language", description)
                self.assertNotIn("ask the user", description)

        created = registry.execute(
            registry.prepare(
                "create_task",
                {"title": "Work out", "due_at": "2026-10-16 05:00"},
                "Please add a task to work out tomorrow at 5am.",
            )
        )
        stored = storage.list_tasks(self.database_path)[0]["due_at"]
        self.assertEqual(stored, storage.normalize_due_at("2026-10-16 05:00"))
        self.assertTrue(stored.endswith("Z"))
        self.assertIn("05:00", created.data["due_local"])

        # Natural language is still rejected, with format guidance for the model.
        for value in ("tomorrow at 5am", "5am", "2026-10-16 5am"):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as raised:
                    registry.prepare(
                        "set_task_due_date",
                        {"task_id": created.data["id"], "due_at": value},
                        "Please set the due date of task 1 to tomorrow at 5am.",
                    )
                self.assertIn("YYYY-MM-DD HH:MM", str(raised.exception))

    def test_system_info_is_structured_and_reports_unavailable_values(self):
        registry = ToolRegistry(self.database_path)
        def which(name):
            return {
                "pmset": "/usr/bin/pmset",
                "sysctl": "/usr/sbin/sysctl",
                "vm_stat": "/usr/bin/vm_stat",
            }.get(name)

        def run(command, **kwargs):
            if command[0].endswith("pmset"):
                output = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t84%; discharging; 2:10 remaining"
            elif command[0].endswith("sysctl"):
                output = "17179869184\n"
            else:
                output = (
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free: 100.\nPages inactive: 200.\nPages speculative: 50.\n"
                )
            return Mock(returncode=0, stdout=output)

        with (
            unittest.mock.patch("tool_registry.sys_platform_is_macos", return_value=True),
            unittest.mock.patch("tool_registry.shutil.which", side_effect=which),
            unittest.mock.patch("tool_registry.os.sysconf", side_effect=ValueError("unsupported")),
            unittest.mock.patch("tool_registry.subprocess.run", side_effect=run),
        ):
            outcome = registry.execute(registry.prepare("get_system_info", {}, "What is my battery status?"))

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.data["local_datetime"])
        self.assertIn("timezone", outcome.data)
        self.assertEqual(outcome.data["battery"]["percentage"], 84)
        self.assertIn("total_bytes", outcome.data["memory"])
        self.assertIn("free_bytes", outcome.data["disk"])

    def test_large_tool_results_are_bounded_and_truthful(self):
        outcome = tool_registry.ToolOutcome(True, "Task created.", {"data": "x" * 20000})
        serialized = outcome.as_content()
        self.assertLessEqual(len(serialized), tool_registry.MAX_RESULT_CHARS)
        self.assertTrue(json.loads(serialized)["ok"])
        self.assertTrue(json.loads(serialized)["data"]["result_truncated"])

    def test_document_context_is_separate_from_persistent_turn_history(self):
        model = Mock(return_value={"role": "assistant", "content": "Found the warranty."})
        runtime = self.runtime(model)
        private_text = "UNIQUE_DOCUMENT_SENTINEL_9481"

        result = runtime.run_turn(
            "What is the warranty?",
            additional_context=[{"role": "system", "content": private_text}],
        )

        sent_messages = model.call_args.args[2]
        self.assertIn({"role": "system", "content": private_text}, sent_messages)
        # Document context sits just before the current user message, after earlier turns.
        self.assertEqual(sent_messages[-2]["content"], private_text)
        self.assertEqual(sent_messages[-1], {"role": "user", "content": "What is the warranty?"})
        self.assertNotIn(private_text, json.dumps(runtime.history))
        self.assertEqual(result.response, "Found the warranty.")

    def test_prompt_prefix_is_stable_across_turns_for_prompt_caching(self):
        from datetime import datetime, timedelta, timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        times = iter([datetime(2026, 10, 15, 9, minute, tzinfo=ist) for minute in (1, 1, 2, 7, 7)])
        model = Mock(
            side_effect=[
                assistant_tool_call("list_tasks", {}),
                {"role": "assistant", "content": "You have no tasks."},
                {"role": "assistant", "content": "Second answer."},
                assistant_tool_call("list_tasks", {}, call_id="call-2"),
                {"role": "assistant", "content": "Third answer."},
            ]
        )
        runtime = AssistantRuntime(
            "http://local", "test-model", database_path=self.database_path,
            model_client=model, clock=lambda: next(times),
        )
        runtime.run_turn("What tasks do I have?")
        runtime.run_turn("Thanks", additional_context=[{"role": "system", "content": "DOC-A excerpt"}])
        runtime.run_turn("Check again", additional_context=[{"role": "system", "content": "DOC-B excerpt"}])

        calls = [call.args[2] for call in model.call_args_list]
        # Every request starts with the same fixed prefix.
        for messages in calls:
            self.assertEqual(messages[1], {"role": "system", "content": assistant_runtime.SYSTEM_RULES})
            self.assertNotIn("Current local date", messages[1]["content"])
        # Each turn's request begins with the previous turn's full request prefix (minus
        # the per-request context), so the cached earlier turns are reused unchanged.
        first_turn_history = runtime.history[:4]
        self.assertEqual(calls[2][2:6], first_turn_history)
        self.assertEqual(calls[3][2:6], first_turn_history)
        self.assertEqual(calls[3][:8], calls[4][:8])
        for messages, minute, document in (
            (calls[0], "09:01", None), (calls[2], "09:02", "DOC-A excerpt"), (calls[3], "09:07", "DOC-B excerpt"),
        ):
            context = [m["content"] for m in messages if m["role"] == "system" and "Current local date" in m["content"]]
            self.assertEqual(len(context), 1)
            self.assertIn(minute, context[0])
            user_index = max(i for i, m in enumerate(messages) if m["role"] == "user")
            volatile = [m["content"] for m in messages[user_index - 2 : user_index]]
            self.assertIn(context[0], volatile)
            if document:
                self.assertEqual(messages[user_index - 1]["content"], document)
        stored = json.dumps(runtime.history)
        for volatile_text in ("Current local date", "DOC-A", "DOC-B"):
            self.assertNotIn(volatile_text, stored)

    def test_post_execution_model_failure_does_not_report_or_repeat_success(self):
        model = Mock(side_effect=[assistant_tool_call("create_task", {"title": "One-time task"}), OSError("local connection failed")])
        runtime = self.runtime(model)

        result = runtime.run_turn("Create a task named One-time task.")

        self.assertIn("Review the activity result before retrying", result.response)
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)


class RuntimeWebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "web.sqlite3"
        self.app = web_app.create_app(self.database_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_web_delete_waits_for_cancel_or_confirmation(self):
        task = storage.create_task("Keep this", database_path=self.database_path)
        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": task["id"]}),
                {"role": "assistant", "content": "Deletion was cancelled."},
            ]
        )
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            chat = self.client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "Delete task 1."}
            )
            pending = chat.get_json()["pending_confirmation"]
            self.assertEqual(chat.status_code, 200)
            self.assertEqual(pending["tool"], "delete_task")
            activity = self.client.get(f"/api/conversations/{conversation_id}/activity")
            self.assertEqual(activity.get_json()["activity"]["status"], "confirmation")
            canceled = self.client.post(
                "/api/confirm",
                json={"conversation_id": conversation_id, "confirmation_id": pending["confirmation_id"], "approved": False},
            )
            self.assertEqual(canceled.status_code, 200)
            self.assertEqual(canceled.get_json()["response"], f"Cancelled: Delete task {task['id']}. It was not run.")
            self.assertEqual(model.call_count, 1)
            duplicate = self.client.post(
                "/api/confirm",
                json={"conversation_id": conversation_id, "confirmation_id": pending["confirmation_id"], "approved": True},
            )

        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

    def start_pending_delete(self, model):
        task = storage.create_task("Keep this", database_path=self.database_path)
        conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
        chat = self.client.post("/api/chat", json={"conversation_id": conversation_id, "message": "Delete task 1."})
        return task, conversation_id, chat.get_json()["pending_confirmation"]["confirmation_id"]

    def test_web_new_message_cancels_pending_and_invalidates_its_id(self):
        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": 1}),
                {"role": "assistant", "content": "Sure, here is something else."},
            ]
        )
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model):
            task, conversation_id, token = self.start_pending_delete(model)
            response = self.client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "Do something else"}
            )
            stale = self.client.post(
                "/api/confirm",
                json={"conversation_id": conversation_id, "confirmation_id": token, "approved": True},
            )
            activity = self.client.get(f"/api/conversations/{conversation_id}/activity").get_json()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["notice"], f"Cancelled: Delete task {task['id']}. It was not run because you sent a new message.")
        self.assertEqual(body["response"], "Sure, here is something else.")
        self.assertIsNone(body["pending_confirmation"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(activity["activity"]["status"], "idle")
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

    def test_web_confirm_in_progress_wins_over_a_new_message(self):
        started, release = threading.Event(), threading.Event()
        real_delete = tool_registry.delete_task

        def slow_delete(*args, **kwargs):
            started.set()
            release.wait(5)
            return real_delete(*args, **kwargs)

        model = Mock(
            side_effect=[
                assistant_tool_call("delete_task", {"task_id": 1}),
                {"role": "assistant", "content": "Deleted."},
                {"role": "assistant", "content": "Next."},
            ]
        )
        results = {}
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model), unittest.mock.patch.object(
            tool_registry, "delete_task", side_effect=slow_delete
        ) as delete:
            task, conversation_id, token = self.start_pending_delete(model)
            confirm = threading.Thread(
                target=lambda: results.setdefault("confirm", self.client.post(
                    "/api/confirm", json={"conversation_id": conversation_id, "confirmation_id": token, "approved": True}
                ))
            )
            confirm.start()
            self.assertTrue(started.wait(5))
            racing = self.client.post("/api/chat", json={"conversation_id": conversation_id, "message": "Cancel!"})
            release.set()
            confirm.join(5)
            after = self.client.post("/api/chat", json={"conversation_id": conversation_id, "message": "Next?"})
        self.assertEqual(racing.status_code, 409)
        self.assertEqual(results["confirm"].status_code, 200)
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(storage.list_tasks(self.database_path), [])
        self.assertEqual(after.status_code, 200)
        self.assertIsNone(after.get_json()["notice"])

    def test_web_new_message_in_progress_wins_over_a_stale_confirm(self):
        started, release = threading.Event(), threading.Event()
        responses = iter([assistant_tool_call("delete_task", {"task_id": 1})])

        def model(*args):
            try:
                return next(responses)
            except StopIteration:
                started.set()
                release.wait(5)
                return {"role": "assistant", "content": "Answered the new message."}

        results = {}
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", side_effect=model):
            task, conversation_id, token = self.start_pending_delete(model)
            chat = threading.Thread(
                target=lambda: results.setdefault("chat", self.client.post(
                    "/api/chat", json={"conversation_id": conversation_id, "message": "Something else"}
                ))
            )
            chat.start()
            self.assertTrue(started.wait(5))
            racing_confirm = self.client.post(
                "/api/confirm", json={"conversation_id": conversation_id, "confirmation_id": token, "approved": True}
            )
            second_message = self.client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "And another"}
            )
            release.set()
            chat.join(5)
            late_confirm = self.client.post(
                "/api/confirm", json={"conversation_id": conversation_id, "confirmation_id": token, "approved": True}
            )
        self.assertEqual(racing_confirm.status_code, 409)
        self.assertEqual(second_message.status_code, 409)
        self.assertEqual(results["chat"].status_code, 200)
        self.assertIn("because you sent a new message", results["chat"].get_json()["notice"])
        self.assertEqual(late_confirm.status_code, 409)
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

    def test_web_activity_is_visible_during_model_request(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_model(endpoint, model, messages, tools):
            started.set()
            release.wait(timeout=3)
            return {"role": "assistant", "content": "Finished."}

        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", blocked_model):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            response_holder = []

            def send_chat():
                with self.app.test_client() as client:
                    response_holder.append(
                        client.post("/api/chat", json={"conversation_id": conversation_id, "message": "Hello"})
                    )

            thread = threading.Thread(target=send_chat)
            thread.start()
            self.assertTrue(started.wait(timeout=3))
            activity = self.client.get(f"/api/conversations/{conversation_id}/activity")
            self.assertEqual(activity.get_json()["activity"]["status"], "thinking")
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(response_holder[0].status_code, 200)

    def test_web_open_website_requires_confirmation_before_launch(self):
        model = Mock(
            side_effect=[
                assistant_tool_call("open_website", {"url": "https://example.com"}),
                {"role": "assistant", "content": "The browser accepted the URL."},
            ]
        )
        browser = Mock(return_value=True)
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model), unittest.mock.patch(
            "tool_registry.webbrowser.open", browser
        ):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            chat = self.client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "Open https://example.com"}
            )
            pending = chat.get_json()["pending_confirmation"]
            self.assertEqual(chat.status_code, 200)
            browser.assert_not_called()
            response = self.client.post(
                "/api/confirm",
                json={"conversation_id": conversation_id, "confirmation_id": pending["confirmation_id"], "approved": True},
            )

        self.assertEqual(response.status_code, 200)
        browser.assert_called_once_with("https://example.com", new=2, autoraise=True)


class OllamaFailureTests(unittest.TestCase):
    """Ollama failures must return clear errors and leave the conversation usable."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "failure.sqlite3"
        with unittest.mock.patch.object(web_app, "load_settings", return_value=("http://ollama.test", "test-model")):
            self.app = web_app.create_app(self.database_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def chat(self, conversation_id, message):
        return self.client.post("/api/chat", json={"conversation_id": conversation_id, "message": message})

    def http_error(self, code, body):
        return urllib.error.HTTPError("http://ollama.test/api/chat", code, "error", {}, io.BytesIO(body))

    def test_failures_return_clear_status_and_the_next_turn_recovers(self):
        cases = [
            ("connection refused", urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), 503, "Could not connect to Ollama"),
            ("request timeout", TimeoutError("timed out"), 503, "Could not connect to Ollama: timed out"),
            ("socket timeout", socket.timeout("timed out"), 503, "Could not connect to Ollama"),
            ("malformed JSON", json.JSONDecodeError("Expecting value", "<html>", 0), 502, "Could not read Ollama's response"),
            ("unexpected response", ValueError("Ollama returned an unexpected response."), 502, "Could not read Ollama's response"),
            ("HTTP 500", self.http_error(500, b'{"error": "model runner crashed"}'), 502, "Ollama request failed (HTTP 500): model runner crashed"),
            ("HTTP 404 plain text", self.http_error(404, b"not found"), 502, "Ollama request failed (HTTP 404): not found"),
        ]
        for label, error, status, message in cases:
            with self.subTest(case=label):
                model = Mock(side_effect=[error, {"role": "assistant", "content": "Back online."}])
                with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model):
                    conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
                    failed = self.chat(conversation_id, "First question")
                    activity = self.client.get(f"/api/conversations/{conversation_id}/activity").get_json()
                    recovered = self.chat(conversation_id, "Second question")

                self.assertEqual(failed.status_code, status)
                self.assertIn(message, failed.get_json()["error"])
                self.assertEqual(activity["activity"]["status"], "idle")
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.get_json()["response"], "Back online.")
                # The failed turn left nothing behind: the retry sends only the new message.
                retry_messages = model.call_args_list[1].args[2]
                conversation = [m for m in retry_messages if m["role"] != "system"]
                self.assertEqual(conversation, [{"role": "user", "content": "Second question"}])

    def test_malformed_tool_calls_fail_cleanly_without_side_effects(self):
        model = Mock(
            side_effect=[
                {"role": "assistant", "content": "", "tool_calls": "create_task"},
                {"role": "assistant", "content": 42},
                {"role": "assistant", "content": "Fine now."},
            ]
        )
        with unittest.mock.patch.object(assistant_runtime, "ask_ollama", model):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            bad_calls = self.chat(conversation_id, "Please create a task to test")
            bad_content = self.chat(conversation_id, "Hello")
            good = self.chat(conversation_id, "Hello again")
        self.assertEqual(bad_calls.status_code, 502)
        self.assertEqual(bad_content.status_code, 502)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(storage.list_tasks(self.database_path), [])
        conversation = [m for m in model.call_args_list[2].args[2] if m["role"] != "system"]
        self.assertEqual(conversation, [{"role": "user", "content": "Hello again"}])

    def test_health_check_failures(self):
        def tags(body):
            response = MagicMock()
            response.__enter__.return_value = io.BytesIO(body)
            return response

        cases = [
            ("down", urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), 503, "Ollama is unavailable"),
            ("timeout", TimeoutError("timed out"), 503, "Ollama is unavailable: timed out"),
            ("invalid JSON", tags(b"<html>"), 503, "Ollama is unavailable"),
            ("JSON list", tags(b'["test-model"]'), 503, "unexpected health response"),
            ("model missing", tags(b'{"models": [{"name": "other:latest"}]}'), 503, "'test-model' is not available"),
        ]
        for label, outcome, status, message in cases:
            with self.subTest(case=label):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with unittest.mock.patch.object(web_app.urllib.request, "urlopen", **kwargs):
                    response = self.client.get("/api/health")
                self.assertEqual(response.status_code, status)
                self.assertFalse(response.get_json()["connected"])
                self.assertIn(message, response.get_json()["error"])

        with unittest.mock.patch.object(
            web_app.urllib.request, "urlopen", return_value=tags(b'{"models": [{"name": "test-model"}]}')
        ):
            healthy = self.client.get("/api/health")
        self.assertEqual((healthy.status_code, healthy.get_json()), (200, {"connected": True, "model": "test-model"}))


class TerminalRuntimeTests(unittest.TestCase):
    def test_terminal_creates_task_through_shared_runtime(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        database_path = Path(temp_dir.name) / "terminal.sqlite3"
        storage.initialize_database(database_path)
        prompts = iter(["Please create a task to water plants.", "/quit"])
        responses = [
            assistant_tool_call("create_task", {"title": "Water plants"}),
            {"role": "assistant", "content": "Task created."},
        ]
        original_runtime = AssistantRuntime

        def runtime_factory(endpoint, model, model_client, activity_callback, tool_registry):
            # Keep the terminal's utility settings but point storage at the test database.
            return original_runtime(
                endpoint,
                model,
                tool_registry=ToolRegistry(database_path, utilities=tool_registry.utilities),
                model_client=model_client,
                activity_callback=activity_callback,
            )

        with (
            unittest.mock.patch.object(main, "load_settings", return_value=("local", "model")),
            unittest.mock.patch.object(main, "initialize_database"),
            unittest.mock.patch.object(main, "AssistantRuntime", side_effect=runtime_factory),
            unittest.mock.patch.object(main, "ask_ollama", side_effect=responses),
            unittest.mock.patch("builtins.input", side_effect=lambda _: next(prompts)),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main.main(), 0)

        tasks = storage.list_tasks(database_path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Water plants")


if __name__ == "__main__":
    unittest.main()