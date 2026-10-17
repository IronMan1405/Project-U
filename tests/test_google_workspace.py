import base64
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import google_workspace
import web_app
from computer_utilities import UtilitySettings
from config import load_google_settings
from google_workspace import (
    GoogleError,
    GoogleSettings,
    GoogleWorkspaceProvider,
    KeychainTokenStore,
    MemoryTokenStore,
    build_google_settings,
    calendar_event,
    granted_capabilities,
    load_client,
    message_header,
    requested_scopes,
)
from tool_registry import ToolDefinition, ToolRegistry, check_confirmation_boundaries
from voice import VoiceSettings

CLIENT_ID = "12345-test.apps.googleusercontent.com"
CLIENT_SECRET = "TEST-CLIENT-SECRET"
REFRESH = "1//TEST-REFRESH-TOKEN"
ACCESS = "ya29.TEST-ACCESS-TOKEN"
NOW = 1_800_000_000.0
READ_SCOPES = " ".join([
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    google_workspace.SCOPE_GMAIL_METADATA,
    google_workspace.SCOPE_CALENDAR_EVENTS_READONLY,
])
# Everything the current consent asks for (stage 1 reads plus stage 2 drafts).
ALL_SCOPES = " ".join([READ_SCOPES, google_workspace.SCOPE_GMAIL_COMPOSE,
                       google_workspace.SCOPE_GMAIL_READONLY, google_workspace.SCOPE_GMAIL_MODIFY])


def id_token(**overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "exp": NOW + 3600,
        "email": "ops@example.org",
        "email_verified": True,
        "hd": "example.org",
    }
    claims.update(overrides)
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """Stands in for urllib's urlopen. Routes are (method, url prefix) -> response or callable."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, prefix, response):
        self.routes.insert(0, (method, prefix, response))
        return self

    def __call__(self, request, timeout=None):
        body = request.data.decode() if request.data else ""
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "auth": request.get_header("Authorization"),
            "form": dict(urllib.parse.parse_qsl(body)),
        })
        for method, prefix, response in self.routes:
            if request.get_method() == method and request.full_url.startswith(prefix):
                if callable(response) and not isinstance(response, (dict, Exception)):
                    response = response(request)
                if isinstance(response, Exception):
                    raise response
                return FakeResponse(json.dumps(response).encode())
        raise AssertionError(f"unexpected request {request.get_method()} {request.full_url}")

    def calls(self, prefix):
        return [r for r in self.requests if r["url"].startswith(prefix)]


def http_error(url, status, payload):
    return urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(json.dumps(payload).encode()))


def api_failure(status, reason):
    return http_error("https://api", status, {"error": {"code": status, "errors": [{"reason": reason}], "message": "PRIVATE TEXT"}})


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.client_file = Path(self.temp_dir.name) / "client.json"
        self.client_file.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}))
        self.clock = Mock(return_value=NOW)
        self.google = FakeGoogle()
        self.google.on("POST", google_workspace.REVOKE_URL, {})

    def tearDown(self):
        self.temp_dir.cleanup()

    def provider(self, record=None, domain="example.org", now=None):
        self.store = MemoryTokenStore(record)
        settings = GoogleSettings(enabled=True, client_secrets_file=self.client_file, workspace_domain=domain)
        return GoogleWorkspaceProvider(settings, store=self.store, opener=self.google, clock=self.clock, now=now)

    def connected(self, scopes=ALL_SCOPES, consent_version=google_workspace.CONSENT_VERSION, **kwargs):
        record = {"refresh_token": REFRESH, "scopes": scopes.split(), "email": "ops@example.org", "hd": "example.org",
                  "consent_version": consent_version}
        return self.provider(record, **kwargs)

    def token_endpoint(self, response=None):
        self.google.on("POST", google_workspace.TOKEN_URL, response or {"access_token": ACCESS, "expires_in": 3600})


class SettingsAndScopeTests(ProviderTestCase):
    def test_settings_require_an_absolute_client_file_and_valid_domain(self):
        self.assertFalse(build_google_settings({}).enabled)
        self.assertIn("client_secrets_file", build_google_settings({}).problem)
        self.assertIn("absolute", build_google_settings({"client_secrets_file": "client.json"}).problem)
        bad_domain = build_google_settings({"client_secrets_file": "/x/c.json", "workspace_domain": "not a domain"})
        self.assertIn("workspace_domain", bad_domain.problem)
        good = build_google_settings({"client_secrets_file": "~/kris/client.json", "workspace_domain": "Example.ORG"},
                                     home=Path("/Users/someone"))
        self.assertTrue(good.enabled)
        self.assertEqual(good.client_secrets_file, Path("/Users/someone/kris/client.json"))
        self.assertEqual(good.workspace_domain, "example.org")

    def test_google_is_off_without_a_config_section(self):
        config = Path(self.temp_dir.name) / "config.ini"
        config.write_text("[ollama]\nendpoint = http://127.0.0.1:11434\nmodel = m\n")
        self.assertFalse(load_google_settings(config).enabled)
        config.write_text(config.read_text() + f"[google]\nclient_secrets_file = {self.client_file}\n")
        self.assertTrue(load_google_settings(config).enabled)

    def test_only_a_desktop_client_file_is_accepted(self):
        self.assertEqual(load_client(self.client_file).client_id, CLIENT_ID)
        cases = {
            "missing.json": None,
            "web.json": {"web": {"client_id": CLIENT_ID, "client_secret": "s"}},
            "bad.json": "{not json",
            "id.json": {"installed": {"client_id": "someone-else", "client_secret": "s"}},
            "nosecret.json": {"installed": {"client_id": CLIENT_ID}},
        }
        for name, content in cases.items():
            path = Path(self.temp_dir.name) / name
            if content is not None:
                path.write_text(content if isinstance(content, str) else json.dumps(content))
            with self.subTest(name=name), self.assertRaises(GoogleError) as raised:
                load_client(path)
            self.assertEqual(raised.exception.kind, "not_configured")
            self.assertNotIn("s\"", raised.exception.message)

    def test_only_the_needed_scopes_are_requested(self):
        self.assertEqual(requested_scopes(), [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.metadata",
            "https://www.googleapis.com/auth/calendar.events.readonly",
            "https://www.googleapis.com/auth/gmail.compose",  # stage 2: drafts
            "https://www.googleapis.com/auth/gmail.readonly",  # stage 3: read
            "https://www.googleapis.com/auth/gmail.modify",  # stage 3: organise
        ])
        for scope in requested_scopes():
            for forbidden in ("gmail.send", "mail.google.com", "gmail.insert", "gmail.settings"):
                self.assertNotIn(forbidden, scope)
            self.assertNotEqual(scope, "https://www.googleapis.com/auth/calendar.events")
            self.assertNotEqual(scope, "https://www.googleapis.com/auth/calendar")
        for capability in google_workspace.FUTURE_CAPABILITIES.values():
            self.assertTrue(capability.consequential)
            for scope in capability.scopes:
                self.assertNotIn(scope, requested_scopes())

    def test_capabilities_follow_the_granted_scopes(self):
        self.assertEqual(granted_capabilities(set(ALL_SCOPES.split())),
                         {"identity": True, "inbox.read": True, "calendar.read": True, "mail.draft": True,
                          "mail.read": True, "mail.modify": True})
        partial = granted_capabilities({"openid", "email", google_workspace.SCOPE_CALENDAR_EVENTS_READONLY})
        self.assertEqual(partial, {"identity": True, "inbox.read": False, "calendar.read": True, "mail.draft": False,
                                   "mail.read": False, "mail.modify": False})


class AuthorizationTests(ProviderTestCase):
    REDIRECT = "http://127.0.0.1:5000/oauth/google/callback"

    def begin(self, provider):
        url = provider.begin_authorization(self.REDIRECT)
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def test_the_consent_url_uses_pkce_offline_access_and_a_domain_hint(self):
        params = self.begin(self.provider())
        self.assertEqual(params["client_id"], CLIENT_ID)
        self.assertEqual(params["redirect_uri"], self.REDIRECT)
        self.assertEqual(params["scope"], " ".join(requested_scopes()))
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["access_type"], "offline")
        self.assertEqual(params["include_granted_scopes"], "true")
        self.assertEqual(params["hd"], "example.org")
        self.assertGreaterEqual(len(params["state"]), 32)
        self.assertNotIn(CLIENT_SECRET, json.dumps(params))

    def complete(self, provider, params, **tokens):
        response = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
                    "scope": ALL_SCOPES, "id_token": id_token()}
        response.update(tokens)
        self.token_endpoint(response)
        return provider.complete_authorization(params["state"], "AUTH-CODE", None)

    def test_a_completed_consent_stores_only_what_is_needed_and_hides_tokens(self):
        provider = self.provider()
        params = self.begin(provider)
        status = self.complete(provider, params)
        exchange = self.google.calls(google_workspace.TOKEN_URL)[0]["form"]
        verifier = exchange["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, params["code_challenge"])
        self.assertEqual(exchange["redirect_uri"], self.REDIRECT)
        self.assertEqual(self.store.record["refresh_token"], REFRESH)
        self.assertEqual(self.store.record["email"], "ops@example.org")
        self.assertTrue(status["connected"])
        self.assertEqual(status["account"], "ops@example.org")
        self.assertNotIn(REFRESH, json.dumps(status))
        self.assertNotIn(ACCESS, json.dumps(status))

    def test_a_sign_in_link_works_once_and_expires(self):
        provider = self.provider()
        params = self.begin(provider)
        self.complete(provider, params)
        with self.assertRaises(GoogleError) as raised:
            provider.complete_authorization(params["state"], "AUTH-CODE", None)
        self.assertEqual(raised.exception.kind, "state")
        with self.assertRaises(GoogleError):
            provider.complete_authorization("forged-state", "AUTH-CODE", None)
        stale = self.begin(provider)
        self.clock.return_value = NOW + google_workspace.PENDING_FLOW_SECONDS + 1
        with self.assertRaises(GoogleError) as raised:
            provider.complete_authorization(stale["state"], "AUTH-CODE", None)
        self.assertEqual(raised.exception.kind, "state")

    def test_denied_consent_and_workspace_policy_are_reported_clearly(self):
        provider = self.provider()
        for error, kind in [("access_denied", "denied"), ("admin_policy_enforced", "policy"), ("server_error", "oauth")]:
            params = self.begin(provider)
            with self.subTest(error=error), self.assertRaises(GoogleError) as raised:
                provider.complete_authorization(params["state"], None, error)
            self.assertEqual(raised.exception.kind, kind)
        self.assertIsNone(self.store.record)

    def test_accounts_outside_the_workspace_domain_are_refused_and_revoked(self):
        provider = self.provider()
        with self.assertRaises(GoogleError) as raised:
            self.complete(provider, self.begin(provider), id_token=id_token(hd=None, email="me@gmail.com"))
        self.assertEqual(raised.exception.kind, "wrong_domain")
        self.assertIsNone(self.store.record)
        self.assertEqual(self.google.calls(google_workspace.REVOKE_URL)[0]["form"]["token"], REFRESH)

    def test_identity_tokens_for_another_app_or_expired_are_refused(self):
        for claims in [{"aud": "other.apps.googleusercontent.com"}, {"iss": "https://evil.example"},
                       {"exp": NOW - 1}, {"email_verified": False}]:
            provider = self.provider()
            with self.subTest(claims=claims), self.assertRaises(GoogleError):
                self.complete(provider, self.begin(provider), id_token=id_token(**claims))
            self.assertIsNone(self.store.record)

    def test_a_rejected_code_or_missing_offline_access_stores_nothing(self):
        provider = self.provider()
        params = self.begin(provider)
        self.google.on("POST", google_workspace.TOKEN_URL, http_error(google_workspace.TOKEN_URL, 400, {"error": "invalid_grant"}))
        with self.assertRaises(GoogleError) as raised:
            provider.complete_authorization(params["state"], "AUTH-CODE", None)
        self.assertEqual(raised.exception.kind, "oauth")
        with self.assertRaises(GoogleError):
            self.complete(provider, self.begin(provider), refresh_token=None)
        self.assertIsNone(self.store.record)

    def test_partially_granted_consent_is_reported(self):
        provider = self.provider()
        status = self.complete(provider, self.begin(provider), scope="openid email " + google_workspace.SCOPE_CALENDAR_EVENTS_READONLY)
        self.assertFalse(status["capabilities"]["inbox.read"])
        self.assertTrue(status["capabilities"]["calendar.read"])
        self.assertIn("Reconnect", status["message"])

    def test_without_configuration_nothing_starts(self):
        provider = GoogleWorkspaceProvider(GoogleSettings(), store=MemoryTokenStore(), opener=self.google)
        self.assertFalse(provider.status()["configured"])
        with self.assertRaises(GoogleError) as raised:
            provider.begin_authorization(self.REDIRECT)
        self.assertEqual(raised.exception.kind, "not_configured")
        self.assertEqual(self.google.requests, [])


class ReadTests(ProviderTestCase):
    def inbox_routes(self):
        self.token_endpoint()
        self.google.on("GET", f"{google_workspace.GMAIL_API}/labels/INBOX", {"id": "INBOX", "messagesUnread": 3})
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages?", {"messages": [{"id": "abc1"}, {"id": "abc2"}]})
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/abc1", {
            "id": "abc1", "labelIds": ["INBOX", "UNREAD"], "internalDate": "1800000000000",
            "payload": {"headers": [{"name": "From", "value": "Ada Lovelace <ada@example.org>"},
                                    {"name": "Subject", "value": "Quarterly review"},
                                    {"name": "Date", "value": "Mon, 1 Jan 2027 09:00:00 +0000"}]},
        })
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/abc2", {
            "id": "abc2", "labelIds": ["INBOX"], "payload": {"headers": [{"name": "From", "value": "noreply@example.org"}]},
        })

    def test_the_inbox_returns_unread_count_and_headers_only(self):
        self.inbox_routes()
        summary = self.connected().inbox_summary()
        self.assertEqual(summary["unread_count"], 3)
        self.assertEqual(summary["messages"][0], {
            "id": "abc1", "from": "Ada Lovelace", "subject": "Quarterly review",
            "received": datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat(), "unread": True,
        })
        self.assertEqual(summary["messages"][1]["from"], "noreply@example.org")
        self.assertEqual(summary["messages"][1]["subject"], "(No subject)")
        detail = self.google.calls(f"{google_workspace.GMAIL_API}/messages/abc1")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(detail["url"]).query)
        self.assertEqual(query["format"], ["metadata"])
        self.assertEqual(query["metadataHeaders"], ["From", "Subject", "Date"])
        self.assertEqual(detail["auth"], f"Bearer {ACCESS}")
        listing = self.google.calls(f"{google_workspace.GMAIL_API}/messages?")[0]["url"]
        self.assertIn("maxResults=10", listing)
        self.assertNotIn(ACCESS, json.dumps(summary))

    def test_results_are_cached_for_a_minute(self):
        self.inbox_routes()
        provider = self.connected()
        provider.inbox_summary()
        provider.inbox_summary()
        self.assertEqual(len(self.google.calls(f"{google_workspace.GMAIL_API}/labels/INBOX")), 1)
        self.clock.return_value = NOW + google_workspace.CACHE_SECONDS + 1
        provider.inbox_summary()
        self.assertEqual(len(self.google.calls(f"{google_workspace.GMAIL_API}/labels/INBOX")), 2)

    def test_an_expired_access_token_is_refreshed_once(self):
        self.inbox_routes()
        responses = [api_failure(401, "authError")]

        def label(request):
            return responses.pop(0) if responses else {"messagesUnread": 0}

        self.google.on("GET", f"{google_workspace.GMAIL_API}/labels/INBOX", label)
        self.assertEqual(self.connected().inbox_summary()["unread_count"], 0)
        self.assertEqual(len(self.google.calls(google_workspace.TOKEN_URL)), 2)

    def test_a_revoked_refresh_token_disconnects_cleanly(self):
        self.google.on("POST", google_workspace.TOKEN_URL, http_error(google_workspace.TOKEN_URL, 400, {"error": "invalid_grant"}))
        provider = self.connected()
        with self.assertRaises(GoogleError) as raised:
            provider.inbox_summary()
        self.assertEqual(raised.exception.kind, "revoked")
        self.assertTrue(raised.exception.reconnect)
        self.assertIsNone(self.store.record)
        self.assertFalse(provider.status()["connected"])

    def test_api_failures_become_safe_messages(self):
        cases = [
            (api_failure(403, "accessNotConfigured"), "not_enabled"),
            (api_failure(403, "insufficientPermissions"), "not_granted"),
            (api_failure(403, "domainPolicy"), "policy"),
            (api_failure(429, "rateLimitExceeded"), "rate_limited"),
            (api_failure(503, "backendError"), "unavailable"),
            (api_failure(404, "notFound"), "not_found"),
            (api_failure(400, "invalidArgument"), "api"),
            (urllib.error.URLError("dns failure"), "network"),
            (TimeoutError(), "network"),
        ]
        for failure, kind in cases:
            self.google = FakeGoogle()
            self.token_endpoint()
            self.google.on("GET", f"{google_workspace.GMAIL_API}/labels/INBOX", failure)
            with self.subTest(kind=kind), self.assertRaises(GoogleError) as raised:
                self.connected().inbox_summary()
            self.assertEqual(raised.exception.kind, kind)
            self.assertNotIn("PRIVATE TEXT", raised.exception.message)
            self.assertNotIn("dns failure", raised.exception.message)

    def test_reads_need_a_connection_and_the_matching_scope(self):
        with self.assertRaises(GoogleError) as raised:
            self.provider().inbox_summary()
        self.assertEqual(raised.exception.kind, "not_connected")
        calendar_only = self.connected(scopes="openid email " + google_workspace.SCOPE_CALENDAR_EVENTS_READONLY)
        with self.assertRaises(GoogleError) as raised:
            calendar_only.inbox_summary()
        self.assertEqual(raised.exception.kind, "not_granted")
        self.assertEqual(self.google.requests, [])

    def test_calendar_covers_today_and_the_next_seven_days(self):
        now = datetime(2027, 3, 10, 15, 30, tzinfo=timezone(timedelta(hours=1)))
        self.token_endpoint()
        self.google.on("GET", f"{google_workspace.CALENDAR_API}/calendars/primary/events", {
            "items": [
                {"summary": "Stand-up", "status": "confirmed",
                 "start": {"dateTime": "2027-03-10T09:30:00+01:00"}, "end": {"dateTime": "2027-03-10T09:45:00+01:00"}},
                {"summary": "Offsite", "start": {"date": "2027-03-12"}, "end": {"date": "2027-03-13"}},
                {"summary": "Cancelled sync", "status": "cancelled", "start": {"dateTime": "2027-03-11T10:00:00+01:00"}},
                {"start": {"dateTime": "2027-03-14T08:00:00+01:00"}},
            ],
            "nextPageToken": "more",
        })
        result = self.connected(now=lambda: now).upcoming_events()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.google.calls(google_workspace.CALENDAR_API)[0]["url"]).query)
        self.assertEqual(query["timeMin"], ["2027-03-10T00:00:00+01:00"])
        self.assertEqual(query["timeMax"], ["2027-03-18T00:00:00+01:00"])
        self.assertEqual(query["singleEvents"], ["true"])
        self.assertEqual([event["title"] for event in result["events"]], ["Stand-up", "Offsite", "(No title)"])
        self.assertTrue(result["events"][1]["all_day"])
        self.assertTrue(result["truncated"])

    def test_header_and_event_parsing_is_defensive(self):
        self.assertEqual(message_header({})["from"], "Unknown sender")
        self.assertEqual(len(message_header({"payload": {"headers": [{"name": "Subject", "value": "x" * 500}]}})["subject"]), 200)
        self.assertIsNone(calendar_event({"summary": "No start"}))
        self.assertIsNone(calendar_event("not a dict"))


class DisconnectAndStorageTests(ProviderTestCase):
    def test_disconnect_revokes_and_forgets_even_if_revocation_fails(self):
        provider = self.connected()
        provider.disconnect()
        self.assertEqual(self.google.calls(google_workspace.REVOKE_URL)[0]["form"]["token"], REFRESH)
        self.assertIsNone(self.store.record)
        failing = self.connected()
        self.google.on("POST", google_workspace.REVOKE_URL, urllib.error.URLError("offline"))
        with self.assertLogs("google_workspace", level="INFO") as logs:
            failing.disconnect()
        self.assertIsNone(self.store.record)
        self.assertNotIn(REFRESH, "\n".join(logs.output))

    def test_keychain_writes_the_secret_over_stdin_never_argv(self):
        run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
        store = KeychainTokenStore(run=run)
        record = {"refresh_token": REFRESH, "scopes": ["openid"], "email": "ops@example.org"}
        with unittest.mock.patch("google_workspace.Path.is_file", return_value=True):
            store.save(record)
            argv = run.call_args.args[0]
            stdin = run.call_args.kwargs["input"]
            self.assertEqual(argv, ["/usr/bin/security", "-i"])
            self.assertNotIn(REFRESH, " ".join(argv))
            encoded = stdin.split('-w "')[1].split('"')[0]
            self.assertEqual(json.loads(base64.urlsafe_b64decode(encoded)), record)
            run.return_value = Mock(returncode=0, stdout=encoded + "\n", stderr="")
            self.assertEqual(store.load(), record)
            run.return_value = Mock(returncode=44, stdout="", stderr="")
            self.assertIsNone(store.load())
            store.delete()  # nothing stored is fine
            run.return_value = Mock(returncode=1, stdout="", stderr="")
            with self.assertRaises(GoogleError):
                store.load()
            run.return_value = Mock(returncode=0, stdout="!!!not base64!!!", stderr="")
            with self.assertRaises(GoogleError) as raised:
                store.load()
            self.assertTrue(raised.exception.reconnect)


class ConfirmationBoundaryTests(unittest.TestCase):
    def test_consequential_google_tools_must_require_confirmation(self):
        execute = Mock()
        unconfirmed = {"google_send_email": ToolDefinition("google_send_email", "Send", {}, execute)}
        with self.assertRaises(ValueError):
            check_confirmation_boundaries(unconfirmed)
        confirmed = {"google_send_email": ToolDefinition("google_send_email", "Send", {}, execute, always_confirm=True)}
        check_confirmation_boundaries(confirmed)

    def test_the_model_has_no_google_tools_in_the_read_only_milestone(self):
        names = {definition["function"]["name"] for definition in ToolRegistry().definitions()}
        self.assertFalse(any(name.startswith("google_") for name in names))

    def test_consequential_operations_are_refused_by_the_provider(self):
        provider = GoogleWorkspaceProvider(GoogleSettings(), store=MemoryTokenStore())
        for operation in ("send_email", "archive", "create_event"):
            with self.subTest(operation=operation), self.assertRaises(GoogleError) as raised:
                provider.perform(operation, to="someone@example.org")
            self.assertEqual(raised.exception.kind, "not_supported")


class GoogleRouteTests(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.app_provider = self.connected()
        self.client = web_app.create_app(
            Path(self.temp_dir.name) / "google.sqlite3",
            utility_settings=UtilitySettings(),
            voice_settings=VoiceSettings(),
            google=self.app_provider,
        ).test_client()
        self.host = {"Host": "127.0.0.1:5000"}

    def test_status_never_contains_tokens(self):
        response = self.client.get("/api/google/status", headers=self.host)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.get_data(as_text=True)
        self.assertTrue(response.get_json()["connected"])
        for secret in (REFRESH, ACCESS, CLIENT_SECRET):
            self.assertNotIn(secret, body)

    def test_connect_returns_a_google_url_with_a_loopback_redirect(self):
        response = self.client.post("/api/google/connect", headers={"Host": "localhost:5000"})
        url = response.get_json()["authorization_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(params["redirect_uri"], "http://127.0.0.1:5000/oauth/google/callback")

    def test_connect_and_disconnect_are_same_origin_posts_only(self):
        for url in ("/api/google/connect", "/api/google/disconnect"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, headers=self.host).status_code, 405)
                cross = self.client.post(url, headers={**self.host, "Origin": "https://evil.example"})
                self.assertEqual(cross.status_code, 403)
        self.assertIsNotNone(self.store.record)  # the cross-site disconnect did nothing

    def test_untrusted_hosts_are_rejected(self):
        for url in ("/api/google/status", "/api/google/inbox", "/api/google/calendar", "/oauth/google/callback"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, headers={"Host": "attacker.example:5000"}).status_code, 400)

    def test_callback_reports_errors_without_reflecting_parameters(self):
        response = self.client.get("/oauth/google/callback?state=forged&error=<script>alert(1)</script>", headers=self.host)
        self.assertEqual(response.status_code, 400)
        page = response.get_data(as_text=True)
        self.assertNotIn("<script>alert", page)
        self.assertIn("expired or was already used", page)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_callback_completes_a_real_flow(self):
        url = self.client.post("/api/google/connect", headers=self.host).get_json()["authorization_url"]
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        self.token_endpoint({"access_token": ACCESS, "refresh_token": "1//NEW", "expires_in": 3600,
                             "scope": ALL_SCOPES, "id_token": id_token()})
        response = self.client.get(f"/oauth/google/callback?state={state}&code=CODE", headers=self.host)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Connected", response.get_data(as_text=True))
        self.assertNotIn("1//NEW", response.get_data(as_text=True))
        self.assertEqual(self.store.record["refresh_token"], "1//NEW")

    def test_read_routes_return_data_or_a_safe_error(self):
        self.token_endpoint()
        self.google.on("GET", f"{google_workspace.GMAIL_API}/labels/INBOX", api_failure(403, "domainPolicy"))
        response = self.client.get("/api/google/inbox", headers=self.host)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["kind"], "policy")
        self.client.post("/api/google/disconnect", headers=self.host)
        response = self.client.get("/api/google/calendar", headers=self.host)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["kind"], "not_connected")
        self.assertTrue(response.get_json()["reconnect"])

    def test_an_unconfigured_app_reports_setup_instead_of_failing(self):
        client = web_app.create_app(
            Path(self.temp_dir.name) / "off.sqlite3", utility_settings=UtilitySettings(),
            voice_settings=VoiceSettings(), google=GoogleWorkspaceProvider(GoogleSettings(), store=MemoryTokenStore()),
        ).test_client()
        self.assertFalse(client.get("/api/google/status", headers=self.host).get_json()["configured"])
        self.assertEqual(client.post("/api/google/connect", headers=self.host).status_code, 409)
        self.assertEqual(client.get("/api/google/inbox", headers=self.host).status_code, 409)


if __name__ == "__main__":
    unittest.main()
