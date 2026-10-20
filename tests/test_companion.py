"""Confirmation policy, spoken confirmation (server side), Mac control, the developer companion,
and the hidden Tasks tab.

Everything here uses fakes or temporary folders: no AppleScript runs, nothing is played,
opened or sent. The Git tests create throwaway repositories with the real /usr/bin/git and are
skipped where it is missing.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import computer_utilities
import developer
import google_workspace
import mac_control
import storage
import tool_registry
import web_app
from computer_utilities import UtilitySettings
from developer import DeveloperCompanion, DeveloperError, DeveloperSettings, build_developer_settings
from mac_control import MacControl, MacControlError
from reminders import AppleReminders
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling helpers, also when run as tests.test_companion
from test_gmail_stage3 import Stage3TestCase  # noqa: E402
from test_planning import RID, FakeOsascript, TempDatabase, record
from timers import TimerService
from tool_registry import ToolRegistry


def tool_call(name, arguments, call_id="call-1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}


# --- Confirmation policy ------------------------------------------------------------------

class LowImpactPolicyTests(TempDatabase):
    def registry(self, **extra):
        self.osascript = FakeOsascript()
        self.osascript.open = [record(title="Email the professor", due="2026-10-19T18:00:00")]
        return ToolRegistry(self.database, reminders=AppleReminders(runner=self.osascript),
                            timers=TimerService(self.database), **extra)

    def test_requested_timers_and_reminders_run_without_confirmation(self):
        registry = self.registry()
        direct = [
            ("start_timer", {"kind": "focus", "minutes": 25}, "I need a 25-minute focus session"),
            ("start_timer", {"kind": "timer", "minutes": 10}, "10 minute timer please"),
            ("start_timer", {"kind": "break", "minutes": 5}, "Okay, and give me a 5 minute break after that"),
            ("cancel_timer", {}, "Actually, cancel it"),
            ("create_reminder", {"title": "Call mom", "due_at": "2026-10-19 19:00"}, "Also, remind me to call mom at 7"),
            ("create_reminder", {"title": "Pay rent", "due_at": None}, "I need a reminder to pay rent"),
        ]
        for name, arguments, message in direct:
            with self.subTest(message=message):
                self.assertFalse(registry.prepare(name, arguments, message).always_confirm)

    def test_completing_or_moving_a_found_reminder_is_direct_when_asked(self):
        registry = self.registry()
        registry.execute(registry.prepare("list_reminders", {}, "What reminders do I have?"))
        self.assertFalse(registry.prepare("complete_reminder", {"reminder_id": RID},
                                          "Mark the professor one as complete").always_confirm)
        self.assertFalse(registry.prepare("reschedule_reminder", {"reminder_id": RID, "due_at": "2026-10-23"},
                                          "Push that to Friday").always_confirm)

    def test_unrequested_low_impact_calls_still_ask(self):
        registry = self.registry()
        registry.execute(registry.prepare("list_reminders", {}, "list"))
        for name, arguments, message in (
            ("start_timer", {"kind": "focus", "minutes": 25}, "What is a pomodoro session?"),
            ("create_reminder", {"title": "Call mom", "due_at": None}, "Mom called earlier"),
            ("complete_reminder", {"reminder_id": RID}, "What's due today?"),
            ("start_timer", {"kind": "timer", "minutes": 5, "remind_when_done": True}, "Set a 5 minute timer"),
        ):
            with self.subTest(message=message):
                self.assertTrue(registry.prepare(name, arguments, message).always_confirm)

    def test_validation_and_target_checks_still_apply_to_direct_actions(self):
        registry = self.registry()
        with self.assertRaises(tool_registry.ToolError):  # a reminder not found in this conversation
            registry.prepare("complete_reminder", {"reminder_id": RID}, "Mark it complete")
        with self.assertRaises(tool_registry.ToolError):  # schema limits
            registry.prepare("start_timer", {"kind": "timer", "minutes": 721}, "Set a timer")

    def test_consequential_actions_keep_their_confirmation(self):
        registry = self.registry()
        task = storage.create_task("Lab report", database_path=self.database)
        note = storage.create_note("N", "text", database_path=self.database)
        for name, arguments, message in (
            ("delete_task", {"task_id": task["id"]}, "Delete task 1 now, don't ask"),
            ("delete_note", {"note_id": note["id"]}, "Delete that note"),
        ):
            with self.subTest(name=name):
                self.assertTrue(registry.prepare(name, arguments, message).always_confirm)
        for name in google_workspace.CONSEQUENTIAL_OPERATIONS:
            definition = ToolRegistry(self.database)._definitions.get(name)
            if definition is not None:
                self.assertTrue(definition.always_confirm, name)

    def test_open_website_and_application_run_directly_only_when_the_user_names_them(self):
        apps = UtilitySettings({}, ("Calendar", "Visual Studio Code"))
        registry = ToolRegistry(self.database, utilities=apps)
        cases = [
            ("open_website", {"url": "https://github.com/"}, "Open GitHub", False),
            ("open_website", {"url": "https://www.youtube.com/"}, "Kris, open youtube.com", False),
            ("open_website", {"url": "https://github.com.evil.example/"}, "Open GitHub", True),
            ("open_website", {"url": "https://example.org/"}, "Open the link from that email", True),
            ("open_website", {"url": "https://github.com/"}, "What is GitHub?", True),
            ("open_application", {"name": "Visual Studio Code"}, "Launch visual studio code", False),
            ("open_application", {"name": "Calendar"}, "Open VS Code", True),
        ]
        for name, arguments, message, confirm in cases:
            with self.subTest(message=message, arguments=arguments):
                self.assertEqual(registry.prepare(name, arguments, message).always_confirm, confirm)
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pw@github.com/", "github.com"):
            with self.subTest(url=url), self.assertRaises(tool_registry.ToolError):
                registry.prepare("open_website", {"url": url}, f"open {url}")

    def test_direct_low_impact_actions_are_logged_as_changes(self):
        registry = self.registry()
        model = Mock(side_effect=[tool_call("start_timer", {"kind": "timer", "minutes": 3}),
                                  {"role": "assistant", "content": "Started."}])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=registry, model_client=model)
        result = runtime.run_turn("Set a 3 minute timer")
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual([(a["tool"], a["status"]) for a in result.activities], [("start_timer", "success")])


class GmailLowImpactTests(Stage3TestCase):
    def test_read_and_star_are_direct_for_a_message_found_and_named_by_the_user(self):
        registry = self.registry()
        self.run_tool(registry, "search_gmail", {"query": "numbers"}, "find the numbers email")
        for action, message in (("mark_email_read", "Mark it as read"), ("star_email", "Star that one"),
                                ("unstar_email", "Unstar it"), ("mark_email_unread", "Mark it unread")):
            with self.subTest(action=action):
                prepared = registry.prepare(action, {"message_id": "m1"}, message)
                self.assertFalse(prepared.always_confirm)
                self.assertTrue(registry.execute(prepared).ok)
        self.assertEqual(len(self.gmail.writes()), 4)

    def test_unfound_messages_unrequested_actions_and_other_mailbox_changes_ask_first(self):
        registry = self.registry()
        self.assertTrue(registry.prepare("mark_email_read", {"message_id": "m1"}, "Mark it as read").always_confirm)
        self.run_tool(registry, "search_gmail", {"query": "numbers"}, "find it")
        self.assertTrue(registry.prepare("mark_email_read", {"message_id": "m1"}, "What does it say?").always_confirm)
        for action, arguments in (("archive_email", {"message_id": "m1"}), ("trash_email", {"message_id": "m1"}),
                                  ("add_label", {"message_id": "m1", "label": "Finance/2027"})):
            with self.subTest(action=action):
                self.assertTrue(registry.prepare(action, arguments, f"{action} it now").always_confirm)
        self.assertEqual(self.gmail.writes(), [])


# --- Spoken confirmation: the server side -------------------------------------------------

class SpokenConfirmationServerTests(TempDatabase):
    """The page resolves a spoken "confirm" through /api/confirm with the confirmation ID it read
    out; the runtime accepts each ID once, so stale or repeated approvals never run anything."""

    def test_an_approval_runs_once_and_stale_or_repeated_ids_are_refused(self):
        runner = Mock(return_value=True)
        model = Mock(side_effect=[
            tool_call("open_application", {"name": "Calendar"}),  # the first turn waits; a new message cancels it
            tool_call("open_application", {"name": "Calendar"}, "call-2"), {"role": "assistant", "content": "Done."},
        ])
        app = web_app.create_app(self.database, utility_settings=UtilitySettings({}, ("Calendar",)),
                                 developer=DeveloperCompanion(), ui_settings={})
        client = app.test_client()
        with patch.object(assistant_runtime, "ask_ollama", model), \
                patch.object(computer_utilities, "run_open", runner), \
                patch.object(computer_utilities, "open_supported", return_value=True):
            conversation = client.post("/api/conversations", json={}).get_json()["conversation_id"]
            first = client.post("/api/chat", json={"conversation_id": conversation,
                                                   "message": "open my schedule app"}).get_json()["pending_confirmation"]
            # A new request replaces the pending action; the first ID is now stale.
            second = client.post("/api/chat", json={"conversation_id": conversation,
                                                    "message": "open my schedule app"}).get_json()["pending_confirmation"]
            stale = client.post("/api/confirm", json={"conversation_id": conversation,
                                                      "confirmation_id": first["confirmation_id"], "approved": True})
            self.assertEqual(stale.status_code, 409)
            runner.assert_not_called()
            approved = client.post("/api/confirm", json={"conversation_id": conversation,
                                                         "confirmation_id": second["confirmation_id"], "approved": True})
            self.assertEqual(approved.status_code, 200)
            repeated = client.post("/api/confirm", json={"conversation_id": conversation,
                                                         "confirmation_id": second["confirmation_id"], "approved": True})
            self.assertEqual(repeated.status_code, 409)
        runner.assert_called_once_with(["-a", "Calendar"])

    def test_spoken_words_in_a_chat_message_are_never_approval(self):
        task = storage.create_task("Lab report", database_path=self.database)
        model = Mock(side_effect=[tool_call("delete_task", {"task_id": task["id"]}),
                                  {"role": "assistant", "content": "Okay."}])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=ToolRegistry(self.database), model_client=model)
        pending = runtime.run_turn("Delete task 1").pending_confirmation
        result = runtime.run_turn("confirm")  # a message, not the approval request: it cancels instead
        self.assertIn("Cancelled", result.notice)
        self.assertEqual(len(storage.list_tasks(database_path=self.database)), 1)
        with self.assertRaises(assistant_runtime.RuntimeBusyError):
            runtime.resolve_confirmation(pending["confirmation_id"], approved=True)


# --- Mac control ----------------------------------------------------------------------------

class FakeMac:
    """Answers mac_control's fixed scripts like macOS would; records the arguments."""

    def __init__(self, volume=40, muted=False, running=("Music",), denied=False):
        self.volume, self.muted, self.running, self.denied = volume, muted, set(running), denied
        self.state = "paused"
        self.calls = []

    def __call__(self, script, arguments):
        self.calls.append((script, list(arguments)))
        done = lambda out: subprocess.CompletedProcess([], 0, out + "\n", "")
        if script == mac_control.GET_VOLUME_SCRIPT:
            return done(f"{self.volume}\x1f{str(self.muted).lower()}")
        if script == mac_control.SET_VOLUME_SCRIPT:
            self.volume = int(arguments[0])
            return done(f"{self.volume}\x1f{str(self.muted).lower()}")
        if script == mac_control.SET_MUTED_SCRIPT:
            self.muted = arguments[0] == "true"
            return done(f"{self.volume}\x1f{str(self.muted).lower()}")
        if script == mac_control.RUNNING_SCRIPT:
            return done(str(arguments[0] in self.running).lower())
        player = next(name for name, bundle in mac_control.PLAYERS.items() if bundle in script)
        if player not in self.running:
            return done("not_running")
        if self.denied:
            return subprocess.CompletedProcess([], 1, "", "execution error: Not authorized to send Apple events to Music. (-1743)")
        if arguments:
            self.state = {"play": "playing", "pause": "paused", "toggle": "playing"}.get(arguments[0], self.state)
        return done(f"{self.state}\x1fSo What\x1fMiles Davis")


class MacControlTests(TempDatabase):
    def registry(self, fake):
        return ToolRegistry(self.database, mac=MacControl(runner=fake),
                            utilities=UtilitySettings({}, ("Music", "Safari")))

    def test_volume_is_validated_set_and_read_back(self):
        fake = FakeMac()
        control = MacControl(runner=fake)
        self.assertEqual(control.set_volume(70), {"volume": 70, "muted": False})
        for bad in (-1, 101, 50.5, True, "60"):
            with self.subTest(level=bad), self.assertRaises(MacControlError):
                control.set_volume(bad)
        self.assertEqual(fake.calls[-1][1], ["70"])  # the invalid values never reached macOS
        registry = self.registry(fake)
        for arguments in ({"level": 101}, {"change": 200}, {"level": "loud"}):
            with self.subTest(arguments=arguments), self.assertRaises(tool_registry.ToolError):
                registry.prepare("set_volume", arguments, "volume")

    def test_relative_changes_are_clamped_and_reported_with_the_previous_level(self):
        fake = FakeMac(volume=95)
        registry = self.registry(fake)
        prepared = registry.prepare("set_volume", {"change": 10}, "Turn it up a bit")
        self.assertFalse(prepared.always_confirm)
        outcome = registry.execute(prepared)
        self.assertTrue(outcome.ok)
        self.assertEqual(fake.volume, 100)
        self.assertIn("(was 95%)", outcome.message)
        both = registry.execute(registry.prepare("set_volume", {"level": 5, "change": 5}, "volume"))
        self.assertFalse(both.ok)

    def test_mute_and_unmute_are_verified(self):
        fake = FakeMac()
        registry = self.registry(fake)
        self.assertEqual(registry.execute(registry.prepare("set_muted", {"muted": True}, "Mute")).message, "Sound muted.")
        self.assertTrue(fake.muted)
        broken = MacControl(runner=lambda s, a: subprocess.CompletedProcess([], 0, "40\x1ffalse", ""))
        with self.assertRaises(MacControlError) as raised:
            broken.set_muted(True)
        self.assertIn("did not mute", raised.exception.message)

    def test_outputs_without_a_settable_volume_are_reported(self):
        control = MacControl(runner=lambda s, a: subprocess.CompletedProcess([], 0, "missing value\x1ffalse", ""))
        with self.assertRaises(MacControlError) as raised:
            control.volume()
        self.assertEqual(raised.exception.kind, "unavailable")

    def test_playback_only_controls_a_running_player_and_reports_permission_errors(self):
        fake = FakeMac(running=("Music",))
        registry = self.registry(fake)
        outcome = registry.execute(registry.prepare("media_control", {"action": "play"}, "Play some music"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, "Music is playing: So What by Miles Davis.")
        spotify = registry.execute(registry.prepare("media_control", {"action": "pause", "player": "Spotify"}, "pause"))
        self.assertFalse(spotify.ok)
        self.assertIn("not running", spotify.message)
        none = self.registry(FakeMac(running=()))
        self.assertIn("Neither", none.execute(none.prepare("media_control", {"action": "next"}, "next")).message)
        denied = self.registry(FakeMac(denied=True))
        result = denied.execute(denied.prepare("media_control", {"action": "pause"}, "pause"))
        self.assertFalse(result.ok)
        self.assertIn("Privacy & Security > Automation", result.message)
        with self.assertRaises(tool_registry.ToolError):
            registry.prepare("media_control", {"action": "shuffle"}, "shuffle")

    def test_player_ids_are_fixed_and_requests_are_arguments(self):
        fake = FakeMac()
        MacControl(runner=fake).media("Music", "next")
        script, arguments = fake.calls[-1]
        self.assertIn('application id "com.apple.Music"', script)
        self.assertEqual(arguments, ["next"])
        with self.assertRaises(MacControlError):
            MacControl(runner=fake).media("Terminal", "play")

    def test_app_status_uses_the_approved_list(self):
        registry = self.registry(FakeMac(running=("Music",)))
        self.assertEqual(registry.execute(registry.prepare("get_app_status", {"name": "Music"}, "?")).message,
                         "Music is running.")
        with self.assertRaises(tool_registry.ToolError):
            registry.prepare("get_app_status", {"name": "Terminal"}, "?")

    def test_mac_tools_are_absent_without_mac_control(self):
        names = {d["function"]["name"] for d in ToolRegistry(self.database).definitions()}
        self.assertFalse({"set_volume", "set_muted", "media_control"} & names)


class SystemAwarenessTests(TempDatabase):
    def test_system_info_includes_load_and_uptime_or_says_they_are_unavailable(self):
        info = ToolRegistry._load_and_uptime()
        self.assertTrue(info["available"])
        self.assertEqual(len(info["load_average"]), 3)
        with patch.object(tool_registry.os, "getloadavg", side_effect=OSError):
            self.assertEqual(ToolRegistry._load_and_uptime(),
                             {"available": False, "reason": "Load averages are unavailable."})
        telemetry = Mock()
        telemetry.snapshot.return_value = {"metrics": {"cpu": {"available": False, "reason": "warming up"}}}
        outcome = ToolRegistry(self.database, telemetry=telemetry).execute(
            ToolRegistry(self.database).prepare("get_system_info", {}, "How is my Mac doing?"))
        self.assertIn("load_and_uptime", outcome.data)


# --- Developer companion --------------------------------------------------------------------

@unittest.skipUnless(developer.GIT.is_file(), "requires /usr/bin/git")
class GitInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.repo = self.home / "proj"
        self.repo.mkdir()
        environment = {**os.environ, "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "t@example.org",
                       "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "t@example.org"}
        git = lambda *a: subprocess.run([str(developer.GIT), *a], cwd=self.repo, env=environment,
                                        capture_output=True, check=True)
        git("init", "-q", "-b", "main")
        (self.repo / "app.py").write_text("print('one')\n")
        (self.repo / ".env").write_text("TOKEN=super-secret-value\n")
        git("add", "app.py")
        git("commit", "-q", "-m", "First commit")
        git("remote", "add", "origin", "https://user:ghp_abcdefghijklmnopqrstuvwxyz123456@github.com/me/proj.git")
        (self.repo / "app.py").write_text("print('two')\nprint('three')\n")
        (self.repo / "notes.md").write_text("draft\n")
        self.companion = DeveloperCompanion(build_developer_settings({"Proj": str(self.repo)}, {}, {}, self.home))

    def tearDown(self):
        self.temp.cleanup()

    def test_status_diff_and_commits_are_read_only_summaries(self):
        status = self.companion.git_status(None)  # one project: it is the default
        self.assertEqual(status["branch"], "main")
        self.assertFalse(status["clean"])
        self.assertEqual(status["unstaged"], ["modified: app.py"])
        self.assertEqual(sorted(status["untracked"]), [".env", "notes.md"])
        diff = self.companion.diff_summary("proj")
        self.assertEqual(diff["unstaged"]["files"], [{"file": "app.py", "added": 2, "removed": 1, "binary": False}])
        self.assertEqual(diff["untracked_files"], 2)
        commits = self.companion.recent_commits("Proj", 5)["commits"]
        self.assertEqual([c["subject"] for c in commits], ["First commit"])
        info = self.companion.repository_info("Proj")
        self.assertEqual(info["remotes"], {"origin": "https://github.com/me/proj.git"})
        everything = repr((status, diff, commits, info))
        self.assertNotIn("super-secret-value", everything)
        self.assertNotIn("ghp_", everything)
        self.assertNotIn("print('two')", everything)  # no file contents

    def test_mutating_git_commands_are_refused_before_git_runs(self):
        runner = Mock()
        companion = DeveloperCompanion(self.companion.settings, git_runner=runner)
        for arguments in (("commit", "-m", "x"), ("push",), ("reset", "--hard"), ("checkout", "main"),
                          ("add", "."), ("remote", "add", "x", "y"), ("stash",), ("clean", "-fd")):
            with self.subTest(arguments=arguments), self.assertRaises(DeveloperError):
                companion._git(self.repo, *arguments)
        runner.assert_not_called()

    def test_unknown_projects_and_non_repositories_are_reported(self):
        plain = self.home / "plain"
        plain.mkdir()
        companion = DeveloperCompanion(build_developer_settings({"A": str(self.repo), "B": str(plain)}, {}, {}, self.home))
        with self.assertRaises(DeveloperError) as raised:
            companion.git_status(None)
        self.assertIn("Which project", raised.exception.message)
        with self.assertRaises(DeveloperError) as raised:
            companion.git_status("B")
        self.assertEqual(raised.exception.kind, "not_a_repository")
        with self.assertRaises(DeveloperError):
            companion.git_status("../etc")

    def test_registry_tools_answer_uncommitted_change_questions(self):
        registry = ToolRegistry(self.home / "r.sqlite3", developer=self.companion)
        outcome = registry.execute(registry.prepare("git_status", {}, "Do I have uncommitted changes?"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, "Proj on main: 0 staged, 1 unstaged, 2 untracked.")
        with self.assertRaises(tool_registry.ToolError):
            registry.prepare("git_status", {"project": "/etc"}, "status")


class ProjectSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        (self.home / "proj").mkdir()
        (self.home / "Library").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_projects_outside_home_shells_and_escaping_programs_are_ignored(self):
        with self.assertLogs("developer", level="WARNING"):
            settings = build_developer_settings(
                {"Proj": str(self.home / "proj"), "Root": "/", "Lib": str(self.home / "Library"), "Gone": "~/nope"},
                {"Proj": "bash -c 'rm -rf ~'\n../outside/run\npython3 -m unittest\nsh run.sh",
                 "Unknown": "python3 -m pytest"},
                {"editor": "Visual Studio Code; rm", "test_timeout_seconds": "99999"},
                self.home,
            )
        self.assertEqual(list(settings.projects), ["Proj"])
        self.assertEqual(len(settings.tests["Proj"]), 1)
        self.assertEqual(settings.tests["Proj"][0][1:], ("-m", "unittest"))
        self.assertIsNone(settings.editor)
        self.assertEqual(settings.test_timeout, developer.TEST_TIMEOUT_RANGE[1])


class TestRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def companion(self, commands, runner=None, timeout=60):
        settings = DeveloperSettings({"Proj": self.project}, {"Proj": tuple(tuple(c) for c in commands)}, None, timeout)
        return DeveloperCompanion(settings, process_runner=runner)

    def test_exit_status_decides_passed_and_failed(self):
        runner = Mock(side_effect=[(0, "Ran 3 tests in 0.1s\n\nOK\n", False),
                                   (1, "Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n", False)])
        result = self.companion([[sys.executable, "-m", "unittest"]] * 2, runner).run_tests("Proj")
        self.assertEqual([run["status"] for run in result["runs"]], ["passed", "failed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["runs"][1]["summary"], "Ran 2 tests in 0.1s; FAILED (failures=1)")

    def test_ok_text_with_a_failing_exit_code_is_a_failure(self):
        runner = Mock(return_value=(2, "OK\n", False))
        self.assertEqual(self.companion([[sys.executable]], runner).run_tests("Proj")["status"], "failed")

    def test_timeouts_and_missing_programs_are_reported(self):
        runner = Mock(return_value=(None, "partial", True))
        result = self.companion([[sys.executable], ["/nonexistent/bin/pytest"]], runner).run_tests("Proj")
        self.assertEqual([run["status"] for run in result["runs"]], ["timed_out", "unavailable"])
        self.assertEqual(result["status"], "timed_out")
        runner.assert_called_once()  # the missing program was never started
        with self.assertRaises(DeveloperError):
            DeveloperCompanion(DeveloperSettings({"Proj": self.project})).run_tests("Proj")  # nothing configured

    def test_a_real_run_stops_the_process_group_at_the_timeout_and_strips_live_switches(self):
        script = self.project / "t.py"
        script.write_text("import os, subprocess, sys, time\n"
                          "print('live', os.environ.get('LIVE_REMINDERS'), os.environ.get('KEEP_ME'))\n"
                          "sys.stdout.flush()\n"
                          "if len(sys.argv) > 1: subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)\n")
        with patch.dict(os.environ, {"LIVE_REMINDERS": "1", "KEEP_ME": "yes"}):
            passed = self.companion([[sys.executable, str(script)]]).run_tests("Proj")
            self.assertEqual(passed["status"], "passed")
            self.assertIn("live None yes", passed["runs"][0]["output_tail"])
            started = time.monotonic()
            slow = DeveloperCompanion(DeveloperSettings({"Proj": self.project}, {"Proj": ((sys.executable, str(script), "slow"),)},
                                                        None, 1)).run_tests("Proj")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(slow["status"], "timed_out")
        self.assertIsNone(slow["runs"][0]["exit_code"])

    def test_output_is_bounded_and_credentials_are_masked(self):
        runner = Mock(return_value=(0, "x" * 10000 + "\nGITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123\npassword: hunter2\nOK\n", False))
        run = self.companion([[sys.executable]], runner).run_tests("Proj")["runs"][0]
        self.assertLessEqual(len(run["output_tail"]), developer.MAX_OUTPUT_TAIL)
        self.assertNotIn("ghp_abc", run["output_tail"])
        self.assertNotIn("hunter2", run["output_tail"])

    def test_registry_runs_tests_directly_only_when_asked_and_keeps_the_latest_result(self):
        runner = Mock(return_value=(0, "OK\n", False))
        registry = ToolRegistry(self.project / "r.sqlite3", developer=self.companion([[sys.executable]], runner))
        self.assertTrue(registry.prepare("run_project_tests", {}, "How is the project going?").always_confirm)
        self.assertIn("No test run", registry.execute(registry.prepare("get_test_results", {}, "?")).message)
        prepared = registry.prepare("run_project_tests", {}, "Run the tests for this project")
        self.assertFalse(prepared.always_confirm)
        self.assertIn("Tests for Proj passed", registry.execute(prepared).message)
        self.assertIn("passed", registry.execute(registry.prepare("get_test_results", {}, "?")).message)

    def test_open_in_editor_uses_the_configured_editor_with_argument_lists(self):
        runner = Mock(return_value=True)
        settings = DeveloperSettings({"Proj": self.project}, {}, "Visual Studio Code")
        registry = ToolRegistry(self.project / "r.sqlite3", developer=DeveloperCompanion(settings), open_runner=runner)
        prepared = registry.prepare("open_in_editor", {"path": "Proj"}, "Open Proj in my editor")
        self.assertFalse(prepared.always_confirm)
        bundle = Path("/Applications/Visual Studio Code.app")
        with patch.object(computer_utilities, "open_supported", return_value=True), \
                patch.object(computer_utilities, "find_application", return_value=bundle):
            self.assertTrue(registry.execute(prepared).ok)
        runner.assert_called_once_with(["-a", str(bundle), str(self.project)])


class MutationBoundaryTests(unittest.TestCase):
    def test_no_file_install_or_git_mutation_tools_exist_and_none_could_skip_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = DeveloperSettings({"P": Path(folder)}, {"P": ((sys.executable,),)}, "Visual Studio Code")
            registry = ToolRegistry(Path(folder) / "r.sqlite3", developer=DeveloperCompanion(settings),
                                    mac=MacControl(runner=FakeMac()))
        names = {d["function"]["name"] for d in registry.definitions()}
        self.assertFalse(names & (tool_registry.DEVELOPER_MUTATIONS | tool_registry.FORBIDDEN_TOOLS))
        unconfirmed = tool_registry.ToolDefinition("git_commit", "", {}, lambda a: None, direct_write=True)
        with self.assertRaises(ValueError):
            tool_registry.check_confirmation_boundaries({"git_commit": unconfirmed})
        shell = tool_registry.ToolDefinition("run_command", "", {}, lambda a: None, always_confirm=True)
        with self.assertRaises(ValueError):
            tool_registry.check_confirmation_boundaries({"run_command": shell})
        confirmed = tool_registry.ToolDefinition("install_package", "", {}, lambda a: None, always_confirm=True)
        tool_registry.check_confirmation_boundaries({"install_package": confirmed})  # allowed only like this


# --- Tasks tab ------------------------------------------------------------------------------

class TasksTabTests(TempDatabase):
    def test_the_tab_is_hidden_by_default_and_tasks_keep_working(self):
        storage.create_task("Existing task", database_path=self.database)
        client = web_app.create_app(self.database, ui_settings={}, developer=DeveloperCompanion()).test_client()
        page = client.get("/").get_data(as_text=True)
        self.assertNotIn('data-view="tasks"', page)
        self.assertIn('id="tasks-view"', page)  # still in the page (hidden), so the script finds its elements
        self.assertIn('data-view="notes"', page)
        self.assertEqual([t["title"] for t in client.get("/api/tasks").get_json()["tasks"]], ["Existing task"])
        registry = ToolRegistry(self.database)
        prepared = registry.prepare("create_task", {"title": "Finish lab report"}, "Add finish lab report to my tasks")
        self.assertFalse(prepared.always_confirm)
        self.assertTrue(registry.execute(prepared).ok)
        self.assertEqual(len(storage.list_tasks(database_path=self.database)), 2)

    def test_the_tab_returns_with_the_ui_switch(self):
        client = web_app.create_app(self.database, ui_settings={"tasks_tab": True},
                                    developer=DeveloperCompanion()).test_client()
        self.assertIn('data-view="tasks"', client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
