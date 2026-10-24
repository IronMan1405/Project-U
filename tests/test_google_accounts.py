"""Several Google accounts: registry, selection, OAuth separation, account-aware tools, briefings.

No live Google: tool routing uses a fake provider per account; OAuth uses the real provider with a
fake Google endpoint and in-memory token stores (one per Keychain item).
"""
import json
import logging
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import google_workspace
import storage
from briefing import build_briefing, for_model
from google_accounts import LEGACY_ITEM, GoogleAccounts
from google_workspace import GoogleError, GoogleSettings, GoogleWorkspaceProvider, MemoryTokenStore
from tool_registry import ToolError, ToolRegistry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_google_workspace import ACCESS, ALL_SCOPES, CLIENT_ID, CLIENT_SECRET, NOW, REFRESH, FakeGoogle, id_token  # noqa: E402

TOMORROW = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)


class FakeProvider:
    """One account's Gmail and Calendar. Records which calls reached it."""

    def __init__(self, email, events=(), fail=None, connected=True):
        self.email, self.events, self.fail, self.is_connected = email, list(events), fail, connected
        self.settings = GoogleSettings(enabled=True, client_secrets_file=Path("/x/client.json"))
        self.calls = []

    def _call(self, name, *args):
        self.calls.append(name)
        if self.fail:
            raise GoogleError("unavailable", self.fail)

    def status(self):
        return {"configured": True, "connected": self.is_connected, "account": self.email if self.is_connected else None,
                "capabilities": {}, "reconnect_required": False, "message": None if self.is_connected else "Not connected."}

    def has_pending(self, state):
        return False

    def search_messages(self, query, limit):
        self._call("search_messages")
        prefix = self.email.split("@")[0]
        return {"messages": [{"id": f"{prefix}1", "subject": f"{query} for {self.email}"}], "more_available": False}

    def read_message(self, message_id, include_thread=False):
        self._call("read_message")
        return {"message": {"id": message_id, "body": "text"}}

    def search_events(self, start, end, query, limit, **filters):
        self._call("search_events")
        self.filters = filters
        return {"events": [dict(event) for event in self.events], "more_available": False}

    def read_event(self, event_id):
        self._call("read_event")
        return {"event_id": event_id}

    def prepare_draft(self, to, cc, subject, body, reply):
        self._call("prepare_draft")
        return {"to": [to or "sender@example.org"], "cc": [], "subject": subject or "", "body": body, "reply": None}

    def create_draft(self, draft):
        self._call("create_draft")
        assert "account" not in draft
        return {"draft_id": "d1"}

    def prepare_send(self, mode, **fields):
        self._call("prepare_send")
        return {"mode": mode, "to": [fields["to"]], "cc": [], "bcc": [], "subject": fields["subject"],
                "body": fields["body"], "draft_id": None, "fingerprint": "f"}

    def send_message(self, message):
        self._call("send_message")
        assert "account" not in message
        return {"message_id": "sent1", "thread_id": "t"}

    def _event_change(self, operation, title="Review"):
        start = TOMORROW.isoformat()
        event = {"title": title, "start": start, "end": (TOMORROW + timedelta(hours=1)).isoformat(), "all_day": False,
                 "time_zone": "Asia/Kolkata", "location": "", "description": "", "attendees": []}
        if operation == "update":
            return {"operation": "update", "event_id": "evt0001", "before": event, "after": event, "changed": ["title"],
                    "notify": False}
        return {"operation": operation, "event": event, "event_id": "evt0001", "notify": False}

    def prepare_event_create(self, title, *args):
        self._call("prepare_event_create")
        return self._event_change("create", title)

    def create_event(self, change):
        self._call("create_event")
        assert "account" not in change
        return {"event_id": "new1", "status": "confirmed"}

    def prepare_event_update(self, event_id, changes):
        self._call("prepare_event_update")
        return self._event_change("update")

    def update_event(self, change):
        self._call("update_event")
        return {"event_id": "evt0001"}

    def prepare_event_delete(self, event_id):
        self._call("prepare_event_delete")
        return self._event_change("delete")

    def delete_event(self, change):
        self._call("delete_event")
        return {"event_id": "evt0001"}


def event(title, hour, event_id):
    start = TOMORROW.replace(hour=hour)
    return {"event_id": event_id, "title": title, "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(), "all_day": False}


class AccountsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "google_accounts.json"

    def tearDown(self):
        self.temp.cleanup()

    def accounts(self, providers, default="personal"):
        """providers: {name: FakeProvider}; the first is the original (legacy) account."""
        names = list(providers)
        self.path.write_text(json.dumps({"version": 1, "default": default, "accounts": [
            {"name": name, "item": LEGACY_ITEM if index == 0 else f"oauth:{name.lower()}", "email": providers[name].email}
            for index, name in enumerate(names)]}))
        by_item = {(LEGACY_ITEM if index == 0 else f"oauth:{name.lower()}"): providers[name] for index, name in enumerate(names)}
        return GoogleAccounts(GoogleSettings(enabled=True, client_secrets_file=Path("/x/client.json")),
                              default_name=names[0], registry_path=self.path, provider_factory=lambda item: by_item[item])

    def two(self, **kwargs):
        self.personal = FakeProvider("me@gmail.example", kwargs.pop("personal_events", ()), **kwargs.pop("personal", {}))
        self.bits = FakeProvider("daks@bits.example", kwargs.pop("bits_events", ()), **kwargs.pop("bits", {}))
        return self.accounts({"personal": self.personal, "BITS": self.bits})


class AccountManagementTests(AccountsCase):
    def test_one_account_is_the_original_default_and_nothing_is_written_until_a_change(self):
        provider = FakeProvider("me@gmail.example")
        accounts = GoogleAccounts(provider.settings, default_name="personal", registry_path=self.path,
                                  provider_factory=lambda item: provider)
        self.assertEqual((accounts.names, accounts.default, accounts.multiple), (["personal"], "personal", False))
        self.assertFalse(self.path.exists())
        self.assertEqual(accounts.resolve(None), "personal")
        self.assertEqual(accounts.status()["accounts"][0]["email"], "me@gmail.example")
        self.assertFalse(self.path.exists())  # reading status never writes

    def test_lookup_by_name_or_email_any_case_and_unknown_accounts(self):
        accounts = self.two()
        self.assertEqual(accounts.resolve("bits"), "BITS")
        self.assertEqual(accounts.resolve("DAKS@bits.example"), "BITS")
        self.assertEqual(accounts.resolve(None), "personal")
        with self.assertRaises(GoogleError) as raised:
            accounts.resolve("work")
        self.assertEqual(raised.exception.kind, "unknown_account")
        self.assertEqual(accounts.mentioned("What's on my BITS calendar?"), ["BITS"])
        self.assertEqual(accounts.mentioned("mail me@gmail.example and bits"), ["personal", "BITS"])
        self.assertEqual(accounts.mentioned("orbits and habits"), [])  # whole words only

    def test_default_can_change_and_is_saved_without_tokens(self):
        accounts = self.two()
        accounts.set_default("bits")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["default"], "BITS")
        self.assertNotIn("token", self.path.read_text())
        self.assertEqual(GoogleAccounts(accounts.settings, registry_path=self.path,
                                        provider_factory=lambda item: self.personal).default, "BITS")
        self.bits.is_connected = False
        with self.assertRaises(GoogleError):  # a disconnected account cannot be the default
            accounts.set_default("BITS")

    def test_invalid_registry_entries_are_ignored(self):
        self.path.write_text(json.dumps({"default": "x", "accounts": [
            {"name": "personal", "item": "oauth"}, {"name": "personal", "item": "oauth:personal"},
            {"name": "evil", "item": "oauth:other"}, {"name": "all", "item": "oauth:all"}, "junk"]}))
        accounts = GoogleAccounts(GoogleSettings(), registry_path=self.path, provider_factory=lambda item: FakeProvider("a@b.c"))
        self.assertEqual(accounts.names, ["personal"])
        self.assertEqual(accounts.default, "personal")

    def test_identity_mismatch_is_surfaced_and_blocks_use(self):
        accounts = self.two()
        self.bits.email = "someone-else@bits.example"
        status = accounts.account_status("BITS")
        self.assertTrue(status["mismatch"])
        self.assertFalse(status["connected"])
        self.assertIn("someone-else@bits.example", status["message"])
        with self.assertRaises(GoogleError):
            accounts.check("BITS")

    def test_disconnect_removes_only_that_account(self):
        accounts = self.two()
        self.bits.disconnect = Mock()
        accounts.disconnect("bits")
        self.bits.disconnect.assert_called_once()
        self.assertEqual(accounts.names, ["personal"])
        self.assertEqual(json.loads(self.path.read_text())["accounts"][0]["item"], LEGACY_ITEM)


class OAuthSeparationTests(AccountsCase):
    """Real providers, fake Google, one in-memory store per Keychain item."""

    def setUp(self):
        super().setUp()
        self.client_file = Path(self.temp.name) / "client.json"
        self.client_file.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}))
        self.google = FakeGoogle()
        self.google.on("POST", google_workspace.REVOKE_URL, {})
        self.stores = {LEGACY_ITEM: MemoryTokenStore({"refresh_token": REFRESH, "scopes": ALL_SCOPES.split(),
                                                     "email": "me@gmail.example", "consent_version": google_workspace.CONSENT_VERSION})}
        self.settings = GoogleSettings(enabled=True, client_secrets_file=self.client_file)
        self.accounts_ = GoogleAccounts(self.settings, default_name="personal", registry_path=self.path,
                                        provider_factory=self.provider)

    def provider(self, item):
        store = self.stores.setdefault(item, MemoryTokenStore())
        return GoogleWorkspaceProvider(self.settings, store=store, opener=self.google, clock=lambda: NOW)

    def sign_in(self, name, email, refresh="1//NEW-REFRESH"):
        url = self.accounts_.begin_authorization("http://127.0.0.1:5000/oauth/google/callback", name)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.google.on("POST", google_workspace.TOKEN_URL, {
            "access_token": ACCESS, "refresh_token": refresh, "expires_in": 3600, "scope": ALL_SCOPES,
            "id_token": id_token(email=email, hd=None)})
        return self.accounts_.complete_authorization(state, "code", None)

    def test_a_second_account_gets_its_own_token_and_the_first_is_untouched(self):
        original = dict(self.stores[LEGACY_ITEM].record)
        result = self.sign_in("bits", "daks@bits.example")
        self.assertEqual(result, {"name": "bits", "email": "daks@bits.example", "new": True})
        self.assertEqual(self.stores["oauth:bits"].record["refresh_token"], "1//NEW-REFRESH")
        self.assertEqual(self.stores[LEGACY_ITEM].record, original)
        self.assertEqual(self.accounts_.names, ["personal", "bits"])
        saved = self.path.read_text()
        self.assertNotIn("REFRESH", saved)
        self.assertNotIn(ACCESS, saved)

    def test_signing_in_with_an_already_connected_account_changes_nothing_and_revokes_nothing(self):
        with self.assertRaises(GoogleError) as raised:
            self.sign_in("work", "me@gmail.example")
        self.assertEqual(raised.exception.kind, "duplicate")
        self.assertIn("already connected as personal", raised.exception.message)
        self.assertEqual(self.google.calls(google_workspace.REVOKE_URL), [])  # revoking would end personal's grant
        self.assertEqual(self.accounts_.names, ["personal"])
        self.assertEqual(self.stores[LEGACY_ITEM].record["refresh_token"], REFRESH)
        self.assertIsNone(self.stores.get("oauth:work").record if "oauth:work" in self.stores else None)

    def test_reconnecting_a_name_with_a_different_google_account_is_refused(self):
        self.sign_in("bits", "daks@bits.example")
        with self.assertRaises(GoogleError) as raised:
            self.sign_in("bits", "intruder@example.org", refresh="1//OTHER")
        self.assertEqual(raised.exception.kind, "mismatch")
        self.assertEqual(self.stores["oauth:bits"].record["refresh_token"], "1//NEW-REFRESH")
        self.assertEqual(len(self.google.calls(google_workspace.REVOKE_URL)), 1)  # only the unwanted new grant

    def test_reconnecting_the_same_account_keeps_other_accounts(self):
        self.sign_in("bits", "daks@bits.example")
        self.sign_in("bits", "daks@bits.example", refresh="1//RENEWED")
        self.assertEqual(self.stores["oauth:bits"].record["refresh_token"], "1//RENEWED")
        self.assertEqual(self.stores[LEGACY_ITEM].record["refresh_token"], REFRESH)

    def test_bad_names_and_tokens_never_logged(self):
        with self.assertRaises(GoogleError):
            self.accounts_.begin_authorization(None, "all")
        with self.assertRaises(GoogleError):
            self.accounts_.begin_authorization(None, "../x")
        logger = logging.getLogger()
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self.sign_in("bits", "daks@bits.example")
            self.sign_in("bits", "daks@bits.example")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        text = " ".join(records)
        for secret in ("1//NEW-REFRESH", REFRESH, ACCESS, CLIENT_SECRET, "code"):
            self.assertNotIn(secret, text)


class ToolRoutingTests(AccountsCase):
    def registry(self, accounts):
        return ToolRegistry(Path(self.temp.name) / "r.sqlite3", google=accounts)

    def run_tool(self, registry, name, arguments, message):
        return registry.execute(registry.prepare(name, arguments, message))

    def test_gmail_search_uses_the_named_account_only(self):
        registry = self.registry(self.two())
        outcome = self.run_tool(registry, "search_gmail", {"query": "flight"}, "Search my personal Gmail for flight tickets")
        self.assertEqual((self.personal.calls, self.bits.calls), (["search_messages"], []))
        self.assertIn("in personal", outcome.message)
        self.run_tool(registry, "search_gmail", {"query": "x", "account": "bits"}, "Show me emails from my BITS account")
        self.assertEqual(self.bits.calls, ["search_messages"])

    def test_an_unnamed_gmail_search_with_several_accounts_asks_which(self):
        registry = self.registry(self.two())
        with self.assertRaisesRegex(ToolError, "Ask the user which account"):
            registry.prepare("search_gmail", {"query": "internship"}, "Search my Gmail for the internship email")
        self.assertEqual(self.personal.calls + self.bits.calls, [])
        # The choice applies to this request only; the default does not change.
        self.run_tool(registry, "search_gmail", {"query": "internship", "account": "BITS"}, "BITS")
        self.assertEqual(registry.google.default, "personal")

    def test_all_accounts_search_labels_results_and_survives_one_failure(self):
        registry = self.registry(self.two(bits={"fail": "Gmail is unavailable."}))
        outcome = self.run_tool(registry, "search_gmail", {"query": "x"}, "Search all my accounts for receipts")
        self.assertTrue(outcome.ok)
        self.assertEqual([group["account"] for group in outcome.data["accounts"]], ["personal"])
        self.assertEqual(outcome.data["unavailable"], [{"account": "BITS", "message": "Gmail is unavailable."}])

    def test_messages_stay_with_the_account_they_came_from(self):
        registry = self.registry(self.two())
        self.run_tool(registry, "search_gmail", {"query": "x"}, "Search my BITS mail")
        self.run_tool(registry, "read_gmail_message", {"message_id": "daks1"}, "Read it")
        self.assertEqual(self.bits.calls, ["search_messages", "read_message"])
        self.assertEqual(self.personal.calls, [])
        with self.assertRaisesRegex(ToolError, "found in the BITS account"):
            registry.prepare("read_gmail_message", {"message_id": "daks1", "account": "personal"}, "Read it")

    def test_drafts_and_sends_name_the_account_and_go_through_it(self):
        registry = self.registry(self.two())
        draft = registry.prepare("create_gmail_draft", {"to": "a@example.org", "body": "Hi"}, "Draft from my BITS account")
        self.assertTrue(draft.always_confirm)
        self.assertIn("in your BITS Gmail", draft.summary)
        self.assertEqual(draft.preview["account"], "BITS (daks@bits.example)")
        registry.execute(draft)
        send = registry.prepare("send_email", {"to": "a@example.org", "subject": "S", "body": "B"}, "Send this from my BITS account")
        self.assertTrue(send.always_confirm)
        self.assertIn("from your BITS account, daks@bits.example", send.summary)
        self.assertEqual(send.preview["account"], "BITS (daks@bits.example)")
        self.assertTrue(registry.execute(send).ok)
        self.assertEqual(self.bits.calls, ["prepare_draft", "create_draft", "prepare_send", "send_message"])
        self.assertEqual(self.personal.calls, [])
        with self.assertRaisesRegex(ToolError, "asked for the BITS account"):  # the model picked the wrong one
            registry.prepare("send_email", {"to": "a@example.org", "subject": "S", "body": "B", "account": "personal"},
                             "Send this from my BITS account")

    def test_calendar_reads_and_changes_use_the_selected_account(self):
        registry = self.registry(self.two(bits_events=[event("Midsem review", 10, "evt0001")]))
        found = self.run_tool(registry, "search_calendar_events", {}, "What's on my BITS calendar tomorrow?")
        self.assertEqual(found.data["account"], "BITS")
        create = registry.prepare("create_calendar_event", {"title": "Lab", "start": TOMORROW.isoformat()[:16]},
                                  "Add a lab on my BITS calendar")
        self.assertIn("on your BITS calendar", create.summary)
        registry.execute(create)
        update = registry.prepare("update_calendar_event", {"event_id": "evt0001", "title": "Moved"}, "Rename it")
        self.assertIn("BITS", update.summary)
        registry.execute(update)
        delete = registry.prepare("delete_calendar_event", {"event_id": "evt0001"}, "Cancel it")
        registry.execute(delete)
        self.assertEqual(self.bits.calls, ["search_events", "prepare_event_create", "create_event", "prepare_event_update",
                                           "update_event", "prepare_event_delete", "delete_event"])
        self.assertEqual(self.personal.calls, [])
        with self.assertRaisesRegex(ToolError, "found in the BITS account, not personal"):  # never cross accounts
            registry.prepare("delete_calendar_event", {"event_id": "evt0001", "account": "personal"}, "Cancel evt0001 in personal")

    def test_an_unnamed_calendar_question_covers_every_calendar_as_one_timeline(self):
        registry = self.registry(self.two(personal_events=[event("Dinner", 18, "pevt001")],
                                          bits_events=[event("Midsem review", 10, "bevt001")]))
        outcome = self.run_tool(registry, "search_calendar_events", {}, "What's on my calendar tomorrow?")
        self.assertEqual((self.personal.calls, self.bits.calls), (["search_events"], ["search_events"]))
        self.assertEqual([(e["title"], e["account"]) for e in outcome.data["events"]],
                         [("Midsem review", "BITS"), ("Dinner", "personal")])
        # Changing one of them still goes to the account it is in.
        self.assertEqual(registry.prepare("delete_calendar_event", {"event_id": "bevt001"}, "Cancel it").arguments["account"], "BITS")
        named = self.run_tool(self.registry(self.two()), "search_calendar_events", {}, "What's on my BITS calendar tomorrow?")
        self.assertEqual((self.personal.calls, self.bits.calls), ([], ["search_events"]))
        self.assertIn("in BITS", named.message)

    def test_a_calendar_that_fails_makes_the_search_partial_not_empty(self):
        registry = self.registry(self.two(bits={"fail": "Google Calendar is unavailable."}))
        outcome = self.run_tool(registry, "search_calendar_events", {}, "What's on my calendar tomorrow?")
        self.assertTrue(outcome.ok)
        self.assertIn("Not checked, so this is not the whole schedule: BITS (Google Calendar is unavailable.)", outcome.message)
        self.assertEqual(outcome.data["accounts_checked"], ["personal"])

    def test_one_account_behaves_exactly_as_before(self):
        only = FakeProvider("me@gmail.example")
        registry = self.registry(self.accounts({"personal": only}))
        outcome = self.run_tool(registry, "search_gmail", {"query": "x"}, "Search my Gmail for the internship email")
        self.assertEqual(outcome.message, "Found 1 message(s).")
        send = registry.prepare("send_email", {"to": "a@example.org", "subject": "S", "body": "B"}, "Send it")
        self.assertEqual(send.summary, "Send email to a@example.org: S")
        self.assertNotIn("account", send.preview)
        # (A bare GoogleWorkspaceProvider, as every earlier Gmail/Calendar test passes, is one account too.)

    def test_account_tools(self):
        registry = self.registry(self.two())
        listed = self.run_tool(registry, "list_google_accounts", {}, "What Google accounts are connected?")
        self.assertEqual([(a["name"], a["email"], a["default"]) for a in listed.data["accounts"]],
                         [("personal", "me@gmail.example", True), ("BITS", "daks@bits.example", False)])
        change = registry.prepare("set_default_google_account", {"account": "bits"}, "Use BITS as my default Google account")
        self.assertFalse(change.always_confirm)
        self.assertIn("now the default", registry.execute(change).message)
        self.assertTrue(registry.prepare("disconnect_google_account", {"account": "bits"},
                                         "Disconnect my BITS Google account").always_confirm)
        browser = Mock(return_value=True)
        registry.browser_open = browser
        registry.google.begin_authorization = Mock(return_value="https://accounts.google.com/o/oauth2/v2/auth?state=s")
        connect = registry.prepare("connect_google_account", {"name": "work"}, "Add my work Google account")
        self.assertFalse(connect.always_confirm)
        outcome = registry.execute(connect)
        self.assertIn("not connected yet", outcome.message)
        browser.assert_called_once()


class BriefingTests(AccountsCase):
    def database(self):
        path = Path(self.temp.name) / "b.sqlite3"
        storage.initialize_database(path)  # never the user's real data
        return path

    def test_a_briefing_covers_every_connected_calendar_and_a_named_one_restricts_it(self):
        accounts = self.two(personal_events=[event("Dinner", 18, "pevt001")], bits_events=[event("Midsem review", 10, "bevt001")])
        result = build_briefing(datetime.now().astimezone(), "tomorrow", database_path=self.database(), google=accounts)
        calendar = result["sources"]["calendar"]
        self.assertEqual({g["account"]: [e["title"] for e in g["events"]] for g in calendar["accounts"]},
                         {"personal": ["Dinner"], "BITS": ["Midsem review"]})
        self.assertEqual((self.personal.calls, self.bits.calls), (["search_events"], ["search_events"]))
        self.assertNotIn("calendar_scope", result["focus"])  # nothing was left out
        # The model sees one timeline in time order, each event with its account.
        timeline = for_model(result)["sources"]["calendar"]
        self.assertEqual([(e["title"], e["account"]) for e in timeline["events"]], [("Midsem review", "BITS"), ("Dinner", "personal")])
        self.assertEqual(timeline["accounts_checked"], ["personal", "BITS"])

        only = build_briefing(datetime.now().astimezone(), "tomorrow", database_path=self.database(), google=accounts,
                              calendar_accounts="BITS")["sources"]["calendar"]
        self.assertEqual(([e["title"] for e in only["events"]], only["account"]), (["Midsem review"], "BITS"))
        self.assertEqual(only["scope"], "Only the BITS calendar, as asked; not included: personal")

    def test_the_same_event_is_listed_once_and_different_events_with_one_title_stay_apart(self):
        shared = dict(event("Club meeting", 16, "sevt001"), ical_uid="club-123@google.com")
        lecture = event("Lecture", 11, "pevt009")
        accounts = self.two(personal_events=[event("Dinner", 18, "pevt001"), shared, lecture],
                            # the same invitation (one iCalUID, another event ID in this account), and a different
                            # event that only shares the lecture's title and time
                            bits_events=[event("Midsem review", 10, "bevt001"), dict(shared, event_id="sevt002"),
                                         dict(lecture, event_id="bevt009")])
        calendar = build_briefing(datetime.now().astimezone(), "tomorrow", database_path=self.database(),
                                  google=accounts)["sources"]["calendar"]
        groups = {g["account"]: [e["title"] for e in g["events"]] for g in calendar["accounts"]}
        self.assertEqual(groups, {"personal": ["Dinner", "Club meeting", "Lecture"], "BITS": ["Midsem review", "Lecture"]})
        self.assertEqual(calendar["accounts"][0]["events"][1]["also_in"], ["BITS"])

    def test_one_unavailable_account_does_not_hide_the_others(self):
        accounts = self.two(personal_events=[event("Dinner", 18, "pevt001")], bits={"fail": "Google Calendar is unavailable."})
        calendar = build_briefing(datetime.now().astimezone(), "tomorrow", database_path=self.database(), google=accounts,
                                  calendar_accounts="all")["sources"]["calendar"]
        self.assertEqual(calendar["status"], "ok")
        self.assertEqual(calendar["unavailable_accounts"], [{"account": "BITS", "reason": "Google Calendar is unavailable."}])

    def test_the_briefing_tool_covers_every_calendar_unless_one_is_named(self):
        accounts = self.two(personal_events=[event("Dinner", 18, "pevt001")], bits_events=[event("Midsem review", 10, "bevt001")])
        database = Path(self.temp.name) / "r.sqlite3"
        storage.initialize_database(database)
        registry = ToolRegistry(database, google=accounts)
        everything = registry.execute(registry.prepare("get_briefing", {"day": "tomorrow"}, "Brief me on tomorrow"))
        self.assertEqual(everything.data["sources"]["calendar"]["accounts_checked"], ["personal", "BITS"])
        named = registry.execute(registry.prepare("get_briefing", {"day": "tomorrow"}, "What's on my BITS calendar tomorrow?"))
        self.assertEqual([e["title"] for e in named.data["sources"]["calendar"]["events"]], ["Midsem review"])
        self.assertIn("Only the BITS calendar, as asked", named.message)
        # Events found in the briefing can then be changed, in their own account only.
        self.assertTrue(registry.prepare("delete_calendar_event", {"event_id": "bevt001"}, "Cancel the midsem review").always_confirm)


if __name__ == "__main__":
    unittest.main()
