"""Stage 4: Gmail sending and Calendar search/read/create/update/cancel (always confirmed)."""
import base64
import email
import email.policy
import json
import logging
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import google_workspace
import storage
import web_app
from assistant_runtime import AssistantRuntime, RuntimeBusyError
from computer_utilities import UtilitySettings
from google_workspace import GoogleError
from tests.test_gmail_stage3 import SECRET_BODY, FakeGmail, b64, headers
from tests.test_google_workspace import ALL_SCOPES, ProviderTestCase, api_failure
from tool_registry import ToolError, ToolRegistry
from voice import VoiceSettings

ZONE = "Asia/Kolkata"
NOW = datetime(2027, 1, 15, 10, 0, tzinfo=ZoneInfo(ZONE))  # "now" for every Calendar test
CAL = google_workspace.CALENDAR_API + "/calendars/primary/events"


def decoded(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)


def body_text(message):
    return message.get_content().replace("\r\n", "\n").rstrip("\n")


def timed(day, start, end, **extra):
    return {"start": {"dateTime": f"{day}T{start}:00+05:30", "timeZone": ZONE},
            "end": {"dateTime": f"{day}T{end}:00+05:30", "timeZone": ZONE}, **extra}


class FakeCalendar:
    """The primary calendar behind the event endpoints K.R.I.S. uses. Honours If-Match."""

    def __init__(self):
        self.events = {
            "past1event": {"summary": "Meeting with Professor X", **timed("2026-10-15", "15:00", "16:00")},
            "today1event": {"summary": "Stand-up", **timed("2027-01-15", "16:00", "16:15")},
            "john1event": {"summary": "Design review with John", "location": "Lab",
                           "attendees": [{"email": "john@example.org"}], **timed("2027-01-25", "14:00", "15:00")},
            "offsite20": {"summary": "Offsite planning", "start": {"date": "2027-02-05"}, "end": {"date": "2027-02-06"}},
            "john2event": {"summary": "Lunch with John", **timed("2027-02-01", "13:00", "14:00")},
            "conf90event": {"summary": "Conference talk", "description": "Slides due", **timed("2027-04-15", "09:00", "17:00")},
            "seriesweekly": {"summary": "Weekly sync", "recurrence": ["RRULE:FREQ=WEEKLY"], **timed("2027-01-20", "11:00", "11:30")},
        }
        for event_id, event in self.events.items():
            event.update(id=event_id, status="confirmed", etag=f'"{event_id}-1"', updated="2027-01-01T00:00:00Z")
        self.requests = []
        self.created = 0

    @staticmethod
    def _start(event):
        start = event["start"]
        value = datetime.fromisoformat(start.get("dateTime") or f"{start['date']}T00:00:00+05:30")
        if value.tzinfo is None:  # written as wall time plus timeZone, as K.R.I.S. sends it
            value = value.replace(tzinfo=ZoneInfo(start.get("timeZone", ZONE)))
        return value

    def __call__(self, request):
        url = urllib.parse.urlsplit(request.full_url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        method = request.get_method()
        body = json.loads(request.data.decode()) if request.data else None
        self.requests.append({"method": method, "path": url.path, "query": query, "body": body,
                              "if_match": request.get_header("If-match")})
        tail = url.path.split("/calendars/primary/events", 1)[1]
        if method == "GET" and tail == "":
            low = datetime.fromisoformat(query["timeMin"])
            high = datetime.fromisoformat(query["timeMax"]) if "timeMax" in query else None
            words = query.get("q", "").lower()
            items = [e for e in self.events.values() if e["status"] != "cancelled"
                     and self._start(e) >= low and (high is None or self._start(e) < high)
                     and (not words or words in json.dumps(e).lower())]
            items.sort(key=self._start)
            return {"items": items[: int(query["maxResults"])]}
        event_id = tail.lstrip("/")
        if method == "POST":
            self.created += 1
            event_id = f"created{self.created}"
            self.events[event_id] = {**body, "id": event_id, "status": "confirmed", "etag": f'"{event_id}-1"'}
            return self.events[event_id]
        if event_id not in self.events:
            raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)
        event = self.events[event_id]
        if method == "GET":
            return event
        if request.get_header("If-match") != event["etag"]:
            raise urllib.error.HTTPError(request.full_url, 412, "precondition", {}, None)
        if method == "PATCH":
            event.update(body)
            version = int(event["etag"].strip('"').rsplit("-", 1)[1]) + 1
            event["etag"] = f'"{event_id}-{version}"'
            return event
        if method == "DELETE":
            event["status"] = "cancelled"
            return {}
        raise AssertionError(method)

    def writes(self):
        return [r for r in self.requests if r["method"] != "GET"]


class Stage4TestCase(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.token_endpoint()
        self.gmail = FakeGmail()
        self.gmail.drafts = {
            "r-draft1": {"id": "r-draft1", "message": {"id": "d1", "threadId": "td1", "payload": {
                "mimeType": "text/plain", "headers": headers(To="grace@example.org", Subject="Lab booking"),
                "body": {"data": b64("Hi Grace,\nThursday at 2pm?\nAda")}}}},
        }
        self.sent = []
        self.calendar = FakeCalendar()
        self.google.on("GET", google_workspace.GMAIL_API, self.gmail_route)
        self.google.on("POST", google_workspace.GMAIL_API, self.gmail_route)
        for method in ("GET", "POST", "PATCH", "DELETE"):
            self.google.on(method, google_workspace.CALENDAR_API, self.calendar)

    def gmail_route(self, request):
        path = urllib.parse.urlsplit(request.full_url).path.split("/gmail/v1/users/me", 1)[1]
        if path == "/messages/send" or path == "/drafts/send":
            body = json.loads(request.data.decode())
            self.sent.append({"path": path, "body": body})
            return {"id": f"sent{len(self.sent)}", "threadId": body.get("threadId", "tnew")}
        if path == "/drafts" and request.get_method() == "POST":
            return {"id": "r-new", "message": {"id": "dm", "threadId": "tdm"}}
        if path == "/drafts" and request.get_method() == "GET":
            return {"drafts": [{"id": i} for i in self.gmail.drafts]}
        if path.startswith("/drafts/") and request.get_method() == "GET":
            draft_id = path.split("/")[2]
            if draft_id not in self.gmail.drafts:
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)
            return self.gmail.drafts[draft_id]
        return self.gmail(request)

    def connected(self, scopes=ALL_SCOPES, consent_version=google_workspace.CONSENT_VERSION, **kwargs):
        return super().connected(scopes=scopes, consent_version=consent_version, now=lambda: NOW, **kwargs)

    def registry(self, provider=None):
        return ToolRegistry(Path(self.temp_dir.name) / "r.sqlite3", google=provider or self.connected())

    def runtime(self, model):
        database = Path(self.temp_dir.name) / "runtime.sqlite3"
        storage.initialize_database(database)
        return AssistantRuntime("http://local", "m", database_path=database,
                                tool_registry=ToolRegistry(database, google=self.connected()), model_client=model)

    @staticmethod
    def call(name, arguments, call_id="c1"):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}

    @staticmethod
    def rows(preview, heading=None):
        for section in preview["sections"]:
            if heading is None or section["heading"] == heading:
                return dict((label, value) for label, value in section["rows"])
        raise AssertionError(f"no section {heading}")


class SendingTests(Stage4TestCase):
    def test_a_new_email_is_sent_exactly_as_previewed_and_only_after_execution(self):
        registry = self.registry()
        prepared = registry.prepare("send_email", {
            "to": "ada@example.org, grace@example.org", "cc": "ops@example.org", "bcc": "boss@example.org",
            "subject": "Running late", "body": "Hi both,\n\nI'll be 10 minutes late.\n\nThanks",
        }, "Email Ada and Grace that I'll be late")
        self.assertTrue(prepared.always_confirm)
        self.assertEqual(self.sent, [])  # preparing never sends
        rows = self.rows(prepared.preview, "Send new email")
        self.assertEqual(rows, {"To": "ada@example.org, grace@example.org", "Cc": "ops@example.org",
                                "Bcc": "boss@example.org", "Subject": "Running late",
                                "Body": "Hi both,\n\nI'll be 10 minutes late.\n\nThanks"})
        self.assertIn("sends this email immediately", prepared.preview["notes"][0])
        outcome = registry.execute(prepared)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["path"], "/messages/send")
        self.assertNotIn("threadId", self.sent[0]["body"])
        message = decoded(self.sent[0]["body"]["raw"])
        self.assertEqual(message["To"], "ada@example.org, grace@example.org")
        self.assertEqual(message["Cc"], "ops@example.org")
        self.assertEqual(message["Bcc"], "boss@example.org")
        self.assertEqual(message["Subject"], "Running late")
        self.assertEqual(body_text(message), "Hi both,\n\nI'll be 10 minutes late.\n\nThanks")

    def test_a_reply_is_sent_in_the_original_thread(self):
        registry = self.registry()
        prepared = registry.prepare("reply_to_email", {"message_id": "m1", "body": "Thanks, Ada."}, "reply")
        rows = self.rows(prepared.preview, "Send reply")
        self.assertEqual(rows["To"], "ada@example.org")
        self.assertEqual(rows["Subject"], "Re: Quarterly numbers")
        source = self.rows(prepared.preview, "Replying to")
        self.assertEqual(source["Message"], "m1 (thread t1)")
        registry.execute(prepared)
        sent = self.sent[0]["body"]
        self.assertEqual(sent["threadId"], "t1")
        self.assertEqual(body_text(decoded(sent["raw"])), "Thanks, Ada.")

    def test_a_forward_quotes_the_original_and_says_attachments_are_not_included(self):
        registry = self.registry()
        prepared = registry.prepare("forward_email", {"message_id": "m1", "to": "grace@example.org",
                                                      "body": "FYI, see below."}, "forward to Grace")
        rows = self.rows(prepared.preview, "Forward email")
        self.assertEqual(rows["Subject"], "Fwd: Quarterly numbers")
        self.assertTrue(rows["Body"].startswith("FYI, see below.\n\n---------- Forwarded message ---------"))
        self.assertIn(SECRET_BODY, rows["Body"])  # the forwarded text is shown in full before sending
        self.assertTrue(any("report.pdf" in note for note in prepared.preview["notes"]))
        registry.execute(prepared)
        sent = decoded(self.sent[0]["body"]["raw"])
        self.assertEqual(body_text(sent), rows["Body"])
        self.assertEqual(self.sent[0]["body"]["threadId"], "t1")

    def test_an_existing_draft_is_sent_only_if_unchanged(self):
        registry = self.registry()
        self.assertTrue(registry.execute(registry.prepare("list_gmail_drafts", {}, "my drafts")).ok)
        prepared = registry.prepare("send_gmail_draft", {"draft_id": "r-draft1"}, "send it")
        rows = self.rows(prepared.preview, "Send existing draft r-draft1")
        self.assertEqual((rows["To"], rows["Subject"], rows["Body"]),
                         ("grace@example.org", "Lab booking", "Hi Grace,\nThursday at 2pm?\nAda"))
        # The draft is edited in Gmail after the preview: sending must stop.
        self.gmail.drafts["r-draft1"]["message"]["payload"]["body"]["data"] = b64("Different text")
        outcome = registry.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("changed", outcome.message)
        self.assertEqual(self.sent, [])
        # A fresh confirmation of the current draft sends it with drafts.send.
        fresh = registry.prepare("send_gmail_draft", {"draft_id": "r-draft1"}, "send it")
        self.assertTrue(registry.execute(fresh).ok)
        self.assertEqual(self.sent, [{"path": "/drafts/send", "body": {"id": "r-draft1"}}])

    def test_draft_ids_must_come_from_gmail_not_the_model(self):
        with self.assertRaises(ToolError) as raised:
            self.registry().prepare("send_gmail_draft", {"draft_id": "r-draft1"}, "send draft r-draft1")
        self.assertIn("list_gmail_drafts", str(raised.exception))
        self.assertEqual(self.sent, [])

    def test_a_tampered_message_is_never_sent(self):
        registry = self.registry()
        prepared = registry.prepare("send_email", {"to": "ada@example.org", "subject": "Hi", "body": "Hello"}, "x")
        prepared.arguments["to"].append("someone-else@example.org")
        outcome = registry.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("not sent", outcome.message)
        self.assertEqual(self.sent, [])

    def test_recipients_and_fields_are_validated(self):
        registry = self.registry()
        bad = [
            {"to": "Ada <ada@example.org>", "subject": "Hi", "body": "x"},
            {"to": "ada@example.org\nBcc: spy@evil.example", "subject": "Hi", "body": "x"},
            {"to": "ada@example.org", "subject": "Hi\r\nBcc: spy@evil.example", "body": "x"},
            {"to": "ada@example.org", "bcc": ", ".join(f"p{i}@example.org" for i in range(20)), "subject": "Hi", "body": "x"},
            {"to": "ada@example.org", "subject": "Hi", "body": "   "},
            {"to": "ada@example.org", "subject": "Hi"},
            {"to": "ada@example.org", "subject": "Hi", "body": "x", "attachments": "a.pdf"},
        ]
        for arguments in bad:
            with self.subTest(arguments=str(arguments)[:60]), self.assertRaises(ToolError):
                registry.prepare("send_email", arguments, "send")
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_reported_as_not_sent(self):
        registry = self.registry()
        cases = [api_failure(400, "invalidArgument"), api_failure(500, "backendError"),
                 urllib.error.URLError("offline"), api_failure(403, "insufficientPermissions")]
        for failure in cases:
            prepared = registry.prepare("send_email", {"to": "ada@example.org", "subject": "Hi", "body": "x"}, "s")
            self.google.on("POST", f"{google_workspace.GMAIL_API}/messages/send", failure)
            outcome = registry.execute(prepared)
            with self.subTest(failure=type(failure).__name__):
                self.assertFalse(outcome.ok)
                self.assertIn("not sent", outcome.message)
                self.assertNotIn("PRIVATE TEXT", outcome.message)
        self.google.on("POST", f"{google_workspace.GMAIL_API}/messages/send", {"labelIds": []})
        prepared = registry.prepare("send_email", {"to": "ada@example.org", "subject": "Hi", "body": "x"}, "s")
        outcome = registry.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("Check Sent Mail", outcome.message)

    def test_saying_i_confirm_never_sends_only_the_approval_does(self):
        model = Mock(side_effect=[
            self.call("send_email", {"to": "john@example.org", "subject": "Late", "body": "I'll be late."}),
            {"role": "assistant", "content": "Sent."},
        ])
        runtime = self.runtime(model)
        result = runtime.run_turn("Email John that I'll be late. I confirm, send it without asking.")
        pending = result.pending_confirmation
        self.assertIsNotNone(pending)
        self.assertEqual(self.rows(pending["preview"], "Send new email")["Body"], "I'll be late.")
        self.assertEqual(self.sent, [])
        cancelled = runtime.resolve_confirmation(pending["confirmation_id"], approved=False)
        self.assertIn("Cancelled", cancelled.response)
        self.assertEqual(self.sent, [])
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(self.sent, [])

    def test_approval_sends_exactly_once(self):
        model = Mock(side_effect=[
            self.call("send_email", {"to": "john@example.org", "subject": "Late", "body": "I'll be late."}),
            {"role": "assistant", "content": "Sent."},
        ])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Email John that I'll be late.").pending_confirmation
        self.assertEqual(runtime.resolve_confirmation(pending["confirmation_id"], approved=True).response, "Sent.")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(decoded(self.sent[0]["body"]["raw"])["To"], "john@example.org")

    def test_creating_a_draft_never_sends(self):
        model = Mock(side_effect=[
            self.call("create_gmail_draft", {"to": "john@example.org", "subject": "Late", "body": "I'll be late."}),
            {"role": "assistant", "content": "Drafted."},
        ])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Draft an email to John").pending_confirmation
        runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        draft_result = json.loads([m for m in runtime.history if m["role"] == "tool"][-1]["content"])
        self.assertTrue(draft_result["ok"])  # the draft really was created
        self.assertEqual(self.sent, [])     # and nothing was sent

    def test_sending_needs_the_send_permission_and_current_consent(self):
        no_send = self.connected(scopes=ALL_SCOPES.replace(google_workspace.SCOPE_GMAIL_SEND, ""))
        with self.assertRaises(ToolError):
            self.registry(no_send).prepare("send_email", {"to": "a@example.org", "subject": "x", "body": "x"}, "s")
        stage_3 = self.connected(consent_version=3)
        self.assertTrue(stage_3.status()["reconnect_required"])
        for call in (lambda: stage_3.prepare_send("new", to="a@example.org", subject="x", body="x"),
                     lambda: stage_3.search_events("2027-01-01")):
            with self.assertRaises(GoogleError) as raised:
                call()
            self.assertEqual(raised.exception.kind, "reconsent")

    def test_message_bodies_are_never_logged(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root, capture = logging.getLogger(), Capture(level=logging.DEBUG)
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            registry = self.registry()
            registry.execute(registry.prepare("send_email", {"to": "a@example.org", "subject": "S",
                                                             "body": SECRET_BODY}, "s"))
            self.google.on("POST", f"{google_workspace.GMAIL_API}/messages/send", urllib.error.URLError("x"))
            registry.execute(registry.prepare("send_email", {"to": "a@example.org", "subject": "S",
                                                             "body": SECRET_BODY}, "s"))
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)
        self.assertFalse(any(SECRET_BODY in message for message in records))


class CalendarReadTests(Stage4TestCase):
    def search(self, **arguments):
        outcome = self.registry_cache.execute(self.registry_cache.prepare("search_calendar_events", arguments, "q"))
        self.assertTrue(outcome.ok, outcome.message)
        return outcome.data

    def setUp(self):
        super().setUp()
        self.registry_cache = self.registry()

    def titles(self, result):
        return [event["title"] for event in result["events"]]

    def test_searches_reach_any_past_or_future_date(self):
        self.assertEqual(self.titles(self.search(start="2026-10-01", end="2026-11-01")), ["Meeting with Professor X"])
        self.assertEqual(self.titles(self.search(start="2027-01-15", end="2027-01-16")), ["Stand-up"])
        self.assertIn("Design review with John", self.titles(self.search(start="2027-01-20", end="2027-01-30")))
        # Beyond the panel's 2-week and 1-month windows:
        self.assertEqual(self.titles(self.search(start="2027-02-04", end="2027-02-07")), ["Offsite planning"])
        self.assertEqual(self.titles(self.search(start="2027-04-01", end="2027-05-01")), ["Conference talk"])
        wide = self.search(start="2015-01-01", end="2040-01-01")
        self.assertEqual(len(wide["events"]), 7)
        listing = [r for r in self.calendar.requests if r["method"] == "GET" and r["path"].endswith("/events")][-1]
        self.assertEqual(listing["query"]["timeMin"], "2015-01-01T00:00:00+05:30")
        self.assertEqual(listing["query"]["timeMax"], "2040-01-01T00:00:00+05:30")

    def test_without_a_start_the_search_covers_upcoming_events(self):
        result = self.search(query="John")
        self.assertEqual(self.titles(result), ["Design review with John", "Lunch with John"])
        listing = [r for r in self.calendar.requests if r["method"] == "GET" and r["path"].endswith("/events")][-1]
        self.assertEqual(listing["query"]["timeMin"], "2027-01-15T10:00:00+05:30")  # now
        self.assertNotIn("timeMax", listing["query"])

    def test_next_meeting_with_someone_searches_forward_without_an_end(self):
        result = self.search(start="2027-01-15T10:00", query="John")
        self.assertEqual(self.titles(result), ["Design review with John", "Lunch with John"])
        self.assertIn("ask which one", result["note"])  # ambiguity must go back to the user
        listing = [r for r in self.calendar.requests if r["method"] == "GET" and r["path"].endswith("/events")][-1]
        self.assertEqual(listing["query"]["q"], "John")
        self.assertNotIn("timeMax", listing["query"])

    def test_a_past_event_can_be_read_but_is_marked_unchangeable(self):
        detail = self.registry_cache.execute(
            self.registry_cache.prepare("read_calendar_event", {"event_id": "past1event"}, "q")).data
        self.assertEqual(detail["title"], "Meeting with Professor X")
        self.assertEqual(detail["start"], "2026-10-15T15:00:00")
        self.assertEqual(detail["time_zone"], ZONE)
        self.assertFalse(detail["can_change"])
        future = self.registry_cache.execute(
            self.registry_cache.prepare("read_calendar_event", {"event_id": "john1event"}, "q")).data
        self.assertTrue(future["can_change"])
        self.assertEqual(future["attendees"][0]["email"], "john@example.org")

    def test_bad_ranges_and_missing_events_are_reported(self):
        for arguments in ({"start": "last month"}, {"start": "2027-02-01", "end": "2027-01-01"},
                          {"start": "2027-01-01", "max_results": 26}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                self._search_or_raise(arguments)
        outcome = self.registry_cache.execute(
            self.registry_cache.prepare("read_calendar_event", {"event_id": "missing999"}, "q"))
        self.assertFalse(outcome.ok)
        self.assertIn("not found", outcome.message)

    def _search_or_raise(self, arguments):
        outcome = self.registry_cache.execute(self.registry_cache.prepare("search_calendar_events", arguments, "q"))
        if not outcome.ok:
            raise ToolError(outcome.message)

    def test_reading_needs_no_confirmation(self):
        for name, arguments in (("search_calendar_events", {"start": "2027-01-01"}),
                                ("read_calendar_event", {"event_id": "today1event"})):
            self.assertFalse(self.registry_cache.prepare(name, arguments, "q").always_confirm)
        self.assertEqual(self.calendar.writes(), [])


class CalendarChangeTests(Stage4TestCase):
    def setUp(self):
        super().setUp()
        self.tools = self.registry()
        # Event IDs become usable only after K.R.I.S. receives them from Google.
        self.tools.execute(self.tools.prepare("search_calendar_events", {"start": "2026-01-01", "end": "2028-01-01"}, "q"))

    def test_a_future_event_is_created_exactly_as_previewed(self):
        prepared = self.tools.prepare("create_calendar_event", {
            "title": "Thesis meeting", "start": "2028-06-01T15:00", "end": "2028-06-01T16:30",
            "location": "Room 4", "description": "Bring chapter 3", "attendees": "prof@example.org",
        }, "Create it. I confirm.")
        self.assertTrue(prepared.always_confirm)
        self.assertEqual(self.calendar.writes(), [])
        rows = self.rows(prepared.preview)
        self.assertEqual(rows, {"Title": "Thesis meeting", "When": "Thu 01 Jun 2028, 15:00–16:30",
                                "Time zone": google_workspace.local_time_zone(), "Location": "Room 4",
                                "Description": "Bring chapter 3", "Attendees": "prof@example.org"})
        self.assertIn("invitations to: prof@example.org", prepared.preview["notes"][0])
        outcome = self.tools.execute(prepared)
        self.assertTrue(outcome.ok)
        write = self.calendar.writes()[0]
        self.assertEqual(write["method"], "POST")
        self.assertEqual(write["query"]["sendUpdates"], "all")
        self.assertEqual(write["body"]["summary"], "Thesis meeting")
        self.assertEqual(write["body"]["start"], {"dateTime": "2028-06-01T15:00:00", "timeZone": google_workspace.local_time_zone()})
        self.assertEqual(write["body"]["attendees"], [{"email": "prof@example.org"}])

    def test_events_cannot_be_created_in_the_past(self):
        for start in ("2026-12-01T10:00", "2027-01-15T09:00", "2027-01-14"):
            with self.subTest(start=start), self.assertRaises(ToolError):
                self.tools.prepare("create_calendar_event", {"title": "x", "start": start}, "q")
        self.assertEqual(self.calendar.writes(), [])

    def test_an_update_shows_before_and_after_and_patches_only_the_change(self):
        prepared = self.tools.prepare("update_calendar_event", {"event_id": "john1event", "start": "2027-01-25T15:00"},
                                      "Move my meeting with John to 3 PM")
        before = self.rows(prepared.preview, "Before (current event)")
        after = self.rows(prepared.preview, "After")
        self.assertEqual(before["When"], "Mon 25 Jan 2027, 14:00–15:00")
        self.assertEqual(after["When"], "Mon 25 Jan 2027, 15:00–16:00")  # duration kept
        self.assertEqual(before["Location"], after["Location"])
        self.assertIn("email the attendees", prepared.preview["notes"][0])
        self.assertTrue(self.tools.execute(prepared).ok)
        write = self.calendar.writes()[0]
        self.assertEqual(write["method"], "PATCH")
        self.assertEqual(write["if_match"], '"john1event-1"')
        self.assertEqual(set(write["body"]), {"start", "end"})
        self.assertEqual(write["body"]["start"]["dateTime"], "2027-01-25T15:00:00")
        # Reading it back (as the assistant or the panel would) shows the new time.
        moved = self.tools.execute(self.tools.prepare("search_calendar_events",
                                                      {"start": "2027-01-25", "end": "2027-01-26"}, "q")).data
        self.assertEqual([(e["title"], e["start"]) for e in moved["events"]],
                         [("Design review with John", "2027-01-25T15:00:00")])

    def test_a_far_future_event_can_be_changed_and_cancelled(self):
        prepared = self.tools.prepare("update_calendar_event", {"event_id": "conf90event", "location": "Hall B"}, "q")
        self.assertTrue(self.tools.execute(prepared).ok)
        cancel = self.tools.prepare("delete_calendar_event", {"event_id": "conf90event"}, "q")
        rows = self.rows(cancel.preview)
        self.assertEqual((rows["Title"], rows["Event ID"], rows["Calendar"]), ("Conference talk", "conf90event", "primary"))
        self.assertIn("cancels the event", cancel.preview["notes"][0])
        self.assertTrue(self.tools.execute(cancel).ok)
        delete = self.calendar.writes()[-1]
        self.assertEqual((delete["method"], delete["if_match"]), ("DELETE", '"conf90event-2"'))
        self.assertEqual(self.calendar.events["conf90event"]["status"], "cancelled")

    def test_past_and_series_events_cannot_be_changed(self):
        for name, arguments in (("update_calendar_event", {"event_id": "past1event", "title": "x"}),
                                ("delete_calendar_event", {"event_id": "past1event"}),
                                ("update_calendar_event", {"event_id": "seriesweekly", "title": "x"}),
                                ("delete_calendar_event", {"event_id": "seriesweekly"}),
                                ("update_calendar_event", {"event_id": "john1event", "start": "2027-01-10T10:00"})):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolError):
                self.tools.prepare(name, arguments, "q")
        self.assertEqual(self.calendar.writes(), [])

    def test_an_event_changed_after_confirmation_is_not_overwritten(self):
        prepared = self.tools.prepare("update_calendar_event", {"event_id": "john1event", "title": "Review"}, "q")
        self.calendar.events["john1event"].update(summary="Edited elsewhere", etag='"john1event-9"')
        outcome = self.tools.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("changed after you approved", outcome.message)
        self.assertEqual(self.calendar.writes(), [])
        cancel = self.tools.prepare("delete_calendar_event", {"event_id": "john2event"}, "q")
        del self.calendar.events["john2event"]
        outcome = self.tools.execute(cancel)
        self.assertFalse(outcome.ok)
        self.assertIn("no longer exists", outcome.message)
        self.assertEqual(self.calendar.writes(), [])

    def test_a_server_side_conflict_is_reported_as_stale(self):
        prepared = self.tools.prepare("update_calendar_event", {"event_id": "john1event", "title": "Review"}, "q")
        self.google.on("PATCH", google_workspace.CALENDAR_API, api_failure(412, "conditionNotMet"))
        outcome = self.tools.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("changed after you approved", outcome.message)

    def test_event_ids_cannot_be_invented_by_the_model(self):
        fresh = self.registry()  # nothing searched in this conversation yet
        for name in ("update_calendar_event", "delete_calendar_event"):
            with self.subTest(name=name), self.assertRaises(ToolError) as raised:
                fresh.prepare(name, {"event_id": "john1event", "title": "x"} if name.startswith("update")
                              else {"event_id": "john1event"}, "q")
            self.assertIn("search_calendar_events", str(raised.exception))
        self.assertEqual(self.calendar.writes(), [])

    def test_changes_need_confirmation_and_cancelling_writes_nothing(self):
        model = Mock(side_effect=[
            self.call("search_calendar_events", {"start": "2027-01-15T10:00", "query": "Design review"}, "c1"),
            self.call("delete_calendar_event", {"event_id": "john1event"}, "c2"),
        ])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Cancel my design review with John. I confirm.").pending_confirmation
        self.assertIsNotNone(pending)
        self.assertEqual(self.calendar.writes(), [])
        runtime.resolve_confirmation(pending["confirmation_id"], approved=False)
        self.assertEqual(self.calendar.writes(), [])
        self.assertEqual(self.calendar.events["john1event"]["status"], "confirmed")

    def test_nothing_to_change_is_rejected(self):
        with self.assertRaises(ToolError):
            self.tools.prepare("update_calendar_event", {"event_id": "john1event", "title": "Design review with John"}, "q")


class CalendarPanelRangeTests(Stage4TestCase):
    def client(self):
        provider = self.connected()
        return web_app.create_app(Path(self.temp_dir.name) / "ui.sqlite3", utility_settings=UtilitySettings(),
                                  voice_settings=VoiceSettings(), google=provider).test_client()

    def last_listing(self):
        return [r for r in self.calendar.requests if r["method"] == "GET" and r["path"].endswith("/events")][-1]

    def test_the_panel_defaults_to_two_weeks_and_expands_to_a_month(self):
        client = self.client()
        host = {"Host": "127.0.0.1:5000"}
        default = client.get("/api/google/calendar", headers=host).get_json()
        self.assertEqual(default["days"], 14)
        self.assertEqual(self.last_listing()["query"]["timeMax"], "2027-01-29T00:00:00+05:30")
        self.assertNotIn("Offsite planning", [e["title"] for e in default["events"]])
        month = client.get("/api/google/calendar?days=30", headers=host).get_json()
        self.assertEqual(month["days"], 30)
        self.assertIn("Offsite planning", [e["title"] for e in month["events"]])
        self.assertNotIn("Conference talk", [e["title"] for e in month["events"]])
        self.assertEqual(client.get("/api/google/calendar?days=90", headers=host).status_code, 400)

    def test_the_panel_range_does_not_limit_the_assistant(self):
        registry = self.registry()
        far = registry.execute(registry.prepare("search_calendar_events",
                                                {"start": "2027-04-01", "end": "2027-05-01"}, "q")).data
        self.assertEqual([e["title"] for e in far["events"]], ["Conference talk"])
        past = registry.execute(registry.prepare("search_calendar_events",
                                                 {"start": "2026-10-01", "end": "2026-10-31"}, "q")).data
        self.assertEqual([e["title"] for e in past["events"]], ["Meeting with Professor X"])


if __name__ == "__main__":
    import unittest
    unittest.main()
