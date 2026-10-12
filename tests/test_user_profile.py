import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main
import user_profile
import web_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProfileLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.temp_dir.name) / "profile.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_profile_uses_defaults(self):
        self.assertEqual(user_profile.load_profile(self.profile_path), user_profile.DEFAULT_PROFILE)

    def test_valid_profile_loads_and_normalizes_values(self):
        expected = {
            "response_style": "warm and concise",
            "explanation_detail": "step by step",
            "interests": ["gardening", "music"],
        }
        self.profile_path.write_text(json.dumps(expected), encoding="utf-8")

        self.assertEqual(user_profile.load_profile(self.profile_path), expected)

    def test_malformed_and_invalid_profiles_use_defaults(self):
        invalid_profiles = [
            "{not json",
            json.dumps({"response_style": 3}),
            json.dumps({"interests": "music"}),
            json.dumps(["not", "an", "object"]),
        ]
        for contents in invalid_profiles:
            with self.subTest(contents=contents):
                self.profile_path.write_text(contents, encoding="utf-8")
                with self.assertLogs("user_profile", level="WARNING"):
                    self.assertEqual(
                        user_profile.load_profile(self.profile_path), user_profile.DEFAULT_PROFILE
                    )

    def test_unreadable_profile_path_uses_defaults(self):
        self.profile_path.mkdir()
        with self.assertLogs("user_profile", level="WARNING"):
            self.assertEqual(
                user_profile.load_profile(self.profile_path), user_profile.DEFAULT_PROFILE
            )

    def test_profile_is_loaded_again_in_a_new_process(self):
        expected = {
            "response_style": "precise",
            "explanation_detail": "brief",
            "interests": [],
        }
        self.profile_path.write_text(json.dumps(expected), encoding="utf-8")
        script = (
            "import json, sys; from user_profile import load_profile; "
            "print(json.dumps(load_profile(sys.argv[1])))"
        )

        result = subprocess.run(
            [sys.executable, "-c", script, str(self.profile_path)],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(json.loads(result.stdout), expected)


class SharedProfileContextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.temp_dir.name) / "profile.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_profile(self, detail):
        self.profile_path.write_text(
            json.dumps(
                {
                    "response_style": "clear",
                    "explanation_detail": detail,
                    "interests": [],
                }
            ),
            encoding="utf-8",
        )

    def test_changed_profile_and_prior_turns_reach_ollama(self):
        self._write_profile("brief")
        conversation = []
        with patch.object(user_profile, "PROFILE_PATH", self.profile_path), patch.object(
            main, "ask_ollama", side_effect=["first answer", "second answer"]
        ) as ask:
            main.respond_to_message("local", "model", conversation, "first question")
            self._write_profile("detailed")
            main.respond_to_message("local", "model", conversation, "follow-up")

        first_messages = ask.call_args_list[0].args[2]
        second_messages = ask.call_args_list[1].args[2]
        self.assertIn('"explanation_detail": "brief"', first_messages[0]["content"])
        self.assertIn('"explanation_detail": "detailed"', second_messages[0]["content"])
        self.assertEqual(
            second_messages[1:],
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "follow-up"},
            ],
        )

    def test_terminal_client_uses_shared_profile_context(self):
        self._write_profile("brief")
        prompts = iter(["hello", "/quit"])
        with (
            patch.object(user_profile, "PROFILE_PATH", self.profile_path),
            patch.object(main, "load_settings", return_value=("local", "model")),
            patch("builtins.input", side_effect=lambda _: next(prompts)),
            patch.object(main, "ask_ollama", return_value="hello back") as ask,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main.main(), 0)

        self.assertIn('"explanation_detail": "brief"', ask.call_args.args[2][0]["content"])

    def test_web_client_keeps_profile_and_conversation_context(self):
        self._write_profile("brief")
        client = web_app.create_app().test_client()
        conversation_id = client.post("/api/conversations", json={}).get_json()["conversation_id"]
        with patch.object(user_profile, "PROFILE_PATH", self.profile_path), patch.object(
            main, "ask_ollama", side_effect=["391", "408; difference 17"]
        ) as ask:
            first = client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "17 × 23?"}
            )
            second = client.post(
                "/api/chat",
                json={"conversation_id": conversation_id, "message": "Compare with 17 × 24."},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        second_messages = ask.call_args_list[1].args[2]
        self.assertEqual(
            second_messages[1:],
            [
                {"role": "user", "content": "17 × 23?"},
                {"role": "assistant", "content": "391"},
                {"role": "user", "content": "Compare with 17 × 24."},
            ],
        )


if __name__ == "__main__":
    unittest.main()