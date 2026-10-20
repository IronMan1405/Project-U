"""Folder search, opening files and folders (default app, a named app, the editor), and Apple
Music library playback.

Temporary folders and fakes only: nothing is opened and no AppleScript runs, except
LiveMusicTests (LIVE_MUSIC=1), which searches the real Music library and, with
LIVE_MUSIC_PLAY="<song name>", plays that one song for a moment and pauses it again.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import computer_utilities
import mac_control
import tool_registry
from computer_utilities import UtilityError, UtilitySettings
from developer import DeveloperCompanion, DeveloperSettings
from mac_control import MacControl, MacControlError
from tool_registry import ToolError, ToolRegistry

VSCODE = Path("/Applications/Visual Studio Code.app")


class Tree(unittest.TestCase):
    """A fake home: Desktop and Documents roots plus a project outside them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        for folder in ("Desktop/robotics", "Desktop/Robotics Club", "Documents/School/Robotics",
                       "Documents/SOLVE/notes", "Desktop/electronics-kits", "Documents/My Electronics",
                       "Project-U/tests", "Project-U/node_modules/robotics", "Project-U/.git/robotics",
                       "Documents/deep/a/b/c/d/e/f/robotics"):
            (self.home / folder).mkdir(parents=True)
        (self.home / "Project-U/config.ini").write_text("[x]\n")
        (self.home / "Documents/Q3 report final.pdf").write_text("x")
        (self.home / "Documents/robotics.txt").write_text("x")  # a file, never a folder result
        outside = self.home / "outside" / "robotics"
        outside.mkdir(parents=True)
        (self.home / "Desktop/link-to-outside").symlink_to(self.home / "outside", target_is_directory=True)
        (self.home / "Documents/loop").symlink_to(self.home / "Documents", target_is_directory=True)
        self.settings = UtilitySettings({"Desktop": self.home / "Desktop", "Documents": self.home / "Documents"},
                                        ("Preview", "Finder", "Terminal"))
        self.developer = DeveloperCompanion(DeveloperSettings({"Project-U": self.home / "Project-U"}, {},
                                                              "Visual Studio Code"))
        self.runner = Mock(return_value=True)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, **extra):
        return ToolRegistry(self.home / "r.sqlite3", utilities=self.settings, developer=self.developer,
                            open_runner=self.runner, **extra)

    def roots(self):
        return self.registry()._search_roots()


class FolderSearchTests(Tree):
    def paths(self, query, **kwargs):
        return [r["path"] for r in computer_utilities.find_folders(self.roots(), query, **kwargs)["results"]]

    def test_exact_and_partial_matches_ignore_case_and_exact_ones_come_first(self):
        found = computer_utilities.find_folders(self.roots(), "robotics")
        paths = [r["path"] for r in found["results"]]
        self.assertEqual(paths[:3], ["Desktop/robotics", "Documents/School/Robotics", "Desktop/Robotics Club"][:2] + paths[2:3])
        self.assertEqual({r["path"] for r in found["results"] if r["exact"]}, {"Desktop/robotics", "Documents/School/Robotics"})
        self.assertIn("Desktop/Robotics Club", paths)
        self.assertTrue(found["ambiguous"])  # two exact matches in different places
        self.assertEqual(self.paths("solve"), ["Documents/SOLVE"])
        self.assertEqual(sorted(self.paths("electronics")), ["Desktop/electronics-kits", "Documents/My Electronics"])
        self.assertFalse(computer_utilities.find_folders(self.roots(), "SOLVE folder")["ambiguous"])

    def test_partial_matches_start_at_word_boundaries(self):
        (self.home / "Documents/Resolve Projects").mkdir()
        (self.home / "Documents/ProjectUBackup").mkdir()
        self.assertEqual(self.paths("solve"), ["Documents/SOLVE"])
        self.assertIn("Documents/ProjectUBackup", self.paths("project u"))
        self.assertTrue(computer_utilities.name_matches_word_starts("my-RoboticsClub_2024", ["robotics", "club"]))
        self.assertFalse(computer_utilities.name_matches_word_starts("Resolve", ["solve"]))

    def test_results_carry_full_locations_to_tell_duplicates_apart(self):
        results = computer_utilities.find_folders(self.roots(), "robotics")["results"]
        locations = {r["path"]: r["location"] for r in results}
        self.assertEqual(locations["Desktop/robotics"], str(self.home / "Desktop/robotics"))

    def test_configured_project_roots_are_found_and_searched(self):
        self.assertEqual(self.paths("Project-U"), ["Project-U"])
        self.assertEqual(self.paths("tests"), ["Project-U/tests"])

    def test_one_root_can_be_searched(self):
        self.assertEqual(sorted(self.paths("electronics", folder="Desktop")), ["Desktop/electronics-kits"])
        with self.assertRaises(UtilityError):
            computer_utilities.find_folders(self.roots(), "x", folder="School")

    def test_symlinks_hidden_dependency_and_deep_folders_are_not_searched_by_default(self):
        paths = self.paths("robotics")
        for unwanted in ("Desktop/link-to-outside/robotics", "Project-U/node_modules/robotics",
                         "Project-U/.git/robotics", "Documents/deep/a/b/c/d/e/f/robotics"):
            self.assertNotIn(unwanted, paths)
        self.assertNotIn("Documents/robotics.txt", paths)
        # Asked for explicitly: hidden and dependency folders are searched, symlinks still never followed.
        everything = self.paths("robotics", include_hidden=True)
        self.assertIn("Project-U/node_modules/robotics", everything)
        self.assertIn("Project-U/.git/robotics", everything)
        self.assertNotIn("Desktop/link-to-outside/robotics", everything)
        self.assertFalse(any("loop" in path for path in self.paths("documents") + self.paths("school")))

    def test_missing_and_unreadable_roots_are_reported(self):
        settings = UtilitySettings({**self.settings.folders, "Gone": self.home / "gone"}, ())
        found = computer_utilities.find_folders(settings, "robotics")
        self.assertEqual(found["unavailable_folders"], [{"folder": "Gone", "reason": "the folder is unavailable"}])
        real_scandir = os.scandir

        def denied(path):
            if Path(path) == self.home / "Desktop":
                raise PermissionError(1, "Operation not permitted")
            return real_scandir(path)

        with patch.object(computer_utilities.os, "scandir", denied):
            found = computer_utilities.find_folders(self.settings, "robotics")
        self.assertIn("Privacy & Security", found["unavailable_folders"][0]["reason"])
        self.assertEqual([r["path"] for r in found["results"]], ["Documents/School/Robotics"])
        outcome = self.registry().execute(self.registry().prepare("find_folders", {"query": "x"}, "find"))
        self.assertTrue(outcome.ok)

    def test_search_strings_are_only_matched_never_interpreted(self):
        for query in ("../outside", "$(rm -rf ~)", "robotics; ls"):
            with self.subTest(query=query):
                results = computer_utilities.find_folders(self.roots(), query)["results"]
                self.assertTrue(all(Path(r["location"].replace("~", str(Path.home()), 1)).is_relative_to(self.home)
                                    or r["location"].startswith(str(self.home)) for r in results))
        with self.assertRaises(UtilityError):
            computer_utilities.find_folders(self.roots(), "!!!")


class OpenTargetTests(Tree):
    def test_project_files_and_folders_open_in_the_editor_with_separate_arguments(self):
        registry = self.registry()
        cases = [("Project-U", "Open Project-U in VS Code", self.home / "Project-U"),
                 ("Project-U/config.ini", "Open config.ini in VS Code", self.home / "Project-U/config.ini"),
                 ("Project-U/tests", "Open the tests folder in VS Code", self.home / "Project-U/tests")]
        for path, message, target in cases:
            with self.subTest(path=path):
                prepared = registry.prepare("open_in_editor", {"path": path}, message)
                self.assertFalse(prepared.always_confirm)
                with patch.object(computer_utilities, "open_supported", return_value=True), \
                        patch.object(computer_utilities, "find_application", return_value=VSCODE):
                    self.assertTrue(registry.execute(prepared).ok)
                self.assertEqual(self.runner.call_args.args[0], ["-a", str(VSCODE), str(target)])

    def test_paths_with_spaces_open_as_one_argument_with_the_default_or_a_named_app(self):
        registry = self.registry()
        found = registry.execute(registry.prepare("find_files", {"query": "q3 report"}, "find my q3 report"))
        path = found.data["results"][0]["path"]
        self.assertEqual(path, "Documents/Q3 report final.pdf")
        with patch.object(computer_utilities, "open_supported", return_value=True), \
                patch.object(computer_utilities, "find_application", return_value=Path("/System/Applications/Preview.app")):
            default = registry.prepare("open_file", {"path": path}, "Open it with its default application")
            self.assertTrue(registry.execute(default).ok)
            self.assertEqual(self.runner.call_args.args[0], [str(self.home / "Documents/Q3 report final.pdf")])
            named = registry.prepare("open_file", {"path": path, "application": "Preview"}, "Open the report in Preview")
            self.assertFalse(named.always_confirm)
            self.assertTrue(registry.execute(named).ok)
        self.assertEqual(self.runner.call_args.args[0],
                         ["-a", "/System/Applications/Preview.app", str(self.home / "Documents/Q3 report final.pdf")])

    def test_a_named_app_that_is_not_installed_is_reported_not_replaced(self):
        registry = self.registry()
        prepared = registry.prepare("open_file", {"path": "Documents/Q3 report final.pdf", "application": "Preview"},
                                    "Open Q3 report final.pdf in Preview")
        with patch.object(computer_utilities, "open_supported", return_value=True), \
                patch.object(computer_utilities, "find_application", return_value=None):
            outcome = registry.execute(prepared)
        self.assertFalse(outcome.ok)
        self.assertIn("Preview is not installed", outcome.message)
        self.runner.assert_not_called()  # no fallback to the default app
        with self.assertRaises(ToolError):  # not an approved app: never invented
            registry.prepare("open_file", {"path": "Documents/Q3 report final.pdf", "application": "Photoshop"}, "x")

    def test_folders_and_risky_apps_are_handled_sensibly(self):
        registry = self.registry()
        self.assertIn("in Finder", registry.prepare("open_file", {"path": "Documents/SOLVE"}, "Open SOLVE").summary)
        with self.assertRaisesRegex(ToolError, "cannot open a folder"):
            registry.prepare("open_file", {"path": "Documents/SOLVE", "application": "Preview"}, "Open SOLVE in Preview")
        with self.assertRaisesRegex(ToolError, "could run them"):
            registry.prepare("open_file", {"path": "Project-U/config.ini", "application": "Terminal"}, "open in Terminal")
        with self.assertRaisesRegex(ToolError, "document types"):  # default app only for documents
            registry.prepare("open_file", {"path": "Project-U/config.ini"}, "Open config.ini")

    def test_missing_targets_escapes_and_symlinks_are_refused(self):
        registry = self.registry()
        for path in ("Project-U/missing.py", "Project-U/../outside", "Desktop/link-to-outside/robotics",
                     "/etc/hosts", "Nowhere/x", "Project-U/node_modules/../config.ini"):
            with self.subTest(path=path), self.assertRaises(ToolError):
                registry.prepare("open_in_editor", {"path": path}, "open it in VS Code")
        self.runner.assert_not_called()

    def test_ambiguous_folder_matches_are_never_opened_silently(self):
        registry = self.registry()
        message = "Find my robotics folder and open it in VS Code"
        found = registry.execute(registry.prepare("find_folders", {"query": "robotics"}, message))
        self.assertIn("ask the user which one", found.message)
        with self.assertRaisesRegex(ToolError, "Several matches"):
            registry.prepare("open_in_editor", {"path": "Desktop/robotics"}, message)
        # The message that names the place tells them apart; so does a later answer.
        registry.execute(registry.prepare("find_folders", {"query": "robotics"}, "open the robotics folder on my Desktop in VS Code"))
        self.assertFalse(registry.prepare("open_in_editor", {"path": "Desktop/robotics"},
                                          "open the robotics folder on my Desktop in VS Code").always_confirm)
        registry.execute(registry.prepare("find_folders", {"query": "robotics"}, message))
        self.assertFalse(registry.prepare("open_in_editor", {"path": "Documents/School/Robotics"},
                                          "The School one").always_confirm)
        # A choice after a search that did not ask to open anything still asks first.
        registry.execute(registry.prepare("find_folders", {"query": "robotics"}, "Find folders named robotics"))
        self.assertTrue(registry.prepare("open_file", {"path": "Documents/School/Robotics"},
                                         "The School one").always_confirm)

    def test_a_single_exact_match_opens_even_with_partial_matches_around(self):
        registry = self.registry()
        message = "Find my SOLVE folder and open it in VS Code"
        registry.execute(registry.prepare("find_folders", {"query": "SOLVE"}, message))
        self.assertFalse(registry.prepare("open_in_editor", {"path": "Documents/SOLVE"}, message).always_confirm)

    def test_unrequested_or_unnamed_opens_still_ask(self):
        registry = self.registry()
        self.assertTrue(registry.prepare("open_in_editor", {"path": "Project-U/config.ini"},
                                         "What's in my project?").always_confirm)
        self.assertTrue(registry.prepare("open_file", {"path": "Documents/Q3 report final.pdf", "application": "Preview"},
                                         "Open the Q3 report final pdf").always_confirm)  # app chosen by the model
        self.assertTrue(registry.prepare("open_file", {"path": "Documents/Q3 report final.pdf"},
                                         "Open something useful").always_confirm)

    def test_routing_tools_are_distinct_and_described_for_their_intent(self):
        definitions = {d["function"]["name"]: d["function"] for d in self.registry().definitions()}
        self.assertIn("folder", definitions["find_folders"]["description"].lower())
        self.assertIn("find_folders", definitions["find_files"]["description"])
        self.assertIn("Read-only", definitions["find_folders"]["description"])
        self.assertEqual(definitions["open_file"]["parameters"]["properties"]["application"]["enum"],
                         ["Preview", "Finder", "Terminal", "Visual Studio Code"])
        self.assertNotIn("open_project", definitions)
        self.assertFalse(self.registry()._definitions["find_folders"].direct_write)

    def test_find_application_checks_real_bundles_and_rejects_odd_names(self):
        self.assertIsNone(computer_utilities.find_application("../../bin/sh"))
        self.assertIsNone(computer_utilities.find_application("No Such App 12345"))
        if Path("/System/Applications/Calculator.app").is_dir():
            self.assertEqual(computer_utilities.find_application("Calculator"), Path("/System/Applications/Calculator.app"))


# --- Apple Music -----------------------------------------------------------------------------

TRACK_A, TRACK_B, LIST = "AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB", "CCCCCCCCCCCCCCCC"


class FakeMusic:
    def __init__(self, library=None, denied=False, plays_something_else=False):
        self.library = library if library is not None else [
            ("track", TRACK_A, "Cornfield Chase", "Hans Zimmer", "Interstellar (Original Motion Picture Soundtrack)"),
            ("track", TRACK_B, "Cornfield Chase", "Hans Zimmer", "Interstellar (Live)"),
            ("playlist", LIST, "Interstellar mix", "", ""),
        ]
        self.denied, self.other = denied, plays_something_else
        self.calls = []

    def __call__(self, script, arguments):
        self.calls.append((script, list(arguments)))
        if self.denied:
            return subprocess.CompletedProcess([], 1, "", "Not authorized to send Apple events to Music. (-1743)")
        if script == mac_control.MUSIC_SEARCH_SCRIPT:
            words = arguments[0].casefold().split()
            hits = ["\x1f".join(item) for item in self.library if all(w in " ".join(item[2:]).casefold() for w in words)]
            return subprocess.CompletedProcess([], 0, "\x1e".join(hits) + "\n", "")
        kind, pid = arguments
        name = next(item[2] for item in self.library if item[1] == pid)
        if self.other:
            return subprocess.CompletedProcess([], 0, f"playing\x1fDDDDDDDDDDDDDDDD\x1fSomething else\x1fX\x1f\n", "")
        track = pid if kind == "track" else TRACK_A
        return subprocess.CompletedProcess([], 0, f"playing\x1f{track}\x1f{name if kind == 'track' else 'Cornfield Chase'}\x1fHans Zimmer\x1f{pid if kind == 'playlist' else ''}\n", "")


class MusicTests(unittest.TestCase):
    def registry(self, fake):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return ToolRegistry(Path(self.temp.name) / "r.sqlite3", mac=MacControl(runner=fake))

    def test_library_search_returns_identifiable_items(self):
        fake = FakeMusic()
        registry = self.registry(fake)
        outcome = registry.execute(registry.prepare("find_music", {"query": "Cornfield Chase Hans Zimmer"}, "?"))
        self.assertEqual([item["item_id"] for item in outcome.data["results"]], [f"track:{TRACK_A}", f"track:{TRACK_B}"])
        self.assertEqual(fake.calls[0][1], ["Cornfield Chase Hans Zimmer"])  # the query is an argument, never code
        self.assertIn("ask which one", outcome.message)

    def test_ambiguous_matches_are_not_played_until_the_user_chooses(self):
        registry = self.registry(FakeMusic())
        message = "Play Cornfield Chase by Hans Zimmer"
        registry.execute(registry.prepare("find_music", {"query": "Cornfield Chase Hans Zimmer"}, message))
        with self.assertRaisesRegex(ToolError, "Several matches"):
            registry.prepare("play_music", {"item_id": f"track:{TRACK_A}"}, message)
        later = registry.prepare("play_music", {"item_id": f"track:{TRACK_B}"}, "The live version.")
        self.assertFalse(later.always_confirm)
        self.assertEqual(later.summary, 'Play "Cornfield Chase" by Hans Zimmer in Music')

    def test_a_playlist_plays_and_playback_is_verified(self):
        registry = self.registry(FakeMusic())
        registry.execute(registry.prepare("find_music", {"query": "Interstellar mix"}, "Play my Interstellar mix playlist"))
        outcome = registry.execute(registry.prepare("play_music", {"item_id": f"playlist:{LIST}"}, "Play my Interstellar mix playlist"))
        self.assertTrue(outcome.ok)
        self.assertIn('the playlist "Interstellar mix"', outcome.message)

    def test_success_is_reported_only_when_the_chosen_item_is_playing(self):
        registry = self.registry(FakeMusic(plays_something_else=True))
        registry.execute(registry.prepare("find_music", {"query": "Interstellar mix"}, "x"))
        outcome = registry.execute(registry.prepare("play_music", {"item_id": f"playlist:{LIST}"}, "play it"))
        self.assertFalse(outcome.ok)
        self.assertIn("did not start", outcome.message)

    def test_unfound_items_bad_ids_and_permission_errors(self):
        registry = self.registry(FakeMusic())
        with self.assertRaisesRegex(ToolError, "find_music first"):
            registry.prepare("play_music", {"item_id": f"track:{TRACK_A}"}, "play it")
        with self.assertRaises(ToolError):
            registry.prepare("play_music", {"item_id": "track:1; do shell script"}, "play")
        empty = self.registry(FakeMusic(library=[]))
        self.assertIn("catalogue cannot be searched",
                      empty.execute(empty.prepare("find_music", {"query": "Interstellar"}, "?")).message)
        denied = self.registry(FakeMusic(denied=True))
        outcome = denied.execute(denied.prepare("find_music", {"query": "x"}, "?"))
        self.assertFalse(outcome.ok)
        self.assertIn("Automation", outcome.message)
        with self.assertRaises(MacControlError):
            mac_control.music_play(MacControl(runner=FakeMusic()), "album:AAAAAAAAAAAAAAAA")

    def test_playback_controls_stay_separate(self):
        names = {d["function"]["name"] for d in self.registry(FakeMusic()).definitions()}
        self.assertTrue({"find_music", "play_music", "media_control"} <= names)


class AppleScriptSourceTests(unittest.TestCase):
    """The fixed scripts are only ever run on a real Mac, so check them here: a script that does
    not compile, or that names a variable after a reserved term, fails every time it is used."""

    SCRIPTS = {name: getattr(mac_control, name) for name in dir(mac_control) if name.endswith("_SCRIPT")}

    def test_no_script_uses_reserved_words_as_variables(self):
        reserved = r"(?:kind|result|st|nd|rd|th|it|me|text|name)"
        pattern = rf"\bset\s+(?:\{{[^}}]*\b{reserved}\b[^}}]*\}}|{reserved}\b)\s+to\b"
        for name, script in self.SCRIPTS.items():
            with self.subTest(script=name):
                self.assertIsNone(__import__("re").search(pattern, script))

    @unittest.skipUnless(Path("/usr/bin/osacompile").is_file(), "requires macOS osacompile")
    def test_every_script_compiles(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, script in self.SCRIPTS.items():
                for player, bundle in mac_control.PLAYERS.items():
                    script_for = script.replace("{bundle}", bundle)
                    if bundle not in script_for:
                        continue
                    # Compiling loads the player's dictionary; skip players that are not installed.
                    if computer_utilities.find_application(player) is None:
                        script_for = None
                    break
                else:
                    script_for = script
                if script_for is None:
                    continue
                with self.subTest(script=name):
                    result = subprocess.run(["/usr/bin/osacompile", "-o", str(Path(folder) / f"{name}.scpt")],
                                            input=script_for, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_errors_are_summarized_safely(self):
        self.assertEqual(mac_control.script_error("291:293: syntax error: Expected expression but found \u201cst\u201d. (-2741)\n"),
                         "syntax error: Expected expression but found \u201cst\u201d. (-2741)")
        self.assertEqual(mac_control.script_error("12:40: execution error: Music got an error: Can\u2019t get track. (-1728)\n"),
                         "Music got an error: Can\u2019t get track. (-1728)")
        self.assertTrue(mac_control.script_error("x" * 1000).endswith("..."))
        self.assertEqual(mac_control.script_error(""), "no details")
        failing = MacControl(runner=lambda s, a: subprocess.CompletedProcess(
            [], 1, "", "1:2: execution error: Music got an error: Can\u2019t get track 1. (-1728)"))
        with self.assertRaises(MacControlError) as raised:
            mac_control.music_play(failing, f"track:{TRACK_A}")
        self.assertEqual(raised.exception.message, "Music reported an error: Music got an error: Can\u2019t get track 1. (-1728).")


@unittest.skipUnless(os.environ.get("LIVE_MUSIC") == "1", "set LIVE_MUSIC=1 to search the real Music library")
class LiveMusicTests(unittest.TestCase):
    def test_library_search_and_optional_verified_play(self):
        control = MacControl()
        query = os.environ.get("LIVE_MUSIC_QUERY", "a")
        items = mac_control.music_search(control, query)
        print(f"\n{len(items)} library item(s) for {query!r}:", [(i["kind"], i["name"], i["artist"]) for i in items[:5]])
        wanted = os.environ.get("LIVE_MUSIC_PLAY")
        if wanted:
            item = next(i for i in mac_control.music_search(control, wanted) if i["name"].casefold() == wanted.casefold())
            state = mac_control.music_play(control, item["item_id"])
            print("playing:", state)
            self.assertEqual((state["state"], state["track"]), ("playing", item["name"]))
            self.assertEqual(control.media("Music", "pause")["state"], "paused")


if __name__ == "__main__":
    unittest.main()
