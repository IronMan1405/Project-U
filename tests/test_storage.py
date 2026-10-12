import sqlite3
import tempfile
import unittest
from pathlib import Path

import storage
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