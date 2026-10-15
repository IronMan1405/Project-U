import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import storage
import web_app
from assistant_runtime import AssistantRuntime
from computer_utilities import UtilitySettings
from tool_registry import ToolError, ToolRegistry
from voice import VoiceSettings


@contextmanager
def pinned_timezone(name):
    """Run with a fixed system time zone (the zone U treats as 'local')."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def tool_call(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
    }


class NormalizeDueAtTests(unittest.TestCase):
    def test_local_wall_times_convert_to_utc_in_python(self):
        cases = {
            "Asia/Kolkata": {
                "2026-10-16 05:00": "2026-10-15 23:30:00Z",
                "2026-10-16T05:00": "2026-10-15 23:30:00Z",
                "2026-10-16 05:00:30": "2026-10-15 23:30:30Z",
                "2026-10-16 03:00": "2026-10-15 21:30:00Z",  # crosses back a day
                "2027-01-01 01:15": "2026-12-31 19:45:00Z",  # crosses back a year
                "2028-02-29 05:00": "2028-02-28 23:30:00Z",  # leap day
            },
            "America/New_York": {
                "2026-07-01 05:00": "2026-07-01 09:00:00Z",  # EDT, UTC-04:00
                "2026-12-31 23:30": "2027-01-01 04:30:00Z",  # EST, crosses forward a year
                "2026-03-08 01:59": "2026-03-08 06:59:00Z",  # just before the gap
                "2026-03-08 03:00": "2026-03-08 07:00:00Z",  # just after the gap
                "2026-11-01 00:59": "2026-11-01 04:59:00Z",  # before the repeated hour
                "2026-11-01 02:00": "2026-11-01 07:00:00Z",  # after the repeated hour
            },
            "UTC": {"2026-10-16 05:00": "2026-10-16 05:00:00Z"},
        }
        for zone, expectations in cases.items():
            with pinned_timezone(zone):
                for value, expected in expectations.items():
                    with self.subTest(zone=zone, value=value):
                        self.assertEqual(storage.normalize_due_at(value), expected)
                        self.assertEqual(storage.validate_due_at(expected), expected)

    def test_explicit_offsets_and_utc_ignore_the_local_zone(self):
        for zone in ("Asia/Kolkata", "America/New_York"):
            with pinned_timezone(zone):
                for value, expected in {
                    "2026-10-16 05:00+05:30": "2026-10-15 23:30:00Z",
                    "2026-10-16T05:00:00-04:00": "2026-10-16 09:00:00Z",
                    "2026-10-16 05:00Z": "2026-10-16 05:00:00Z",
                    "2026-10-16 00:30+14:00": "2026-10-15 10:30:00Z",
                    "2026-03-08 02:30-05:00": "2026-03-08 07:30:00Z",  # explicit offset in a gap
                }.items():
                    with self.subTest(zone=zone, value=value):
                        self.assertEqual(storage.normalize_due_at(value), expected)

    def test_dates_and_canonical_values_are_kept_exactly(self):
        with pinned_timezone("America/New_York"):
            for value in ("2026-10-16", "2026-10-15 23:30:00Z", "2026-10-15 23:30:00.123456Z"):
                with self.subTest(value=value):
                    self.assertEqual(storage.normalize_due_at(value), value)
            self.assertIsNone(storage.normalize_due_at(None))

    def test_daylight_saving_gaps_and_overlaps_are_rejected(self):
        with pinned_timezone("America/New_York"):
            for value in ("2026-03-08 02:00", "2026-03-08 02:30", "2026-11-01 01:00", "2026-11-01 01:30"):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "daylight-saving"):
                    storage.normalize_due_at(value)

    def test_invalid_inputs_are_rejected_not_reinterpreted(self):
        with pinned_timezone("Asia/Kolkata"):
            for value in (
                "tomorrow at 5am", "5am", "2026-10-16 5:00", "2026-10-16 05:00 AM", "2026-10-16 5am",
                "2026-10-16 24:00", "2026-10-16 05:60", "2026-02-30 05:00", "2026-13-01",
                "16/10/2026 05:00", "2026-10-16 05", "2026-10-16 05:00+15:00", "2026-10-16 05:00+05:60",
                "2026-10-16 05:00+0530", "2026-10-16 05:00:00.5", "2026-10-16T05:00:00.5Z",
                "2026-10-16 05:00 IST", " 2026-10-16 05:00", "", 1760590800,
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    storage.normalize_due_at(value)

    def test_description_round_trips_to_the_same_local_wall_time(self):
        for zone, label in (("Asia/Kolkata", "IST, UTC+05:30"), ("America/New_York", None)):
            with pinned_timezone(zone):
                for local in ("2026-10-16 05:00", "2026-12-31 23:30", "2026-07-04 12:15"):
                    with self.subTest(zone=zone, local=local):
                        stored = storage.normalize_due_at(local)
                        described = storage.describe_due_at(stored)
                        self.assertIn(local, described)
                        if label:
                            self.assertTrue(described.endswith(label))
                self.assertEqual(storage.describe_due_at("2026-10-16"), "Fri 2026-10-16")
                self.assertEqual(storage.describe_due_at(None), "")


class DueTimeWorkflowTests(unittest.TestCase):
    """The audit case: 'tomorrow at 5 AM' in IST must be stored as 05:00 IST, not 06:00."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "due.sqlite3"
        storage.initialize_database(self.database_path)
        self.now = datetime(2026, 10, 15, 21, 40, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def tearDown(self):
        self.temp_dir.cleanup()

    def runtime(self, model):
        return AssistantRuntime(
            "http://local", "test-model", database_path=self.database_path,
            model_client=model, clock=lambda: self.now,
        )

    def test_tomorrow_at_5am_is_stored_and_reported_as_5am_local(self):
        with pinned_timezone("Asia/Kolkata"):
            model = Mock(
                side_effect=[
                    tool_call("create_task", {"title": "Work out", "due_at": "2026-10-16 05:00"}),
                    {"role": "assistant", "content": "Added for 5 AM tomorrow."},
                ]
            )
            runtime = self.runtime(model)
            result = runtime.run_turn("Please add a task to work out tomorrow at 5am.")

            prompt = next(
                m["content"] for m in model.call_args_list[0].args[2]
                if m["role"] == "system" and "Current local date" in m["content"]
            )
            self.assertIn("'tomorrow at 5 AM' -> 2026-10-16 05:00", prompt)
            task = storage.list_tasks(self.database_path)[0]
            self.assertEqual(task["due_at"], "2026-10-15 23:30:00Z")  # 05:00 IST, not 00:30Z (06:00)
            tool_message = next(m for m in runtime.history if m["role"] == "tool")
            self.assertIn("Fri 2026-10-16 05:00 IST, UTC+05:30", tool_message["content"])
            self.assertEqual(result.activities[0]["summary"], "Task created, due Fri 2026-10-16 05:00 IST, UTC+05:30.")

    def test_confirmation_summary_matches_the_stored_instant(self):
        with pinned_timezone("Asia/Kolkata"):
            model = Mock(
                side_effect=[
                    tool_call("create_task", {"title": "Work out", "due_at": "2026-10-16T05:00"}),
                    {"role": "assistant", "content": "Done."},
                ]
            )
            runtime = self.runtime(model)
            pending = runtime.run_turn("set a task to work out tomorrow at 5am").pending_confirmation
            self.assertEqual(
                pending["summary"], "Create task: Work out (due Fri 2026-10-16 05:00 IST, UTC+05:30)"
            )
            self.assertEqual(storage.list_tasks(self.database_path), [])
            runtime.resolve_confirmation(pending["confirmation_id"], True)
            stored = storage.list_tasks(self.database_path)[0]["due_at"]
            self.assertEqual(stored, "2026-10-15 23:30:00Z")
            self.assertIn(storage.describe_due_at(stored), pending["summary"])

    def test_changing_a_due_time_and_listing_report_local_time(self):
        with pinned_timezone("America/New_York"):
            registry = ToolRegistry(self.database_path)
            task = storage.create_task("Report", database_path=self.database_path)
            change = registry.prepare(
                "set_task_due_date",
                {"task_id": task["id"], "due_at": "2026-12-01 09:30"},
                "Please change the due date of task 1 to Dec 1 at 9:30am.",
            )
            self.assertEqual(change.arguments["due_at"], "2026-12-01 14:30:00Z")
            self.assertEqual(change.summary, f"Set due date of task {task['id']} to Tue 2026-12-01 09:30 EST, UTC-05:00")
            registry.execute(change)
            listed = registry.execute(registry.prepare("list_tasks", {}, "List my tasks")).data["tasks"][0]
            self.assertEqual(listed["due_at"], "2026-12-01 14:30:00Z")
            self.assertEqual(listed["due_local"], "Tue 2026-12-01 09:30 EST, UTC-05:00")

            with self.assertRaisesRegex(ToolError, "daylight-saving"):
                registry.prepare(
                    "set_task_due_date",
                    {"task_id": task["id"], "due_at": "2026-11-01 01:30"},
                    "Please set the due date of task 1 to Nov 1 at 1:30am.",
                )
            self.assertEqual(storage.list_tasks(self.database_path)[0]["due_at"], "2026-12-01 14:30:00Z")

    def test_web_api_contract_is_unchanged(self):
        client = web_app.create_app(
            self.database_path, utility_settings=UtilitySettings(), voice_settings=VoiceSettings()
        ).test_client()
        for value in ("2026-10-16 05:00", "2026-10-16T05:00+05:30"):
            with self.subTest(value=value):
                self.assertEqual(client.post("/api/tasks", json={"title": "x", "due_at": value}).status_code, 400)
        created = client.post("/api/tasks", json={"title": "x", "due_at": "2026-10-15 23:30:00Z"})
        self.assertEqual(created.get_json()["task"]["due_at"], "2026-10-15 23:30:00Z")
        self.assertNotIn("due_local", created.get_json()["task"])


if __name__ == "__main__":
    unittest.main()
