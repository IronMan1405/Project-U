import json
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import computer_utilities
from computer_utilities import UtilityError, UtilitySettings
from storage import (
    TOOL_DUE_AT_FORMAT_DESCRIPTION,
    TOOL_DUE_AT_PATTERN,
    RecordNotFound,
    create_note,
    create_task,
    delete_note,
    delete_task,
    describe_due_at,
    get_note,
    initialize_database,
    list_notes,
    list_tasks,
    normalize_due_at,
    set_task_completed,
    update_note,
    update_task,
)


MAX_ARGUMENT_BYTES = 8_000
MAX_RESULT_CHARS = 8_000
MAX_LIST_ITEMS = 10
MAX_NOTE_CONTENT_RESULT = 5_000
MAX_NOTE_WRITE_CHARS = 5_000
MAX_TASK_DESCRIPTION_RESULT = 200

logger = logging.getLogger(__name__)


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    message: str
    data: object = None

    def as_content(self) -> str:
        result = {"ok": self.ok, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(content) <= MAX_RESULT_CHARS:
            return content
        if self.ok:
            return json.dumps(
                {
                    "ok": True,
                    "message": "The operation completed, but its result was too large to return in full.",
                    "data": {"result_truncated": True},
                },
                separators=(",", ":"),
            )
        return json.dumps(
            {"ok": False, "message": "The operation failed; details exceeded the result limit."},
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PreparedToolCall:
    name: str
    arguments: dict
    summary: str
    always_confirm: bool
    direct_write: bool


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], ToolOutcome]
    always_confirm: bool = False
    direct_write: bool = False

    def ollama_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _positive_id_schema() -> dict:
    return {"type": "integer", "minimum": 1}


def validate_arguments(schema: dict, value: object) -> dict:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ToolError("Tool arguments exceed the size limit.")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ToolError("Tool arguments are not valid JSON.") from error
    if not isinstance(value, dict):
        raise ToolError("Tool arguments must be a JSON object.")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ToolError("Tool arguments exceed the size limit.")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if schema.get("additionalProperties") is False:
        extra = value.keys() - properties.keys()
        if extra:
            raise ToolError(f"Unexpected argument: {sorted(extra)[0]}.")
    missing = set(required) - value.keys()
    if missing:
        raise ToolError(f"Missing required argument: {sorted(missing)[0]}.")

    for name, item in value.items():
        definition = properties.get(name)
        if definition is None:
            continue
        expected = definition.get("type")
        if isinstance(expected, list):
            if item is None and "null" in expected:
                continue
            expected = next((kind for kind in expected if kind != "null"), None)
        if expected == "string":
            if not isinstance(item, str):
                raise ToolError(f"{name} must be text.")
            if "pattern" in definition and not re.search(definition["pattern"], item):
                detail = definition.get("description")
                raise ToolError(f"{name} has an invalid format." + (f" {detail}" if detail else ""))
            if "enum" in definition and item not in definition["enum"]:
                raise ToolError(f"{name} must be one of: {', '.join(definition['enum'])}.")
            if name == "title" and not item.strip():
                raise ToolError("title must contain non-whitespace text.")
            if "minLength" in definition and len(item) < definition["minLength"]:
                raise ToolError(f"{name} is too short.")
            if "maxLength" in definition and len(item) > definition["maxLength"]:
                raise ToolError(f"{name} exceeds the {definition['maxLength']}-character limit.")
        elif expected == "integer":
            if not isinstance(item, int) or isinstance(item, bool):
                raise ToolError(f"{name} must be an integer.")
            if item < definition.get("minimum", 0):
                raise ToolError(f"{name} is out of range.")
        elif expected == "boolean" and not isinstance(item, bool):
            raise ToolError(f"{name} must be true or false.")
    return value


def _explicit_write_requested(name: str, message: str) -> bool:
    prefixes = r"\s*(?:(?:please|can you|could you|would you)\s+)?"
    patterns = {
        "create_task": [
            rf"^{prefixes}(?:add|create|make|record)\b.{{0,120}}\b(?:task|todo)\b",
        ],
        "set_task_completed": [
            rf"^{prefixes}(?:mark|complete|finish|reopen)\b.{{0,120}}\b(?:task|todo)\b",
        ],
        "set_task_due_date": [
            rf"^{prefixes}(?:set|change|update|move|push|reschedule|remove|clear)\b.{{0,120}}\b(?:due|deadline)\b",
        ],
        "create_note": [
            rf"^{prefixes}(?:create|write|make|record|take)\b.{{0,120}}\bnote\b",
        ],
        "update_note": [
            rf"^{prefixes}(?:edit|update|change|revise)\b.{{0,120}}\bnote\b",
        ],
    }
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns.get(name, []))


def _normalized_content(value: str) -> str:
    return " ".join(value.split()).casefold()


def _note_preview(content: str) -> str:
    preview = " ".join(content.split())
    if len(preview) > 120:
        preview = preview[:117] + "..."
    return f"content: {preview!r}"


def validate_http_url(value: str) -> str:
    if not value or value != value.strip() or len(value) > 2048:
        raise ToolError("URL must be non-empty and no longer than 2,048 characters.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ToolError("URL is malformed.") from error
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ToolError("Only absolute HTTP or HTTPS URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("URLs containing embedded credentials are not allowed.")
    if any(ord(character) <= 32 or character in "<>\"'" for character in host):
        raise ToolError("URL host is malformed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError as error:
            raise ToolError("URL host is malformed.") from error
        labels = ascii_host.split(".")
        label_pattern = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
        if (
            not ascii_host
            or len(ascii_host) > 253
            or any(not label_pattern.fullmatch(label) for label in labels)
            or (re.fullmatch(r"[0-9.]+", ascii_host) and "." in ascii_host)
        ):
            raise ToolError("URL host is malformed.")
    return value


class ToolRegistry:
    def __init__(
        self,
        database_path: str | Path | None = None,
        browser_open: Callable[..., bool] | None = None,
        utilities: UtilitySettings | None = None,
        open_runner: Callable[[list[str]], bool] | None = None,
    ):
        self.database_path = database_path
        self.browser_open = browser_open or webbrowser.open
        self.utilities = utilities or UtilitySettings()
        self.open_runner = open_runner or computer_utilities.run_open
        task_id = _positive_id_schema()
        note_id = _positive_id_schema()
        due_at_description = (
            f"Machine-format due date, never natural language: {TOOL_DUE_AT_FORMAT_DESCRIPTION}. "
            "Specific times are supported: write the user's local date and time as they said it "
            "(e.g. 'tomorrow at 5 AM' -> '<tomorrow> 05:00'); do not convert to UTC, U does that."
        )
        self._definitions = {
            "create_task": ToolDefinition(
                "create_task",
                "Create a task only when the user explicitly asks for one. "
                "It may have a due date with or without a specific time (see due_at).",
                _object_schema(
                    {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 5000},
                        "due_at": {
                            "type": "string",
                            "pattern": TOOL_DUE_AT_PATTERN,
                            "description": f"Optional. {due_at_description}",
                        },
                    },
                    ("title",),
                ),
                self._create_task,
                direct_write=True,
            ),
            "list_tasks": ToolDefinition(
                "list_tasks",
                "List the user's tasks, including completion status and due_at (null when unset).",
                _object_schema({}),
                self._list_tasks,
            ),
            "set_task_completed": ToolDefinition(
                "set_task_completed",
                "Complete or reopen an existing task by ID. Does not change its due date.",
                _object_schema(
                    {"task_id": task_id, "completed": {"type": "boolean"}},
                    ("task_id", "completed"),
                ),
                self._set_task_completed,
                direct_write=True,
            ),
            "set_task_due_date": ToolDefinition(
                "set_task_due_date",
                "Set, change, or clear the due date (with or without a specific time) "
                "of an existing task by ID. "
                "Pass due_at as null to remove the due date. Does not change completion status.",
                _object_schema(
                    {
                        "task_id": task_id,
                        "due_at": {
                            "type": ["string", "null"],
                            "pattern": TOOL_DUE_AT_PATTERN,
                            "description": f"{due_at_description} Use null to clear.",
                        },
                    },
                    ("task_id", "due_at"),
                ),
                self._set_task_due_date,
                direct_write=True,
            ),
            "delete_task": ToolDefinition(
                "delete_task",
                "Permanently delete an existing task by ID. Requires user confirmation.",
                _object_schema({"task_id": task_id}, ("task_id",)),
                self._delete_task,
                always_confirm=True,
            ),
            "list_notes": ToolDefinition(
                "list_notes",
                "List saved note titles and short previews.",
                _object_schema({}),
                self._list_notes,
            ),
            "get_note": ToolDefinition(
                "get_note",
                "Read a saved note by ID. Long note contents may be truncated in the result.",
                _object_schema({"note_id": note_id}, ("note_id",)),
                self._get_note,
            ),
            "create_note": ToolDefinition(
                "create_note",
                "Create a note only when the user explicitly asks to write or save one.",
                _object_schema(
                    {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "content": {"type": "string", "maxLength": MAX_NOTE_WRITE_CHARS},
                    },
                    ("title", "content"),
                ),
                self._create_note,
                direct_write=True,
            ),
            "update_note": ToolDefinition(
                "update_note",
                "Edit a saved note by ID only when the user explicitly asks to change it.",
                _object_schema(
                    {
                        "note_id": note_id,
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "content": {"type": "string", "maxLength": MAX_NOTE_WRITE_CHARS},
                    },
                    ("note_id", "title", "content"),
                ),
                self._update_note,
                direct_write=True,
            ),
            "delete_note": ToolDefinition(
                "delete_note",
                "Permanently delete a saved note by ID. Requires user confirmation.",
                _object_schema({"note_id": note_id}, ("note_id",)),
                self._delete_note,
                always_confirm=True,
            ),
            "get_system_info": ToolDefinition(
                "get_system_info",
                "Get the local date/time and timezone, plus available battery, memory, and disk status.",
                _object_schema({}),
                self._get_system_info,
            ),
            "open_website": ToolDefinition(
                "open_website",
                "Ask to open a validated HTTP or HTTPS URL in the default browser. Requires user confirmation.",
                _object_schema(
                    {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
                    ("url",),
                ),
                self._open_website,
                always_confirm=True,
            ),
        }
        self._register_utilities()

    def _register_utilities(self) -> None:
        folder_labels = list(self.utilities.folders)
        if folder_labels:
            self._definitions["find_files"] = ToolDefinition(
                "find_files",
                "Search file names (not contents) in the user's approved root folders and "
                f"their subfolders. Approved roots: {', '.join(folder_labels)}. Returns at most "
                f"{computer_utilities.MAX_FILE_RESULTS} matches as 'Root/subfolder/file'. "
                "Each call is one search: report exactly what it returned, and only say you "
                "searched again after calling this tool again.",
                _object_schema(
                    {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": computer_utilities.MAX_QUERY_CHARS,
                            "description": "Words from the file name, e.g. 'performance sheet'. "
                            "Every word must appear in the name; case, spaces, underscores "
                            "and CamelCase are ignored, so the exact file name is not needed.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Optional approved root to limit the search to: one of "
                            f"{', '.join(folder_labels)}. Not a subfolder name; subfolders "
                            "are searched automatically.",
                        },
                        "file_type": {
                            "type": "string",
                            "pattern": r"^\.?[A-Za-z0-9]{1,10}$",
                            "description": "Optional file extension filter, e.g. 'pdf'.",
                        },
                    },
                    ("query",),
                ),
                self._find_files,
            )
            self._definitions["open_file"] = ToolDefinition(
                "open_file",
                "Ask to open a document from an approved folder in its default macOS app. "
                "Requires user confirmation. Only use a path returned by find_files. "
                f"Allowed types: {', '.join(sorted(computer_utilities.OPENABLE_EXTENSIONS))}.",
                _object_schema(
                    {
                        "path": {
                            "type": "string",
                            "minLength": 3,
                            "maxLength": computer_utilities.MAX_OPEN_PATH_CHARS,
                            "description": "Exact 'Folder/relative/path' value from find_files.",
                        }
                    },
                    ("path",),
                ),
                self._open_file,
                always_confirm=True,
            )
        if self.utilities.apps:
            self._definitions["open_application"] = ToolDefinition(
                "open_application",
                "Ask to open an approved macOS application. Requires user confirmation. "
                f"Approved applications: {', '.join(self.utilities.apps)}.",
                _object_schema(
                    {"name": {"type": "string", "enum": list(self.utilities.apps)}},
                    ("name",),
                ),
                self._open_application,
                always_confirm=True,
            )

    def definitions(self) -> list[dict]:
        return [definition.ollama_definition() for definition in self._definitions.values()]

    def prepare(
        self,
        name: str,
        raw_arguments: object,
        user_message: str,
        allow_derived_note_content: bool = False,
    ) -> PreparedToolCall:
        definition = self._definitions.get(name)
        if definition is None:
            raise ToolError("The requested tool is not available.")
        arguments = validate_arguments(definition.parameters, raw_arguments)
        if arguments.get("due_at") is not None:
            # Normalize once, here, so the confirmation summary and the stored value are
            # the same instant: local wall times become canonical UTC in Python.
            try:
                arguments = {**arguments, "due_at": normalize_due_at(arguments["due_at"])}
            except ValueError as error:
                raise ToolError(str(error)) from error
        if name == "open_website":
            arguments = {"url": validate_http_url(arguments["url"])}
        if name == "find_files" and arguments.get("folder") is not None:
            try:
                root = computer_utilities.resolve_search_root(self.utilities, arguments["folder"])
            except UtilityError as error:
                raise ToolError(str(error)) from error
            arguments = {**arguments, "folder": root}
        if name == "open_file":
            try:
                display_path, _ = computer_utilities.resolve_approved_file(
                    self.utilities, arguments["path"]
                )
            except UtilityError as error:
                raise ToolError(str(error)) from error
            arguments = {"path": display_path}
        direct_intent = _explicit_write_requested(name, user_message)
        if name in {"create_note", "update_note"}:
            supplied_content = _normalized_content(arguments["content"])
            user_content = _normalized_content(user_message)
            explicit_content = bool(supplied_content) and supplied_content in user_content
            document_request = bool(
                allow_derived_note_content
                and re.search(r"\b(document|file|attachment|pdf)\b", user_message, re.IGNORECASE)
            )
            direct_intent = direct_intent and (explicit_content or document_request)
        summary = self._summary(name, arguments)
        requires_confirmation = definition.always_confirm or (
            definition.direct_write and not direct_intent
        )
        return PreparedToolCall(
            name=name,
            arguments=arguments,
            summary=summary,
            always_confirm=requires_confirmation,
            direct_write=definition.direct_write,
        )

    def execute(self, call: PreparedToolCall) -> ToolOutcome:
        definition = self._definitions.get(call.name)
        if definition is None:
            return ToolOutcome(False, "The requested tool is not available.")
        try:
            return definition.execute(call.arguments)
        except RecordNotFound as error:
            return ToolOutcome(False, str(error))
        except Exception:
            logger.exception("Registered tool failed: %s", call.name)
            return ToolOutcome(False, "The operation failed. No internal details are available.")

    @staticmethod
    def _summary(name: str, arguments: dict) -> str:
        if name == "create_task":
            due = f" (due {describe_due_at(arguments['due_at'])})" if arguments.get("due_at") else ""
            return f"Create task: {arguments['title']}{due}"
        if name == "set_task_completed":
            state = "complete" if arguments["completed"] else "reopen"
            return f"Mark task {arguments['task_id']} to {state}"
        if name == "set_task_due_date":
            if arguments["due_at"] is None:
                return f"Clear due date of task {arguments['task_id']}"
            return f"Set due date of task {arguments['task_id']} to {describe_due_at(arguments['due_at'])}"
        if name == "delete_task":
            return f"Delete task {arguments['task_id']}"
        if name == "create_note":
            return f"Create note: {arguments['title']} ({_note_preview(arguments['content'])})"
        if name == "update_note":
            return f"Edit note {arguments['note_id']}: {arguments['title']} ({_note_preview(arguments['content'])})"
        if name == "delete_note":
            return f"Delete note {arguments['note_id']}"
        if name == "open_website":
            return f"Open website: {arguments['url']}"
        if name == "open_application":
            return f"Open application: {arguments['name']}"
        if name == "open_file":
            return f"Open file: {arguments['path']}"
        if name == "find_files":
            scope = arguments.get("folder") or "approved folders"
            kind = f" {arguments['file_type'].lstrip('.')}" if arguments.get("file_type") else ""
            return f"Search {scope} for{kind} file names matching {arguments['query']!r}"
        return name.replace("_", " ").capitalize()

    def _create_task(self, arguments: dict) -> ToolOutcome:
        task = create_task(
            arguments["title"].strip(),
            arguments.get("description", ""),
            self.database_path,
            due_at=arguments.get("due_at"),
        )
        if task["due_at"] is None:
            return ToolOutcome(True, "Task created.", task)
        due = describe_due_at(task["due_at"])
        return ToolOutcome(True, f"Task created, due {due}.", {**task, "due_local": due})

    def _list_tasks(self, arguments: dict) -> ToolOutcome:
        tasks = list_tasks(self.database_path)
        bounded_tasks = []
        for task in tasks[:MAX_LIST_ITEMS]:
            description = task["description"]
            bounded_tasks.append(
                {
                    **task,
                    **({"due_local": describe_due_at(task["due_at"])} if task["due_at"] else {}),
                    "description": description[:MAX_TASK_DESCRIPTION_RESULT],
                    "description_truncated": len(description) > MAX_TASK_DESCRIPTION_RESULT,
                }
            )
        return ToolOutcome(
            True,
            f"Found {len(tasks)} task(s).",
            {"tasks": bounded_tasks, "truncated": len(tasks) > MAX_LIST_ITEMS},
        )

    def _set_task_completed(self, arguments: dict) -> ToolOutcome:
        task = set_task_completed(
            arguments["task_id"], arguments["completed"], self.database_path
        )
        status = "completed" if task["completed"] else "reopened"
        return ToolOutcome(True, f"Task {status}.", task)

    def _set_task_due_date(self, arguments: dict) -> ToolOutcome:
        task = update_task(arguments["task_id"], self.database_path, due_at=arguments["due_at"])
        if task["due_at"] is None:
            return ToolOutcome(True, "Task due date cleared.", task)
        due = describe_due_at(task["due_at"])
        return ToolOutcome(True, f"Task due date set to {due}.", {**task, "due_local": due})

    def _delete_task(self, arguments: dict) -> ToolOutcome:
        delete_task(arguments["task_id"], self.database_path)
        return ToolOutcome(True, "Task deleted.", {"task_id": arguments["task_id"]})

    def _list_notes(self, arguments: dict) -> ToolOutcome:
        notes = list_notes(self.database_path)
        return ToolOutcome(
            True,
            f"Found {len(notes)} note(s).",
            {"notes": notes[:MAX_LIST_ITEMS], "truncated": len(notes) > MAX_LIST_ITEMS},
        )

    def _get_note(self, arguments: dict) -> ToolOutcome:
        note = get_note(arguments["note_id"], self.database_path)
        content = note["content"]
        note["content"] = content[:MAX_NOTE_CONTENT_RESULT]
        note["content_truncated"] = len(content) > MAX_NOTE_CONTENT_RESULT
        note["content_characters"] = len(content)
        return ToolOutcome(True, "Note retrieved.", note)

    def _create_note(self, arguments: dict) -> ToolOutcome:
        note = create_note(
            arguments["title"].strip(), arguments["content"], self.database_path
        )
        return ToolOutcome(True, "Note created.", note)

    def _update_note(self, arguments: dict) -> ToolOutcome:
        note = update_note(
            arguments["note_id"],
            arguments["title"].strip(),
            arguments["content"],
            self.database_path,
        )
        return ToolOutcome(True, "Note updated.", note)

    def _delete_note(self, arguments: dict) -> ToolOutcome:
        delete_note(arguments["note_id"], self.database_path)
        return ToolOutcome(True, "Note deleted.", {"note_id": arguments["note_id"]})

    @staticmethod
    def _get_system_info(arguments: dict) -> ToolOutcome:
        now = datetime.now().astimezone()
        info = {
            "local_datetime": now.isoformat(timespec="seconds"),
            "timezone": now.tzname(),
            "battery": ToolRegistry._battery_status(),
            "memory": ToolRegistry._memory_status(),
            "disk": ToolRegistry._disk_status(),
        }
        return ToolOutcome(True, "Local system information collected.", info)

    @staticmethod
    def _battery_status() -> dict:
        if not sys_platform_is_macos():
            return {"available": False, "reason": "Battery status is only implemented for macOS."}
        executable = shutil.which("pmset")
        if executable is None:
            return {"available": False, "reason": "The pmset utility is unavailable."}
        try:
            result = subprocess.run(
                [executable, "-g", "batt"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "reason": "Battery status could not be read."}
        if result.returncode != 0:
            return {"available": False, "reason": "Battery status is unavailable."}
        output = result.stdout.strip()
        match = re.search(r"(\d{1,3})%", output)
        state = re.search(r"\b(charging|discharging|charged|AC Power|UPS Power)\b", output, re.I)
        if not match:
            return {"available": False, "reason": "No battery percentage was reported."}
        try:
            percentage = int(match.group(1))
        except ValueError:
            return {"available": False, "reason": "Battery percentage was unreadable."}
        return {
            "available": True,
            "percentage": percentage,
            "state": state.group(1) if state else "unknown",
        }

    @staticmethod
    def _memory_status() -> dict:
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            total_pages = os.sysconf("SC_PHYS_PAGES")
            available_pages = os.sysconf("SC_AVPHYS_PAGES")
            total = page_size * total_pages
            available = page_size * available_pages
            if total <= 0 or available < 0:
                raise ValueError("invalid memory counters")
            return {"available": True, "total_bytes": total, "available_bytes": available}
        except (OSError, ValueError, TypeError, KeyError):
            if sys_platform_is_macos():
                return ToolRegistry._mac_memory_status()
            return {"available": False, "reason": "Memory usage is unavailable."}

    @staticmethod
    def _mac_memory_status() -> dict:
        sysctl = shutil.which("sysctl")
        vm_stat = shutil.which("vm_stat")
        if not sysctl or not vm_stat:
            return {"available": False, "reason": "macOS memory utilities are unavailable."}
        try:
            total_result = subprocess.run(
                [sysctl, "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            stats_result = subprocess.run(
                [vm_stat],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if total_result.returncode != 0 or stats_result.returncode != 0:
                raise ValueError("macOS memory utilities returned an error")
            total_bytes = int(total_result.stdout.strip())
            page_size_match = re.search(r"page size of (\d+) bytes", stats_result.stdout)
            if not page_size_match:
                raise ValueError("vm_stat did not report its page size")
            page_size = int(page_size_match.group(1))
            page_counts = {}
            for label in ("Pages free", "Pages inactive", "Pages speculative"):
                match = re.search(rf"{re.escape(label)}:\s+(\d+)\.", stats_result.stdout)
                if not match:
                    raise ValueError(f"vm_stat did not report {label.lower()}")
                page_counts[label] = int(match.group(1))
            available_bytes = sum(page_counts.values()) * page_size
            if total_bytes <= 0 or available_bytes < 0:
                raise ValueError("vm_stat reported invalid memory counters")
            return {
                "available": True,
                "total_bytes": total_bytes,
                "available_bytes": available_bytes,
                "available_estimate": True,
            }
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"available": False, "reason": "macOS memory usage could not be read."}

    @staticmethod
    def _disk_status() -> dict:
        try:
            usage = shutil.disk_usage(Path.home())
            return {
                "available": True,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        except OSError:
            return {"available": False, "reason": "Disk usage is unavailable."}

    def _find_files(self, arguments: dict) -> ToolOutcome:
        try:
            found = computer_utilities.find_files(
                self.utilities,
                arguments["query"],
                arguments.get("folder"),
                arguments.get("file_type"),
            )
        except UtilityError as error:
            return _utility_failure(error)
        kind = f" {found['file_type']}" if found["file_type"] else ""
        message = (
            f"Searched {', '.join(found['folders_searched'])} (including subfolders) for"
            f"{kind} file names containing all of: {', '.join(found['query_terms'])}. "
        )
        if found["retry"]:
            retry = found["retry"]
            message += (
                f"None matched, so it searched again treating {', '.join(retry['type_words'])} as a "
                f"file type: {', '.join(retry['query_terms'])} in {'/'.join(retry['file_types'])} files. "
            )
        message += f"Found {len(found['results'])} file(s)."
        if found["incomplete_folders"]:
            message += (
                f" The {computer_utilities.MAX_SCANNED_ENTRIES:,}-entry search limit was reached "
                f"before finishing: {', '.join(found['incomplete_folders'])}; matches there may "
                "be missing. Searching one root with folder, or a more specific query, can help."
            )
        elif found["result_limit_reached"]:
            message += " The result limit was reached; more matches may exist."
        return ToolOutcome(True, message, found)

    def _open_application(self, arguments: dict) -> ToolOutcome:
        try:
            name = computer_utilities.resolve_app_name(self.utilities, arguments["name"])
        except UtilityError as error:
            return _utility_failure(error)
        if not computer_utilities.open_supported():
            return ToolOutcome(False, "Opening applications is only supported on macOS.")
        if not self.open_runner(["-a", name]):
            return ToolOutcome(False, f"macOS could not open {name}.")
        return ToolOutcome(True, f"Requested macOS to open {name}.", {"name": name})

    def _open_file(self, arguments: dict) -> ToolOutcome:
        # Re-check at execution time: the file may have changed while awaiting confirmation.
        try:
            display_path, file_path = computer_utilities.resolve_approved_file(
                self.utilities, arguments["path"]
            )
        except UtilityError as error:
            return _utility_failure(error)
        if not computer_utilities.open_supported():
            return ToolOutcome(False, "Opening files is only supported on macOS.")
        if not self.open_runner([str(file_path)]):
            return ToolOutcome(False, f"macOS could not open {display_path}.")
        return ToolOutcome(True, f"Requested macOS to open {display_path}.", {"path": display_path})

    def _open_website(self, arguments: dict) -> ToolOutcome:
        url = validate_http_url(arguments["url"])
        try:
            accepted = bool(self.browser_open(url, new=2, autoraise=True))
        except Exception:
            logger.exception("Default browser could not open a requested URL")
            return ToolOutcome(False, "The default browser could not open the requested URL.")
        if not accepted:
            return ToolOutcome(False, "The default browser did not accept the URL.")
        return ToolOutcome(True, "The default browser accepted the URL.", {"url": url})


def _utility_failure(error: UtilityError) -> ToolOutcome:
    return ToolOutcome(False, str(error))


def sys_platform_is_macos() -> bool:
    return os.name == "posix" and Path("/System/Library/CoreServices/SystemVersion.plist").exists()
