import sqlite3
import tempfile
import unittest
from pathlib import Path

import storage
from tool_registry import ToolError, ToolRegistry
import web_app


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "test.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_and_schema_are_created_automatically(self):
        storage.initialize_database(self.database_path)

        self.assertTrue(self.database_path.is_file())
        connection = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
                )
            }
        finally:
            connection.close()
        self.assertTrue({"tasks", "notes"}.issubset(tables))

    def test_legacy_task_schema_migrates_without_losing_data(self):
        self.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
            "completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)))"
        )
        connection.execute(
            "INSERT INTO tasks (title, description, created_at, completed) VALUES (?, ?, ?, ?)",
            ("Legacy task", "Keep this", "2025-01-02T03:04:05+00:00", 1),
        )
        connection.commit()
        connection.close()

        storage.initialize_database(self.database_path)
        storage.initialize_database(self.database_path)

        task = storage.list_tasks(self.database_path)[0]
        self.assertEqual(task["title"], "Legacy task")
        self.assertEqual(task["description"], "Keep this")
        self.assertTrue(task["completed"])
        self.assertIsNone(task["due_at"])

    def test_task_lifecycle_and_persistence(self):
        storage.initialize_database(self.database_path)
        task = storage.create_task("Write tests", "Cover both clients", self.database_path)
        self.assertIs(task["completed"], False)
        self.assertTrue(task["created_at"])
        self.assertEqual(storage.list_tasks(self.database_path)[0]["title"], "Write tests")

        completed = storage.set_task_completed(task["id"], True, self.database_path)
        self.assertIs(completed["completed"], True)
        reopened = storage.set_task_completed(task["id"], False, self.database_path)
        self.assertIs(reopened["completed"], False)

        storage.initialize_database(self.database_path)
        self.assertEqual(storage.list_tasks(self.database_path)[0]["id"], task["id"])
        storage.delete_task(task["id"], self.database_path)
        self.assertEqual(storage.list_tasks(self.database_path), [])
        with self.assertRaises(storage.RecordNotFound):
            storage.delete_task(task["id"], self.database_path)

    def test_task_lifecycle_with_due_date_and_persistence(self):
        storage.initialize_database(self.database_path)
        due_date = "2023-10-01"
        task = storage.create_task(
            "Write tests", "Cover both clients", self.database_path, due_at=due_date
        )
        self.assertIs(task["completed"], False)
        self.assertTrue(task["created_at"])
        self.assertEqual(storage.list_tasks(self.database_path)[0]["title"], "Write tests")
        self.assertEqual(storage.list_tasks(self.database_path)[0]["due_at"], due_date)

        completed = storage.set_task_completed(
            task["id"], True, self.database_path, due_at=due_date
        )
        self.assertIs(completed["completed"], True)
        reopened = storage.set_task_completed(
            task["id"], False, self.database_path, due_at=due_date
        )
        self.assertIs(reopened["completed"], False)

        storage.initialize_database(self.database_path)
        self.assertEqual(storage.list_tasks(self.database_path)[0]["id"], task["id"])
        storage.delete_task(task["id"], self.database_path)
        self.assertEqual(storage.list_tasks(self.database_path), [])
        with self.assertRaises(storage.RecordNotFound):
            storage.delete_task(task["id"], self.database_path)

    def test_completion_and_reopening_preserve_due_date_when_omitted(self):
        storage.initialize_database(self.database_path)
        task = storage.create_task(
            "Keep deadline", database_path=self.database_path, due_at="2026-11-03"
        )

        completed = storage.set_task_completed(task["id"], True, self.database_path)
        reopened = storage.set_task_completed(task["id"], False, self.database_path)

        self.assertEqual(completed["due_at"], "2026-11-03")
        self.assertEqual(reopened["due_at"], "2026-11-03")
        cleared = storage.set_task_completed(
            task["id"], False, self.database_path, due_at=None
        )
        self.assertIsNone(cleared["due_at"])

    def test_update_task_sets_changes_and_clears_due_date_independently(self):
        storage.initialize_database(self.database_path)
        task = storage.create_task("Plan trip", database_path=self.database_path)
        self.assertIsNone(task["due_at"])

        dated = storage.update_task(task["id"], self.database_path, due_at="2026-11-03")
        self.assertEqual(dated["due_at"], "2026-11-03")
        self.assertIs(dated["completed"], False)

        timed = storage.update_task(
            task["id"], self.database_path, due_at="2026-11-04 09:30:00Z"
        )
        self.assertEqual(timed["due_at"], "2026-11-04 09:30:00Z")

        completed = storage.update_task(task["id"], self.database_path, completed=True)
        self.assertIs(completed["completed"], True)
        self.assertEqual(completed["due_at"], "2026-11-04 09:30:00Z")

        cleared = storage.update_task(task["id"], self.database_path, due_at=None)
        self.assertIsNone(cleared["due_at"])
        self.assertIs(cleared["completed"], True)
        self.assertIsNone(storage.list_tasks(self.database_path)[0]["due_at"])

        with self.assertRaises(storage.RecordNotFound):
            storage.update_task(999, self.database_path, due_at="2026-11-03")
        with self.assertRaises(storage.RecordNotFound):
            storage.update_task(999, self.database_path)

    def test_invalid_due_dates_are_rejected_without_writing(self):
        storage.initialize_database(self.database_path)
        task = storage.create_task(
            "Keep", database_path=self.database_path, due_at="2026-11-03"
        )
        invalid_values = (
            "2026-02-30",
            "2026-13-01",
            "26-11-03",
            "2026-11-03T09:30:00Z",
            "2026-11-03 09:30:00",
            "2026-11-03 09:30:00+00:00",
            "2026-11-03\t09:30:00Z",
            "2026-11-03 25:00:00Z",
            "tomorrow",
            "",
            20261103,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    storage.create_task("Bad", database_path=self.database_path, due_at=value)
                with self.assertRaises(ValueError):
                    storage.update_task(task["id"], self.database_path, due_at=value)
        self.assertEqual(len(storage.list_tasks(self.database_path)), 1)
        self.assertEqual(storage.list_tasks(self.database_path)[0]["due_at"], "2026-11-03")
        for value in ("2028-02-29", "2026-11-03 09:30:00Z", "2026-11-03 09:30:00.123456Z"):
            with self.subTest(valid=value):
                self.assertEqual(storage.validate_due_at(value), value)

    def test_note_lifecycle_preserves_content_and_persists(self):
        storage.initialize_database(self.database_path)
        content = "First line\n\n  indented line\n"
        note = storage.create_note("Draft", content, self.database_path)
        self.assertEqual(storage.get_note(note["id"], self.database_path)["content"], content)
        self.assertEqual(storage.list_notes(self.database_path)[0]["preview"], content[:180])

        edited_content = "Edited\nwith exact newlines\n"
        updated = storage.update_note(
            note["id"], "Updated draft", edited_content, self.database_path
        )
        self.assertEqual(updated["content"], edited_content)
        self.assertGreaterEqual(updated["updated_at"], note["updated_at"])
        storage.initialize_database(self.database_path)
        self.assertEqual(storage.get_note(note["id"], self.database_path)["content"], edited_content)

        storage.delete_note(note["id"], self.database_path)
        with self.assertRaises(storage.RecordNotFound):
            storage.get_note(note["id"], self.database_path)


class StorageApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "api.sqlite3"
        self.client = web_app.create_app(self.database_path).test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_task_routes_create_list_complete_reopen_and_delete(self):
        created = self.client.post(
            "/api/tasks", json={"title": "Review", "description": "Read notes"}
        )
        self.assertEqual(created.status_code, 201)
        task = created.get_json()["task"]
        self.assertFalse(task["completed"])
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"][0]["id"], task["id"])

        completed = self.client.patch(f"/api/tasks/{task['id']}", json={"completed": True})
        self.assertTrue(completed.get_json()["task"]["completed"])
        reopened = self.client.patch(f"/api/tasks/{task['id']}", json={"completed": False})
        self.assertFalse(reopened.get_json()["task"]["completed"])

        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 204)
        self.assertEqual(self.client.patch(f"/api/tasks/{task['id']}", json={"completed": True}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 404)

    def test_task_routes_create_list_complete_reopen_and_delete_with_due_date(self):
        created = self.client.post(
            "/api/tasks", json={"title": "Review", "description": "Read notes", "due_at": "2023-10-01"}
        )
        self.assertEqual(created.status_code, 201)
        task = created.get_json()["task"]
        self.assertFalse(task["completed"])
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"][0]["id"], task["id"])
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"][0]["due_at"], "2023-10-01")

        completed = self.client.patch(f"/api/tasks/{task['id']}", json={"completed": True})
        self.assertTrue(completed.get_json()["task"]["completed"])
        reopened = self.client.patch(f"/api/tasks/{task['id']}", json={"completed": False})
        self.assertFalse(reopened.get_json()["task"]["completed"])

        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 204)
        self.assertEqual(self.client.patch(f"/api/tasks/{task['id']}", json={"completed": True}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 404)

    def test_task_api_uses_shared_due_date_validation_and_explicit_clear(self):
        registry = ToolRegistry(self.database_path)
        with self.assertRaises(ToolError):
            registry.prepare(
                "create_task",
                {"title": "Invalid date", "due_at": "2026-02-30"},
                "Create a task named Invalid date.",
            )
        invalid = self.client.post(
            "/api/tasks", json={"title": "Invalid date", "due_at": "2026-02-30"}
        )
        self.assertEqual(invalid.status_code, 400)
        created = self.client.post(
            "/api/tasks", json={"title": "Valid date", "due_at": "2026-11-03"}
        )
        task = created.get_json()["task"]
        completed = self.client.patch(f"/api/tasks/{task['id']}", json={"completed": True})
        self.assertEqual(completed.get_json()["task"]["due_at"], "2026-11-03")
        cleared = self.client.patch(
            f"/api/tasks/{task['id']}", json={"completed": True, "due_at": None}
        )
        self.assertIsNone(cleared.get_json()["task"]["due_at"])

    def test_task_patch_updates_completion_and_due_date_independently(self):
        created = self.client.post(
            "/api/tasks", json={"title": "Pay rent", "due_at": "2026-11-01"}
        )
        self.assertEqual(created.status_code, 201)
        task = created.get_json()["task"]
        self.assertEqual(task["due_at"], "2026-11-01")
        url = f"/api/tasks/{task['id']}"

        moved = self.client.patch(url, json={"due_at": "2026-11-02 18:00:00Z"})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.get_json()["task"]["due_at"], "2026-11-02 18:00:00Z")
        self.assertIs(moved.get_json()["task"]["completed"], False)

        completed = self.client.patch(url, json={"completed": True})
        self.assertIs(completed.get_json()["task"]["completed"], True)
        self.assertEqual(completed.get_json()["task"]["due_at"], "2026-11-02 18:00:00Z")

        both = self.client.patch(url, json={"completed": False, "due_at": "2026-11-05"})
        self.assertIs(both.get_json()["task"]["completed"], False)
        self.assertEqual(both.get_json()["task"]["due_at"], "2026-11-05")

        cleared = self.client.patch(url, json={"due_at": None})
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.get_json()["task"]["due_at"])
        self.assertIs(cleared.get_json()["task"]["completed"], False)
        listed = self.client.get("/api/tasks").get_json()["tasks"][0]
        self.assertIn("due_at", listed)
        self.assertIsNone(listed["due_at"])

        undated = self.client.post("/api/tasks", json={"title": "No date"}).get_json()["task"]
        self.assertIn("due_at", undated)
        self.assertIsNone(undated["due_at"])

    def test_task_api_rejects_invalid_due_dates_and_empty_patches(self):
        task = self.client.post(
            "/api/tasks", json={"title": "Dated", "due_at": "2026-11-01"}
        ).get_json()["task"]
        url = f"/api/tasks/{task['id']}"
        for value in ("2026-02-30", "2026-11-01T10:00:00Z", "next week", 5, ["2026-11-01"]):
            with self.subTest(value=value):
                created = self.client.post("/api/tasks", json={"title": "Bad", "due_at": value})
                self.assertEqual(created.status_code, 400)
                self.assertIn("due_at", created.get_json()["error"])
                patched = self.client.patch(url, json={"due_at": value})
                self.assertEqual(patched.status_code, 400)
                self.assertIn("due_at", patched.get_json()["error"])
                combined = self.client.patch(url, json={"completed": True, "due_at": value})
                self.assertEqual(combined.status_code, 400)
        self.assertEqual(self.client.patch(url, json={}).status_code, 400)
        self.assertEqual(self.client.patch(url, json={"title": "x"}).status_code, 400)
        self.assertEqual(
            self.client.patch(url, json={"completed": "yes", "due_at": None}).status_code, 400
        )
        unchanged = self.client.get("/api/tasks").get_json()["tasks"][0]
        self.assertEqual(unchanged["due_at"], "2026-11-01")
        self.assertIs(unchanged["completed"], False)
        self.assertEqual(self.client.patch("/api/tasks/999", json={"due_at": None}).status_code, 404)

    def test_task_form_offers_native_due_date_inputs(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="task-due-date" name="due_date" type="date"', page)
        self.assertIn('id="task-due-time" name="due_time" type="time"', page)

    def test_note_routes_create_read_edit_and_delete(self):
        content = "One\nTwo\n"
        created = self.client.post("/api/notes", json={"title": "Scratch", "content": content})
        self.assertEqual(created.status_code, 201)
        note_id = created.get_json()["note"]["id"]
        self.assertEqual(self.client.get(f"/api/notes/{note_id}").get_json()["note"]["content"], content)
        self.assertEqual(self.client.get("/api/notes").get_json()["notes"][0]["title"], "Scratch")

        edited = self.client.patch(
            f"/api/notes/{note_id}", json={"title": "Edited", "content": "New\ntext"}
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.get_json()["note"]["content"], "New\ntext")
        self.assertEqual(self.client.delete(f"/api/notes/{note_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/notes/{note_id}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/notes/{note_id}").status_code, 404)

    def test_invalid_input_is_rejected(self):
        self.assertEqual(self.client.post("/api/tasks", json={"title": " "}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/tasks", json={"title": "valid", "description": 42}).status_code,
            400,
        )
        self.assertEqual(self.client.patch("/api/tasks/1", json={"completed": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/notes", json={"title": "Note"}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/notes", json={"title": "Note", "content": []}).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch("/api/notes/1", json={"title": " ", "content": "x"}).status_code,
            400,
        )
        self.assertEqual(self.client.get("/api/notes/999").status_code, 404)

    def test_user_text_is_stored_as_data_not_sql(self):
        title = "x'); DROP TABLE tasks; --"
        created = self.client.post("/api/tasks", json={"title": title})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"][0]["title"], title)
        self.assertTrue(self.database_path.is_file())

    def test_unavailable_database_returns_generic_error(self):
        blocked_parent = Path(self.temp_dir.name) / "not-a-directory"
        blocked_parent.write_text("file", encoding="utf-8")
        client = web_app.create_app(blocked_parent / "data.sqlite3").test_client()

        response = client.get("/api/tasks")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Local data store is unavailable."})
        self.assertNotIn("Traceback", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
