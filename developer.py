"""Developer companion: read-only Git inspection and allowlisted test runs for approved projects.

Only projects listed in config.ini [projects] are reachable; the model names a project by its
label, never by a path, and nothing outside those folders is read. Git runs as the fixed
`/usr/bin/git` with argument lists (no shell), only for the read-only subcommands in
READ_ONLY_GIT, with optional locks, pagers, external diff and text-conversion programs, and
fsmonitor hooks turned off. Diffs are summarized as per-file line counts; file contents are never
returned. Remote URLs are shown without embedded credentials.

Tests run only the commands written in config.ini [project_tests] for that project (split like
a shell would, but executed without one), from the project folder, with a timeout and bounded
output. Opt-in real-machine test switches (LIVE_*, *_SMOKE) are removed from their environment.
Nothing here stages, commits, resets, checks out, pushes, edits files or installs anything.
"""

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

GIT = Path("/usr/bin/git")
GIT_TIMEOUT_SECONDS = 15
READ_ONLY_GIT = frozenset({"status", "diff", "log", "rev-parse", "remote"})
DEFAULT_TEST_TIMEOUT = 300
TEST_TIMEOUT_RANGE = (10, 1800)
MAX_LISTED_FILES = 30
MAX_COMMITS = 20
MAX_OUTPUT_TAIL = 2500  # characters of each command's output returned to the model
MAX_CAPTURE_BYTES = 2_000_000
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash", "env", "sudo", "osascript"})
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'+-]{0,63}$")
_DROPPED_ENV = re.compile(r"^(?:LIVE_|VOICE_SMOKE$|.*_SMOKE$)")
# Common credential shapes, masked in returned test output.
_SECRETS = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|ya29\.[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+)"
)

logger = logging.getLogger(__name__)


class DeveloperError(Exception):
    """kind: not_configured, invalid, unavailable, not_a_repository, timeout or failed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class DeveloperSettings:
    projects: dict[str, Path] = field(default_factory=dict)
    tests: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    editor: str | None = None
    test_timeout: int = DEFAULT_TEST_TIMEOUT


def build_developer_settings(projects: dict[str, str], tests: dict[str, str], developer: dict[str, str],
                             home: Path | None = None) -> DeveloperSettings:
    """Validate the configured projects, test commands and editor, skipping (and logging) bad entries."""
    home = (home or Path.home()).resolve()
    approved: dict[str, Path] = {}
    for label, raw in projects.items():
        label = label.strip()
        try:
            path = Path(raw.strip()).expanduser().resolve(strict=True)
        except OSError:
            logger.warning("Ignoring project %r: folder does not exist", label)
            continue
        if not _LABEL.fullmatch(label) or label.casefold() in {item.casefold() for item in approved}:
            logger.warning("Ignoring project %r: invalid or duplicate label", label)
        elif not path.is_dir() or path == home or not path.is_relative_to(home):
            logger.warning("Ignoring project %r: must be a folder inside the home folder", label)
        elif path.relative_to(home).parts[0] == "Library":
            logger.warning("Ignoring project %r: Library folders are not allowed", label)
        else:
            approved[label] = path

    commands: dict[str, tuple[tuple[str, ...], ...]] = {}
    for label, raw in tests.items():
        project = _match_label(approved, label.strip())
        if project is None:
            logger.warning("Ignoring tests for %r: not a configured project", label)
            continue
        parsed = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(_test_command(shlex.split(line), approved[project]))
            except ValueError as error:
                logger.warning("Ignoring a test command for %r: %s", project, error)
        if parsed:
            commands[project] = tuple(parsed)

    editor = (developer.get("editor") or "").strip() or None
    if editor and not _APP_NAME.fullmatch(editor):
        logger.warning("Ignoring editor %r: invalid application name", editor)
        editor = None
    try:
        timeout = int(developer.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT))
    except ValueError:
        timeout = DEFAULT_TEST_TIMEOUT
    low, high = TEST_TIMEOUT_RANGE
    return DeveloperSettings(approved, commands, editor, min(max(timeout, low), high))


def _match_label(projects: dict, label: str) -> str | None:
    return next((name for name in projects if name.casefold() == label.casefold()), None)


def _test_command(argv: list[str], project: Path) -> tuple[str, ...]:
    if not argv:
        raise ValueError("empty command")
    program = argv[0]
    if Path(program).name in SHELLS:
        raise ValueError("shells and wrappers are not allowed; write the test command itself")
    if "/" in program:
        written = Path(program)
        if not written.is_absolute():
            # A virtual environment's interpreter may be a symlink out of the project; the path
            # as written must still be inside it.
            if ".." in written.parts:
                raise ValueError("relative programs must be inside the project")
            written = project / written
        program = str(written)
    else:
        found = shutil.which(program)
        if found is None:
            raise ValueError(f"{program} was not found on PATH")
        program = found
    return (program, *argv[1:])


def _sanitized_remote(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unreadable URL)"
    if parts.scheme and parts.hostname:
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return url.split("@")[-1] if "@" in url and ":" in url else url  # git@github.com:a/b -> github.com:a/b


def redact(text: str) -> str:
    return _SECRETS.sub("[redacted]", text)


def summarize_test_output(output: str) -> str | None:
    """One line from the common test runners' summaries, when one is found."""
    patterns = (
        r"^Ran \d+ tests? in [\d.]+s\s*$",  # unittest
        r"^(?:OK|FAILED)(?: \(.*\))?\s*$",
        r"^=+ .*(?:passed|failed|error).* =+$",  # pytest
        r"^# (?:tests|pass|fail|skipped) \d+$",  # node --test
        r"^Tests?:\s+.*$",  # jest
    )
    lines = []
    for line in output.splitlines():
        if any(re.match(pattern, line.strip()) for pattern in patterns):
            lines.append(line.strip())
    return "; ".join(lines[-6:]) or None


def _child_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not _DROPPED_ENV.match(key)}


def _run_process(argv: list[str], cwd: Path, timeout: int) -> tuple[int | None, str, bool]:
    """Run argv (no shell) in its own process group; on timeout the whole group is stopped."""
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=_child_environment(), start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output[-MAX_CAPTURE_BYTES:].decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        return None, (output or b"")[-MAX_CAPTURE_BYTES:].decode("utf-8", "replace"), True


class DeveloperCompanion:
    def __init__(self, settings: DeveloperSettings | None = None,
                 git_runner: Callable[[list[str], Path], subprocess.CompletedProcess] | None = None,
                 process_runner: Callable[[list[str], Path, int], tuple[int | None, str, bool]] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings or DeveloperSettings()
        self.git_runner = git_runner or self._run_git
        self.process_runner = process_runner or _run_process
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._test_lock = threading.Lock()
        self._results: dict[str, dict] = {}

    @property
    def projects(self) -> list[str]:
        return list(self.settings.projects)

    def project(self, label: str | None) -> tuple[str, Path]:
        """Resolve a project label; with one project configured, it is the default."""
        projects = self.settings.projects
        if not projects:
            raise DeveloperError("not_configured", "No projects are configured. Add them to config.ini under [projects].")
        if not label:
            if len(projects) == 1:
                return next(iter(projects.items()))
            raise DeveloperError("invalid", f"Which project: {', '.join(projects)}?")
        name = _match_label(projects, label.strip())
        if name is None:
            raise DeveloperError("invalid", f"That is not a configured project. Projects: {', '.join(projects)}.")
        return name, projects[name]

    def describe(self) -> list[dict]:
        home = Path.home()
        return [{"project": name,
                 "folder": "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path),
                 "tests_configured": name in self.settings.tests,
                 "editor": self.settings.editor}
                for name, path in self.settings.projects.items()]

    # --- Git (read-only) --------------------------------------------------------------------

    @staticmethod
    def _run_git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess:
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LC_ALL": "C",
                       "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
                       "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run([str(GIT), *arguments], cwd=cwd, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS, env=environment, stdin=subprocess.DEVNULL, check=False)

    def _git(self, path: Path, *arguments: str) -> str:
        if not arguments or arguments[0] not in READ_ONLY_GIT:
            raise DeveloperError("invalid", "Only read-only Git commands are available.")
        if arguments[0] == "remote" and list(arguments[1:]) != ["-v"]:
            raise DeveloperError("invalid", "Only read-only Git commands are available.")
        safety = ["-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "diff.external=",
                  "--no-pager"]
        try:
            result = self.git_runner([*safety, *arguments], path)
        except subprocess.TimeoutExpired:
            raise DeveloperError("timeout", "Git did not answer in time.") from None
        except OSError:
            raise DeveloperError("unavailable", "Git could not be started (install the Xcode Command Line Tools).") from None
        if result.returncode != 0:
            if "not a git repository" in (result.stderr or "").lower():
                raise DeveloperError("not_a_repository", "That project folder is not a Git repository.")
            raise DeveloperError("failed", "Git reported an error.")
        return result.stdout

    def git_status(self, label: str | None) -> dict:
        name, path = self.project(label)
        output = self._git(path, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
        lines = output.splitlines()
        header = lines[0][3:] if lines and lines[0].startswith("## ") else ""
        branch, upstream, ahead, behind = header, None, 0, 0
        match = re.match(r"^(?:No commits yet on )?(\S+?)(?:\.\.\.(\S+))?(?: \[(.*)\])?$", header)
        if match:
            branch, upstream = match.group(1), match.group(2)
            for part in (match.group(3) or "").split(", "):
                if part.startswith("ahead "):
                    ahead = int(part.split()[1])
                elif part.startswith("behind "):
                    behind = int(part.split()[1])
        staged, unstaged, untracked, conflicted = [], [], [], []
        for line in lines[1:]:
            code, file = line[:2], line[3:]
            if code == "??":
                untracked.append(file)
            elif "U" in code or code in ("AA", "DD"):
                conflicted.append(file)
            else:
                if code[0] != " ":
                    staged.append(f"{_STATUS_WORDS.get(code[0], code[0])}: {file}")
                if code[1] != " ":
                    unstaged.append(f"{_STATUS_WORDS.get(code[1], code[1])}: {file}")
        return {
            "project": name,
            "branch": branch if branch != "HEAD" else "(detached HEAD)",
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "clean": not (staged or unstaged or untracked or conflicted),
            "counts": {"staged": len(staged), "unstaged": len(unstaged), "untracked": len(untracked),
                       "conflicted": len(conflicted)},
            "staged": staged[:MAX_LISTED_FILES],
            "unstaged": unstaged[:MAX_LISTED_FILES],
            "untracked": untracked[:MAX_LISTED_FILES],
            "conflicted": conflicted[:MAX_LISTED_FILES],
            "lists_truncated": max(len(staged), len(unstaged), len(untracked), len(conflicted)) > MAX_LISTED_FILES,
        }

    def diff_summary(self, label: str | None) -> dict:
        name, path = self.project(label)
        summary = {"project": name}
        for key, extra in (("unstaged", ()), ("staged", ("--cached",))):
            output = self._git(path, "diff", *extra, "--numstat", "--no-ext-diff", "--no-textconv", "--no-color")
            files, added, removed = [], 0, 0
            for line in output.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                plus, minus, file = parts
                binary = plus == "-"
                if not binary:
                    added, removed = added + int(plus), removed + int(minus)
                files.append({"file": file, "added": None if binary else int(plus),
                              "removed": None if binary else int(minus), "binary": binary})
            summary[key] = {"files_changed": len(files), "lines_added": added, "lines_removed": removed,
                            "files": files[:MAX_LISTED_FILES]}
        untracked = self._git(path, "status", "--porcelain=v1", "--untracked-files=normal")
        summary["untracked_files"] = sum(1 for line in untracked.splitlines() if line.startswith("??"))
        summary["note"] = "Line counts per file only; file contents are not shown."
        return summary

    def recent_commits(self, label: str | None, count: int = 5) -> dict:
        name, path = self.project(label)
        if not isinstance(count, int) or not 1 <= count <= MAX_COMMITS:
            raise DeveloperError("invalid", f"Ask for 1 to {MAX_COMMITS} commits.")
        try:
            output = self._git(path, "log", f"-n{count}", "--no-color", "--date=iso-strict",
                               "--format=%h%x1f%an%x1f%ad%x1f%s")
        except DeveloperError as error:
            if error.kind == "failed":  # a repository without commits
                return {"project": name, "commits": []}
            raise
        commits = []
        for line in output.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"commit": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3][:200]})
        return {"project": name, "commits": commits}

    def repository_info(self, label: str | None) -> dict:
        name, path = self.project(label)
        status = self.git_status(name)
        remotes = {}
        for line in self._git(path, "remote", "-v").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes[parts[0]] = _sanitized_remote(parts[1])
        last = self.recent_commits(name, 1)["commits"]
        return {"project": name, "branch": status["branch"], "upstream": status["upstream"],
                "ahead": status["ahead"], "behind": status["behind"], "remotes": remotes,
                "last_commit": last[0] if last else None}

    # --- Tests --------------------------------------------------------------------------------

    def run_tests(self, label: str | None) -> dict:
        name, path = self.project(label)
        commands = self.settings.tests.get(name)
        if not commands:
            raise DeveloperError("not_configured", f"No test command is configured for {name}. "
                                 "Add one to config.ini under [project_tests].")
        if not self._test_lock.acquire(blocking=False):
            raise DeveloperError("unavailable", "A test run is already in progress.")
        try:
            started = self.clock()
            runs = []
            for argv in commands:
                display = " ".join(shlex.quote(Path(part).name if index == 0 else part)
                                   for index, part in enumerate(argv))
                if not Path(argv[0]).exists():
                    runs.append({"command": display, "status": "unavailable", "exit_code": None,
                                 "summary": f"{Path(argv[0]).name} was not found.", "output_tail": ""})
                    continue
                try:
                    code, output, timed_out = self.process_runner(list(argv), path, self.settings.test_timeout)
                except OSError as error:
                    runs.append({"command": display, "status": "unavailable", "exit_code": None,
                                 "summary": f"The command could not be started ({error.strerror or 'error'}).",
                                 "output_tail": ""})
                    continue
                status = "timed_out" if timed_out else "passed" if code == 0 else "failed"
                runs.append({
                    "command": display, "status": status, "exit_code": code,
                    "summary": (f"Stopped after {self.settings.test_timeout} s." if timed_out
                                else summarize_test_output(output)),
                    "output_tail": redact(output[-MAX_OUTPUT_TAIL:]),
                })
            statuses = {run["status"] for run in runs}
            overall = ("passed" if statuses == {"passed"} else "timed_out" if "timed_out" in statuses
                       else "failed" if "failed" in statuses else "unavailable")
            result = {"project": name, "status": overall, "started_at": started.isoformat(timespec="seconds"),
                      "finished_at": self.clock().isoformat(timespec="seconds"), "runs": runs}
            self._results[name] = result
            return result
        finally:
            self._test_lock.release()

    def last_results(self, label: str | None) -> dict | None:
        name, _ = self.project(label)
        return self._results.get(name)


_STATUS_WORDS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "type changed"}
