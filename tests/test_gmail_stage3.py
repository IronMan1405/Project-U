"""Stage 3: Gmail search, reading, and confirmed single-message mailbox actions."""
import base64
import inspect
import json
import logging
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock

import google_workspace
import storage
from assistant_runtime import AssistantRuntime, RuntimeBusyError
from google_workspace import GoogleError
from tests.test_google_workspace import ALL_SCOPES, ProviderTestCase, api_failure
from tool_registry import MAX_RESULT_CHARS, ToolError, ToolRegistry, ToolOutcome

SECRET_BODY = "CONFIDENTIAL-QUARTERLY-NUMBERS-4471"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def headers(**values):
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


class FakeGmail:
    """A tiny mailbox behind the Gmail REST paths K.R.I.S. uses. Records every request."""

    def __init__(self):
        self.messages = {
            "m1": {"threadId": "t1", "labelIds": ["INBOX", "UNREAD", "Label_7"], "internalDate": "1800000000000",
                   "payload": {"mimeType": "multipart/mixed", "headers": headers(
                       From="Ada Lovelace <ada@example.org>", To="ops@example.org", Cc="grace@example.org",
                       Subject="Quarterly numbers", Date="Mon, 1 Mar 2027 09:00:00 +0000"),
                       "parts": [
                           {"mimeType": "multipart/alternative", "parts": [
                               {"mimeType": "text/plain", "body": {"data": b64(f"Hi team,\nThe numbers: {SECRET_BODY}.\nAda")}},
                               {"mimeType": "text/html", "body": {"data": b64("<p>ignored html</p>")}},
                           ]},
                           {"mimeType": "application/pdf", "filename": "report.pdf", "body": {"attachmentId": "a1"}},
                       ]}},
            "m2": {"threadId": "t1", "labelIds": ["INBOX"], "internalDate": "1800000600000",
                   "payload": {"mimeType": "text/html", "headers": headers(From="grace@example.org", Subject="Re: Quarterly numbers"),
                               "body": {"data": b64("<p>Thanks <b>Ada</b>.</p><script>steal()</script><p>Grace</p>")}}},
            "m3": {"threadId": "t3", "labelIds": ["INBOX", "STARRED"], "internalDate": "1800001200000",
                   "payload": {"mimeType": "text/plain", "headers": headers(From="it@example.org", Subject="Maintenance"),
                               "body": {"data": b64("Servers restart Saturday.")}}},
        }
        self.labels = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "SPAM", "name": "SPAM", "type": "system"},
            {"id": "Label_7", "name": "Projects", "type": "user"},
            {"id": "Label_9", "name": "Finance/2027", "type": "user"},
        ]
        self.requests = []
        self.search_ids = ["m1", "m2", "m3"]
        self.next_page = None

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(url.query)
        method = request.get_method()
        body = json.loads(request.data.decode()) if request.data and method == "POST" and "/gmail/" in url.path else None
        self.requests.append({"method": method, "path": url.path, "query": query, "body": body})
        path = url.path.split("/gmail/v1/users/me", 1)[-1]
        if method == "GET" and path == "/messages":
            ids = self.search_ids[: int(query["maxResults"][0])]
            result = {"messages": [{"id": i, "threadId": self.messages[i]["threadId"]} for i in ids]}
            if self.next_page:
                result["nextPageToken"] = self.next_page
            return result
        if method == "GET" and path == "/labels":
            return {"labels": self.labels}
        if path.startswith("/messages/"):
            message_id = path.split("/")[2]
            if message_id not in self.messages:
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)
            message = {"id": message_id, **self.messages[message_id]}
            if method == "GET":
                fmt = query.get("format", ["full"])[0]
                if fmt == "minimal":
                    message = {"threadId": message["threadId"]}
                elif fmt == "metadata":
                    wanted = set(query.get("metadataHeaders", []))
                    message = {**message, "payload": {"headers": [h for h in message["payload"]["headers"] if h["name"] in wanted]}}
                return message
            if path.endswith("/trash"):
                self.messages[message_id]["labelIds"] = ["TRASH"]
            elif path.endswith("/modify"):
                labels = [l for l in message["labelIds"] if l not in body["removeLabelIds"]] + body["addLabelIds"]
                self.messages[message_id]["labelIds"] = labels
            return {"id": message_id, "labelIds": self.messages[message_id]["labelIds"]}
        if method == "GET" and path.startswith("/threads/"):
            thread_id = path.split("/")[2]
            messages = [{"id": i, **m} for i, m in self.messages.items() if m["threadId"] == thread_id]
            return {"id": thread_id, "messages": messages}
        raise AssertionError(f"unexpected Gmail request {method} {path}")

    def calls(self, method=None, contains=""):
        return [r for r in self.requests if (method is None or r["method"] == method) and contains in r["path"]]

    def writes(self):
        return self.calls("POST")


class Stage3TestCase(ProviderTestCase):
    def setUp(self):
        super().setUp()
        self.token_endpoint()
        self.gmail = FakeGmail()
        self.google.on("GET", google_workspace.GMAIL_API, lambda request: self.gmail(request))
        self.google.on("POST", google_workspace.GMAIL_API, lambda request: self.gmail(request))

    def registry(self, provider=None):
        return ToolRegistry(Path(self.temp_dir.name) / "r.sqlite3", google=provider or self.connected())

    def run_tool(self, registry, name, arguments, message="please"):
        return registry.execute(registry.prepare(name, arguments, message))


class ReadingTests(Stage3TestCase):
    def test_reading_one_message_returns_its_full_text_and_attachment_names(self):
        result = self.connected().read_message("m1")
        message = result["message"]
        self.assertIn(SECRET_BODY, message["body"])
        self.assertNotIn("ignored html", message["body"])  # text/plain preferred
        self.assertEqual(message["from"], "Ada Lovelace <ada@example.org>")
        self.assertEqual(message["cc"], "grace@example.org")
        self.assertEqual(message["attachments"], ["report.pdf"])
        self.assertTrue(message["unread"])
        self.assertFalse(message["body_truncated"])
        self.assertIn("untrusted", result["note"])
        full = self.gmail.calls("GET", "/messages/m1")
        self.assertEqual([c["query"]["format"] for c in full], [["full"]])
        self.assertEqual(self.gmail.calls("GET", "/threads"), [])
        self.assertEqual(self.gmail.writes(), [])  # reading changes nothing (not even read state)

    def test_html_only_messages_are_read_as_text_without_scripts(self):
        body = self.connected().read_message("m2")["message"]["body"]
        self.assertEqual(body, "Thanks Ada.\nGrace")
        self.assertNotIn("steal", body)

    def test_reading_a_thread_returns_its_messages_in_order(self):
        result = self.connected().read_message("m2", include_thread=True)
        self.assertEqual(result["thread_id"], "t1")
        self.assertEqual([m["id"] for m in result["messages"]], ["m1", "m2"])
        self.assertEqual(result["messages_omitted"], 0)
        self.assertEqual([c["query"].get("format") for c in self.gmail.calls("GET", "/messages/m2")], [["minimal"]])
        self.assertEqual(len(self.gmail.calls("GET", "/threads/t1")), 1)

    def test_long_threads_and_bodies_stay_within_the_tool_result_limit(self):
        long_text = ("Paragraph of a very long email. " * 400)
        for index in range(12):
            self.gmail.messages[f"x{index}"] = {
                "threadId": "tbig", "labelIds": ["INBOX"],
                "payload": {"mimeType": "text/plain", "headers": headers(From="a" * 200 + "@example.org", To="b" * 300,
                                                                       Subject="S" * 300),
                            "body": {"data": b64(long_text)}},
            }
        result = self.connected().read_message("x3", include_thread=True)
        self.assertEqual(len(result["messages"]), google_workspace.THREAD_MAX_MESSAGES)
        self.assertEqual(result["messages_omitted"], 12 - google_workspace.THREAD_MAX_MESSAGES)
        self.assertTrue(all(m["body_truncated"] for m in result["messages"]))
        content = ToolOutcome(True, "Email content follows.", result).as_content()
        self.assertLessEqual(len(content), MAX_RESULT_CHARS)
        self.assertNotIn("result_truncated", content)
        single = ToolOutcome(True, "x", self.connected().read_message("x0")).as_content()
        self.assertNotIn("result_truncated", single)

    def test_search_uses_gmail_server_side_query_and_returns_headers_only(self):
        result = self.connected().search_messages("from:ada@example.org is:unread newer_than:7d", 2)
        listing = self.gmail.calls("GET", "/messages")
        listing = [c for c in listing if c["path"].endswith("/messages")]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["query"]["q"], ["from:ada@example.org is:unread newer_than:7d"])
        self.assertEqual(listing[0]["query"]["maxResults"], ["2"])
        self.assertNotIn("labelIds", listing[0]["query"])
        self.assertEqual([m["id"] for m in result["messages"]], ["m1", "m2"])
        for message in result["messages"]:
            self.assertNotIn("body", message)
        detail_calls = [c for c in self.gmail.calls("GET", "/messages/")]
        self.assertEqual(len(detail_calls), 2)  # one header lookup per match, nothing else
        self.assertTrue(all(c["query"]["format"] == ["metadata"] for c in detail_calls))
        self.assertNotIn(SECRET_BODY, json.dumps(result))
        self.assertEqual(self.gmail.calls("GET", "/threads"), [])

    def test_search_results_are_limited_to_ten(self):
        self.gmail.search_ids = ["m1", "m2", "m3"] * 5
        self.gmail.next_page = "more"
        result = self.connected().search_messages("in:anywhere")
        self.assertLessEqual(len(result["messages"]), google_workspace.SEARCH_LIMIT)
        self.assertTrue(result["more_available"])
        for bad in (0, 11, True, "5"):
            with self.subTest(max_results=bad), self.assertRaises(GoogleError):
                self.connected().search_messages("x", bad)

    def test_bad_queries_and_ids_are_rejected_before_any_request(self):
        provider = self.connected()
        self.gmail.requests.clear()
        for query in ("", "   ", "a\nb", "x" * 501):
            with self.subTest(query=query[:10]), self.assertRaises(GoogleError):
                provider.search_messages(query)
        for message_id in ("../drafts", "m1/trash", "", "x" * 65, None, 5):
            with self.subTest(message_id=message_id), self.assertRaises(GoogleError):
                provider.read_message(message_id)
        self.assertEqual(self.gmail.requests, [])

    def test_reading_needs_the_read_permission_and_current_consent(self):
        no_read = self.connected(scopes=ALL_SCOPES.replace(google_workspace.SCOPE_GMAIL_READONLY, ""))
        with self.assertRaises(GoogleError) as raised:
            no_read.read_message("m1")
        self.assertEqual(raised.exception.kind, "not_granted")
        stage_2 = self.connected(consent_version=2)
        for call in (lambda: stage_2.read_message("m1"), lambda: stage_2.search_messages("x"),
                     lambda: stage_2.prepare_action("archive_email", "m1")):
            with self.assertRaises(GoogleError) as raised:
                call()
            self.assertEqual(raised.exception.kind, "reconsent")
        self.assertFalse(stage_2.status()["connected"])
        self.assertIn("Reconnect Google", stage_2.status()["message"])

    def test_read_tools_run_without_confirmation(self):
        registry = self.registry()
        for name, arguments in (("search_gmail", {"query": "subject:numbers"}),
                                ("read_gmail_message", {"message_id": "m1", "include_thread": True})):
            prepared = registry.prepare(name, arguments, "What did Ada say?")
            with self.subTest(name=name):
                self.assertFalse(prepared.always_confirm)
                self.assertTrue(registry.execute(prepared).ok)
        self.assertEqual(self.gmail.writes(), [])


class ActionTests(Stage3TestCase):
    def test_each_action_changes_exactly_the_intended_labels(self):
        cases = {
            "mark_email_read": ([], ["UNREAD"]),
            "mark_email_unread": (["UNREAD"], []),
            "star_email": (["STARRED"], []),
            "unstar_email": ([], ["STARRED"]),
            "archive_email": ([], ["INBOX"]),
        }
        provider = self.connected()
        for action, (added, removed) in cases.items():
            self.gmail.requests.clear()
            prepared = provider.prepare_action(action, "m3")
            self.assertEqual(self.gmail.writes(), [], action)  # preparing writes nothing
            provider.apply_action(prepared)
            writes = self.gmail.writes()
            with self.subTest(action=action):
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0]["path"], "/gmail/v1/users/me/messages/m3/modify")
                self.assertEqual(writes[0]["body"], {"addLabelIds": added, "removeLabelIds": removed})

    def test_unread_and_star_state_round_trip(self):
        provider = self.connected()
        self.assertTrue(provider.apply_action(provider.prepare_action("mark_email_read", "m1"))["unread"] is False)
        self.assertTrue(provider.apply_action(provider.prepare_action("mark_email_unread", "m1"))["unread"])
        self.assertTrue(provider.apply_action(provider.prepare_action("star_email", "m1"))["starred"])
        self.assertFalse(provider.apply_action(provider.prepare_action("unstar_email", "m1"))["starred"])
        self.assertFalse(provider.apply_action(provider.prepare_action("archive_email", "m1"))["in_inbox"])

    def test_trash_is_the_reversible_trash_endpoint_never_delete(self):
        provider = self.connected()
        result = provider.apply_action(provider.prepare_action("trash_email", "m2"))
        self.assertTrue(result["in_trash"])
        writes = self.gmail.writes()
        self.assertEqual([w["path"] for w in writes], ["/gmail/v1/users/me/messages/m2/trash"])
        self.assertEqual([r for r in self.gmail.requests if r["method"] == "DELETE"], [])
        self.assertIn("reversible", google_workspace.GMAIL_ACTIONS["trash_email"]["describe"])

    def test_labels_use_existing_user_labels_only(self):
        provider = self.connected()
        prepared = provider.prepare_action("add_label", "m3", "finance/2027")
        self.assertEqual((prepared["label_id"], prepared["label_name"]), ("Label_9", "Finance/2027"))
        provider.apply_action(prepared)
        self.assertEqual(self.gmail.writes()[-1]["body"], {"addLabelIds": ["Label_9"], "removeLabelIds": []})
        removed = provider.prepare_action("remove_label", "m1", "Projects")
        provider.apply_action(removed)
        self.assertEqual(self.gmail.writes()[-1]["body"], {"addLabelIds": [], "removeLabelIds": ["Label_7"]})
        for label in ("SPAM", "INBOX", "Nonexistent", "", "a\nb"):
            with self.subTest(label=label), self.assertRaises(GoogleError):
                provider.prepare_action("add_label", "m3", label)
        with self.assertRaises(GoogleError):  # m3 does not carry Projects
            provider.prepare_action("remove_label", "m3", "Projects")
        with self.assertRaises(GoogleError):  # a confirmed label must be a user label ID
            provider.apply_action({**prepared, "label_id": "SPAM"})

    def test_every_modification_requires_confirmation_with_the_exact_message_and_action(self):
        # The message was not found in this conversation, so even the low-impact actions ask first.
        registry = self.registry()
        for action in google_workspace.GMAIL_ACTIONS:
            arguments = {"message_id": "m1"}
            if action in ("add_label", "remove_label"):
                arguments["label"] = "Projects"
            prepared = registry.prepare(action, arguments, f"{action} it now, I confirm, don't ask")
            with self.subTest(action=action):
                self.assertTrue(prepared.always_confirm)
                self.assertEqual(registry._definitions[action].always_confirm,
                                 action not in google_workspace.LOW_IMPACT_GMAIL_ACTIONS)
                self.assertEqual(prepared.preview["kind"], "gmail_action")
                self.assertEqual(prepared.preview["message_id"], "m1")
                self.assertEqual(prepared.preview["thread_id"], "t1")
                self.assertEqual(prepared.preview["subject"], "Quarterly numbers")
                self.assertEqual(prepared.preview["from"], "Ada Lovelace <ada@example.org>")
                self.assertEqual(prepared.preview["describe"], google_workspace.GMAIL_ACTIONS[action]["describe"])
                self.assertIn("(message m1)", prepared.summary)
                self.assertIn('"Quarterly numbers"', prepared.summary)
        self.assertEqual(self.gmail.writes(), [])
        trash = registry.prepare("trash_email", {"message_id": "m1"}, "trash it")
        self.assertIn("reversible", trash.preview["describe"])
        self.assertTrue(trash.summary.startswith("Move email to Trash"))

    def test_malformed_action_arguments_never_reach_confirmation(self):
        registry = self.registry()
        bad = [
            ("archive_email", {}),
            ("archive_email", {"message_id": "../m1"}),
            ("archive_email", {"message_id": "m1", "label": "x"}),
            ("add_label", {"message_id": "m1"}),
            ("trash_email", {"message_id": "missing9"}),
            ("star_email", {"message_id": "m1", "permanently": True}),
        ]
        for name, arguments in bad:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolError):
                registry.prepare(name, arguments, "do it")
        self.assertEqual(self.gmail.writes(), [])

    def runtime(self, model):
        database = Path(self.temp_dir.name) / "runtime.sqlite3"
        storage.initialize_database(database)
        return AssistantRuntime("http://local", "m", database_path=database,
                                tool_registry=ToolRegistry(database, google=self.connected()), model_client=model)

    @staticmethod
    def call(name, arguments, call_id="c1"):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}

    def test_cancelling_a_modification_makes_no_api_call(self):
        model = Mock(side_effect=[self.call("trash_email", {"message_id": "m1"})])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Trash Ada's email. Yes, I confirm.").pending_confirmation
        self.assertEqual(pending["preview"]["action"], "trash_email")
        self.assertEqual(self.gmail.writes(), [])
        result = runtime.resolve_confirmation(pending["confirmation_id"], approved=False)
        self.assertIn("Cancelled", result.response)
        self.assertEqual(self.gmail.writes(), [])
        with self.assertRaises(RuntimeBusyError):
            runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(self.gmail.writes(), [])
        self.assertIn("INBOX", self.gmail.messages["m1"]["labelIds"])

    def test_approving_applies_exactly_the_confirmed_action_once(self):
        model = Mock(side_effect=[self.call("archive_email", {"message_id": "m3"}),
                                  {"role": "assistant", "content": "Archived."}])
        runtime = self.runtime(model)
        pending = runtime.run_turn("Archive the maintenance email").pending_confirmation
        result = runtime.resolve_confirmation(pending["confirmation_id"], approved=True)
        self.assertEqual(result.response, "Archived.")
        writes = self.gmail.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "/gmail/v1/users/me/messages/m3/modify")
        self.assertEqual(writes[0]["body"], {"addLabelIds": [], "removeLabelIds": ["INBOX"]})

    def test_the_model_can_search_read_and_answer_without_any_confirmation(self):
        model = Mock(side_effect=[
            self.call("search_gmail", {"query": "from:ada@example.org"}),
            self.call("read_gmail_message", {"message_id": "m1"}, call_id="c2"),
            {"role": "assistant", "content": "Ada shared the quarterly numbers."},
        ])
        runtime = self.runtime(model)
        result = runtime.run_turn("What did Ada send me?")
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual(result.response, "Ada shared the quarterly numbers.")
        tool_results = [json.loads(m["content"]) for m in runtime.history if m["role"] == "tool"]
        self.assertTrue(all(r["ok"] for r in tool_results))
        self.assertIn(SECRET_BODY, tool_results[1]["data"]["message"]["body"])
        self.assertEqual(self.gmail.writes(), [])


class ErrorAndSafetyTests(Stage3TestCase):
    def test_api_errors_are_normalised_for_reads_and_actions(self):
        cases = [
            (api_failure(403, "insufficientPermissions"), "not_granted"),
            (api_failure(403, "domainPolicy"), "policy"),
            (api_failure(404, "notFound"), "not_found"),
            (api_failure(429, "rateLimitExceeded"), "rate_limited"),
            (api_failure(500, "backendError"), "unavailable"),
            (urllib.error.URLError("offline"), "network"),
        ]
        for failure, kind in cases:
            self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/m1", failure)
            with self.subTest(kind=kind), self.assertRaises(GoogleError) as raised:
                self.connected().read_message("m1")
            self.assertEqual(raised.exception.kind, kind)
            self.assertNotIn("PRIVATE TEXT", raised.exception.message)

    def test_the_metadata_scope_conflict_is_recognised(self):
        conflict = urllib.error.HTTPError(
            "https://api", 403, "forbidden", {},
            __import__("io").BytesIO(json.dumps({"error": {"code": 403, "message": "Metadata scope doesn't allow format FULL",
                                                           "errors": [{"reason": "forbidden"}]}}).encode()))
        self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/m1", conflict)
        with self.assertRaises(GoogleError) as raised:
            self.connected().read_message("m1")
        self.assertEqual(raised.exception.kind, "metadata_scope")
        self.assertNotIn("FULL", raised.exception.message)

    def test_an_expired_token_is_refreshed_once_for_actions(self):
        provider = self.connected()
        prepared = provider.prepare_action("star_email", "m3")
        failures = [api_failure(401, "authError")]
        real = self.gmail

        def flaky(request):
            if request.get_method() == "POST" and failures:
                return failures.pop(0)
            return real(request)

        self.google.on("POST", google_workspace.GMAIL_API, flaky)
        provider.apply_action(prepared)
        self.assertEqual(len(self.google.calls(google_workspace.TOKEN_URL)), 2)
        self.assertEqual(len(real.writes()), 1)

    def test_modifications_need_the_modify_permission(self):
        no_modify = self.connected(scopes=ALL_SCOPES.replace(google_workspace.SCOPE_GMAIL_MODIFY, ""))
        with self.assertRaises(GoogleError) as raised:
            no_modify.prepare_action("archive_email", "m1")
        self.assertEqual(raised.exception.kind, "not_granted")
        with self.assertRaises(GoogleError):
            no_modify.apply_action({"action": "archive_email", "message_id": "m1"})
        self.assertEqual(self.gmail.writes(), [])

    def test_no_permanent_deletion_batch_or_generic_gmail_operation_exists(self):
        source = inspect.getsource(google_workspace).replace("messages.delete / batchDelete", "")
        for forbidden in ("batchDelete", "batchModify", "messages.delete", "/import", "/insert"):
            self.assertNotIn(forbidden, source, forbidden)
        # The only DELETE request is Calendar event cancellation (stage 4), never Gmail.
        delete_lines = [line for line in source.splitlines() if '"DELETE"' in line]
        self.assertTrue(delete_lines)
        self.assertTrue(all("CALENDAR_API" in line for line in delete_lines), delete_lines)
        provider = self.connected()
        for name in ("delete_message", "delete", "send_email", "send_draft", "request", "call_api", "raw_request"):
            self.assertFalse(hasattr(provider, name), name)
        for operation in ("delete", "send_email", "batchDelete"):
            with self.assertRaises(GoogleError):
                provider.perform(operation)
        self.assertNotIn("https://mail.google.com/", google_workspace.requested_scopes())
        names = {d["function"]["name"] for d in self.registry().definitions()}
        gmail_tools = {n for n in names if "gmail" in n or "email" in n or n in ("add_label", "remove_label")}
        self.assertEqual(gmail_tools, {"create_gmail_draft", "update_gmail_draft", "search_gmail", "read_gmail_message",
                                       "list_gmail_drafts", *google_workspace.GMAIL_ACTIONS,
                                       *google_workspace.SEND_TOOLS})
        self.assertFalse(any("delete" in n and ("gmail" in n or "email" in n) for n in names))
        for action in set(google_workspace.GMAIL_ACTIONS) - google_workspace.LOW_IMPACT_GMAIL_ACTIONS:
            self.assertIn(action, google_workspace.CONSEQUENTIAL_OPERATIONS)
        self.assertEqual(google_workspace.LOW_IMPACT_GMAIL_ACTIONS,
                         {"mark_email_read", "mark_email_unread", "star_email", "unstar_email"})

    def test_email_content_is_never_logged(self):
        registry = self.registry()
        handler_records = []

        class Capture(logging.Handler):
            def emit(self, record):
                handler_records.append(record.getMessage())

        capture = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            self.run_tool(registry, "read_gmail_message", {"message_id": "m1", "include_thread": True})
            self.run_tool(registry, "search_gmail", {"query": "numbers"})
            self.google.on("GET", f"{google_workspace.GMAIL_API}/messages/m1", urllib.error.URLError(SECRET_BODY))
            outcome = self.run_tool(registry, "read_gmail_message", {"message_id": "m1"})
            self.assertFalse(outcome.ok)
            self.assertNotIn(SECRET_BODY, outcome.message)
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)
        for message in handler_records:
            self.assertNotIn(SECRET_BODY, message)
            self.assertNotIn("Quarterly numbers", message)


if __name__ == "__main__":
    import unittest
    unittest.main()
