"""Apple Reminders, the source of truth for reminders, through its AppleScript interface.

Reminders is scriptable (lists; reminders with name, due date, all-day due date, remind me
date, completed, id). Each operation runs one fixed AppleScript with `/usr/bin/osascript`;
titles, dates and IDs are passed as arguments, never as code. Every change is read back from
Reminders before it is reported. Reminders itself alerts at the due time and syncs with the
user's other devices, so reminders work whether or not K.R.I.S. is running.

Permissions: the first use makes macOS ask whether the app that runs K.R.I.S. (for example
Terminal) may control Reminders, and for access to reminders. Until both are allowed, every
operation fails with a clear message; nothing is bypassed.

There is no delete operation: K.R.I.S. never deletes reminders.
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

OSASCRIPT = Path("/usr/bin/osascript")
TIMEOUT_SECONDS = 20  # the first call may have to launch Reminders
MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 1000
MAX_LIST_CHARS = 100
FIELD, RECORD = "\x1f", "\x1e"
REMINDER_ID = re.compile(r"^x-apple-reminder://[A-Za-z0-9-]{8,64}$")

_HELPERS = """
on pad(n)
  set s to (n as integer) as text
  if (count of s) < 2 then set s to "0" & s
  return s
end pad

on iso(d)
  if d is missing value then return ""
  return ((year of d) as integer as text) & "-" & my pad(month of d as integer) & "-" & my pad(day of d) & "T" & my pad(hours of d) & ":" & my pad(minutes of d) & ":" & my pad(seconds of d)
end iso

on mkdate(y, mo, d, h, mi)
  set t to current date
  set day of t to 1
  set year of t to (y as integer)
  set month of t to (mo as integer)
  set day of t to (d as integer)
  set hours of t to (h as integer)
  set minutes of t to (mi as integer)
  set seconds of t to 0
  return t
end mkdate

on record_of(r)
  tell application "Reminders"
    return (id of r) & (character id 31) & (name of r) & (character id 31) & (name of container of r) & (character id 31) & my iso(due date of r) & (character id 31) & my iso(allday due date of r) & (character id 31) & ((completed of r) as text)
  end tell
end record_of

on set_due(r, mode, y, mo, d, h, mi)
  tell application "Reminders"
    if mode is "time" then
      set t to my mkdate(y, mo, d, h, mi)
      set due date of r to t
      set remind me date of r to t
    else if mode is "day" then
      set allday due date of r to my mkdate(y, mo, d, 0, 0)
    end if
  end tell
end set_due
"""

LIST_SCRIPT = _HELPERS + """
on run argv
  set output to {}
  tell application "Reminders"
    repeat with l in lists
      set lname to name of l
      set {ids, names, dues, alldays} to {id, name, due date, allday due date} of (reminders of l whose completed is false)
      repeat with i from 1 to count of ids
        set end of output to (item i of ids) & (character id 31) & (item i of names) & (character id 31) & lname & (character id 31) & my iso(item i of dues) & (character id 31) & my iso(item i of alldays) & (character id 31) & "false"
      end repeat
    end repeat
  end tell
  set AppleScript's text item delimiters to (character id 30)
  return output as text
end run
"""

CREATE_SCRIPT = _HELPERS + """
on run argv
  set {title, listName, notes, mode, y, mo, d, h, mi} to argv
  tell application "Reminders"
    if listName is "" then
      set target to default list
    else
      set target to list listName
    end if
    set r to make new reminder at end of reminders of target with properties {name:title}
    if notes is not "" then set body of r to notes
  end tell
  my set_due(r, mode, y, mo, d, h, mi)
  return my record_of(r)
end run
"""

COMPLETE_SCRIPT = _HELPERS + """
on run argv
  tell application "Reminders"
    set r to reminder id (item 1 of argv)
    set completed of r to true
  end tell
  return my record_of(r)
end run
"""

RESCHEDULE_SCRIPT = _HELPERS + """
on run argv
  set {rid, mode, y, mo, d, h, mi} to argv
  tell application "Reminders" to set r to reminder id rid
  my set_due(r, mode, y, mo, d, h, mi)
  return my record_of(r)
end run
"""

GET_SCRIPT = _HELPERS + """
on run argv
  tell application "Reminders" to set r to reminder id (item 1 of argv)
  return my record_of(r)
end run
"""

_PERMISSION_HELP = (
    "K.R.I.S. is not allowed to use Reminders. In System Settings > Privacy & Security, allow the app "
    "that runs K.R.I.S. (for example Terminal) under Automation > Reminders and under Reminders, then try again."
)


class RemindersError(Exception):
    """kind: unavailable, denied, not_found, timeout, invalid or failed; message is safe to show."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Due:
    """A reminder due time: a local date-and-time, or a date for an all-day reminder."""
    value: datetime | date

    @property
    def all_day(self) -> bool:
        return not isinstance(self.value, datetime)

    def arguments(self) -> list[str]:
        if self.all_day:
            return ["day", str(self.value.year), str(self.value.month), str(self.value.day), "0", "0"]
        return ["time", str(self.value.year), str(self.value.month), str(self.value.day),
                str(self.value.hour), str(self.value.minute)]


def _run_osascript(script: str, arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(OSASCRIPT), "-", *arguments], input=script, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, check=False)


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


class AppleReminders:
    def __init__(self, runner: Callable[[str, list[str]], subprocess.CompletedProcess] | None = None):
        self.runner = runner or _run_osascript

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and OSASCRIPT.is_file()

    def _call(self, script: str, arguments: list[str]) -> str:
        if self.runner is _run_osascript and not self.available():
            raise RemindersError("unavailable", "Apple Reminders is only available on macOS.")
        try:
            result = self.runner(script, arguments)
        except subprocess.TimeoutExpired:
            raise RemindersError("timeout", "Reminders did not respond in time. Try again in a moment.") from None
        except OSError:
            raise RemindersError("unavailable", "Reminders could not be reached.") from None
        if result.returncode == 0:
            return result.stdout.rstrip("\n")
        error = result.stderr or ""
        if "-1743" in error or "not allowed" in error.lower() or "not authorized" in error.lower():
            raise RemindersError("denied", _PERMISSION_HELP)
        if "-1728" in error or "-1719" in error or "Can’t get" in error or "Can't get" in error:
            raise RemindersError("not_found", "That reminder or list was not found in Reminders.")
        if "-600" in error or "-10810" in error or "-609" in error:
            raise RemindersError("unavailable", "Reminders could not be opened.")
        raise RemindersError("failed", "Reminders reported an error.")

    @staticmethod
    def _parse(record: str) -> dict:
        fields = record.split(FIELD)
        if len(fields) != 6 or not fields[0]:
            raise RemindersError("failed", "Reminders returned something K.R.I.S. could not read.")
        rid, title, list_name, due, all_day, completed = fields
        item = {"reminder_id": rid, "title": title, "list": list_name, "completed": completed == "true",
                "due": None, "all_day": False}
        # Reminders reports `allday due date` for timed reminders too (seen live on macOS 26), so a
        # reminder is all-day only when its `due date` has no time of day.
        if due and not (all_day and due[11:16] == "00:00"):
            item["due"] = due[:16].replace("T", " ")
        elif all_day or due:
            item.update(due=(all_day or due)[:10], all_day=True)
        return item

    def list_open(self) -> list[dict]:
        """Every incomplete reminder in every list."""
        output = self._call(LIST_SCRIPT, [])
        return [self._parse(record) for record in output.split(RECORD) if record]

    def get(self, reminder_id: str) -> dict:
        return self._parse(self._call(GET_SCRIPT, [validate_reminder_id(reminder_id)]))

    def create(self, title: str, due: Due | None, list_name: str | None = None, notes: str | None = None) -> dict:
        title = " ".join((title or "").split())
        if not title or len(title) > MAX_TITLE_CHARS:
            raise RemindersError("invalid", f"A reminder needs a title of 1 to {MAX_TITLE_CHARS} characters.")
        list_name = (list_name or "").strip()
        if len(list_name) > MAX_LIST_CHARS or len((notes or "")) > MAX_NOTES_CHARS:
            raise RemindersError("invalid", "The list name or notes are too long.")
        duplicate = self.find_duplicate(title, due)
        if duplicate is not None:
            raise RemindersError("duplicate", f"\"{duplicate['title']}\" is already in Reminders"
                                 f"{' for ' + duplicate['due'] if duplicate['due'] else ''}; nothing new was added.")
        due_arguments = due.arguments() if due else ["none", "0", "0", "0", "0", "0"]
        created = self._parse(self._call(CREATE_SCRIPT, [title, list_name, notes or "", *due_arguments]))
        if normalize_title(created["title"]) != normalize_title(title) or not self._due_matches(created, due):
            raise RemindersError("failed", f"The reminder was added to {created['list']}, but Reminders reports it as "
                                 f"\"{created['title']}\" due {self._describe(created)} instead of "
                                 f"{self._describe_due(due)}. Check it in Reminders.")
        return created

    @staticmethod
    def _describe(item: dict) -> str:
        return "(no date)" if not item["due"] else f"{item['due']}{' (all day)' if item['all_day'] else ''}"

    @staticmethod
    def _describe_due(due: Due | None) -> str:
        if due is None:
            return "(no date)"
        return f"{due.value.isoformat()} (all day)" if due.all_day else due.value.strftime("%Y-%m-%d %H:%M")

    def find_duplicate(self, title: str, due: Due | None) -> dict | None:
        """An open reminder with the same title and the same due date (or both undated)."""
        wanted = normalize_title(title)
        for item in self.list_open():
            if normalize_title(item["title"]) == wanted and self._same_day(item, due):
                return item
        return None

    @staticmethod
    def _same_day(item: dict, due: Due | None) -> bool:
        if due is None:
            return item["due"] is None
        return item["due"] is not None and item["due"][:10] == due.value.isoformat()[:10]

    @staticmethod
    def _due_matches(item: dict, due: Due | None) -> bool:
        if due is None:
            return item["due"] is None
        if due.all_day:
            return item["due"] == due.value.isoformat()
        return item["due"] == due.value.strftime("%Y-%m-%d %H:%M")

    def complete(self, reminder_id: str) -> dict:
        done = self._parse(self._call(COMPLETE_SCRIPT, [validate_reminder_id(reminder_id)]))
        if not done["completed"]:
            raise RemindersError("failed", "Reminders did not mark the reminder as completed.")
        return done

    def reschedule(self, reminder_id: str, due: Due) -> dict:
        moved = self._parse(self._call(RESCHEDULE_SCRIPT, [validate_reminder_id(reminder_id), *due.arguments()]))
        if not self._due_matches(moved, due):
            raise RemindersError("failed", f"Reminders did not save the new due time: it reports {self._describe(moved)} "
                                 f"instead of {self._describe_due(due)}.")
        return moved


def validate_reminder_id(value: str) -> str:
    if not isinstance(value, str) or not REMINDER_ID.fullmatch(value):
        raise RemindersError("invalid", "That is not a Reminders ID; find the reminder with list_reminders first.")
    return value
