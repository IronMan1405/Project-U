"""Calendar filters and labels, Gmail scheduling boundary, quick capture and retrieval, and
targeted UI interaction (with a fake Accessibility backend).

Nothing here needs Google, a desktop or real data, except LiveUITests (LIVE_UI=1, read-only:
it lists Finder's named controls if the terminal has Accessibility permission).
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import calendar_filters
import google_workspace
import storage
import tool_registry
import ui_control
from google_workspace import GoogleError
from tool_registry import ToolError, ToolRegistry
from ui_control import Element, UIController, UIControlError, UIControlSettings, build_ui_control_settings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_google_workspace import ALL_SCOPES, ProviderTestCase  # noqa: E402

LABELS_SCOPE = google_workspace.SCOPE_CALENDAR_CALENDARS_READONLY
ROBOTICS = "0c1e7e4e-6b37-4c39-9a4b-1f2d9e5f0a11"


def item(event_id, title, *, description="", attendees=(), video=False, label=None, event_type="default"):
    start = datetime(2027, 3, 1, 10, tzinfo=timezone.utc)
    body = {"id": event_id, "summary": title, "description": description, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat()}, "eventType": event_type,
            "attendees": [{"email": "me@example.org", "self": True}, *({"email": a} for a in attendees)]}
    if video:
        body["hangoutLink"] = "https://meet.google.com/abc"
    if label:
        body["eventLabelId"] = label
    return body


EVENTS = [
    item("evtmeet1", "Project sync", attendees=["ada@example.org"]),
    item("evtmeet2", "Advisor", video=True),
    item("evtexam1", "CS F211 midsem"),
    item("evtlect1", "Data structures", description="Lecture 12, room 2210"),
    item("evtlabd1", "Embedded lab"),
    item("evtrobo1", "Arm calibration", label=ROBOTICS),
    item("evtfocus", "Deep work", event_type="focusTime"),
    item("evttitle", "Robotics club social"),
]


class CalendarFilterTests(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.token_endpoint()
        self.requests = []

        def events(request):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.requests.append(query)
            wanted = query.get("eventTypes", [None])[0]
            return {"items": [e for e in EVENTS if wanted in (None, e["eventType"])]}

        self.google.on("GET", f"{google_workspace.CALENDAR_API}/calendars/primary/events", events)
        self.google.on("GET", f"{google_workspace.CALENDAR_API}/calendars/primary?",
                       {"labelProperties": {"eventLabels": [{"id": ROBOTICS, "name": "Robotics", "backgroundColor": "#039be5"}]}})

    def search(self, provider=None, **filters):
        provider = provider or self.connected()
        return provider.search_events("2027-03-01", "2027-03-02", None, 25, **filters)

    def titles(self, found):
        return [event["title"] for event in found["events"]]

    def test_meetings_are_recognised_by_metadata_or_meeting_words_and_say_why(self):
        found = self.search(kind="meeting")
        self.assertEqual(self.titles(found), ["Project sync", "Advisor"])
        self.assertEqual([e["matched_by"] for e in found["events"]], [["has 1 other attendee(s)"], ["has a video-call link"]])
        self.assertIn("Google Calendar has no 'meeting' type", found["filters"])

    def test_exams_lectures_and_labs_use_listed_words_in_title_or_description(self):
        self.assertEqual(self.titles(self.search(kind="exam")), ["CS F211 midsem"])
        lectures = self.search(kind="lecture")
        self.assertEqual(self.titles(lectures), ["Data structures"])
        self.assertEqual(lectures["events"][0]["matched_by"], ["description word 'lecture'"])
        self.assertEqual(self.titles(self.search(kind="lab")), ["Embedded lab"])
        self.assertNotIn("description", lectures["events"][0])  # descriptions are matched, never returned

    def test_title_filter_matches_titles_only(self):
        self.assertEqual(self.titles(self.search(title_contains="robotics")), ["Robotics club social"])

    def test_event_type_is_googles_own_filter(self):
        found = self.search(event_type="focusTime")
        self.assertEqual(self.titles(found), ["Deep work"])
        self.assertEqual(self.requests[-1]["eventTypes"], ["focusTime"])
        with self.assertRaises(GoogleError):
            self.search(event_type="meeting")

    def test_a_label_filter_needs_the_opt_in_permission_and_never_falls_back(self):
        with self.assertRaises(GoogleError) as raised:
            self.search(label="Robotics")
        self.assertEqual(raised.exception.kind, "labels_not_granted")
        self.assertIn("calendar_labels = on", raised.exception.message)
        self.assertEqual(self.requests, [])  # no text search was run instead

    def test_with_the_permission_labels_match_by_name(self):
        provider = self.connected(scopes=ALL_SCOPES + " " + LABELS_SCOPE)
        found = self.search(provider, label="robotics")
        self.assertEqual(self.titles(found), ["Arm calibration"])  # not "Robotics club social"
        self.assertEqual(found["events"][0]["matched_by"], ["Google Calendar label 'Robotics'"])
        with self.assertRaises(GoogleError) as raised:
            self.search(provider, label="Important")
        self.assertIn("Its labels: Robotics", raised.exception.message)

    def test_label_permission_is_requested_only_when_switched_on(self):
        settings = google_workspace.build_google_settings({"client_secrets_file": str(self.client_file), "calendar_labels": "on"})
        self.assertTrue(settings.calendar_labels)
        off = self.connected().begin_authorization("http://127.0.0.1:5000/oauth/google/callback")
        self.assertNotIn(urllib.parse.quote(LABELS_SCOPE, safe=""), off)
        on = google_workspace.GoogleWorkspaceProvider(settings, store=self.store, opener=self.google, clock=self.clock)
        self.assertIn(urllib.parse.quote(LABELS_SCOPE, safe=""), on.begin_authorization("http://127.0.0.1:5000/cb"))
        self.assertNotIn("calendar.labels", google_workspace.ENABLED_CAPABILITIES)  # nobody is forced to re-consent

    def test_filters_combine_and_unfiltered_searches_are_unchanged(self):
        self.assertEqual(self.titles(self.search(kind="meeting", title_contains="sync")), ["Project sync"])
        plain = self.search()
        self.assertEqual(len(plain["events"]), len(EVENTS))
        self.assertNotIn("matched_by", plain["events"][0])
        self.assertNotIn("filters", plain)

    def test_the_tool_passes_filters_through_the_selected_account(self):
        registry = ToolRegistry(Path(self.temp_dir.name) / "r.sqlite3", google=self.connected())
        outcome = registry.execute(registry.prepare("search_calendar_events",
                                                    {"start": "2027-03-01", "end": "2027-03-02", "kind": "exam"},
                                                    "Find my exams"))
        self.assertEqual(self.titles(outcome.data), ["CS F211 midsem"])
        self.assertIn("Filtered by exams, recognised by", outcome.message)

    def test_relative_day_words_mean_that_day(self):
        now = datetime(2027, 2, 28, 15, tzinfo=timezone.utc)
        provider = self.connected(now=lambda: now)
        provider.search_events("tomorrow", None, None, 25)
        self.assertEqual(self.requests[-1]["timeMin"], ["2027-03-01T00:00:00+00:00"])
        self.assertEqual(self.requests[-1]["timeMax"], ["2027-03-02T00:00:00+00:00"])
        provider.search_events("tomorrow", "tomorrow", None, 25)  # relative words work as both ends too
        self.assertEqual(self.requests[-1]["timeMax"], ["2027-03-02T00:00:00+00:00"])
        provider.search_events("2027-03-05", "2027-03-05", None, 25)  # one date as both ends: that whole day
        self.assertEqual((self.requests[-1]["timeMin"], self.requests[-1]["timeMax"]),
                         (["2027-03-05T00:00:00+00:00"], ["2027-03-06T00:00:00+00:00"]))

    def test_an_account_name_is_not_also_a_search_word(self):
        registry = ToolRegistry(Path(self.temp_dir.name) / "r.sqlite3", google=self.connected())
        name = registry.google.default
        prepared = registry.prepare("search_calendar_events", {"kind": "meeting", "query": name},
                                    f"Search my {name} calendar for meetings")
        self.assertNotIn("query", prepared.arguments)

    def test_kind_words_are_whole_words(self):
        self.assertIsNone(calendar_filters.kind_reason({"summary": "Labrador walk"}, "lab"))
        self.assertIsNone(calendar_filters.kind_reason({"summary": "Classic films"}, "lecture"))


class GmailSchedulingBoundaryTests(unittest.TestCase):
    def test_there_is_no_scheduling_tool_and_send_tools_say_so(self):
        provider = google_workspace.GoogleWorkspaceProvider(
            google_workspace.GoogleSettings(enabled=True, client_secrets_file=Path("/x/c.json")),
            store=google_workspace.MemoryTokenStore())
        with tempfile.TemporaryDirectory() as folder:
            definitions = {d["function"]["name"]: d["function"] for d in
                           ToolRegistry(Path(folder) / "r.sqlite3", google=provider).definitions()}
        self.assertFalse([name for name in definitions if "schedul" in name])
        for name in google_workspace.SEND_TOOLS:
            self.assertIn("no scheduled sending", definitions[name]["description"])


class TempDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "c.sqlite3"
        storage.initialize_database(self.database)
        self.registry = ToolRegistry(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, name, arguments, message):
        prepared = self.registry.prepare(name, arguments, message)
        return prepared, self.registry.execute(prepared)


class QuickCaptureTests(TempDatabase):
    def test_remember_saves_the_users_words_with_time_tags_and_source(self):
        message = "Remember this idea: use an eye-in-hand camera for the SOLVE arm."
        prepared, outcome = self.run_tool("capture_note", {"text": "Use an eye-in-hand camera for the SOLVE arm.",
                                                           "tags": ["SOLVE", "camera"]}, message)
        self.assertFalse(prepared.always_confirm)
        self.assertTrue(outcome.message.startswith("Saved as note 1 ("))
        note = storage.search_notes(["camera"], database_path=self.database)["notes"][0]
        self.assertEqual((note["tags"], note["source"]), (["solve", "camera"], "K.R.I.S. conversation"))
        self.assertTrue(datetime.fromisoformat(note["created_at"]).tzinfo is not None)
        self.assertEqual(note["title"], "Use an eye-in-hand camera for the SOLVE arm.")

    def test_only_explicit_requests_in_the_users_own_words_save_directly(self):
        cases = [
            ("Professor X wants me to meet Monday", "Professor X wants to meet me on Monday.", True),  # not asked
            ("Remember that Professor X wants me to meet Monday", "Professor X wants me to meet Monday", False),
            ("Remember that idea you gave earlier", "Use a stereo camera with structured light", True),  # not their words
        ]
        for message, text, confirm in cases:
            with self.subTest(message=message):
                self.assertEqual(self.registry.prepare("capture_note", {"text": text}, message).always_confirm, confirm)

    def test_the_same_text_is_not_saved_twice(self):
        self.run_tool("capture_note", {"text": "Order more servos"}, "Remember: order more servos")
        _, again = self.run_tool("capture_note", {"text": "order more servos!"}, "Remember: order more servos!")
        self.assertIn("Already saved", again.message)
        self.assertTrue(again.data["already_saved"])
        self.assertEqual(len(storage.list_notes(self.database)), 1)

    def test_keyword_search_tags_recency_and_honest_misses(self):
        for text, tags in (("Use an eye-in-hand camera for the SOLVE arm", ["solve"]),
                           ("SOLVE arm needs a stronger base", ["solve"]), ("Camera budget is 5000", [])):
            self.run_tool("capture_note", {"text": text, "tags": tags}, f"Remember {text}")
        _, found = self.run_tool("search_notes", {"query": "camera SOLVE"}, "What ideas have I saved about SOLVE cameras?")
        self.assertEqual([n["excerpt"][:21] for n in found.data["notes"]], ["Use an eye-in-hand ca"])
        self.assertIn("keyword search, not semantic", found.message)
        _, both = self.run_tool("search_notes", {"query": "Budget", "tag": "budget"}, "What did I save about budget?")
        self.assertEqual(len(both.data["notes"]), 1)  # untagged, found by its words: the repeated tag is ignored
        _, untagged = self.run_tool("search_notes", {"tag": "budget"}, "What did I save about budget?")
        self.assertIn("No note is tagged 'budget', but 1 saved note(s) contain the word", untagged.message)
        _, tagged = self.run_tool("search_notes", {"tag": "SOLVE"}, "Find my notes about SOLVE")
        self.assertEqual(len(tagged.data["notes"]), 2)
        self.assertIn("ask which", tagged.message)  # several: the model must not pick one
        _, today = self.run_tool("search_notes", {"saved_on": date.today().isoformat()}, "What did I save today?")
        self.assertEqual(len(today.data["notes"]), 3)
        _, yesterday = self.run_tool("search_notes", {"saved_on": (date.today() - timedelta(days=1)).isoformat()}, "yesterday?")
        self.assertEqual(yesterday.data["notes"], [])
        _, missing = self.run_tool("search_notes", {"query": "lidar"}, "What did I ask you to remember about lidar?")
        self.assertIn("Nothing about this was saved", missing.message)

    def test_search_words_are_matched_literally(self):
        self.run_tool("capture_note", {"text": "Battery at 100% after tests"}, "Remember battery at 100% after tests")
        self.run_tool("capture_note", {"text": "Battery at 1000 cycles"}, "Remember battery at 1000 cycles")
        _, found = self.run_tool("search_notes", {"query": "100%"}, "find 100%")
        self.assertEqual(len(found.data["notes"]), 1)

    def test_nothing_is_saved_without_a_capture_call(self):
        runtime_model = Mock(return_value={"role": "assistant", "content": "Nice idea!"})
        import assistant_runtime
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=self.registry, model_client=runtime_model)
        runtime.run_turn("I think an eye-in-hand camera would suit the SOLVE arm.")
        self.assertEqual(storage.list_notes(self.database), [])
        self.assertIn("never decide on your own to remember things", assistant_runtime.SYSTEM_RULES)


class CaptureToTaskTests(TempDatabase):
    def test_turning_a_saved_note_into_a_dated_task(self):
        _, saved = self.run_tool("capture_note", {"text": "Professor X wants me to meet Monday"},
                                 "Remember that Professor X wants me to meet Monday")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        prepared, outcome = self.run_tool("create_task", {"title": "Meet Professor X", "due_at": tomorrow,
                                                          "note_id": saved.data["note_id"]}, "Turn that into a task for tomorrow")
        self.assertFalse(prepared.always_confirm)
        self.assertIn("due", outcome.message)
        task = storage.list_tasks(self.database)[0]
        self.assertEqual(task["due_at"], tomorrow)
        self.assertIn(f"From saved note {saved.data['note_id']}", task["description"])

    def test_identical_open_tasks_are_not_duplicated(self):
        self.run_tool("create_task", {"title": "Order servos"}, "Add order servos to my tasks")
        _, again = self.run_tool("create_task", {"title": "order servos."}, "Make this a task")
        self.assertIn("already exists", again.message)
        self.assertEqual(len(storage.list_tasks(self.database)), 1)
        self.run_tool("create_task", {"title": "Order servos", "due_at": "2027-01-05"}, "Add order servos to my tasks for Jan 5")
        self.assertEqual(len(storage.list_tasks(self.database)), 2)  # a different due date is a different task
        storage.set_task_completed(1, True, self.database)
        self.run_tool("create_task", {"title": "Order servos"}, "Add order servos to my tasks")
        self.assertEqual(len(storage.list_tasks(self.database)), 3)  # the identical one was completed

    def test_existing_notes_keep_working(self):
        note = storage.create_note("Plain", "Written in the Notes tab", self.database)
        self.assertEqual(storage.get_note(note["id"], self.database)["content"], "Written in the Notes tab")
        found = storage.search_notes(["notes", "tab"], database_path=self.database)["notes"][0]
        self.assertEqual((found["tags"], found["source"]), ([], ""))


# --- UI interaction --------------------------------------------------------------------------

def element(path, role, title="", window="Downloads", **extra):
    return Element(path=path, role=role, title=title, window=window, **extra)


class FakeBackend:
    def __init__(self, elements=None, trusted=True, running=True):
        self._elements = elements if elements is not None else [
            element((0, 0, 1), "AXButton", description="Back"),
            element((0, 0, 2), "AXButton", "Share"),
            element((0, 0, 3), "AXTextField", "Search", identifier="search"),
            element((0, 0, 4), "AXTextField", "Password", subrole="AXSecureTextField", secure=True),
            element((0, 0, 5), "AXButton", "Disabled", enabled=False),
            element((0, 1, 1), "AXButton", "Share", window="Documents"),
            element((1, 0, 0), "AXMenuItem", "New Folder", window="", menu="File"),
            element((0, 0, 6), "AXGroup", "Sidebar"),
        ]
        self.is_trusted, self.running = trusted, running
        self.performed = []

    def trusted(self):
        return self.is_trusted

    def pid(self, app):
        return 42 if self.running else None

    def elements(self, pid):
        return list(self._elements)

    def perform(self, pid, target, action, text):
        self.performed.append((target.path, action, text))
        return {"verified": "ok"}


class UIControllerTests(unittest.TestCase):
    def controller(self, **backend):
        self.backend = FakeBackend(**backend)
        return UIController(UIControlSettings(("Finder", "TextEdit")), self.backend)

    def test_inspect_lists_named_actionable_elements_read_only(self):
        result = self.controller().inspect("finder", role="button")
        self.assertEqual([(e["name"], e.get("window")) for e in result["elements"]],
                         [("Back", "Downloads"), ("Share", "Downloads"), ("Disabled", "Downloads"), ("Share", "Documents")])
        self.assertEqual(self.backend.performed, [])
        self.assertEqual(self.controller().inspect("Finder", role="menu item")["elements"][0]["menu"], "File")

    def test_elements_are_matched_by_role_and_exact_name(self):
        controller = self.controller()
        self.assertEqual(controller.find("Finder", "button", "back").path, (0, 0, 1))
        self.assertEqual(controller.find("Finder", "menu item", "New Folder", menu="File").path, (1, 0, 0))
        with self.assertRaises(UIControlError) as missing:
            controller.find("Finder", "button", "Delete everything")
        self.assertEqual(missing.exception.kind, "not_found")
        with self.assertRaises(UIControlError) as ambiguous:
            controller.find("Finder", "button", "Share")
        self.assertEqual(ambiguous.exception.kind, "ambiguous")
        self.assertEqual(len(ambiguous.exception.candidates), 2)
        self.assertEqual(controller.find("Finder", "button", "Share", window="Documents").path, (0, 1, 1))
        with self.assertRaises(UIControlError):
            controller.find("Finder", "text field", "Back")  # the wrong role never matches

    def test_press_focus_and_set_text(self):
        controller = self.controller()
        controller.act("Finder", "button", "Back", "press")
        controller.act("Finder", "text field", "Search", "focus")
        controller.act("Finder", "text field", "Search", "set_text", text="report")
        self.assertEqual(self.backend.performed, [((0, 0, 1), "press", None), ((0, 0, 3), "focus", None),
                                                  ((0, 0, 3), "set_text", "report")])

    def test_unsafe_or_invalid_actions_are_refused_before_acting(self):
        controller = self.controller()
        for args, kind in ((("Finder", "text field", "Password", "set_text", "hunter2"), "invalid"),
                           (("Finder", "button", "Disabled", "press"), "failed"),
                           (("Finder", "text field", "Search", "press"), "invalid"),
                           (("Finder", "button", "Back", "set_text", "x"), "invalid"),
                           (("Finder", "button", "Back", "drag"), "invalid")):
            with self.subTest(args=args), self.assertRaises(UIControlError) as raised:
                controller.act(*args)
            self.assertEqual(raised.exception.kind, kind)
        self.assertEqual(self.backend.performed, [])

    def test_disallowed_apps_permission_and_not_running(self):
        with self.assertRaises(UIControlError) as raised:
            self.controller().inspect("Calculator")
        self.assertIn("cannot interact with Calculator yet", raised.exception.message)
        with self.assertRaises(UIControlError) as raised:
            self.controller(trusted=False).inspect("Finder")
        self.assertEqual(raised.exception.kind, "permission")
        self.assertIn("Privacy & Security > Accessibility", raised.exception.message)
        with self.assertRaises(UIControlError) as raised:
            self.controller(running=False).inspect("Finder")
        self.assertEqual(raised.exception.kind, "not_running")
        with self.assertRaises(UIControlError) as raised:
            UIController(UIControlSettings(), FakeBackend()).inspect("Finder")
        self.assertEqual(raised.exception.kind, "not_configured")

    def test_dangerous_apps_can_never_be_configured(self):
        with self.assertLogs("ui_control", level="WARNING"):
            settings = build_ui_control_settings("Finder, System Settings, Terminal, Safari, Google Chrome, TextEdit")
        self.assertEqual(settings.apps, ("Finder", "TextEdit"))


class UIToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.registry = ToolRegistry(Path(self.temp.name) / "r.sqlite3",
                                     ui=UIController(UIControlSettings(("Finder",)), self.backend))

    def tearDown(self):
        self.temp.cleanup()

    def test_pressing_and_typing_are_always_approved_first_and_name_the_exact_element(self):
        press = self.registry.prepare("ui_action", {"app": "Finder", "role": "button", "name": "Back", "action": "press"},
                                      "Click the Back button in Finder, no need to ask")
        self.assertTrue(press.always_confirm)
        self.assertEqual(press.summary, 'Press the button "Back" in Finder (window "Downloads")')
        typed = self.registry.prepare("ui_action", {"app": "Finder", "role": "text field", "name": "Search",
                                                    "action": "set_text", "text": "budget"}, "Type budget in the search field")
        self.assertTrue(typed.always_confirm)
        self.assertIn("budget", typed.summary)
        self.assertEqual(self.backend.performed, [])
        self.assertTrue(self.registry.execute(press).ok)
        self.assertEqual(self.backend.performed, [((0, 0, 1), "press", None)])

    def test_focus_is_direct_only_when_asked(self):
        arguments = {"app": "Finder", "role": "text field", "name": "Search", "action": "focus"}
        self.assertFalse(self.registry.prepare("ui_action", arguments, "Focus the search field in Finder").always_confirm)
        self.assertTrue(self.registry.prepare("ui_action", arguments, "What is in Finder?").always_confirm)

    def test_targets_are_validated_before_any_approval(self):
        for arguments, expected in (
            ({"app": "Finder", "role": "button", "name": "Share", "action": "press"}, "Candidates"),
            ({"app": "Finder", "role": "button", "name": "Nope", "action": "press"}, "No button named"),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ToolError, expected):
                self.registry.prepare("ui_action", arguments, "Click it")
        with self.assertRaises(ToolError):  # not in the app list: the schema refuses it
            self.registry.prepare("ui_action", {"app": "Safari", "role": "button", "name": "Send", "action": "press"}, "x")

    def test_tools_exist_only_when_configured_and_no_free_form_control_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            names = {d["function"]["name"] for d in ToolRegistry(Path(folder) / "r.sqlite3").definitions()}
        self.assertFalse({"inspect_ui", "ui_action"} & names)
        names = {d["function"]["name"] for d in self.registry.definitions()}
        self.assertTrue({"inspect_ui", "ui_action"} <= names)
        self.assertFalse(names & tool_registry.FORBIDDEN_TOOLS)
        schema = next(d for d in self.registry.definitions() if d["function"]["name"] == "ui_action")["function"]["parameters"]
        self.assertFalse({"x", "y", "coordinates", "keys", "script"} & set(schema["properties"]))


@unittest.skipUnless(os.environ.get("LIVE_UI") == "1", "set LIVE_UI=1 to list Finder's controls (read-only)")
class LiveUITests(unittest.TestCase):
    def test_finder_controls_can_be_read(self):
        controller = UIController(UIControlSettings(("Finder",)))
        if not controller.backend.trusted():
            self.skipTest(ui_control.PERMISSION_HELP)
        result = controller.inspect("Finder")
        print("\n", [(e["role"], e["name"]) for e in result["elements"][:20]])
        self.assertTrue(result["elements"])


if __name__ == "__main__":
    unittest.main()
