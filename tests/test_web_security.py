import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import storage
import web_app
from computer_utilities import UtilitySettings
from voice import VoiceSettings


TRUSTED = ("127.0.0.1:5000", "localhost:5000", "127.0.0.1", "localhost")
UNTRUSTED = (
    "attacker.example:5000",
    "attacker.example",
    "127.0.0.1.attacker.example:5000",
    "localhost.attacker.example",
    "192.168.1.20:5000",
    "0.0.0.0:5000",
    "[::1]:5000",
    "",
)


def tool_call(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
    }


class TrustedHostTests(unittest.TestCase):
    """Requests must name a loopback host, so DNS-rebinding pages cannot use the API."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "security.sqlite3"
        self.app = web_app.create_app(
            self.database_path, utility_settings=UtilitySettings(), voice_settings=VoiceSettings()
        )
        self.client = self.app.test_client()
        self.task = storage.create_task("Private task", "secret detail", self.database_path)
        self.note = storage.create_note("Private note", "secret note body", self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_loopback_hosts_keep_working(self):
        for host in TRUSTED:
            with self.subTest(host=host):
                headers = {"Host": host}
                self.assertEqual(self.client.get("/", headers=headers).status_code, 200)
                tasks = self.client.get("/api/tasks", headers=headers)
                self.assertEqual(tasks.status_code, 200)
                self.assertEqual(tasks.get_json()["tasks"][0]["title"], "Private task")
                created = self.client.post("/api/conversations", json={}, headers=headers)
                self.assertEqual(created.status_code, 200)

    def test_untrusted_hosts_cannot_read_or_change_anything(self):
        requests = [
            ("get", "/", None),
            ("get", "/api/health", None),
            ("get", "/api/tasks", None),
            ("get", "/api/notes", None),
            ("get", f"/api/notes/{self.note['id']}", None),
            ("get", "/api/voice/status", None),
            ("post", "/api/conversations", {}),
            ("post", "/api/tasks", {"title": "Injected"}),
            ("patch", f"/api/tasks/{self.task['id']}", {"completed": True, "due_at": None}),
            ("delete", f"/api/tasks/{self.task['id']}", None),
            ("post", "/api/notes", {"title": "Injected", "content": "x"}),
            ("delete", f"/api/notes/{self.note['id']}", None),
            ("post", "/api/voice/speak", {"text": "hello"}),
        ]
        for host in UNTRUSTED:
            for method, url, body in requests:
                with self.subTest(host=host, method=method, url=url):
                    kwargs = {"headers": {"Host": host}}
                    if body is not None:
                        kwargs["json"] = body
                    response = getattr(self.client, method)(url, **kwargs)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn(b"secret", response.data)
                    self.assertNotIn(b"Private", response.data)
        tasks = storage.list_tasks(self.database_path)
        self.assertEqual([(t["title"], t["completed"]) for t in tasks], [("Private task", False)])
        self.assertEqual([n["title"] for n in storage.list_notes(self.database_path)], ["Private note"])

    def test_untrusted_host_cannot_start_a_chat_or_tool_action(self):
        model = Mock(return_value=tool_call("delete_task", {"task_id": self.task["id"]}))
        with patch.object(assistant_runtime, "ask_ollama", model):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            response = self.client.post(
                "/api/chat",
                json={"conversation_id": conversation_id, "message": "Delete task 1"},
                headers={"Host": "attacker.example:5000"},
            )
            upload = self.client.post(
                "/api/voice/transcribe",
                data={"audio": (io.BytesIO(b"\x1a\x45\xdf\xa3"), "r.webm", "audio/webm")},
                content_type="multipart/form-data",
                headers={"Host": "attacker.example:5000"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(upload.status_code, 400)
        model.assert_not_called()
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

    def test_untrusted_host_cannot_approve_a_pending_confirmation(self):
        model = Mock(
            side_effect=[
                tool_call("delete_task", {"task_id": self.task["id"]}),
                {"role": "assistant", "content": "Deletion cancelled."},
            ]
        )
        with patch.object(assistant_runtime, "ask_ollama", model):
            conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
            chat = self.client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "Delete task 1"}
            )
            pending = chat.get_json()["pending_confirmation"]
            self.assertIsNotNone(pending)

            approval = {
                "conversation_id": conversation_id,
                "confirmation_id": pending["confirmation_id"],
                "approved": True,
            }
            for host in UNTRUSTED:
                with self.subTest(host=host):
                    forged = self.client.post("/api/confirm", json=approval, headers={"Host": host})
                    self.assertEqual(forged.status_code, 400)
            self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

            # The real user can still resolve the same pending action from a loopback host.
            cancelled = self.client.post(
                "/api/confirm",
                json={**approval, "approved": False},
                headers={"Host": "127.0.0.1:5000"},
            )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["response"], f"Cancelled: Delete task {self.task['id']}. It was not run.")
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)

    def test_trusted_hosts_are_exact_loopback_names(self):
        self.assertEqual(self.app.config["TRUSTED_HOSTS"], ["127.0.0.1", "localhost"])


if __name__ == "__main__":
    unittest.main()
