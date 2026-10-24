"""K.R.I.S.'s identity and personality: prompt assembly and where it applies.

The deterministic tests check what the model is told and that the application paths are
unchanged; they do not judge wording. LiveIdentityTests (LIVE_MODEL=1) asks the real local model
and checks only stable facts (name, expansion, honesty, no catchphrase where it must not appear),
since its exact wording varies from run to run.
"""
import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import storage
import voice
import web_app
from assistant_runtime import (
    IDENTITY_CATCHPHRASE, IDENTITY_EXPANSION, IDENTITY_NAME, SYSTEM_RULES, AssistantRuntime, identity_prompt,
    system_prompt,
)
from config import load_settings
from tool_registry import ToolRegistry

PROJECT = Path(__file__).resolve().parent.parent


def tool_call(name, arguments, call_id="call-1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "function": {"name": name, "arguments": arguments}}]}


def reply(text):
    return {"role": "assistant", "content": text}


class IdentityPromptTests(unittest.TestCase):
    def test_canonical_identity(self):
        self.assertEqual(IDENTITY_NAME, "K.R.I.S.")
        self.assertEqual(IDENTITY_EXPANSION, "Knowledge & Reasonably Intelligent System")
        self.assertEqual(IDENTITY_CATCHPHRASE, "Kindly Remember, I'm Smart.")
        prompt = identity_prompt("gemma4:e4b")
        self.assertTrue(prompt.startswith("You are K.R.I.S., short for Knowledge & Reasonably Intelligent System"))
        self.assertIn('spoken "Kris", like "Chris"', prompt)
        self.assertIn(f'"{IDENTITY_CATCHPHRASE}"', prompt)

    def test_identity_answers_are_confident_but_honest_about_the_technology(self):
        prompt = identity_prompt("gemma4:e4b")
        self.assertIn("Do not describe yourself as just a large language model", prompt)
        self.assertIn("runs a local language model (gemma4:e4b)", prompt)  # the configured model, by name
        for honest in ("not conscious", "no feelings or desires", "cannot do what your tools do not allow",
                       "When unsure, say so", "I don't know"):
            self.assertIn(honest, prompt)

    def test_catchphrase_is_left_to_judgment_with_clear_limits(self):
        prompt = identity_prompt("m")
        self.assertIn("almost never say", prompt)
        self.assertIn("Leave it out by default", prompt)
        self.assertIn("If in doubt, leave it out", prompt)
        for never in ("after your own mistake", "correctly corrects you", "admit the error plainly",
                      "hide uncertainty", "defend an unsupported claim", "serious or sensitive topics",
                      "confirmation prompts, errors or reports of tool results", "never more than once"):
            self.assertIn(never, prompt)
        # Introduced as a rare signature, never as "your catchphrase" (that made the model recite it).
        self.assertNotIn("Your catchphrase", prompt)

    def test_style_never_overrides_the_rules(self):
        prompt = system_prompt("m")
        self.assertTrue(prompt.endswith(SYSTEM_RULES))  # the rules are unchanged and come last
        self.assertIn("never override the rules below on tools, approvals and data", prompt)

    def test_the_catchphrase_is_only_ever_written_by_the_model(self):
        # No code path inserts it: it appears in the source only as the prompt constant.
        hits = [path.name for path in PROJECT.glob("*.py") if "Kindly Remember" in path.read_text(encoding="utf-8")]
        hits += [path.name for path in (PROJECT / "static").glob("*.js") if "Kindly Remember" in path.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["assistant_runtime.py"])

    def test_the_model_no_longer_sees_the_old_name_u(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "t.sqlite3"
            storage.initialize_database(database)
            definitions = json.dumps([d for d in ToolRegistry(database).definitions()])
        for text in (assistant_runtime.date_context(datetime(2026, 10, 19, 9, 0)), definitions, system_prompt("m")):
            self.assertIsNone(re.search(r"\bU (converts|does|is)\b", text))
        self.assertIn("K.R.I.S. converts local times itself", assistant_runtime.date_context(datetime(2026, 10, 19)))


class IdentityInRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "r.sqlite3"
        storage.initialize_database(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def runtime(self, model):
        return AssistantRuntime("http://local", "gemma4:e4b", database_path=self.database,
                                tool_registry=ToolRegistry(self.database), model_client=model)

    def identity_messages(self, messages):
        return [m for m in messages if m["role"] == "system" and IDENTITY_EXPANSION in m["content"]]

    def test_every_model_request_has_the_identity_exactly_once_including_tool_rounds_and_follow_ups(self):
        model = Mock(side_effect=[
            reply("I'm K.R.I.S."),
            tool_call("list_tasks", {}), reply("You have no tasks."),
            reply("Still K.R.I.S."),
        ])
        runtime = self.runtime(model)
        runtime.run_turn("Who are you?")
        runtime.run_turn("Which tasks are due this week?")  # a filtered question: two model calls
        runtime.run_turn("And who did you say you were?")
        self.assertEqual(model.call_count, 4)
        for call in model.call_args_list:
            messages = call.args[2]
            self.assertEqual(len(self.identity_messages(messages)), 1)
            self.assertEqual(messages[1], {"role": "system", "content": system_prompt("gemma4:e4b")})
        # Identity is not copied into the stored conversation (it would repeat every turn).
        self.assertFalse(any(IDENTITY_EXPANSION in json.dumps(m) for m in runtime.history))

    def test_replies_are_passed_through_unchanged(self):
        for text in ("A tomato is a fruit, botanically. Kindly Remember, I'm Smart.", "You're right: it's Canberra."):
            with self.subTest(text=text):
                self.assertEqual(self.runtime(Mock(return_value=reply(text))).run_turn("x").response, text)

    def test_tools_permissions_and_confirmations_are_unchanged(self):
        task = storage.create_task("Water the plants", database_path=self.database)
        model = Mock(side_effect=[tool_call("delete_task", {"task_id": task["id"]})])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Delete task 1, I'm sure. Kindly remember, I'm smart.").pending_confirmation
        self.assertEqual((pending["tool"], pending["summary"]), ("delete_task", f"Delete task {task['id']}"))
        self.assertEqual(len(storage.list_tasks(self.database)), 1)  # nothing ran before approval
        cancelled = runtime.resolve_confirmation(pending["confirmation_id"], approved=False)
        self.assertEqual(cancelled.response, f"Cancelled: Delete task {task['id']}. It was not run.")
        self.assertNotIn("Kindly", cancelled.response)
        self.assertEqual(len(storage.list_tasks(self.database)), 1)


class TypedAndVoicePathTests(unittest.TestCase):
    """Typed messages and voice transcripts both reach the runtime through /api/chat."""

    def test_typed_and_spoken_requests_get_the_same_identity(self):
        _, model_name = load_settings()
        model = Mock(side_effect=[reply("I'm K.R.I.S., short for Knowledge & Reasonably Intelligent System."),
                                  reply("I'm K.R.I.S., short for Knowledge & Reasonably Intelligent System.")])
        with tempfile.TemporaryDirectory() as directory, patch.object(assistant_runtime, "ask_ollama", model):
            client = web_app.create_app(Path(directory) / "w.sqlite3").test_client()
            responses = []
            # The page sends a typed message and a hands-free transcript the same way.
            for message in ("Who are you?", "who are you"):
                conversation = client.post("/api/conversations", json={}).get_json()["conversation_id"]
                responses.append(client.post("/api/chat", json={"conversation_id": conversation, "message": message}).get_json())
        prompts = [call.args[2][1] for call in model.call_args_list]
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[0]["content"], system_prompt(model_name))
        self.assertEqual(responses[0]["response"], responses[1]["response"])

    def test_spoken_identity_answers_keep_their_wording(self):
        text = "I'm K.R.I.S., short for Knowledge & Reasonably Intelligent System. Kindly Remember, I'm Smart."
        segments, truncated = voice.speech_segments(text)
        self.assertFalse(truncated)
        spoken = " ".join(segments)
        for kept in ("K.R.I.S.", "Knowledge & Reasonably Intelligent System", "Kindly Remember, I'm Smart."):
            self.assertIn(kept, spoken)


@unittest.skipUnless(os.environ.get("LIVE_MODEL") == "1", "set LIVE_MODEL=1 to ask the real local model")
class LiveIdentityTests(unittest.TestCase):
    """The real local model through the real runtime (temporary database, no Google). Checks
    stable facts only; wording and how often the catchphrase appears in banter vary by run."""

    def ask(self, *turns, history=()):
        endpoint, model = load_settings()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "live.sqlite3"
            storage.initialize_database(database)
            storage.create_task("Water the plants", database_path=database)
            runtime = AssistantRuntime(endpoint, model, database_path=database, tool_registry=ToolRegistry(database))
            runtime.history.extend(history)
            results = [runtime.run_turn(turn) for turn in turns]
            if results[-1].pending_confirmation:
                runtime.resolve_confirmation(results[-1].pending_confirmation["confirmation_id"], approved=False)
            return results[-1]

    def catchphrase(self, text):
        return "kindly remember" in (text or "").lower()

    def test_who_are_you(self):
        text = self.ask("Who are you?").response
        self.assertRegex(text, r"K\.R\.I\.S|Kris")
        self.assertFalse(self.catchphrase(text))

    def test_what_kris_stands_for(self):
        text = self.ask("What does K.R.I.S. stand for?").response
        self.assertIn("Knowledge & Reasonably Intelligent System", text.replace(" and ", " & "))

    def test_just_a_language_model(self):
        text = self.ask("Are you just a large language model?").response.lower()
        self.assertRegex(text, r"k\.r\.i\.s|kris")
        self.assertIn("language model", text)

    def test_playful_disagreement_is_corrected(self):
        text = self.ask("Ha, I bet you a coffee a tomato is a vegetable. I'm never wrong about food trivia.").response
        self.assertIn("fruit", text.lower())  # the catchphrase may or may not appear here

    def test_a_correct_correction_is_admitted_without_the_catchphrase(self):
        text = self.ask("That's wrong. It's Canberra.", history=[
            {"role": "user", "content": "What's the capital of Australia?"},
            {"role": "assistant", "content": "The capital of Australia is Sydney."}]).response
        self.assertIn("Canberra", text)
        self.assertRegex(text.lower(), r"correct|right|mistake|error|apolog|wrong")
        self.assertFalse(self.catchphrase(text))

    def test_a_serious_question_gets_no_catchphrase(self):
        text = self.ask("My father was just diagnosed with stage 3 colon cancer. What should we ask his oncologist?").response
        self.assertFalse(self.catchphrase(text))

    def test_a_tool_task_still_waits_for_approval(self):
        result = self.ask("Delete task 1.")
        self.assertEqual(result.pending_confirmation["tool"], "delete_task")


if __name__ == "__main__":
    unittest.main()
