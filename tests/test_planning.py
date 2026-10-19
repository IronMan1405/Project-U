"""Daily briefing, Apple Reminders, and K.R.I.S. timers and focus sessions.

Reminders and Google are fakes here (no osascript runs, nothing is sent). LiveRemindersTests
(LIVE_REMINDERS=1) talks to the real Reminders app and leaves one completed test reminder;
LiveNotificationTests (LIVE_NOTIFY=1) shows one real macOS notification.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import google_workspace
import notifications
import reminders
import storage
import timers
import web_app
from briefing import build_briefing
from reminders import AppleReminders, Due, RemindersError
from timers import TimerError, TimerService
from tool_registry import ToolError, ToolRegistry


class FixedZone:
    """Run a test in a fixed local time zone (the briefing and due dates use the Mac's zone)."""

    def __init__(self, zone):
        self.zone = zone

    def __enter__(self):
        self.previous = os.environ.get("TZ")
        os.environ["TZ"] = self.zone
        time.tzset()

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous
        time.tzset()


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class TempDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "p.sqlite3"
        storage.initialize_database(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()


# --- Timers -------------------------------------------------------------------------------

class TimerTests(TempDatabase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc))
        self.sent = []
        self.service = self.make()

    def make(self):
        return TimerService(self.database, notifier=lambda title, message: self.sent.append(message) or True,
                            clock=self.clock)

    def test_a_timer_counts_down_finishes_once_and_notifies_once(self):
        timer = self.service.start("timer", 25 * 60, "Study")
        self.assertEqual((timer["status"], timer["remaining_seconds"], timer["name"]),
                         ("running", 1500, '25-minute timer "Study"'))
        self.clock.advance(minutes=10)
        self.assertEqual(self.service.active()[0]["remaining_seconds"], 900)
        self.clock.advance(minutes=15)
        finished = self.service.process_due()
        self.assertEqual([t["status"] for t in finished], ["done"])
        self.assertEqual(self.service.process_due(), [])  # never twice
        self.assertEqual(self.sent, ['Your 25-minute timer "Study" is done.'])
        self.assertEqual(self.service.active(), [])
        self.assertEqual([t["status"] for t in self.service.recent()], ["done"])

    def test_a_break_after_that_starts_exactly_when_the_focus_session_ends(self):
        focus = self.service.start("focus", 50 * 60)
        pause = self.service.start("break", 10 * 60, after_timer_id=focus["timer_id"])
        self.assertEqual(pause["status"], "queued")
        self.clock.advance(minutes=53)  # the check ran three minutes late
        self.service.process_due()
        running = self.service.active()
        self.assertEqual([(t["kind"], t["status"], t["remaining_seconds"]) for t in running], [("break", "running", 420)])

    def test_cancel_one_the_only_one_or_ask_which(self):
        with self.assertRaisesRegex(TimerError, "No timer is running"):
            self.service.cancel()
        study = self.service.start("timer", 600, "Study")
        self.assertEqual([t["status"] for t in self.service.cancel()], ["cancelled"])
        first = self.service.start("timer", 600, "Tea")
        self.service.start("focus", 1200)
        with self.assertRaisesRegex(TimerError, "Several timers are running .*Ask which one"):
            self.service.cancel()
        self.assertEqual(self.service.cancel(first["timer_id"])[0]["timer_id"], first["timer_id"])
        with self.assertRaisesRegex(TimerError, "not running or queued"):
            self.service.cancel(study["timer_id"])
        self.clock.advance(hours=1)
        self.service.process_due()
        self.assertEqual(self.sent, ["Your 20-minute focus session is done."])  # cancelled ones never alert

    def test_cancelling_a_timer_cancels_what_was_queued_after_it(self):
        focus = self.service.start("focus", 1500)
        self.service.start("break", 300, after_timer_id=focus["timer_id"])
        self.assertEqual(len(self.service.cancel(focus["timer_id"])), 2)
        self.assertEqual(self.service.active(), [])

    def test_names_read_naturally(self):
        for seconds, kind, label, name in ((45, "timer", "", "45-second timer"), (1500, "focus", "Lab", '25-minute focus session "Lab"'),
                                           (90, "break", "", "1 minute 30 seconds break")):
            self.assertEqual(timers.timer_name({"duration_seconds": seconds, "kind": kind, "label": label}), name)

    def test_limits(self):
        for seconds in (5, 13 * 3600):
            with self.assertRaises(TimerError):
                self.service.start("timer", seconds)
        with self.assertRaises(TimerError):
            self.service.start("alarm", 60)
        with self.assertRaisesRegex(TimerError, "not running or queued"):
            self.service.start("break", 60, after_timer_id=999)
        for _ in range(timers.MAX_ACTIVE):
            self.service.start("timer", 60)
        with self.assertRaisesRegex(TimerError, "already running"):
            self.service.start("timer", 60)

    def test_timers_survive_a_restart_and_ones_that_ended_meanwhile_are_missed_not_done(self):
        keeps = self.service.start("timer", 3600, "Long")
        ends = self.service.start("timer", 300, "Short")
        self.clock.advance(minutes=20)  # K.R.I.S. was closed for these 20 minutes
        restarted = self.make()
        recovered = restarted.recover()
        self.assertEqual([(t["timer_id"], t["status"]) for t in recovered], [(ends["timer_id"], "missed")])
        self.assertIn("while K.R.I.S. was not running", self.sent[-1])
        self.assertEqual([(t["timer_id"], t["remaining_seconds"]) for t in restarted.active()],
                         [(keeps["timer_id"], 2400)])

    def test_a_notification_failure_never_breaks_a_timer(self):
        service = TimerService(self.database, notifier=Mock(side_effect=OSError("no")), clock=self.clock)
        service.start("timer", 60)
        self.clock.advance(minutes=2)
        with self.assertLogs("timers", "WARNING"):
            self.assertEqual(len(service.process_due()), 1)

    def test_background_checker_starts_recovers_and_stops(self):
        service = self.make()
        with patch.object(service, "recover") as recover:
            service.start_background()
            service.start_background()  # idempotent
            recover.assert_called_once()
        service.stop_background()
        self.assertIsNone(service._thread)


# --- Apple Reminders (fake osascript) ------------------------------------------------------

RID = "x-apple-reminder://ABCDEF12-3456-7890-ABCD-EF1234567890"


def record(rid=RID, title="Email the professor", list_name="Reminders", due="", all_day="", completed="false"):
    return "\x1f".join([rid, title, list_name, due, all_day, completed])


class FakeOsascript:
    """Answers each fixed script by name and records the arguments it was given."""

    def __init__(self):
        self.open = []
        self.calls = []
        self.fail = None

    def __call__(self, script, arguments):
        name = next(n for n in ("LIST", "CREATE", "COMPLETE", "RESCHEDULE", "GET")
                    if getattr(reminders, f"{n}_SCRIPT") == script)
        self.calls.append((name, list(arguments)))
        if self.fail:
            return subprocess.CompletedProcess([], 1, "", self.fail)
        if name == "LIST":
            return subprocess.CompletedProcess([], 0, "\x1e".join(self.open), "")
        if name == "CREATE":
            title, list_name, _, mode, y, mo, d, h, mi = arguments
            # Like the real app (macOS 26): `allday due date` is reported for timed reminders too, and
            # an all-day reminder's `due date` reads back as midnight.
            day = f"{int(y):04}-{int(mo):02}-{int(d):02}"
            due = f"{day}T{int(h):02}:{int(mi):02}:00" if mode == "time" else (f"{day}T00:00:00" if mode == "day" else "")
            all_day = f"{day}T00:00:00" if mode in ("time", "day") else ""
            return subprocess.CompletedProcess([], 0, record(title=title, list_name=list_name or "Reminders", due=due, all_day=all_day), "")
        if name == "COMPLETE":
            return subprocess.CompletedProcess([], 0, record(completed="true"), "")
        if name == "RESCHEDULE":
            _, mode, y, mo, d, h, mi = arguments
            return subprocess.CompletedProcess([], 0, record(due=f"{int(y):04}-{int(mo):02}-{int(d):02}T{int(h):02}:{int(mi):02}:00"), "")
        return subprocess.CompletedProcess([], 0, record(), "")


class RemindersTests(unittest.TestCase):
    def setUp(self):
        self.osascript = FakeOsascript()
        self.reminders = AppleReminders(runner=self.osascript)

    def test_create_passes_everything_as_arguments_and_reads_it_back(self):
        title = 'Email the "professor" -e do shell script'
        item = self.reminders.create(title, Due(datetime(2026, 10, 19, 18, 0)), notes="About the lab")
        self.assertEqual(item["due"], "2026-10-19 18:00")
        name, arguments = self.osascript.calls[-1]
        self.assertEqual(name, "CREATE")
        self.assertEqual(arguments, [title, "", "About the lab", "time", "2026", "10", "19", "18", "0"])
        self.assertNotIn(title, reminders.CREATE_SCRIPT)  # data never becomes code

    def test_all_day_and_undated_reminders(self):
        self.assertTrue(self.reminders.create("Pay rent", Due(date(2026, 10, 20)))["all_day"])
        self.assertIsNone(self.reminders.create("Buy stamps", None)["due"])

    def test_an_exact_duplicate_is_refused_and_nothing_is_created(self):
        self.osascript.open = [record(title="Email the professor!", due="2026-10-19T09:00:00")]
        with self.assertRaises(RemindersError) as raised:
            self.reminders.create("email the professor", Due(datetime(2026, 10, 19, 18, 0)))
        self.assertEqual(raised.exception.kind, "duplicate")
        self.assertNotIn("CREATE", [name for name, _ in self.osascript.calls])
        # A different day is not a duplicate.
        self.reminders.create("Email the professor", Due(datetime(2026, 10, 20, 18, 0)))

    def test_a_change_that_does_not_read_back_is_not_reported_as_done(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, "", ""),
                                   subprocess.CompletedProcess([], 0, record(title="Something else"), "")])
        with self.assertRaisesRegex(RemindersError, 'was added to Reminders, but Reminders reports it as "Something else"'):
            AppleReminders(runner=runner).create("Email the professor", None)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, record(completed="false"), ""))
        with self.assertRaisesRegex(RemindersError, "did not mark"):
            AppleReminders(runner=runner).complete(RID)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, record(due="2026-10-19T09:00:00"), ""))
        with self.assertRaisesRegex(RemindersError, "did not save the new due time"):
            AppleReminders(runner=runner).reschedule(RID, Due(datetime(2026, 10, 23, 9, 0)))

    def test_a_timed_reminder_is_not_mistaken_for_an_all_day_one(self):
        parse = AppleReminders._parse
        timed = parse(record(due="2026-10-20T09:00:00", all_day="2026-10-20T00:00:00"))
        self.assertEqual((timed["due"], timed["all_day"]), ("2026-10-20 09:00", False))
        all_day = parse(record(due="2026-10-20T00:00:00", all_day="2026-10-20T00:00:00"))
        self.assertEqual((all_day["due"], all_day["all_day"]), ("2026-10-20", True))
        only_all_day = parse(record(all_day="2026-10-20T00:00:00"))
        self.assertEqual((only_all_day["due"], only_all_day["all_day"]), ("2026-10-20", True))
        self.assertIsNone(parse(record())["due"])

    def test_complete_and_reschedule(self):
        self.assertTrue(self.reminders.complete(RID)["completed"])
        self.assertEqual(self.reminders.reschedule(RID, Due(datetime(2026, 10, 23, 9, 30)))["due"], "2026-10-23 09:30")

    def test_errors_are_mapped_to_clear_messages(self):
        for stderr, kind in (
            ("execution error: Not authorized to send Apple events to Reminders. (-1743)", "denied"),
            ("execution error: Reminders got an error: Can’t get reminder id \"x\". (-1728)", "not_found"),
            ("execution error: Application isn’t running. (-600)", "unavailable"),
            ("something else (-2700)", "failed"),
        ):
            self.osascript.fail = stderr
            with self.subTest(kind=kind), self.assertRaises(RemindersError) as raised:
                self.reminders.list_open()
            self.assertEqual(raised.exception.kind, kind)
        timeout = AppleReminders(runner=Mock(side_effect=subprocess.TimeoutExpired("osascript", 20)))
        with self.assertRaisesRegex(RemindersError, "did not respond"):
            timeout.list_open()

    def test_permission_denied_explains_what_to_allow(self):
        self.osascript.fail = "(-1743)"
        with self.assertRaises(RemindersError) as raised:
            self.reminders.list_open()
        self.assertIn("Privacy & Security", raised.exception.message)
        self.assertIn("Automation", raised.exception.message)

    def test_ids_and_inputs_are_validated_before_anything_runs(self):
        for bad in ("", "123", "x-apple-reminder://../../etc", "javascript:alert(1)"):
            with self.assertRaises(RemindersError):
                self.reminders.complete(bad)
        with self.assertRaises(RemindersError):
            self.reminders.create("", None)
        with self.assertRaises(RemindersError):
            self.reminders.create("x" * 201, None)
        self.assertEqual(self.osascript.calls, [])


# --- Briefing ------------------------------------------------------------------------------

class FakeGoogle:
    def __init__(self, events=None, error=None, enabled=True):
        self.settings = Mock(enabled=enabled)
        self.events, self.error, self.ranges = events or [], error, []

    def search_events(self, start, end, query, max_results):
        self.ranges.append((start, end))
        if self.error:
            raise self.error
        return {"events": self.events, "more_available": False}


class BriefingTests(TempDatabase):
    NOW = datetime(2026, 10, 19, 14, 0)  # local wall time, made aware inside the zone

    def brief(self, day="today", google=None, reminder_items=(), reminders_error=None, zone="Asia/Kolkata", now=None):
        osascript = FakeOsascript()
        osascript.open = list(reminder_items)
        if reminders_error:
            osascript.fail = reminders_error
        with FixedZone(zone):
            moment = (now or self.NOW).astimezone()
            return build_briefing(moment, day, database_path=self.database, google=google,
                                  reminders=AppleReminders(runner=osascript),
                                  timers=TimerService(self.database, clock=lambda: moment))

    def task(self, title, due_at=None, completed=False):
        created = storage.create_task(title, due_at=due_at, database_path=self.database)
        if completed:
            storage.set_task_completed(created["id"], True, self.database)
        return created

    def test_mixed_sources_are_sorted_into_overdue_due_and_undated(self):
        with FixedZone("Asia/Kolkata"):
            self.task("Submit lab report", storage.normalize_due_at("2026-10-18 17:00"))  # yesterday
            self.task("Call the bank", storage.normalize_due_at("2026-10-19 11:00"))      # earlier today
            self.task("Prepare slides", storage.normalize_due_at("2026-10-19 18:00"))     # later today
            self.task("Water the plants", "2026-10-19")
            self.task("Read paper")                                                      # no due date
            self.task("Old finished", "2026-10-01", completed=True)
            self.task("Next week", "2026-10-26")
        google = FakeGoogle([{"event_id": "e1", "title": "Design review", "start": "2026-10-19T15:00:00+05:30",
                              "end": "2026-10-19T16:00:00+05:30", "all_day": False}])
        result = self.brief(google=google, reminder_items=[
            record(title="Email the professor", due="2026-10-19T18:00:00"),
            record(rid=RID[:-1] + "1", title="Renew passport", due="2026-10-15T10:00:00"),
            record(rid=RID[:-1] + "2", title="Dentist", due="2026-10-25T10:00:00"),
        ])
        tasks = result["sources"]["tasks"]
        self.assertEqual([t["title"] for t in tasks["overdue"]["items"]], ["Submit lab report", "Call the bank"])
        self.assertEqual([t["title"] for t in tasks["due"]["items"]], ["Prepare slides", "Water the plants"])
        self.assertEqual([t["title"] for t in tasks["no_due_date"]["items"]], ["Read paper"])
        self.assertEqual(tasks["later_count"], 1)
        self.assertNotIn("Old finished", json.dumps(result))
        rem = result["sources"]["reminders"]
        self.assertEqual([r["title"] for r in rem["overdue"]["items"]], ["Renew passport"])
        self.assertEqual([r["title"] for r in rem["due"]["items"]], ["Email the professor"])
        self.assertEqual(result["sources"]["calendar"]["events"][0]["title"], "Design review")
        self.assertEqual(result["unavailable"], [])
        self.assertEqual((result["date"], result["weekday"]), ("2026-10-19", "Monday"))
        self.assertIn("UTC+05:30", result["timezone"])

    def test_the_calendar_range_is_the_local_day(self):
        google = FakeGoogle()
        self.brief(google=google, zone="America/New_York")
        self.assertEqual(google.ranges, [("2026-10-19T00:00:00-04:00", "2026-10-20T00:00:00-04:00")])
        self.brief("tomorrow", google=google, zone="America/New_York")
        self.assertEqual(google.ranges[-1][0], "2026-10-20T00:00:00-04:00")

    def test_tomorrow(self):
        with FixedZone("Asia/Kolkata"):
            self.task("Today thing", storage.normalize_due_at("2026-10-19 20:00"))
            self.task("Tomorrow thing", "2026-10-20")
        result = self.brief("tomorrow", reminder_items=[record(title="Later today", due="2026-10-19T20:00:00")])
        self.assertEqual([t["title"] for t in result["sources"]["tasks"]["due"]["items"]], ["Tomorrow thing"])
        self.assertEqual(result["sources"]["reminders"]["due"]["items"], [])  # today's are not tomorrow's

    def test_an_empty_day_is_reported_as_empty_not_unavailable(self):
        result = self.brief(google=FakeGoogle())
        self.assertEqual(result["sources"]["calendar"]["events"], [])
        self.assertEqual(result["sources"]["tasks"]["due"]["items"], [])
        self.assertEqual(result["unavailable"], [])

    def test_missing_and_failing_sources_are_named_never_shown_as_empty(self):
        result = self.brief(google=None, reminders_error="(-1743)")
        self.assertEqual(result["sources"]["calendar"]["status"], "not_connected")
        self.assertEqual(result["sources"]["reminders"]["status"], "unavailable")
        self.assertNotIn("events", result["sources"]["calendar"])
        self.assertEqual(len(result["unavailable"]), 2)
        failing = FakeGoogle(error=google_workspace.GoogleError("reconnect", "Reconnect Google to continue."))
        result = self.brief(google=failing)
        self.assertEqual(result["sources"]["calendar"], {"status": "unavailable", "reason": "Reconnect Google to continue."})
        self.assertEqual(result["sources"]["tasks"]["status"], "ok")  # one failure never hides the rest

    def test_the_same_item_in_tasks_and_reminders_appears_once(self):
        self.task("Finish lab report", "2026-10-19")
        result = self.brief(reminder_items=[record(title="Finish lab report.", due="2026-10-19T17:00:00")])
        due = result["sources"]["tasks"]["due"]["items"]
        self.assertEqual([(t["title"], t.get("also_in")) for t in due], [("Finish lab report", "Apple Reminders")])
        self.assertEqual(result["sources"]["reminders"]["due"]["items"], [])

    def test_active_timers_are_included(self):
        moment = datetime(2026, 10, 19, 8, 30, tzinfo=timezone.utc)
        TimerService(self.database, clock=lambda: moment).start("focus", 3000)
        result = self.brief(now=datetime(2026, 10, 19, 14, 0))
        self.assertEqual(result["sources"]["timers"]["active"][0]["kind"], "focus")


# --- Tools: intent, confirmation, ambiguity, parity -----------------------------------------

class PlanningToolTests(TempDatabase):
    def setUp(self):
        super().setUp()
        self.osascript = FakeOsascript()
        self.registry = ToolRegistry(self.database, reminders=AppleReminders(runner=self.osascript),
                                     timers=TimerService(self.database))

    def prepared(self, name, arguments, message):
        return self.registry.prepare(name, arguments, message)

    def test_reminder_tools_exist_only_when_reminders_are_available(self):
        names = {d["function"]["name"] for d in ToolRegistry(self.database).definitions()}
        self.assertTrue({"get_briefing", "start_timer", "list_timers", "cancel_timer"} <= names)
        self.assertFalse(any("reminder" in n for n in names))
        names = {d["function"]["name"] for d in self.registry.definitions()}
        self.assertTrue({"list_reminders", "create_reminder", "complete_reminder", "reschedule_reminder"} <= names)
        self.assertFalse(any("delete" in n and "reminder" in n for n in names))  # never deletes reminders

    def test_routine_requests_run_directly_and_typed_and_spoken_wording_match(self):
        for message in ("Remind me to email the professor at 6 PM.", "Kris, remind me to email the professor at 6 PM",
                        "hey kris remind me to email the professor at 6pm"):
            with self.subTest(message=message):
                call = self.prepared("create_reminder", {"title": "Email the professor", "due_at": "2026-10-19 18:00"}, message)
                self.assertFalse(call.always_confirm)
        for name, arguments, message in (
            ("create_task", {"title": "Finish my lab report", "due_at": "2026-10-20"}, "Kris, add finish my lab report to my tasks for tomorrow"),
            ("start_timer", {"kind": "timer", "minutes": 45, "label": "Study"}, "Set a 45-minute study timer."),
            ("start_timer", {"kind": "focus", "minutes": 50}, "Kris, start a 50-minute focus session"),
            ("cancel_timer", {}, "Cancel the timer."),
        ):
            with self.subTest(message=message):
                self.assertFalse(self.prepared(name, arguments, message).always_confirm)

    def test_a_write_the_user_did_not_ask_for_needs_confirmation(self):
        call = self.prepared("create_reminder", {"title": "Something"}, "What's on today?")
        self.assertTrue(call.always_confirm)
        self.assertEqual(call.summary, "Create reminder: Something")
        self.assertTrue(self.prepared("start_timer", {"kind": "timer", "minutes": 5}, "I'm tired.").always_confirm)

    def test_a_reminder_must_be_found_before_it_can_be_changed(self):
        for name, arguments in (("complete_reminder", {"reminder_id": RID}),
                                ("reschedule_reminder", {"reminder_id": RID, "due_at": "2026-10-23 09:00"})):
            with self.subTest(name=name), self.assertRaisesRegex(ToolError, "only reminders found"):
                self.prepared(name, arguments, "Mark that reminder as complete")
        self.osascript.open = [record(due="2026-10-19T18:00:00")]
        listed = self.registry.execute(self.prepared("list_reminders", {"range": "today"}, "What reminders do I have today?"))
        self.assertTrue(listed.ok)
        call = self.prepared("complete_reminder", {"reminder_id": RID}, "Mark the professor reminder as complete.")
        self.assertFalse(call.always_confirm)
        self.assertTrue(self.registry.execute(call).ok)
        move = self.prepared("reschedule_reminder", {"reminder_id": RID, "due_at": "2026-10-23 09:00"}, "Move that reminder to Friday at 9.")
        self.assertFalse(move.always_confirm)
        outcome = self.registry.execute(move)
        self.assertEqual((outcome.ok, outcome.message), (True, "Reminder moved to 2026-10-23 09:00."))

    def test_local_times_reach_reminders_as_local_wall_time(self):
        with FixedZone("America/New_York"):
            call = self.prepared("create_reminder", {"title": "Email the professor", "due_at": "2026-10-19 18:00"},
                                 "Remind me to email the professor at 6 PM")
            self.assertEqual(call.arguments["due_at"], "2026-10-19 22:00:00Z")  # stored canonical UTC
            self.registry.execute(call)
        self.assertEqual(self.osascript.calls[-1][1][3:], ["time", "2026", "10", "19", "18", "0"])  # 6 PM local

    def test_failures_are_reported_truthfully(self):
        self.osascript.fail = "(-1743)"
        outcome = self.registry.execute(self.prepared("create_reminder", {"title": "X"}, "Remind me to X"))
        self.assertFalse(outcome.ok)
        self.assertIn("not allowed to use Reminders", outcome.message)
        cancel = self.registry.execute(self.prepared("cancel_timer", {}, "Cancel the timer"))
        self.assertEqual((cancel.ok, cancel.message), (False, "No timer is running."))

    def test_timer_tools(self):
        started = self.registry.execute(self.prepared("start_timer", {"kind": "focus", "minutes": 50}, "Start a 50-minute focus session"))
        self.assertTrue(started.ok)
        self.assertIn("Started the 50-minute focus session", started.message)
        after = self.registry.execute(self.prepared(
            "start_timer", {"kind": "break", "minutes": 10, "after_timer_id": started.data["timer_id"]}, "Give me a 10-minute break after that"))
        self.assertIn("Queued", after.message)
        left = self.registry.execute(self.prepared("list_timers", {}, "How much time is left?"))
        self.assertEqual(len(left.data["active"]), 2)
        with self.assertRaises(ToolError):
            self.prepared("start_timer", {"kind": "timer", "minutes": 721}, "Set a timer")
        ambiguous = self.registry.execute(self.prepared("start_timer", {"kind": "timer", "minutes": 5}, "Set a 5-minute timer"))
        self.assertTrue(ambiguous.ok)
        outcome = self.registry.execute(self.prepared("cancel_timer", {}, "Cancel the timer"))
        self.assertFalse(outcome.ok)
        self.assertIn("Ask which one", outcome.message)

    def test_briefing_tool_names_what_is_unavailable(self):
        self.osascript.fail = "(-1743)"
        outcome = self.registry.execute(self.prepared("get_briefing", {}, "Kris, brief me."))
        self.assertTrue(outcome.ok)
        self.assertIn("Not available: calendar: Google Calendar is not connected.; reminders:", outcome.message)
        self.assertNotIn("_reminder_ids", outcome.data)


class TimerReminderTests(TempDatabase):
    """An Apple Reminder at a timer's end (only when asked), completed when the timer is cancelled."""

    def setUp(self):
        super().setUp()
        self.osascript = FakeOsascript()
        self.clock = Clock(datetime(2026, 10, 19, 9, 0, 20, tzinfo=timezone.utc))
        self.closed = []
        self.service = TimerService(self.database, clock=self.clock, reminder_closer=self.closed.append)
        self.registry = ToolRegistry(self.database, reminders=AppleReminders(runner=self.osascript), timers=self.service)

    def start(self, message, **arguments):
        call = self.registry.prepare("start_timer", {"kind": "focus", "minutes": 25, **arguments}, message)
        return call, self.registry.execute(call)

    def test_asked_for_reminder_is_created_verified_and_rounded_up_to_the_minute(self):
        with FixedZone("Asia/Kolkata"):
            call, outcome = self.start("Set a 25-minute focus session and remind me when it's over.", remind_when_done=True)
        self.assertFalse(call.always_confirm)
        self.assertEqual(call.summary, "Start a 25-minute focus session, with an Apple Reminder when it ends")
        name, arguments = self.osascript.calls[-1]
        self.assertEqual(name, "CREATE")
        self.assertEqual(arguments[0], "K.R.I.S. timer 1: 25-minute focus session ends")
        # Ends 09:25:20 UTC = 14:55:20 IST; Reminders has no seconds, so 14:56 (never early).
        self.assertEqual(arguments[3:], ["time", "2026", "10", "19", "14", "56"])
        self.assertIn("Apple Reminder set for 14:56; it alerts even if K.R.I.S. is closed.", outcome.message)
        self.assertEqual(self.service.active()[0]["reminder"]["state"], "open")

    def test_no_reminder_unless_asked_and_a_reminder_the_user_did_not_ask_for_needs_confirmation(self):
        self.start("Start a 25-minute focus session.")
        self.assertNotIn("CREATE", [n for n, _ in self.osascript.calls])
        call = self.registry.prepare("start_timer", {"kind": "focus", "minutes": 25, "remind_when_done": True},
                                     "Start a 25-minute focus session.")
        self.assertTrue(call.always_confirm)

    def test_a_failed_reminder_never_undoes_the_timer_and_is_reported(self):
        self.osascript.fail = "(-1743)"
        _, outcome = self.start("Start a focus session and remind me when it ends", remind_when_done=True)
        self.assertTrue(outcome.ok)
        self.assertIn("could not be created", outcome.message)
        self.assertIn("alerts only while K.R.I.S. is running", outcome.message)
        self.assertEqual(len(self.service.active()), 1)
        no_reminders = ToolRegistry(self.database, timers=self.service)
        outcome = no_reminders.execute(no_reminders.prepare(
            "start_timer", {"kind": "timer", "minutes": 5, "remind_when_done": True}, "Set a 5-minute timer and remind me"))
        self.assertIn("Apple Reminders is not available", outcome.message)

    def test_a_queued_break_gets_a_reminder_at_its_own_end(self):
        with FixedZone("UTC"):
            _, focus = self.start("Start a 25-minute focus session")
            call = self.registry.prepare("start_timer", {"kind": "break", "minutes": 10, "after_timer_id": focus.data["timer_id"],
                                                         "remind_when_done": True}, "Give me a 10-minute break after that and remind me when it's over")
            self.registry.execute(call)
        self.assertEqual(self.osascript.calls[-1][1][3:], ["time", "2026", "10", "19", "9", "36"])  # 09:35:20 -> 09:36

    def test_cancelling_completes_the_reminder_or_says_it_will_still_alert(self):
        with FixedZone("UTC"):
            self.start("Start a focus session and remind me when it's over", remind_when_done=True)
            outcome = self.registry.execute(self.registry.prepare("cancel_timer", {}, "Cancel the timer"))
        self.assertIn("Its reminder for 09:26 was marked complete.", outcome.message)
        self.assertEqual(self.osascript.calls[-1][0], "COMPLETE")
        with FixedZone("UTC"):
            self.start("Start a focus session and remind me when it's over", remind_when_done=True)
            self.osascript.fail = "(-600)"
            outcome = self.registry.execute(self.registry.prepare("cancel_timer", {}, "Cancel the timer"))
        self.assertTrue(outcome.ok)  # the timer itself was cancelled
        self.assertIn("could not be marked complete", outcome.message)
        self.assertIn("it will still alert at 09:26 unless you complete it in Reminders", outcome.message)

    def test_finished_timers_reminders_are_completed_after_they_alert(self):
        with FixedZone("UTC"):
            self.start("Start a focus session and remind me when it's over", remind_when_done=True)
        self.clock.advance(minutes=26)  # 09:26:20: ended, reminder due 09:26 just alerted
        self.service.process_due()
        self.assertEqual(self.service.close_finished_reminders(), [])  # give the reminder time to alert
        self.clock.advance(minutes=2)
        self.assertEqual(self.service.close_finished_reminders(), [1])
        self.assertEqual(self.closed, [RID])  # the fake Reminders gives every reminder this ID
        self.assertEqual(self.service.close_finished_reminders(), [])  # only once

    def test_a_reminder_that_cannot_be_completed_is_left_for_the_user(self):
        failing = TimerService(self.database, clock=self.clock, reminder_closer=Mock(side_effect=RemindersError("unavailable", "x")))
        timer = failing.start("timer", 60)
        failing.attach_reminder(timer["timer_id"], RID, self.clock() + timedelta(minutes=1))
        self.clock.advance(minutes=5)
        failing.process_due()
        with self.assertLogs("timers", "WARNING"):
            self.assertEqual(failing.close_finished_reminders(), [])
        self.assertEqual(failing.recent()[0]["reminder"]["state"], "failed")

    def test_an_older_timers_table_gains_the_reminder_columns(self):
        old = Path(self.temp_dir.name) / "old.sqlite3"
        import sqlite3
        with sqlite3.connect(old) as connection:
            connection.execute("CREATE TABLE timers (id INTEGER PRIMARY KEY, kind TEXT, label TEXT, duration_seconds INTEGER,"
                               " status TEXT, created_at TEXT, starts_at TEXT, ends_at TEXT, after_timer_id INTEGER, finished_at TEXT)")
        storage.initialize_database(old)
        with sqlite3.connect(old) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(timers)")}
        self.assertTrue({"reminder_id", "reminder_due", "reminder_state"} <= columns)


class PlanningRuntimeTests(TempDatabase):
    """Through the runtime (the typed and voice paths share it): activity and confirmations."""

    def test_activity_is_logged_and_a_failure_is_not_reported_as_success(self):
        osascript = FakeOsascript()
        osascript.fail = "(-1743)"
        registry = ToolRegistry(self.database, reminders=AppleReminders(runner=osascript), timers=TimerService(self.database))
        model = Mock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "create_reminder", "arguments": {"title": "Email the professor", "due_at": "2026-10-19 18:00"}}},
                {"id": "c2", "function": {"name": "start_timer", "arguments": {"kind": "timer", "minutes": 25}}}]},
            {"role": "assistant", "content": "The timer started; the reminder could not be created."},
        ])
        runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                     tool_registry=registry, model_client=model)
        result = runtime.run_turn("Remind me to email the professor at 6 PM and set a 25-minute timer")
        # Both were asked for, so both run directly (low-impact policy); the reminder's failure is
        # recorded as a failure and the timer, the second request in the sentence, still starts.
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual([(a["tool"], a["status"]) for a in result.activities],
                         [("create_reminder", "failed"), ("start_timer", "success")])

    def test_spoken_and_typed_requests_get_the_same_protection(self):
        task = storage.create_task("Lab report", database_path=self.database)
        for message in ("Delete the lab report task.", "Kris, delete the lab report task"):
            model = Mock(side_effect=[{"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "delete_task", "arguments": {"task_id": task["id"]}}}]}])
            runtime = assistant_runtime.AssistantRuntime("http://local", "m", database_path=self.database,
                                                         tool_registry=ToolRegistry(self.database), model_client=model)
            with self.subTest(message=message):
                self.assertEqual(runtime.run_turn(message).pending_confirmation["tool"], "delete_task")


class TimersApiTests(TempDatabase):
    def test_timers_endpoint_and_persistence_across_app_restarts(self):
        clock = Clock(datetime.now(timezone.utc))
        service = TimerService(self.database, clock=clock)
        service.start("focus", 1500, "Writing")
        client = web_app.create_app(self.database, timers=TimerService(self.database, clock=clock)).test_client()
        data = client.get("/api/timers").get_json()
        self.assertEqual([t["label"] for t in data["active"]], ["Writing"])
        self.assertEqual(client.get("/api/timers").headers["Cache-Control"], "no-store")
        clock.advance(minutes=30)
        restarted = TimerService(self.database, clock=clock)
        restarted.recover()
        data = web_app.create_app(self.database, timers=restarted).test_client().get("/api/timers").get_json()
        self.assertEqual([t["status"] for t in data["recently_finished"]], ["missed"])


class NotificationTests(unittest.TestCase):
    def test_text_is_passed_as_arguments_and_failures_are_quiet(self):
        with patch.object(notifications.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(notifications, "OSASCRIPT", Path("/bin/sh")), patch.object(notifications.sys, "platform", "darwin"):
            self.assertTrue(notifications.notify("K.R.I.S.", 'Your "x" timer is done; do shell script "rm"'))
        arguments, kwargs = run.call_args
        self.assertEqual(arguments[0][2:], ["K.R.I.S.", 'Your "x" timer is done; do shell script "rm"'])
        self.assertEqual(kwargs["input"], notifications.SCRIPT)
        with patch.object(notifications.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 10)), \
                patch.object(notifications, "OSASCRIPT", Path("/bin/sh")), patch.object(notifications.sys, "platform", "darwin"), \
                self.assertLogs("notifications", "WARNING"):
            self.assertFalse(notifications.notify("K.R.I.S.", "x"))


@unittest.skipUnless(os.environ.get("LIVE_REMINDERS") == "1", "set LIVE_REMINDERS=1 to use the real Reminders app")
class LiveRemindersTests(unittest.TestCase):
    """Creates one uniquely named reminder in the default list, moves it, and completes it (K.R.I.S.
    never deletes reminders, so a completed test reminder remains)."""

    def test_create_find_reschedule_complete(self):
        live = AppleReminders()
        title = f"K.R.I.S. live test {uuid.uuid4().hex[:8]}"
        tomorrow = datetime.now() + timedelta(days=1)
        created = live.create(title, Due(tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)))
        self.assertEqual(created["title"], title)
        self.assertIn(created["reminder_id"], [item["reminder_id"] for item in live.list_open()])
        with self.assertRaises(RemindersError):
            live.create(title, Due(tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)))  # duplicate
        moved = live.reschedule(created["reminder_id"], Due(tomorrow.replace(hour=11, minute=30, second=0, microsecond=0)))
        self.assertTrue(moved["due"].endswith("11:30"))
        self.assertTrue(live.complete(created["reminder_id"])["completed"])
        self.assertNotIn(created["reminder_id"], [item["reminder_id"] for item in live.list_open()])


@unittest.skipUnless(os.environ.get("LIVE_REMINDERS") == "1", "set LIVE_REMINDERS=1 to use the real Reminders app")
class LiveTimerReminderTests(TempDatabase):
    """A 30-minute timer with a real end reminder, then cancelled: the reminder must be completed."""

    def test_timer_reminder_is_created_then_completed_on_cancel(self):
        live = AppleReminders()
        registry = ToolRegistry(self.database, reminders=live, timers=TimerService(self.database))
        started = registry.execute(registry.prepare(
            "start_timer", {"kind": "focus", "minutes": 30, "label": "live test", "remind_when_done": True},
            "Start a 30-minute focus session and remind me when it's over"))
        self.assertIn("Apple Reminder set for", started.message, started.message)
        reminder_id = registry.timers.reminder_id(started.data["timer_id"])
        self.assertIn(reminder_id, [item["reminder_id"] for item in live.list_open()])
        cancelled = registry.execute(registry.prepare("cancel_timer", {}, "Cancel the timer"))
        self.assertIn("was marked complete", cancelled.message, cancelled.message)
        self.assertNotIn(reminder_id, [item["reminder_id"] for item in live.list_open()])


@unittest.skipUnless(os.environ.get("LIVE_NOTIFY") == "1", "set LIVE_NOTIFY=1 to show one real notification")
class LiveNotificationTests(unittest.TestCase):
    def test_a_notification_is_accepted(self):
        self.assertTrue(notifications.notify("K.R.I.S.", "Live notification test: you can ignore this."))


if __name__ == "__main__":
    unittest.main()
