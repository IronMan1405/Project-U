"""Approved computer utilities: bounded file search in configured folders and app launching.

Only folders and applications listed in config.ini are reachable. The model never supplies
filesystem paths for searching (only configured folder labels), and launches use a fixed
system executable with argument lists, never a shell.
"""

import logging
import os
import re
import stat
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


MAX_SEARCH_DEPTH = 4
MAX_SCANNED_ENTRIES = 5_000
MAX_FILE_RESULTS = 20
SEARCH_CHUNK = 50  # entries examined per root before moving to the next root
MAX_QUERY_CHARS = 100
MAX_OPEN_PATH_CHARS = 1_024
OPEN_COMMAND = "/usr/bin/open"
# Document types U may open. Anything else (apps, scripts, installers, .command files,
# disk images, unknown types) is refused because opening it could run code.
OPENABLE_EXTENSIONS = frozenset(
    {".pdf", ".txt", ".md", ".csv", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg"}
)
OPEN_TIMEOUT_SECONDS = 10
# Directories macOS presents as single items; their contents are never searched.
PACKAGE_SUFFIXES = (
    ".app", ".bundle", ".framework", ".kext", ".pkg", ".plugin", ".photoslibrary",
)

_FILE_TYPE = re.compile(r"^\.?[A-Za-z0-9]{1,10}$")
_WORD = re.compile(r"[^\W_]+")
# Filler words dropped from natural-language queries ("my performance sheet file").
_QUERY_FILLER = frozenset({"a", "an", "the", "my", "file", "files", "named", "called"})
_FOLDER_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'+-]{0,63}$")

logger = logging.getLogger(__name__)


class UtilityError(ValueError):
    pass


@dataclass(frozen=True)
class UtilitySettings:
    folders: dict[str, Path] = field(default_factory=dict)
    apps: tuple[str, ...] = ()


def build_utility_settings(
    folders: dict[str, str],
    apps: list[str],
    home: Path | None = None,
) -> UtilitySettings:
    """Validate configured folders and apps, skipping (and logging) unsafe entries."""
    home = (home or Path.home()).resolve()
    approved_folders: dict[str, Path] = {}
    for label, raw_path in folders.items():
        label = label.strip()
        reason = _folder_rejection(label, raw_path, home, approved_folders)
        if reason:
            logger.warning("Ignoring utility folder %r: %s", label, reason)
            continue
        approved_folders[label] = Path(raw_path.strip()).expanduser().resolve(strict=True)

    approved_apps: list[str] = []
    for name in apps:
        name = name.strip()
        if not name:
            continue
        if not _APP_NAME.fullmatch(name):
            logger.warning("Ignoring utility application %r: invalid name", name)
            continue
        if name.casefold() not in {app.casefold() for app in approved_apps}:
            approved_apps.append(name)
    return UtilitySettings(approved_folders, tuple(approved_apps))


def _folder_rejection(
    label: str, raw_path: str, home: Path, existing: dict[str, Path]
) -> str | None:
    if not _FOLDER_LABEL.fullmatch(label):
        return "labels must be 1-40 letters, digits, spaces, '_' or '-'"
    if label.casefold() in {item.casefold() for item in existing}:
        return "duplicate label"
    expanded = Path(raw_path.strip()).expanduser()
    if not expanded.is_absolute():
        return "folder must be an absolute path or start with ~"
    try:
        resolved = expanded.resolve(strict=True)
    except OSError:
        return "folder does not exist"
    if not resolved.is_dir():
        return "not a directory"
    if resolved == home or not resolved.is_relative_to(home):
        return "folder must be inside, and not equal to, the home folder"
    relative_parts = resolved.relative_to(home).parts
    if relative_parts[0] == "Library" or any(part.startswith(".") for part in relative_parts):
        return "Library and hidden folders are not allowed"
    return None


def resolve_folder_label(settings: UtilitySettings, label: str) -> str:
    for configured in settings.folders:
        if configured.casefold() == label.casefold():
            return configured
    raise UtilityError("That folder is not an approved folder.")


def resolve_search_root(settings: UtilitySettings, label: str) -> str:
    """Like resolve_folder_label, but explains roots versus subfolders for find_files."""
    try:
        return resolve_folder_label(settings, label)
    except UtilityError:
        roots = ", ".join(settings.folders) or "none"
        raise UtilityError(
            f"{label!r} is not an approved root folder, so it cannot be used as folder. "
            f"Approved roots: {roots}. Subfolders inside an approved root are searched "
            "automatically: omit folder or pass the approved root, and matches appear as "
            "'<root>/<subfolder>/<file>'. Folders outside the approved roots cannot be searched."
        ) from None


def query_terms(query: str) -> list[str]:
    """Split a natural-language query into lowercase words, dropping filler words."""
    words = _WORD.findall(query.casefold())
    meaningful = [word for word in words if word not in _QUERY_FILLER]
    return meaningful or words


def name_matches(name: str, terms: list[str]) -> bool:
    """True when every term appears in the name, ignoring case, spaces, '_', '-', '.' and
    camelCase/PascalCase boundaries ("performance sheet" matches "PerformanceSheet.pdf")."""
    compact = "".join(_WORD.findall(name.casefold()))
    return all(term in compact for term in terms)


def normalize_file_type(file_type: str | None) -> str | None:
    if file_type is None:
        return None
    if not _FILE_TYPE.fullmatch(file_type):
        raise UtilityError("file_type must be an extension such as pdf or .docx.")
    return "." + file_type.lstrip(".").lower()


def _is_hidden(name: str, entry_stat: os.stat_result) -> bool:
    return name.startswith(".") or bool(getattr(entry_stat, "st_flags", 0) & stat.UF_HIDDEN)


# Words people use for a kind of file rather than part of its name. Used only for a
# retry when a multi-word search finds nothing ("budget spreadsheets" -> budget in .xlsx...).
TYPE_WORDS = {
    **dict.fromkeys(("spreadsheet", "spreadsheets"), (".xlsx", ".xls", ".csv", ".numbers")),
    **dict.fromkeys(("pdf", "pdfs"), (".pdf",)),
    **dict.fromkeys(("document", "documents", "doc", "docs"), (".docx", ".doc", ".pdf", ".txt", ".md", ".rtf", ".pages")),
    **dict.fromkeys(("image", "images", "photo", "photos", "picture", "pictures"), (".png", ".jpg", ".jpeg", ".heic", ".gif")),
    **dict.fromkeys(("presentation", "presentations", "slides", "slideshow"), (".pptx", ".ppt", ".key")),
}


def find_files(
    settings: UtilitySettings,
    query: str,
    folder: str | None = None,
    file_type: str | None = None,
) -> dict:
    """Keyword file-name search of approved roots; contents are never read.

    If a multi-word search finds nothing and some words only describe a kind of file
    (TYPE_WORDS), it is retried once with those words as an extension filter, using what
    is left of the same entry budget.
    """
    if len(query) > MAX_QUERY_CHARS:
        raise UtilityError(f"query must be at most {MAX_QUERY_CHARS} characters.")
    terms = query_terms(query)
    if not terms:
        raise UtilityError("query must contain at least one letter or digit.")
    suffix = normalize_file_type(file_type)
    labels = [resolve_search_root(settings, folder)] if folder else list(settings.folders)
    if not labels:
        raise UtilityError("No approved folders are configured.")

    scan = _scan(settings, labels, terms, {suffix} if suffix else None, MAX_SCANNED_ENTRIES)
    examined = scan["examined"]
    retry = None
    name_terms = [term for term in terms if term not in TYPE_WORDS]
    type_terms = [term for term in terms if term in TYPE_WORDS]
    remaining = MAX_SCANNED_ENTRIES - examined
    if not scan["results"] and suffix is None and type_terms and name_terms and remaining > 0:
        suffixes = {extension for term in type_terms for extension in TYPE_WORDS[term]}
        scan = _scan(settings, labels, name_terms, suffixes, remaining)
        examined += scan["examined"]
        retry = {"query_terms": name_terms, "file_types": sorted(suffixes), "type_words": type_terms}
    return {
        "query_terms": terms,
        "file_type": suffix,
        "folders_searched": labels,
        "results": scan["results"],
        "truncated": scan["budget_exhausted"] or scan["results_full"],
        "result_limit_reached": scan["results_full"],
        "incomplete_folders": scan["incomplete"],
        "entries_examined": examined,
        "retry": retry,
    }


def _scan(
    settings: UtilitySettings,
    labels: list[str],
    terms: list[str],
    suffixes: set[str] | None,
    budget: int,
) -> dict:
    """Walk the approved roots breadth-first and in turn, SEARCH_CHUNK entries at a time,
    so the entry budget is spread across roots instead of being spent on the first one."""
    directories = {label: deque([(settings.folders[label], 0)]) for label in labels}
    pending = {label: deque() for label in labels}  # (DirEntry, depth) awaiting examination
    has_work = lambda label: bool(directories[label] or pending[label])
    results: list[dict] = []
    examined = 0
    budget_exhausted = False
    results_full = False
    while not (budget_exhausted or results_full) and any(has_work(label) for label in labels):
        for label in labels:
            root = settings.folders[label]
            taken = 0
            while taken < SEARCH_CHUNK and has_work(label) and not (budget_exhausted or results_full):
                if not pending[label]:
                    directory, depth = directories[label].popleft()
                    try:
                        with os.scandir(directory) as iterator:
                            entries = sorted(iterator, key=lambda item: item.name.casefold())
                    except OSError:
                        continue
                    pending[label].extend((entry, depth) for entry in entries)
                    continue
                if examined >= budget:
                    budget_exhausted = True
                    break
                entry, depth = pending[label].popleft()
                examined += 1
                taken += 1
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_hidden(entry.name, entry_stat) or stat.S_ISLNK(entry_stat.st_mode):
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if depth + 1 < MAX_SEARCH_DEPTH and not entry.name.lower().endswith(PACKAGE_SUFFIXES):
                        directories[label].append((Path(entry.path), depth + 1))
                    continue
                if not stat.S_ISREG(entry_stat.st_mode) or not name_matches(entry.name, terms):
                    continue
                if suffixes and Path(entry.name).suffix.lower() not in suffixes:
                    continue
                if len(results) >= MAX_FILE_RESULTS:
                    results_full = True
                    break
                relative = Path(entry.path).relative_to(root).as_posix()
                results.append(
                    {
                        "path": f"{label}/{relative}",
                        "size_bytes": entry_stat.st_size,
                        "modified": datetime.fromtimestamp(entry_stat.st_mtime)
                        .astimezone()
                        .isoformat(timespec="seconds"),
                    }
                )
    return {
        "results": results,
        "examined": examined,
        "budget_exhausted": budget_exhausted,
        "results_full": results_full,
        "incomplete": [label for label in labels if has_work(label)] if budget_exhausted else [],
    }


def resolve_approved_file(settings: UtilitySettings, value: str) -> tuple[str, Path]:
    """Map 'Folder/relative/path' (as returned by find_files) to a safe, existing document.

    Every component is checked with lstat, so no symbolic link is followed anywhere in the
    path; hidden and package components are refused; the final item must be a regular file
    with an approved extension and must still resolve inside the approved folder.
    Returns the canonical display path and the absolute file path.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_OPEN_PATH_CHARS:
        raise UtilityError("path must be a file path returned by find_files.")
    if "\0" in value or "\\" in value:
        raise UtilityError("path contains unsupported characters.")
    label_text, separator, relative_text = value.partition("/")
    if not separator or not relative_text:
        raise UtilityError("path must look like 'Folder/relative/path' from find_files.")
    label = resolve_folder_label(settings, label_text)
    parts = relative_text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UtilityError("path must not contain empty, '.' or '..' components.")

    root = settings.folders[label]
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise UtilityError("The approved folder is unavailable.") from error
    if not stat.S_ISDIR(root_stat.st_mode) or root.resolve() != root:
        raise UtilityError("The approved folder is unavailable.")

    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            part_stat = os.lstat(current)
        except OSError as error:
            raise UtilityError("That file does not exist in the approved folder.") from error
        if stat.S_ISLNK(part_stat.st_mode):
            raise UtilityError("Symbolic links cannot be opened.")
        if _is_hidden(part, part_stat):
            raise UtilityError("Hidden files and folders cannot be opened.")
        is_last = index == len(parts) - 1
        if not is_last:
            if not stat.S_ISDIR(part_stat.st_mode):
                raise UtilityError("That file does not exist in the approved folder.")
            if part.lower().endswith(PACKAGE_SUFFIXES):
                raise UtilityError("Files inside application or system packages cannot be opened.")
        elif not stat.S_ISREG(part_stat.st_mode):
            raise UtilityError("Only regular files can be opened.")

    if current.suffix.lower() not in OPENABLE_EXTENSIONS:
        allowed = ", ".join(sorted(OPENABLE_EXTENSIONS))
        raise UtilityError(f"Only these document types can be opened: {allowed}.")
    if current.resolve(strict=True) != current or not current.is_relative_to(root):
        raise UtilityError("That file is outside the approved folder.")
    return f"{label}/{relative_text}", current


def resolve_app_name(settings: UtilitySettings, name: str) -> str:
    for app in settings.apps:
        if app.casefold() == name.strip().casefold():
            return app
    raise UtilityError("That application is not on the approved list.")


def open_supported() -> bool:
    return (
        os.name == "posix"
        and Path("/System/Library/CoreServices/SystemVersion.plist").exists()
        and Path(OPEN_COMMAND).is_file()
    )


def run_open(arguments: list[str]) -> bool:
    """Run macOS `open` with a fixed executable and an argument list (no shell)."""
    try:
        result = subprocess.run(
            [OPEN_COMMAND, *arguments],
            capture_output=True,
            text=True,
            timeout=OPEN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
