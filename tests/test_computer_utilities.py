import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_runtime
import computer_utilities
import config
import web_app
from computer_utilities import UtilitySettings, build_utility_settings
from tool_registry import ToolError, ToolRegistry


def assistant_tool_call(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
    }


class UtilityTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name).resolve() / "home"
        self.documents = self.home / "Documents"
        self.documents.mkdir(parents=True)
        self.database_path = Path(self.temp_dir.name) / "utilities.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, relative, content="x"):
        path = self.documents / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def settings(self, apps=("Calendar", "Notes")):
        return UtilitySettings({"Documents": self.documents}, tuple(apps))


class UtilitySettingsTests(UtilityTestCase):
    def test_config_loads_approved_folders_and_apps(self):
        config_path = Path(self.temp_dir.name) / "config.ini"
        config_path.write_text(
            "[utilities]\nallowed_apps = Calendar, Notes, calendar, , Bad/App\n\n"
            f"[utility_folders]\nDocuments = {self.documents}\n",
            encoding="utf-8",
        )
        settings = config.load_utility_settings(config_path, home=self.home)
        self.assertEqual(settings.folders, {"Documents": self.documents})
        self.assertEqual(settings.apps, ("Calendar", "Notes"))

    def test_missing_config_or_sections_disable_utilities(self):
        self.assertEqual(
            config.load_utility_settings(Path(self.temp_dir.name) / "absent.ini"), UtilitySettings()
        )
        config_path = Path(self.temp_dir.name) / "config.ini"
        config_path.write_text("[ollama]\nendpoint = x\nmodel = y\n", encoding="utf-8")
        self.assertEqual(config.load_utility_settings(config_path, home=self.home), UtilitySettings())

    def test_unsafe_folders_are_rejected(self):
        (self.home / "Library" / "Mail").mkdir(parents=True)
        (self.home / ".ssh").mkdir()
        outside = Path(self.temp_dir.name).resolve() / "outside"
        outside.mkdir()
        (self.home / "LinkOut").symlink_to(outside, target_is_directory=True)
        self.write("note.txt")
        folders = {
            "Home": str(self.home),
            "Root": "/",
            "Mail": str(self.home / "Library" / "Mail"),
            "Keys": str(self.home / ".ssh"),
            "Outside": str(outside),
            "LinkOut": str(self.home / "LinkOut"),
            "Missing": str(self.home / "Missing"),
            "File": str(self.documents / "note.txt"),
            "Relative": "Documents",
            "Bad/Label": str(self.documents),
            "Documents": str(self.documents),
            "documents": str(self.documents),
        }
        with self.assertLogs("computer_utilities", level="WARNING"):
            settings = build_utility_settings(folders, [], home=self.home)
        self.assertEqual(settings.folders, {"Documents": self.documents})

    def test_tools_are_only_offered_when_configured(self):
        names = lambda registry: {d["function"]["name"] for d in registry.definitions()}
        self.assertFalse({"find_files", "open_application"} & names(ToolRegistry(self.database_path)))
        only_apps = ToolRegistry(self.database_path, utilities=UtilitySettings({}, ("Notes",)))
        self.assertIn("open_application", names(only_apps))
        self.assertNotIn("find_files", names(only_apps))
        both = ToolRegistry(self.database_path, utilities=self.settings())
        self.assertTrue({"find_files", "open_application"} <= names(both))


class FindFilesTests(UtilityTestCase):
    def test_finds_file_names_case_insensitively_with_relative_paths(self):
        self.write("Reports/Q3 Budget.pdf", "contents mention nothing")
        self.write("budget-notes.txt")
        self.write("unrelated.txt", "budget appears only in contents")
        registry = ToolRegistry(self.database_path, utilities=self.settings())

        prepared = registry.prepare("find_files", {"query": "BUDGET"}, "Find my budget files")
        self.assertFalse(prepared.always_confirm)
        outcome = registry.execute(prepared)

        self.assertTrue(outcome.ok)
        paths = [item["path"] for item in outcome.data["results"]]
        self.assertEqual(paths, ["Documents/budget-notes.txt", "Documents/Reports/Q3 Budget.pdf"])
        self.assertFalse(outcome.data["truncated"])
        for item in outcome.data["results"]:
            self.assertNotIn(str(self.home), item["path"])
            self.assertIn("size_bytes", item)
            self.assertIn("modified", item)

    def test_skips_hidden_symlinked_package_and_too_deep_entries(self):
        outside = Path(self.temp_dir.name).resolve() / "outside"
        outside.mkdir()
        (outside / "secret-plan.txt").write_text("x", encoding="utf-8")
        self.write(".hidden-plan.txt")
        self.write(".git/plan.txt")
        self.write("Tool.app/Contents/plan.txt")
        (self.documents / "linked-plan.txt").symlink_to(outside / "secret-plan.txt")
        (self.documents / "LinkedDir").symlink_to(outside, target_is_directory=True)
        self.write("a/b/c/plan-depth3.txt")
        self.write("a/b/c/d/plan-depth4.txt")
        flagged = self.write("flagged-plan.txt")
        if hasattr(os, "chflags") and hasattr(stat, "UF_HIDDEN"):
            os.chflags(flagged, stat.UF_HIDDEN)

        found = computer_utilities.find_files(self.settings(), "plan")

        paths = [item["path"] for item in found["results"]]
        expected = ["Documents/a/b/c/plan-depth3.txt"]
        if not (hasattr(os, "chflags") and hasattr(stat, "UF_HIDDEN")):
            expected.insert(0, "Documents/flagged-plan.txt")
        self.assertEqual(paths, expected)

    def test_results_and_scanning_are_bounded(self):
        for index in range(computer_utilities.MAX_FILE_RESULTS + 5):
            self.write(f"match-{index:02}.txt")
        found = computer_utilities.find_files(self.settings(), "match")
        self.assertEqual(len(found["results"]), computer_utilities.MAX_FILE_RESULTS)
        self.assertTrue(found["truncated"])

        with patch.object(computer_utilities, "MAX_SCANNED_ENTRIES", 3):
            limited = computer_utilities.find_files(self.settings(), "match")
        self.assertLessEqual(len(limited["results"]), 3)
        self.assertTrue(limited["truncated"])

    def test_rejects_unknown_folders_and_bad_queries(self):
        registry = ToolRegistry(self.database_path, utilities=self.settings())
        for arguments in (
            {"query": "x", "folder": "Downloads"},
            {"query": "x", "folder": "/Users"},
            {"query": "x", "folder": "../Documents"},
            {"query": ""},
            {"query": "x" * 101},
            {"query": "x", "path": "/etc"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                registry.prepare("find_files", arguments, "Find files")
        whitespace = registry.execute(registry.prepare("find_files", {"query": "   "}, "Find files"))
        self.assertFalse(whitespace.ok)


class KeywordSearchTests(UtilityTestCase):
    def paths(self, query, **kwargs):
        found = computer_utilities.find_files(self.settings(), query, **kwargs)
        return [item["path"] for item in found["results"]]

    def test_keywords_ignore_case_spaces_underscores_and_camel_case(self):
        for name in (
            "PerformanceSheet.pdf",
            "performanceSheetDraft.pdf",
            "performance_sheet.pdf",
            "Performance Sheet.pdf",
            "performance-sheet.PDF",
            "Q3.Performance.Sheet.v2.xlsx",
        ):
            self.write(f"Nested/{name}")
        self.write("Performance Review.pdf")
        self.write("Sheet Music.pdf")
        expected_all = sorted(
            f"Documents/Nested/{name}"
            for name in (
                "PerformanceSheet.pdf", "performanceSheetDraft.pdf", "performance_sheet.pdf",
                "Performance Sheet.pdf", "performance-sheet.PDF", "Q3.Performance.Sheet.v2.xlsx",
            )
        )
        for query in (
            "performance sheet", "Performance Sheet", "PerformanceSheet", "performance_sheet",
            "sheet performance", "my performance sheet file",
        ):
            with self.subTest(query=query):
                self.assertEqual(sorted(self.paths(query)), expected_all)

    def test_partial_words_extensions_and_type_filter(self):
        self.write("PerformanceSheet.pdf")
        self.write("PerformanceSheet.docx")
        self.write("perfume list.txt")
        self.assertEqual(
            sorted(self.paths("perf sheet")),
            ["Documents/PerformanceSheet.docx", "Documents/PerformanceSheet.pdf"],
        )
        self.assertEqual(self.paths("performance sheet pdf"), ["Documents/PerformanceSheet.pdf"])
        self.assertEqual(self.paths("performancesheet.pdf"), ["Documents/PerformanceSheet.pdf"])
        for file_type in ("pdf", ".pdf", "PDF"):
            with self.subTest(file_type=file_type):
                self.assertEqual(
                    self.paths("performance", file_type=file_type), ["Documents/PerformanceSheet.pdf"]
                )
        self.assertEqual(self.paths("perf", file_type="txt"), ["Documents/perfume list.txt"])
        self.assertEqual(self.paths("performance review"), [])
        for bad in ("../pdf", "p d f", "verylongextension", ""):
            with self.subTest(bad_type=bad), self.assertRaises(computer_utilities.UtilityError):
                computer_utilities.find_files(self.settings(), "x", file_type=bad)

    def test_tool_accepts_file_type_and_reports_terms_and_folders(self):
        self.write("Reports/PerformanceSheet.pdf")
        registry = ToolRegistry(self.database_path, utilities=self.settings())
        prepared = registry.prepare(
            "find_files", {"query": "performance sheet", "file_type": "pdf"}, "Find my performance sheet pdf"
        )
        self.assertEqual(
            prepared.summary, "Search approved folders for pdf file names matching 'performance sheet'"
        )
        outcome = registry.execute(prepared)
        self.assertEqual(
            outcome.message,
            "Searched Documents (including subfolders) for .pdf file names containing all of: "
            "performance, sheet. Found 1 file(s).",
        )
        self.assertEqual(outcome.data["query_terms"], ["performance", "sheet"])
        self.assertEqual(outcome.data["results"][0]["path"], "Documents/Reports/PerformanceSheet.pdf")
        with self.assertRaises(ToolError):
            registry.prepare("find_files", {"query": "x", "file_type": "p/df"}, "Find")

        empty = registry.execute(registry.prepare("find_files", {"query": "budget"}, "Find budget"))
        self.assertTrue(empty.ok)
        self.assertEqual(empty.data["results"], [])
        self.assertIn("Found 0 file(s).", empty.message)
        self.assertNotIn("limit", empty.message)


class TypeWordRetryTests(UtilityTestCase):
    def setUp(self):
        super().setUp()
        for name in ("Finance/Budget 2025.xlsx", "Finance/Budget 2026.xlsx", "Finance/Budget notes.txt",
                     "Invoice April.pdf", "Spreadsheet Tips.pdf"):
            self.write(name)

    def find(self, query, **kwargs):
        return computer_utilities.find_files(self.settings(), query, **kwargs)

    def test_type_words_become_a_file_type_filter_after_zero_matches(self):
        found = self.find("budget spreadsheets")
        self.assertEqual(
            sorted(item["path"] for item in found["results"]),
            ["Documents/Finance/Budget 2025.xlsx", "Documents/Finance/Budget 2026.xlsx"],
        )
        self.assertEqual(found["query_terms"], ["budget", "spreadsheets"])
        self.assertEqual(found["retry"]["query_terms"], ["budget"])
        self.assertEqual(found["retry"]["type_words"], ["spreadsheets"])
        self.assertIn(".xlsx", found["retry"]["file_types"])

        registry = ToolRegistry(self.database_path, utilities=self.settings())
        outcome = registry.execute(registry.prepare("find_files", {"query": "budget spreadsheets"}, "Find it"))
        self.assertIn("None matched, so it searched again treating spreadsheets as a file type: budget in", outcome.message)
        self.assertTrue(outcome.message.endswith("Found 2 file(s)."))

    def test_no_retry_when_ordinary_matching_already_finds_files(self):
        for query, expected in (
            ("spreadsheet tips", ["Documents/Spreadsheet Tips.pdf"]),  # type word that is in the name
            ("invoice pdf", ["Documents/Invoice April.pdf"]),            # extension matches by name
            ("budget", None),
        ):
            with self.subTest(query=query):
                found = self.find(query)
                self.assertIsNone(found["retry"])
                if expected:
                    self.assertEqual([item["path"] for item in found["results"]], expected)
        self.assertEqual(len(self.find("budget")["results"]), 3)

        registry = ToolRegistry(self.database_path, utilities=self.settings())
        message = registry.execute(registry.prepare("find_files", {"query": "budget"}, "Find it")).message
        self.assertNotIn("searched again", message)

    def test_no_retry_for_explicit_type_single_words_or_only_type_words(self):
        for query, kwargs in (
            ("budget spreadsheets", {"file_type": "xlsx"}),  # caller chose the type explicitly
            ("spreadsheets", {}),                            # nothing left to search names for
            ("photos documents", {}),                         # only type words
            ("missing spreadsheets", {}),                     # retried, still nothing
        ):
            with self.subTest(query=query, **kwargs):
                found = self.find(query, **kwargs)
                self.assertEqual(found["results"], [])
                if query == "missing spreadsheets":
                    self.assertIsNotNone(found["retry"])
                else:
                    self.assertIsNone(found["retry"])

    def test_retry_shares_the_scan_limit_and_stays_in_approved_roots(self):
        outside = Path(self.temp_dir.name).resolve() / "outside"
        outside.mkdir()
        (outside / "Budget secret.xlsx").write_text("x", encoding="utf-8")
        (self.documents / "Linked").symlink_to(outside, target_is_directory=True)
        found = self.find("budget spreadsheets")
        self.assertNotIn("secret", " ".join(item["path"] for item in found["results"]))

        for index in range(30):
            self.write(f"filler-{index:02}.txt")
        total_entries = sum(1 for _ in self.documents.rglob("*"))
        with patch.object(computer_utilities, "MAX_SCANNED_ENTRIES", total_entries + 5):
            limited = self.find("budget spreadsheets")
        self.assertLessEqual(limited["entries_examined"], total_entries + 5)
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["incomplete_folders"], ["Documents"])

        with patch.object(computer_utilities, "MAX_SCANNED_ENTRIES", 5):
            exhausted = self.find("budget spreadsheets")
        self.assertIsNone(exhausted["retry"])  # no budget left, so no second walk
        self.assertEqual(exhausted["entries_examined"], 5)


class SearchScopeTests(UtilityTestCase):
    def setUp(self):
        super().setUp()
        self.desktop = self.home / "Desktop"
        (self.desktop / "Dakshesh" / "Pictures").mkdir(parents=True)
        (self.desktop / "Dakshesh" / "Pictures" / "PerformanceSheet.png").write_text("x", encoding="utf-8")
        self.outside = Path(self.temp_dir.name).resolve() / "outside"
        self.outside.mkdir()
        (self.outside / "PerformanceSheet.pdf").write_text("x", encoding="utf-8")
        (self.desktop / "Escape").symlink_to(self.outside, target_is_directory=True)
        self.scoped = UtilitySettings({"Desktop": self.desktop, "Documents": self.documents}, ())

    def test_subfolders_are_found_only_through_their_approved_root(self):
        found = computer_utilities.find_files(self.scoped, "performance sheet")
        self.assertEqual(
            [item["path"] for item in found["results"]],
            ["Desktop/Dakshesh/Pictures/PerformanceSheet.png"],
        )
        for item in found["results"]:
            label, relative = item["path"].split("/", 1)
            resolved = (self.scoped.folders[label] / relative).resolve()
            self.assertTrue(resolved.is_relative_to(self.scoped.folders[label]))

    def test_subfolder_or_outside_folder_as_root_is_refused_with_explanation(self):
        registry = ToolRegistry(self.database_path, utilities=self.scoped)
        for folder in ("Dakshesh", "Desktop/Dakshesh", "Escape", str(self.outside), "../outside", "Pictures"):
            with self.subTest(folder=folder):
                with self.assertRaises(ToolError) as raised:
                    registry.prepare("find_files", {"query": "performance", "folder": folder}, "Find it")
                message = str(raised.exception)
                self.assertIn("is not an approved root folder", message)
                self.assertIn("Approved roots: Desktop, Documents", message)
                self.assertIn("Subfolders inside an approved root are searched automatically", message)
        root = registry.prepare("find_files", {"query": "performance", "folder": "desktop"}, "Find it")
        self.assertEqual(root.arguments["folder"], "Desktop")
        self.assertEqual(
            [item["path"] for item in registry.execute(root).data["results"]],
            ["Desktop/Dakshesh/Pictures/PerformanceSheet.png"],
        )

    def test_entry_budget_is_shared_fairly_and_incomplete_roots_are_named(self):
        for index in range(40):
            (self.documents / f"filler-{index:02}.txt").write_text("x", encoding="utf-8")
        (self.desktop / "Report.pdf").write_text("x", encoding="utf-8")
        registry = ToolRegistry(self.database_path, utilities=self.scoped)
        with patch.object(computer_utilities, "MAX_SCANNED_ENTRIES", 10):
            outcome = registry.execute(registry.prepare("find_files", {"query": "report"}, "Find report"))
        self.assertEqual([item["path"] for item in outcome.data["results"]], ["Desktop/Report.pdf"])
        self.assertLessEqual(outcome.data["entries_examined"], 10)
        self.assertTrue(outcome.data["truncated"])
        self.assertIn("Documents", outcome.data["incomplete_folders"])
        self.assertIn("search limit was reached before finishing: ", outcome.message)

        # Documents comes first and its single directory alone exceeds the budget, yet
        # Desktop still gets its turn because roots alternate every SEARCH_CHUNK entries.
        reordered = UtilitySettings({"Documents": self.documents, "Desktop": self.desktop}, ())
        with (
            patch.object(computer_utilities, "MAX_SCANNED_ENTRIES", 10),
            patch.object(computer_utilities, "SEARCH_CHUNK", 3),
        ):
            found = computer_utilities.find_files(reordered, "report")
        self.assertEqual([item["path"] for item in found["results"]], ["Desktop/Report.pdf"])
        self.assertEqual(found["entries_examined"], 10)
        self.assertEqual(found["incomplete_folders"], ["Documents", "Desktop"])

    def test_each_search_claim_corresponds_to_a_real_tool_call(self):
        model = Mock(
            side_effect=[
                assistant_tool_call("find_files", {"query": "performance sheet"}),
                {"role": "assistant", "content": "I found one file."},
            ]
        )
        runtime = assistant_runtime.AssistantRuntime(
            "http://local",
            "test-model",
            tool_registry=ToolRegistry(self.database_path, utilities=self.scoped),
            model_client=model,
        )
        result = runtime.run_turn("Find my performance sheet")
        searches = [item for item in result.activities if item["tool"] == "find_files"]
        self.assertEqual(len(searches), 1)
        tool_messages = [m for m in runtime.history if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("Desktop/Dakshesh/Pictures/PerformanceSheet.png", tool_messages[0]["content"])
        self.assertIn("Searched Desktop, Documents (including subfolders)", tool_messages[0]["content"])


class OpenApplicationTests(UtilityTestCase):
    def test_only_approved_apps_open_after_confirmation(self):
        runner = Mock(return_value=True)
        registry = ToolRegistry(self.database_path, utilities=self.settings(), open_runner=runner)

        prepared = registry.prepare("open_application", {"name": "Calendar"}, "Please open Calendar")
        self.assertTrue(prepared.always_confirm)
        self.assertEqual(prepared.summary, "Open application: Calendar")
        with patch.object(computer_utilities, "open_supported", return_value=True):
            outcome = registry.execute(prepared)
        self.assertTrue(outcome.ok)
        runner.assert_called_once_with(["-a", "Calendar"])

        for name in ("Terminal", "calendar", "/System/Applications/Calendar.app", "-a Terminal", ""):
            with self.subTest(name=name), self.assertRaises(ToolError):
                registry.prepare("open_application", {"name": name}, "Open it")
        runner.assert_called_once()

    def test_launch_failures_and_unsupported_platforms_are_reported(self):
        runner = Mock(return_value=False)
        registry = ToolRegistry(self.database_path, utilities=self.settings(), open_runner=runner)
        prepared = registry.prepare("open_application", {"name": "Notes"}, "Open Notes")
        with patch.object(computer_utilities, "open_supported", return_value=True):
            failed = registry.execute(prepared)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.message, "macOS could not open Notes.")

        runner.reset_mock()
        with patch.object(computer_utilities, "open_supported", return_value=False):
            unsupported = registry.execute(prepared)
        self.assertFalse(unsupported.ok)
        runner.assert_not_called()

    def test_run_open_uses_fixed_executable_without_shell(self):
        with patch.object(computer_utilities.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(computer_utilities.run_open(["-a", "Notes"]))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/open", "-a", "Notes"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], computer_utilities.OPEN_TIMEOUT_SECONDS)
        with patch.object(
            computer_utilities.subprocess, "run",
            side_effect=computer_utilities.subprocess.TimeoutExpired("open", 10),
        ):
            self.assertFalse(computer_utilities.run_open(["-a", "Notes"]))

    def test_web_open_application_waits_for_confirmation(self):
        runner = Mock(return_value=True)
        model = Mock(
            side_effect=[
                assistant_tool_call("open_application", {"name": "Calendar"}),
                {"role": "assistant", "content": "Calendar was opened."},
                assistant_tool_call("open_application", {"name": "Calendar"}, call_id="call-2"),
                {"role": "assistant", "content": "Cancelled."},
            ]
        )
        client = web_app.create_app(self.database_path, utility_settings=self.settings()).test_client()
        with (
            patch.object(assistant_runtime, "ask_ollama", model),
            patch.object(computer_utilities, "run_open", runner),
            patch.object(computer_utilities, "open_supported", return_value=True),
        ):
            conversation_id = client.post("/api/conversations", json={}).get_json()["conversation_id"]
            for approved in (True, False):
                chat = client.post(
                    "/api/chat", json={"conversation_id": conversation_id, "message": "Open Calendar"}
                )
                pending = chat.get_json()["pending_confirmation"]
                self.assertEqual(pending["summary"], "Open application: Calendar")
                client.post(
                    "/api/confirm",
                    json={
                        "conversation_id": conversation_id,
                        "confirmation_id": pending["confirmation_id"],
                        "approved": approved,
                    },
                )
        runner.assert_called_once_with(["-a", "Calendar"])


class OpenFileTests(UtilityTestCase):
    def registry(self, runner):
        return ToolRegistry(self.database_path, utilities=self.settings(), open_runner=runner)

    def test_found_document_opens_by_absolute_path_after_confirmation(self):
        report = self.write("Reports/Q3 Budget.pdf")
        runner = Mock(return_value=True)
        registry = self.registry(runner)
        found = registry.execute(registry.prepare("find_files", {"query": "budget"}, "Find budget"))
        path = found.data["results"][0]["path"]
        self.assertEqual(path, "Documents/Reports/Q3 Budget.pdf")

        prepared = registry.prepare("open_file", {"path": path}, "Open the budget report")
        self.assertTrue(prepared.always_confirm)
        self.assertEqual(prepared.summary, "Open file: Documents/Reports/Q3 Budget.pdf")
        with patch.object(computer_utilities, "open_supported", return_value=True):
            outcome = registry.execute(prepared)

        self.assertTrue(outcome.ok)
        runner.assert_called_once_with([str(report)])
        self.assertTrue(runner.call_args.args[0][0].startswith("/"))
        self.assertEqual(outcome.data, {"path": "Documents/Reports/Q3 Budget.pdf"})

    def test_paths_outside_or_escaping_the_approved_folder_are_refused(self):
        self.write("Reports/ok.pdf")
        outside = Path(self.temp_dir.name).resolve() / "outside"
        outside.mkdir()
        (outside / "secret.pdf").write_text("x", encoding="utf-8")
        (self.home / "private.pdf").write_text("x", encoding="utf-8")
        (self.documents / "LinkedDir").symlink_to(outside, target_is_directory=True)
        (self.documents / "linked.pdf").symlink_to(outside / "secret.pdf")
        (self.documents / "Reports" / "inner-link.pdf").symlink_to(self.documents / "Reports" / "ok.pdf")
        runner = Mock(return_value=True)
        registry = self.registry(runner)
        for path in (
            "Documents/../private.pdf",
            "Documents/Reports/../../private.pdf",
            "Documents/./Reports/ok.pdf",
            "Documents//Reports/ok.pdf",
            "Documents/Reports/",
            "Documents",
            "Documents/",
            str(self.documents / "Reports" / "ok.pdf"),
            "/etc/hosts",
            "~/Documents/Reports/ok.pdf",
            "Downloads/Reports/ok.pdf",
            "Documents\\Reports\\ok.pdf",
            "Documents/Reports/ok.pdf\0.txt",
            "Documents/LinkedDir/secret.pdf",
            "Documents/linked.pdf",
            "Documents/Reports/inner-link.pdf",
            "Documents/Reports/missing.pdf",
            "Documents/" + "a/" * 600 + "x.pdf",
        ):
            with self.subTest(path=path), self.assertRaises(ToolError):
                registry.prepare("open_file", {"path": path}, "Open it")
        for path in ("Documents/../private.pdf", "Documents/./Reports/ok.pdf", "Documents//Reports/ok.pdf"):
            with self.subTest(component_check=path), self.assertRaisesRegex(ToolError, "'..' components"):
                registry.prepare("open_file", {"path": path}, "Open it")
        runner.assert_not_called()

    def test_hidden_package_directory_and_disallowed_types_are_refused(self):
        self.write(".secret.pdf")
        self.write(".config/notes.txt")
        self.write("Tool.app/Contents/readme.txt")
        (self.documents / "Folder.pdf").mkdir()
        flagged = self.write("flagged.pdf")
        for name in (
            "run.command", "install.sh", "Setup.pkg", "image.dmg", "macro.docm", "script.py",
            "page.html", "noextension", "archive.zip", "shortcut.webloc", "report.PDF.command",
        ):
            self.write(name)
        registry = self.registry(Mock(return_value=True))
        refused = [
            ".secret.pdf", ".config/notes.txt", "Tool.app/Contents/readme.txt", "Folder.pdf",
            "run.command", "install.sh", "Setup.pkg", "image.dmg", "macro.docm", "script.py",
            "page.html", "noextension", "archive.zip", "shortcut.webloc", "report.PDF.command",
        ]
        if hasattr(os, "chflags") and hasattr(stat, "UF_HIDDEN"):
            os.chflags(flagged, stat.UF_HIDDEN)
            refused.append("flagged.pdf")
        for name in refused:
            with self.subTest(name=name), self.assertRaises(ToolError):
                registry.prepare("open_file", {"path": f"Documents/{name}"}, "Open it")

        for name in ("notes.txt", "data.csv", "Slides.PPTX", "photo.jpeg", "readme.md"):
            self.write(name)
            with self.subTest(allowed=name):
                prepared = registry.prepare("open_file", {"path": f"Documents/{name}"}, "Open it")
                self.assertEqual(prepared.arguments["path"], f"Documents/{name}")

    def test_file_is_rechecked_at_execution_after_confirmation(self):
        target = self.write("plan.pdf")
        outside = Path(self.temp_dir.name).resolve() / "outside.pdf"
        outside.write_text("x", encoding="utf-8")
        runner = Mock(return_value=True)
        registry = self.registry(runner)
        prepared = registry.prepare("open_file", {"path": "Documents/plan.pdf"}, "Open plan")

        target.unlink()
        target.symlink_to(outside)
        with patch.object(computer_utilities, "open_supported", return_value=True):
            swapped = registry.execute(prepared)
        self.assertFalse(swapped.ok)
        self.assertEqual(swapped.message, "Symbolic links cannot be opened.")

        target.unlink()
        with patch.object(computer_utilities, "open_supported", return_value=True):
            removed = registry.execute(prepared)
        self.assertFalse(removed.ok)
        runner.assert_not_called()

    def test_failures_and_unsupported_platforms_are_reported(self):
        self.write("plan.pdf")
        runner = Mock(return_value=False)
        registry = self.registry(runner)
        prepared = registry.prepare("open_file", {"path": "Documents/plan.pdf"}, "Open plan")
        with patch.object(computer_utilities, "open_supported", return_value=True):
            failed = registry.execute(prepared)
        self.assertEqual(failed.message, "macOS could not open Documents/plan.pdf.")
        runner.reset_mock()
        with patch.object(computer_utilities, "open_supported", return_value=False):
            self.assertFalse(registry.execute(prepared).ok)
        runner.assert_not_called()

    def test_open_file_is_only_offered_with_approved_folders(self):
        names = lambda registry: {d["function"]["name"] for d in registry.definitions()}
        self.assertNotIn("open_file", names(ToolRegistry(self.database_path)))
        apps_only = ToolRegistry(self.database_path, utilities=UtilitySettings({}, ("Notes",)))
        self.assertNotIn("open_file", names(apps_only))
        self.assertIn("open_file", names(self.registry(Mock())))

    def test_web_open_file_waits_for_confirmation(self):
        report = self.write("plan.pdf")
        runner = Mock(return_value=True)
        model = Mock(
            side_effect=[
                assistant_tool_call("open_file", {"path": "Documents/plan.pdf"}),
                {"role": "assistant", "content": "Opened."},
            ]
        )
        client = web_app.create_app(self.database_path, utility_settings=self.settings()).test_client()
        with (
            patch.object(assistant_runtime, "ask_ollama", model),
            patch.object(computer_utilities, "run_open", runner),
            patch.object(computer_utilities, "open_supported", return_value=True),
        ):
            conversation_id = client.post("/api/conversations", json={}).get_json()["conversation_id"]
            chat = client.post(
                "/api/chat", json={"conversation_id": conversation_id, "message": "Open my plan"}
            )
            pending = chat.get_json()["pending_confirmation"]
            self.assertEqual(pending["summary"], "Open file: Documents/plan.pdf")
            runner.assert_not_called()
            client.post(
                "/api/confirm",
                json={
                    "conversation_id": conversation_id,
                    "confirmation_id": pending["confirmation_id"],
                    "approved": True,
                },
            )
        runner.assert_called_once_with([str(report)])


if __name__ == "__main__":
    unittest.main()
