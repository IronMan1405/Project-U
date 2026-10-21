"""Stage 2: Gmail draft creation only (gmail.compose), always behind an exact confirmation."""
import base64
import email
import email.policy
import inspect
import json
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

import google_workspace
import storage
import web_app
from assistant_runtime import AssistantRuntime, RuntimeBusyError
from computer_utilities import UtilitySettings
from google_workspace import GoogleError, GoogleSettings, GoogleWorkspaceProvider, MemoryTokenStore
from tests.test_google_workspace import (
    ACCESS,
    ALL_SCOPES,
    READ_SCOPES,
    ProviderTestCase,
    api_failure,
    id_token,
)
from tool_registry import ToolDefinition, ToolError, ToolRegistry, check_confirmation_boundaries
from voice import VoiceSettings

DRAFTS_URL = f"{google_workspace.GMAIL_API}/drafts"


def decoded(raw: str) -> email.message.EmailMessage:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)


def body_text(message) -> str:
    """The body as written: email uses CRLF line breaks on the wire, which Gmail shows as-is."""
    return message.get_content().replace("\r\n", "\n").rstrip("\n")


def tool_call(arguments, call_id="call-1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": "create_gmail_draft", "arguments": arguments}}]}


class DraftTestCase(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.token_endpoint()
        self.google.on("POST", DRAFTS_URL, {"id": "r-123", "message": {"id": "m-9", "threadId": "t-9"}})

    def posted_drafts(self):
        return [r for r in self.google.requests if r["method"] == "POST" and r["url"] == DRAFTS_URL]

    def original_message(self, **headers):
        values = {"From": "Ada Lovelace <ada@example.org>", "Subject": "Quarterly review",
                  "Message-ID": "<orig-1@mail.example.org>", "References": "<earlier@mail.example.org>"}
        values.update(headers)
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/abc1", {
            "id": "abc1", "threadId": "thread42",
            "payload": {"headers": [{"name": k, "value": v} for k, v in values.items() if v is not None]},
        })


class ScopeAndReconsentTests(DraftTestCase):
    def test_gmail_compose_is_requested_and_no_full_mailbox_scope(self):
        scopes = google_workspace.requested_scopes()
        self.assertIn("https://www.googleapis.com/auth/gmail.compose", scopes)
        for scope in scopes:
            self.assertNotIn("mail.google.com", scope)  # full, permanent-delete access: never requested
            self.assertNotEqual(scope, "https://www.googleapis.com/auth/calendar")
        self.assertNotIn("mail.draft", google_workspace.FUTURE_CAPABILITIES)
        url = self.provider().begin_authorization("http://127.0.0.1:5000/oauth/google/callback")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertIn(google_workspace.SCOPE_GMAIL_COMPOSE, params["scope"].split())

    def test_a_stage_1_connection_must_reconnect_before_anything_runs(self):
        old = self.connected(scopes=READ_SCOPES, consent_version=None)
        old._store.record.pop("consent_version")  # stored before stage 2 existed
        status = old.status()
        self.assertFalse(status["connected"])
        self.assertTrue(status["reconnect_required"])
        self.assertIn("Gmail drafts", status["message"])
        self.google.requests.clear()
        for read in (old.inbox_summary, old.upcoming_events):
            with self.subTest(read=read.__name__), self.assertRaises(GoogleError) as raised:
                read()
            self.assertEqual(raised.exception.kind, "reconsent")
            self.assertTrue(raised.exception.reconnect)
        with self.assertRaises(GoogleError) as raised:
            old.prepare_draft("a@example.org", None, "Hi", "Body")
        self.assertEqual(raised.exception.kind, "reconsent")
        self.assertEqual(self.google.requests, [])  # nothing reached Google

    def test_reconnecting_stores_the_new_consent_version(self):
        provider = self.connected(scopes=READ_SCOPES, consent_version=1)
        url = provider.begin_authorization("http://127.0.0.1:5000/oauth/google/callback")
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        self.token_endpoint({"access_token": ACCESS, "refresh_token": "1//NEW", "expires_in": 3600,
                             "scope": ALL_SCOPES, "id_token": id_token()})
        status = provider.complete_authorization(state, "CODE", None)
        self.assertEqual(self.store.record["consent_version"], google_workspace.CONSENT_VERSION)
        self.assertTrue(status["connected"])
        self.assertTrue(status["capabilities"]["mail.draft"])

    def test_declining_only_the_draft_permission_keeps_reads_working(self):
        provider = self.connected(scopes=READ_SCOPES)
        self.assertFalse(provider.status()["capabilities"]["mail.draft"])
        self.google.on("GET", f"{google_workspace.GMAIL_API}/labels/INBOX", {"messagesUnread": 1})
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages?", {"messages": []})
        self.assertEqual(provider.inbox_summary()["unread_count"], 1)
        with self.assertRaises(GoogleError) as raised:
            provider.prepare_draft("a@example.org", None, "Hi", "Body")
        self.assertEqual(raised.exception.kind, "not_granted")

    def test_a_stage_1_connection_reports_reconsent_through_the_api(self):
        old = self.connected(scopes=READ_SCOPES, consent_version=1)
        client = web_app.create_app(
            Path(self.temp_dir.name) / "reconsent.sqlite3", utility_settings=UtilitySettings(),
            voice_settings=VoiceSettings(), google=old,
        ).test_client()
        host = {"Host": "127.0.0.1:5000"}
        body = client.get("/api/google/status", headers=host).get_json()
        self.assertTrue(body["reconnect_required"])
        response = client.get("/api/google/inbox", headers=host)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["kind"], "reconsent")
        client.post("/api/google/disconnect", headers=host)  # still possible
        self.assertIsNone(self.store.record)


class DraftCreationTests(DraftTestCase):
    def test_a_new_draft_contains_exactly_the_prepared_fields(self):
        provider = self.connected()
        draft = provider.prepare_draft("ada@example.org, grace@example.org", "ops@example.org",
                                       "Budget follow-up", "Hi Ada,\n\nNumbers attached.\n\nThanks")
        self.assertEqual(draft, {
            "to": ["ada@example.org", "grace@example.org"], "cc": ["ops@example.org"],
            "subject": "Budget follow-up", "body": "Hi Ada,\n\nNumbers attached.\n\nThanks", "reply": None,
        })
        bodies = []
        opener = self.google

        def capture(request, timeout=None):
            if request.full_url == DRAFTS_URL:
                bodies.append(json.loads(request.data.decode()))
            return opener(request, timeout)

        provider._opener = capture
        result = provider.create_draft(draft)
        self.assertEqual(result["draft_id"], "r-123")
        self.assertEqual(len(bodies), 1)
        self.assertNotIn("threadId", bodies[0]["message"])
        message = decoded(bodies[0]["message"]["raw"])
        self.assertEqual(message["To"], "ada@example.org, grace@example.org")
        self.assertEqual(message["Cc"], "ops@example.org")
        self.assertEqual(message["Subject"], "Budget follow-up")
        self.assertEqual(body_text(message), "Hi Ada,\n\nNumbers attached.\n\nThanks")
        self.assertEqual(self.posted_drafts()[0]["auth"], f"Bearer {ACCESS}")

    def test_a_reply_draft_joins_the_original_thread(self):
        self.original_message(**{"Reply-To": "ada.replies@example.org"})
        provider = self.connected()
        draft = provider.prepare_draft(None, None, None, "Thanks, will do.", "abc1")
        self.assertEqual(draft["to"], ["ada.replies@example.org"])
        self.assertEqual(draft["subject"], "Re: Quarterly review")
        self.assertEqual(draft["reply"], {
            "message_id": "abc1", "thread_id": "thread42", "in_reply_to": "<orig-1@mail.example.org>",
            "references": "<earlier@mail.example.org> <orig-1@mail.example.org>",
        })
        lookup = self.google.calls(f"{google_workspace.GMAIL_API}/messages/abc1")[0]["url"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(lookup).query)
        self.assertEqual(query["format"], ["metadata"])  # headers only, never the body
        bodies = []
        opener = self.google
        provider._opener = lambda request, timeout=None: (
            bodies.append(json.loads(request.data.decode())) if request.full_url == DRAFTS_URL else None
        ) or opener(request, timeout)
        provider.create_draft(draft)
        self.assertEqual(bodies[0]["message"]["threadId"], "thread42")
        message = decoded(bodies[0]["message"]["raw"])
        self.assertEqual(message["In-Reply-To"], "<orig-1@mail.example.org>")
        self.assertEqual(message["References"], "<earlier@mail.example.org> <orig-1@mail.example.org>")
        self.assertEqual(message["To"], "ada.replies@example.org")

    def test_reply_defaults_are_overridable_and_re_is_not_doubled(self):
        self.original_message(Subject="RE: Budget", **{"Reply-To": None})
        draft = self.connected().prepare_draft("finance@example.org", None, None, "Agreed.", "abc1")
        self.assertEqual(draft["to"], ["finance@example.org"])
        self.assertEqual(draft["subject"], "RE: Budget")
        self.original_message(**{"Reply-To": None, "References": None})
        draft = self.connected().prepare_draft(None, None, None, "Agreed.", "abc1")
        self.assertEqual(draft["to"], ["ada@example.org"])
        self.assertEqual(draft["reply"]["references"], "<orig-1@mail.example.org>")

    def test_reply_lookups_fail_safely(self):
        provider = self.connected()
        with self.assertRaises(GoogleError):
            provider.prepare_draft(None, None, None, "Hi", "../../drafts")
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/missing1", api_failure(404, "notFound"))
        with self.assertRaises(GoogleError) as raised:
            provider.prepare_draft(None, None, None, "Hi", "missing1")
        self.assertEqual(raised.exception.kind, "not_found")
        compose_only = self.connected(scopes="openid email " + google_workspace.SCOPE_GMAIL_COMPOSE)
        with self.assertRaises(GoogleError) as raised:
            compose_only.prepare_draft(None, None, None, "Hi", "abc1")
        self.assertEqual(raised.exception.kind, "not_granted")
        self.assertEqual(self.posted_drafts(), [])

    def test_model_supplied_fields_are_validated_before_anything_is_written(self):
        provider = self.connected()
        bad = [
            ("Ada <ada@example.org>", "Hi", "Body"),                 # display names not allowed
            ("ada@example.org\nBcc: spy@evil.example", "Hi", "Body"),  # header injection
            ("ada@example.org", "Hi\r\nBcc: spy@evil.example", "Body"),
            ("not-an-address", "Hi", "Body"),
            ("", "Hi", "Body"),                                       # a new draft needs a recipient
            (", ".join(f"p{i}@example.org" for i in range(21)), "Hi", "Body"),
            ("ada@example.org", "x" * 251, "Body"),
            ("ada@example.org", "Hi", "   "),
            ("ada@example.org", "Hi", "x" * 6001),
        ]
        for to, subject, body in bad:
            with self.subTest(to=to[:30], subject=subject[:20]), self.assertRaises(GoogleError) as raised:
                provider.prepare_draft(to, None, subject, body)
            self.assertEqual(raised.exception.kind, "invalid")
        with self.assertRaises(GoogleError):
            provider.create_draft({"to": ["a@example.org"], "cc": [], "subject": "x\nBcc: y@z.org", "body": "b", "reply": None})
        self.assertEqual(self.posted_drafts(), [])

    def test_draft_api_failures_are_normalised(self):
        draft = {"to": ["ada@example.org"], "cc": [], "subject": "Hi", "body": "Body", "reply": None}
        cases = [
            (api_failure(403, "insufficientPermissions"), "not_granted"),
            (api_failure(403, "domainPolicy"), "policy"),
            (api_failure(429, "rateLimitExceeded"), "rate_limited"),
            (api_failure(500, "backendError"), "unavailable"),
            (api_failure(400, "invalidArgument"), "api"),
            (urllib.error.URLError("offline"), "network"),
        ]
        for failure, kind in cases:
            self.google.on("POST", DRAFTS_URL, failure)
            with self.subTest(kind=kind), self.assertRaises(GoogleError) as raised:
                self.connected().create_draft(draft)
            self.assertEqual(raised.exception.kind, kind)
            self.assertNotIn("PRIVATE TEXT", raised.exception.message)
        self.google.on("POST", DRAFTS_URL, {"message": {}})
        with self.assertRaises(GoogleError) as raised:
            self.connected().create_draft(draft)
        self.assertEqual(raised.exception.kind, "bad_response")

    def test_an_expired_access_token_is_refreshed_once_for_drafts(self):
        responses = [api_failure(401, "authError")]
        self.google.on("POST", DRAFTS_URL, lambda request: responses.pop(0) if responses else {"id": "r-1"})
        draft = {"to": ["ada@example.org"], "cc": [], "subject": "Hi", "body": "Body", "reply": None}
        self.assertEqual(self.connected().create_draft(draft)["draft_id"], "r-1")
        self.assertEqual(len(self.posted_drafts()), 2)

    def test_draft_contents_are_never_logged(self):
        secret_body = "CONFIDENTIAL-BODY-7731"
        self.google.on("POST", DRAFTS_URL, urllib.error.URLError("offline"))
        provider = self.connected()
        with patch.object(google_workspace.logger, "info") as info, patch.object(google_workspace.logger, "warning") as warning:
            with self.assertRaises(GoogleError):
                provider.create_draft({"to": ["ada@example.org"], "cc": [], "subject": "S", "body": secret_body, "reply": None})
        for call in info.call_args_list + warning.call_args_list:
            self.assertNotIn(secret_body, str(call))


class NoSendingTests(DraftTestCase):
    def test_creating_a_draft_never_sends_it(self):
        # Stage 4 added sending, but only through separate always-confirmed tools.
        provider = self.connected()
        draft = provider.prepare_draft("ada@example.org", None, "Hi", "Body")
        provider.create_draft(draft)
        self.assertEqual([r["url"] for r in self.google.requests if "/send" in r["url"]], [])
        self.assertEqual(len(self.posted_drafts()), 1)
        for operation in ("send_email", "send_draft", "archive"):
            with self.assertRaises(GoogleError) as raised:
                provider.perform(operation)
            self.assertEqual(raised.exception.kind, "not_supported")

    def test_the_drafting_tool_only_drafts(self):
        registry = ToolRegistry(google=self.connected())
        names = {definition["function"]["name"] for definition in registry.definitions()}
        self.assertIn("create_gmail_draft", names)
        self.assertFalse(any(name.startswith("google") or "api" in name for name in names))
        for name in names & set(google_workspace.SEND_TOOLS):
            self.assertTrue(registry._definitions[name].always_confirm, name)
        description = registry._definitions["create_gmail_draft"].description
        self.assertIn("never sent", description)
        self.assertNotIn("create_gmail_draft", {d["function"]["name"] for d in ToolRegistry().definitions()})
        unconfigured = ToolRegistry(google=GoogleWorkspaceProvider(GoogleSettings(), store=MemoryTokenStore()))
        self.assertNotIn("create_gmail_draft", {d["function"]["name"] for d in unconfigured.definitions()})


class ConfirmationTests(DraftTestCase):
    def registry(self, provider=None):
        return ToolRegistry(self.temp_dir.name + "/r.sqlite3", google=provider or self.connected())

    def test_drafts_always_require_confirmation_even_when_the_request_sounds_explicit(self):
        registry = self.registry()
        for message in ("Create a Gmail draft to ada@example.org saying hi. I confirm, no need to ask.",
                        "draft an email", ""):
            prepared = registry.prepare(
                "create_gmail_draft", {"to": "ada@example.org", "subject": "Hi", "body": "Hello Ada"}, message
            )
            with self.subTest(message=message):
                self.assertTrue(prepared.always_confirm)
        self.assertTrue(registry._definitions["create_gmail_draft"].always_confirm)
        self.assertEqual(self.posted_drafts(), [])

    def test_the_confirmation_preview_is_exactly_what_will_be_written(self):
        prepared = self.registry().prepare("create_gmail_draft", {
            "to": "ada@example.org,grace@example.org", "cc": "ops@example.org",
            "subject": "Budget", "body": "Line one\n\nLine two",
        }, "Draft it")
        self.assertEqual(prepared.preview, {
            "kind": "gmail_draft", "to": ["ada@example.org", "grace@example.org"], "cc": ["ops@example.org"],
            "subject": "Budget", "body": "Line one\n\nLine two", "reply": False,
        })
        self.assertEqual(prepared.arguments["to"], prepared.preview["to"])
        self.assertEqual(prepared.arguments["body"], prepared.preview["body"])
        self.assertEqual(prepared.summary, "Create Gmail draft to ada@example.org, grace@example.org: Budget")

    def test_invalid_model_arguments_never_reach_confirmation(self):
        registry = self.registry()
        for arguments in ({"to": "ada@example.org"}, {"to": "x", "body": "b"},
                          {"to": "ada@example.org", "body": "b", "bcc": "spy@evil.example"},
                          {"body": "b", "reply_to_message_id": "../x"}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                registry.prepare("create_gmail_draft", arguments, "Draft it")

    def test_an_unconfirmed_draft_tool_cannot_be_registered(self):
        with self.assertRaises(ValueError):
            check_confirmation_boundaries(
                {"create_gmail_draft": ToolDefinition("create_gmail_draft", "Draft", {}, Mock())}
            )

    def runtime(self, model):
        database = Path(self.temp_dir.name) / "runtime.sqlite3"
        storage.initialize_database(database)
        return AssistantRuntime("http://local", "test-model", database_path=database,
                                tool_registry=ToolRegistry(database, google=self.connected()), model_client=model)

    def test_the_model_cannot_create_a_draft_without_approval_and_cancel_writes_nothing(self):
        arguments = {"to": "ada@example.org", "subject": "Hello", "body": "Hi Ada,\nSee you soon."}
        model = Mock(side_effect=[tool_call(arguments), {"role": "assistant", "content": "unused"}])
        runtime = self.runtime(model)
        result = runtime.run_turn("Draft an email to Ada saying see you soon. Go ahead and create it, I confirm.")
        pending = result.pending_confirmation
        self.assertIsNotNone(pending)
        self.assertEqual(pending["preview"]["body"], "Hi Ada,\nSee you soon.")
        self.assertEqual(pending["preview"]["to"], ["ada@example.org"])
        self.assertEqual(self.posted_drafts(), [])  # nothing written before approval
        cancelled = runtime.resolve_confirmation(pending["confirmation_id"], approved=False)
        self.assertIn("Cancelled", cancelled.response)
        self.assertEqual(self.posted_drafts(), [])
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(pending["confirmation_id"], approved=True)  # cannot approve later
        self.assertEqual(self.posted_drafts(), [])

    def test_approval_writes_exactly_the_previewed_draft_once(self):
        arguments = {"to": "ada@example.org", "cc": "ops@example.org", "subject": "Hello", "body": "Hi Ada,\nSee you soon."}
        model = Mock(side_effect=[tool_call(arguments), {"role": "assistant", "content": "Draft saved in Gmail."}])
        runtime = self.runtime(model)
        bodies = []
        provider = runtime.registry.google.provider()  # the default (only) account
        opener = self.google
        provider._opener = lambda request, timeout=None: (
            bodies.append(json.loads(request.data.decode())) if request.full_url == DRAFTS_URL else None
        ) or opener(request, timeout)
        pending = runtime.run_turn("Draft an email to Ada").pending_confirmation
        result = runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(result.response, "Draft saved in Gmail.")
        self.assertEqual(len(bodies), 1)
        message = decoded(bodies[0]["message"]["raw"])
        self.assertEqual(message["To"], ", ".join(pending["preview"]["to"]))
        self.assertEqual(message["Cc"], ", ".join(pending["preview"]["cc"]))
        self.assertEqual(message["Subject"], pending["preview"]["subject"])
        self.assertEqual(body_text(message), pending["preview"]["body"])
        tool_result = json.loads(runtime.history[-2]["content"])
        self.assertTrue(tool_result["ok"])
        self.assertIn("not been sent", tool_result["message"])

    def test_a_new_message_while_a_draft_waits_cancels_it(self):
        arguments = {"to": "ada@example.org", "subject": "Hello", "body": "Hi"}
        model = Mock(side_effect=[tool_call(arguments), {"role": "assistant", "content": "OK."}])
        runtime = self.runtime(model)
        runtime.run_turn("Draft an email to Ada")
        result = runtime.run_turn("Actually, never mind.")
        self.assertIn("Cancelled", result.notice)
        self.assertEqual(self.posted_drafts(), [])

    def test_the_chat_response_carries_the_preview_for_the_confirmation_panel(self):
        database = Path(self.temp_dir.name) / "s.sqlite3"
        storage.initialize_database(database)
        model = Mock(side_effect=[tool_call({"to": "ada@example.org", "subject": "Hi", "body": "Exact body"})])
        runtime = AssistantRuntime("http://local", "m", database_path=database,
                                   tool_registry=ToolRegistry(database, google=self.connected()), model_client=model)
        serialized = web_app._serialize_turn_result(runtime.run_turn("Draft it"))
        self.assertEqual(serialized["pending_confirmation"]["preview"]["body"], "Exact body")
        self.assertEqual(serialized["pending_confirmation"]["preview"]["to"], ["ada@example.org"])
        self.assertEqual(self.posted_drafts(), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
