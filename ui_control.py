"""Targeted interaction with named UI elements in approved apps, through macOS Accessibility.

This is a foundation, not computer control. Every action names a structured target:
application (from [ui_control] apps in config.ini), optional window, element role (button,
menu item, text field, ...), element name, and action (press, focus, set_text). The element
must be found exactly once; otherwise K.R.I.S. lists the candidates or says it is missing.
There are no coordinates, screenshots, keystrokes, mouse movement, drag and drop, scripts or
multi-step plans.

Mechanism: the native Accessibility API (AXUIElement, in ApplicationServices) through ctypes:
an app's element tree is read with AXUIElementCopyAttributeValue (AXRole, AXSubrole, AXTitle,
AXDescription, AXIdentifier, AXEnabled, AXChildren, AXWindows, AXMenuBar), a press is
AXUIElementPerformAction(AXPress), focus sets AXFocused, and text entry sets AXValue on a
text field (never on a secure/password field). A found element is identified by its path of
child indexes, and that path is checked again (same role and name) just before acting.

Permission: macOS must list the app that runs K.R.I.S. (for example Terminal) under System
Settings > Privacy & Security > Accessibility. Without it every call reports exactly that;
K.R.I.S. never prompts on its own or works around it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import computer_utilities

logger = logging.getLogger(__name__)

# User-facing role -> Accessibility roles, and the actions each allows.
ROLES = {
    "button": ("AXButton",),
    "menu item": ("AXMenuItem", "AXMenuBarItem"),
    "text field": ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"),
    "checkbox": ("AXCheckBox",),
    "radio button": ("AXRadioButton",),
    "pop-up button": ("AXPopUpButton", "AXMenuButton"),
    "link": ("AXLink",),
    "tab": ("AXTab", "AXRadioButton"),
}
ACTIONS = ("press", "focus", "set_text")
ACTION_ROLES = {
    "press": ("button", "menu item", "checkbox", "radio button", "pop-up button", "link", "tab"),
    "focus": tuple(ROLES),
    "set_text": ("text field",),
}
# Never controllable, whatever the configuration: they could change security settings, run
# code, or approve actions (a browser can show K.R.I.S.'s own approval panel or Gmail).
DENIED_APPS = frozenset({
    "system settings", "system preferences", "terminal", "iterm", "iterm2", "warp", "keychain access",
    "script editor", "automator", "installer", "shortcuts", "activity monitor", "disk utility",
    "safari", "google chrome", "firefox", "arc", "microsoft edge", "brave browser", "opera", "opera gx",
})
MAX_DEPTH = 12
MAX_NODES = 2500
MAX_LISTED = 80
MAX_TEXT = 1000

PERMISSION_HELP = ("K.R.I.S. does not have Accessibility permission. In System Settings > Privacy & Security > "
                   "Accessibility, turn on the app that runs K.R.I.S. (for example Terminal), then try again.")


class UIControlError(Exception):
    """kind: not_configured, not_allowed, not_running, permission, not_found, ambiguous, invalid, failed."""

    def __init__(self, kind: str, message: str, candidates: list | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.candidates = candidates or []


@dataclass(frozen=True)
class UIControlSettings:
    apps: tuple[str, ...] = ()


def build_ui_control_settings(raw_apps: str) -> UIControlSettings:
    apps = []
    for name in (raw_apps or "").split(","):
        name = name.strip()
        if not name:
            continue
        if name.casefold() in DENIED_APPS:
            logger.warning("Ignoring ui_control app %r: it can never be controlled", name)
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'+-]{0,63}", name) and name.casefold() not in {a.casefold() for a in apps}:
            apps.append(name)
    return UIControlSettings(tuple(apps))


@dataclass
class Element:
    """One element found in an app: its path (child indexes from the app), role and labels."""
    path: tuple[int, ...]
    role: str
    subrole: str = ""
    title: str = ""
    description: str = ""
    identifier: str = ""
    enabled: bool = True
    window: str = ""
    menu: str = ""
    secure: bool = False
    children: list = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.title or self.description or self.identifier

    def kind(self) -> str | None:
        for label, roles in ROLES.items():
            if self.role in roles and not (label == "tab" and self.subrole != "AXTabButton" and self.role == "AXRadioButton"):
                return label
        return None

    def view(self) -> dict:
        view = {"role": self.kind() or self.role, "name": self.name, "enabled": self.enabled}
        if self.window:
            view["window"] = self.window
        if self.menu:
            view["menu"] = self.menu
        return view


class UIController:
    """Validates structured targets and carries out one action through a backend."""

    def __init__(self, settings: UIControlSettings | None = None, backend=None):
        self.settings = settings or UIControlSettings()
        self.backend = backend if backend is not None else AXBackend()

    def app(self, name: str) -> str:
        if not self.settings.apps:
            raise UIControlError("not_configured", "UI interaction is off: no apps are listed under [ui_control] in config.ini.")
        for app in self.settings.apps:
            if app.casefold() == str(name).strip().casefold():
                return app
        raise UIControlError("not_allowed", f"K.R.I.S. cannot interact with {str(name)[:60]} yet. Apps it may interact "
                             f"with: {', '.join(self.settings.apps)} (set under [ui_control] in config.ini).")

    def _elements(self, app: str) -> list[Element]:
        if not self.backend.trusted():
            raise UIControlError("permission", PERMISSION_HELP)
        pid = self.backend.pid(app)
        if pid is None:
            raise UIControlError("not_running", f"{app} is not running.")
        return self.backend.elements(pid)

    def inspect(self, app: str, role: str | None = None, window: str | None = None, name: str | None = None) -> dict:
        """Named, actionable elements of an app (optionally one role, window or name), read only."""
        app = self.app(app)
        if role is not None and role not in ROLES:
            raise UIControlError("invalid", f"role must be one of {', '.join(ROLES)}.")
        listed = []
        for element in self._elements(app):
            kind = element.kind()
            if kind is None or not element.name:
                continue
            if role and kind != role:
                continue
            if window and element.window.casefold() != window.casefold():
                continue
            if name and name.casefold() not in element.name.casefold():
                continue
            listed.append(element.view())
        return {"app": app, "elements": listed[:MAX_LISTED], "more": len(listed) > MAX_LISTED}

    def find(self, app: str, role: str, name: str, window: str | None = None, menu: str | None = None) -> Element:
        app = self.app(app)
        if role not in ROLES:
            raise UIControlError("invalid", f"role must be one of {', '.join(ROLES)}.")
        wanted = " ".join(str(name).split()).casefold()
        if not wanted:
            raise UIControlError("invalid", "Name the element.")
        matches = [e for e in self._elements(app) if e.kind() == role and e.name.casefold() == wanted
                   and (window is None or e.window.casefold() == window.casefold())
                   and (menu is None or e.menu.casefold() == menu.casefold())]
        if not matches:
            raise UIControlError("not_found", f"No {role} named \"{name}\" was found in {app}"
                                 + (f" (window \"{window}\")" if window else "") + ".")
        if len(matches) > 1:
            raise UIControlError("ambiguous", f"{len(matches)} {role}s named \"{name}\" were found in {app}; say which "
                                 "window or menu.", [m.view() for m in matches[:10]])
        return matches[0]

    def act(self, app: str, role: str, name: str, action: str, text: str | None = None,
            window: str | None = None, menu: str | None = None) -> dict:
        if action not in ACTIONS:
            raise UIControlError("invalid", f"action must be one of {', '.join(ACTIONS)}.")
        if role not in ACTION_ROLES[action]:
            raise UIControlError("invalid", f"A {role} cannot be used with {action}.")
        if action == "set_text" and (not isinstance(text, str) or len(text) > MAX_TEXT):
            raise UIControlError("invalid", f"Give the text to enter (at most {MAX_TEXT} characters).")
        element = self.find(app, role, name, window, menu)
        if not element.enabled:
            raise UIControlError("failed", f"The {role} \"{element.name}\" is disabled.")
        if action == "set_text" and element.secure:
            raise UIControlError("invalid", "K.R.I.S. never types into password fields.")
        app = self.app(app)
        pid = self.backend.pid(app)
        result = self.backend.perform(pid, element, action, text)
        return {"app": app, "role": role, "name": element.name, "action": action, **result}


# --- The real backend: Accessibility through ctypes ----------------------------------------------

class AXBackend:
    """AXUIElement access. Every CoreFoundation object created or copied here is released."""

    def __init__(self):
        self._ax = self._cf = None

    def _load(self):
        if self._ax is not None:
            return
        if sys.platform != "darwin":
            raise UIControlError("not_configured", "UI interaction needs macOS.")
        ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp, i32, i64 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_long
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        ax.AXUIElementCreateApplication.restype, ax.AXUIElementCreateApplication.argtypes = vp, [i32]
        ax.AXUIElementCopyAttributeValue.restype = i32
        ax.AXUIElementCopyAttributeValue.argtypes = [vp, vp, ctypes.POINTER(vp)]
        ax.AXUIElementSetAttributeValue.restype, ax.AXUIElementSetAttributeValue.argtypes = i32, [vp, vp, vp]
        ax.AXUIElementPerformAction.restype, ax.AXUIElementPerformAction.argtypes = i32, [vp, vp]
        ax.AXUIElementSetMessagingTimeout.restype, ax.AXUIElementSetMessagingTimeout.argtypes = i32, [vp, ctypes.c_float]
        cf.CFStringCreateWithCString.restype, cf.CFStringCreateWithCString.argtypes = vp, [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype, cf.CFStringGetLength.argtypes = i64, [vp]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, i64, ctypes.c_uint32]
        cf.CFGetTypeID.restype, cf.CFGetTypeID.argtypes = ctypes.c_ulong, [vp]
        cf.CFStringGetTypeID.restype = cf.CFArrayGetTypeID.restype = cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetCount.restype, cf.CFArrayGetCount.argtypes = i64, [vp]
        cf.CFArrayGetValueAtIndex.restype, cf.CFArrayGetValueAtIndex.argtypes = vp, [vp, i64]
        cf.CFBooleanGetValue.restype, cf.CFBooleanGetValue.argtypes = ctypes.c_bool, [vp]
        cf.CFRetain.restype, cf.CFRetain.argtypes = vp, [vp]
        cf.CFRelease.argtypes = [vp]
        self._true = vp.in_dll(cf, "kCFBooleanTrue")
        self._ax, self._cf = ax, cf
        self._strings: dict[str, int] = {}

    def _s(self, text: str):
        """A CFString for an attribute or action name (kept for the life of the backend)."""
        if text not in self._strings:
            self._strings[text] = self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)
        return self._strings[text]

    def trusted(self) -> bool:
        try:
            self._load()
        except (OSError, UIControlError):
            return False
        return bool(self._ax.AXIsProcessTrusted())

    def pid(self, app: str) -> int | None:
        bundle = computer_utilities.find_application(app)
        if bundle is None:
            return None
        executable = str(bundle / "Contents" / "MacOS") + "/"
        try:
            output = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5,
                                    check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in output.splitlines():
            pid, _, command = line.strip().partition(" ")
            if command.strip().startswith(executable) and pid.isdigit():
                return int(pid)
        return None

    def _copy(self, element, attribute: str):
        value = ctypes.c_void_p()
        if self._ax.AXUIElementCopyAttributeValue(element, self._s(attribute), ctypes.byref(value)) != 0:
            return None
        return value.value

    def _text(self, element, attribute: str) -> str:
        value = self._copy(element, attribute)
        if not value:
            return ""
        try:
            if self._cf.CFGetTypeID(value) != self._cf.CFStringGetTypeID():
                return ""
            size = self._cf.CFStringGetLength(value) * 4 + 1
            buffer = ctypes.create_string_buffer(size)
            return buffer.value.decode("utf-8", "replace")[:200] if self._cf.CFStringGetCString(value, buffer, size, 0x08000100) else ""
        finally:
            self._cf.CFRelease(value)

    def _flag(self, element, attribute: str, default: bool) -> bool:
        value = self._copy(element, attribute)
        if not value:
            return default
        try:
            return bool(self._cf.CFBooleanGetValue(value)) if self._cf.CFGetTypeID(value) == self._cf.CFBooleanGetTypeID() else default
        finally:
            self._cf.CFRelease(value)

    def _children(self, element, attribute: str = "AXChildren") -> list:
        value = self._copy(element, attribute)
        if not value:
            return []
        if self._cf.CFGetTypeID(value) != self._cf.CFArrayGetTypeID():
            self._cf.CFRelease(value)
            return []
        items = [self._cf.CFRetain(self._cf.CFArrayGetValueAtIndex(value, i)) for i in range(self._cf.CFArrayGetCount(value))]
        self._cf.CFRelease(value)
        return items

    def _roots(self, application) -> list:
        """The app's windows (path root 0) and menu bar (path root 1)."""
        menu_bar = self._copy(application, "AXMenuBar")
        return [self._children(application, "AXWindows"), [menu_bar] if menu_bar else []]

    def elements(self, pid: int) -> list[Element]:
        self._load()
        application = self._ax.AXUIElementCreateApplication(pid)
        self._ax.AXUIElementSetMessagingTimeout(application, ctypes.c_float(1.0))
        found: list[Element] = []
        try:
            for root, items in enumerate(self._roots(application)):
                for index, item in enumerate(items):
                    window = self._text(item, "AXTitle") if root == 0 else ""
                    self._walk(item, (root, index), window, "", 0, found)
        finally:
            self._cf.CFRelease(application)
        return found

    def _walk(self, element, path, window, menu, depth, found) -> None:
        try:
            if len(found) >= MAX_NODES:
                return
            role = self._text(element, "AXRole")
            node = Element(path=path, role=role, subrole=self._text(element, "AXSubrole"),
                           title=self._text(element, "AXTitle"), description=self._text(element, "AXDescription"),
                           identifier=self._text(element, "AXIdentifier"), enabled=self._flag(element, "AXEnabled", True),
                           window=window, menu=menu)
            node.secure = node.subrole == "AXSecureTextField"
            found.append(node)
            if depth < MAX_DEPTH:
                child_menu = node.title if role in ("AXMenuBarItem", "AXMenu") and node.title else menu
                for index, child in enumerate(self._children(element)):
                    self._walk(child, path + (index,), window, child_menu, depth + 1, found)
        finally:
            self._cf.CFRelease(element)

    def _resolve(self, application, path):
        """The element at a path (retained), or None if the tree changed."""
        roots = self._roots(application)
        try:
            items = roots[path[0]]
            current = self._cf.CFRetain(items[path[1]])
        except IndexError:
            current = None
        for group in roots:
            for item in group:
                self._cf.CFRelease(item)
        for index in path[2:]:
            if current is None:
                return None
            children = self._children(current)
            self._cf.CFRelease(current)
            current = self._cf.CFRetain(children[index]) if index < len(children) else None
            for child in children:
                self._cf.CFRelease(child)
        return current

    def perform(self, pid: int, target: Element, action: str, text: str | None) -> dict:
        self._load()
        application = self._ax.AXUIElementCreateApplication(pid)
        try:
            element = self._resolve(application, target.path)
            if element is None:
                raise UIControlError("not_found", "The element moved or disappeared; inspect the app again.")
            try:
                # The same element must still be there: same role and labels.
                if (self._text(element, "AXRole"), self._text(element, "AXTitle"), self._text(element, "AXDescription")) != \
                        (target.role, target.title, target.description):
                    raise UIControlError("not_found", "The window changed since it was inspected; nothing was done.")
                if action == "press":
                    error = self._ax.AXUIElementPerformAction(element, self._s("AXPress"))
                    if error != 0:
                        raise UIControlError("failed", f"macOS refused to press it (Accessibility error {error}).")
                    return {"verified": "macOS accepted the press"}
                if action == "focus":
                    error = self._ax.AXUIElementSetAttributeValue(element, self._s("AXFocused"), self._true)
                    if error != 0:
                        raise UIControlError("failed", f"macOS refused to focus it (Accessibility error {error}).")
                    return {"verified": "focused" if self._flag(element, "AXFocused", False) else "focus requested; not confirmed"}
                value = self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)
                try:
                    error = self._ax.AXUIElementSetAttributeValue(element, self._s("AXValue"), value)
                finally:
                    self._cf.CFRelease(value)
                if error != 0:
                    raise UIControlError("failed", f"macOS refused to set the text (Accessibility error {error}).")
                written = self._text(element, "AXValue")
                return {"verified": "text set and read back" if written == text[:200] else "text set; it reads back differently"}
            finally:
                self._cf.CFRelease(element)
        finally:
            self._cf.CFRelease(application)
