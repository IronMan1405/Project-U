"""Several Google accounts, each with a local name ("personal", "work", ...), one of them the default.

Each account is an ordinary GoogleWorkspaceProvider with its own Keychain item, so tokens,
access tokens and caches never mix. The original single account keeps the Keychain item it
always used ("oauth") and becomes the default under the name in config.ini's
[google] default_account (or "primary"): nothing is migrated, moved or deleted, and it does
not have to reconnect. Further accounts use the item "oauth:<name>".

The list of accounts (names, the email each was verified as, which Keychain item, which is the
default) is kept in data/google_accounts.json. It never contains tokens. A name is only a local
label: the email shown for it always comes from Google's verified sign-in, and a mismatch
between the email recorded at connection and the one in the stored token is reported.

Connecting refuses, without changing anything, a sign-in whose email is already connected
under another name, or a reconnection of an existing name with a different Google account.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from google_workspace import GoogleError, GoogleSettings, GoogleWorkspaceProvider, KeychainTokenStore

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(__file__).resolve().parent / "data" / "google_accounts.json"
LEGACY_ITEM = "oauth"
DEFAULT_NAME = "primary"
MAX_ACCOUNTS = 6
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
ALL = "all"


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name)) and name.casefold() != ALL


class GoogleAccounts:
    def __init__(
        self,
        settings: GoogleSettings | None = None,
        *,
        default_name: str | None = None,
        registry_path: Path | None = REGISTRY_PATH,
        provider_factory: Callable[[str], GoogleWorkspaceProvider] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings or GoogleSettings()
        self._path = registry_path
        self._clock = clock
        self._lock = threading.RLock()
        self._factory = provider_factory or (
            lambda item: GoogleWorkspaceProvider(self.settings, store=KeychainTokenStore(account=item)))
        self.redirect_uri = "http://127.0.0.1:5000/oauth/google/callback"  # updated by each UI connect
        self._pending: dict[str, tuple[str, GoogleWorkspaceProvider]] = {}  # state -> (name, provider)
        seed = default_name if default_name and valid_name(default_name) else DEFAULT_NAME
        self._registry = self._load(seed)
        self._providers = {entry["name"]: self._factory(entry["item"]) for entry in self._registry["accounts"]}

    @classmethod
    def single(cls, provider: GoogleWorkspaceProvider, name: str = DEFAULT_NAME) -> "GoogleAccounts":
        """One account around an existing provider (tests and callers that predate accounts)."""
        accounts = cls(provider.settings, default_name=name, registry_path=None,
                       provider_factory=lambda item: provider)
        return accounts

    # --- Registry file ---------------------------------------------------------------------

    def _load(self, seed: str) -> dict:
        fresh = {"version": 1, "default": seed, "accounts": [{"name": seed, "item": LEGACY_ITEM, "email": None}]}
        if self._path is None:
            return fresh
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # First start: the existing connection is the default account. Nothing is written until
            # something changes (an account is connected, the default is chosen, one is disconnected).
            return fresh
        except (OSError, ValueError):
            logger.warning("Google accounts: %s could not be read; using the original account only.", self._path.name)
            return fresh
        accounts, items, names = [], set(), set()
        for entry in data.get("accounts") or []:
            if not isinstance(entry, dict):
                continue
            name, item = entry.get("name"), entry.get("item")
            if (not valid_name(name) or name.casefold() in names or item in items
                    or item not in (LEGACY_ITEM, f"oauth:{str(name).casefold()}")):
                logger.warning("Google accounts: ignoring an invalid entry.")
                continue
            email = entry.get("email") if isinstance(entry.get("email"), str) else None
            accounts.append({"name": name, "item": item, "email": email})
            names.add(name.casefold())
            items.add(item)
        if not accounts:
            return fresh
        default = data.get("default")
        if not any(entry["name"] == default for entry in accounts):
            default = accounts[0]["name"]
        return {"version": 1, "default": default, "accounts": accounts}

    def _save(self, registry: dict | None = None) -> None:
        if self._path is None:
            return
        registry = registry or self._registry
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".google_accounts.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(registry, file, indent=2)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise GoogleError("storage", "The list of Google accounts could not be saved.")

    # --- Lookup ----------------------------------------------------------------------------

    @property
    def names(self) -> list[str]:
        return [entry["name"] for entry in self._registry["accounts"]]

    @property
    def default(self) -> str:
        return self._registry["default"]

    @property
    def multiple(self) -> bool:
        return len(self._registry["accounts"]) > 1

    def _entry(self, name: str) -> dict:
        return next(entry for entry in self._registry["accounts"] if entry["name"] == name)

    def resolve(self, reference: str | None) -> str:
        """An account name from a name or an email (any case); None means the default."""
        if reference is None or not str(reference).strip():
            return self.default
        wanted = str(reference).strip().casefold()
        for entry in self._registry["accounts"]:
            if entry["name"].casefold() == wanted or (entry["email"] or "").casefold() == wanted:
                return entry["name"]
        raise GoogleError("unknown_account", f"There is no connected Google account called \"{str(reference)[:40]}\". "
                          f"Accounts: {', '.join(self.names)}.")

    def provider(self, name: str | None = None) -> GoogleWorkspaceProvider:
        return self._providers[self.resolve(name)]

    def label(self, name: str) -> str:
        """How an account is named to the user: its local name and verified email."""
        email = self.email(name)
        return f"{name} ({email})" if email else name

    def email(self, name: str) -> str | None:
        stored = self._entry(name)["email"]
        if stored:
            return stored
        try:
            return self._providers[name].status().get("account")
        except GoogleError:
            return None

    def mentioned(self, text: str) -> list[str]:
        """Accounts the text names, by local name or email (whole words)."""
        folded = (text or "").casefold()
        found = []
        for entry in self._registry["accounts"]:
            words = [entry["name"].casefold()] + ([entry["email"].casefold()] if entry["email"] else [])
            if any(re.search(rf"(?<![\w@.-]){re.escape(word)}(?![\w@-])", folded) for word in words):
                found.append(entry["name"])
        return found

    # --- Status ----------------------------------------------------------------------------

    def account_status(self, name: str) -> dict:
        entry = self._entry(name)
        status = self._providers[name].status()
        verified = status.get("account")
        mismatch = bool(entry["email"] and verified and entry["email"].casefold() != verified.casefold())
        if verified and not entry["email"] and status.get("connected"):
            entry["email"] = verified  # the original account's verified email (in memory; saved with the next change)
        message = status.get("message")
        if mismatch:
            message = (f"This account was connected as {entry['email']}, but its stored sign-in is for {verified}. "
                       "Check it, then disconnect and connect it again.")
        return {"name": name, "email": verified or entry["email"], "default": name == self.default,
                "connected": bool(status.get("connected")) and not mismatch, "mismatch": mismatch,
                "reconnect_required": bool(status.get("reconnect_required")), "capabilities": status.get("capabilities"),
                "message": message}

    def status(self) -> dict:
        """The default account's status (the shape the UI always used) plus every account."""
        accounts = [self.account_status(name) for name in self.names]
        default = next(account for account in accounts if account["default"])
        base = self._providers[self.default].status()
        base.update(connected=default["connected"], default_account=self.default, accounts=accounts)
        if default["mismatch"]:
            base["message"] = default["message"]
        return base

    def connected(self) -> list[str]:
        return [account["name"] for account in (self.account_status(name) for name in self.names) if account["connected"]]

    def check(self, name: str) -> GoogleWorkspaceProvider:
        """The provider for an account that is safe to use (its identity still matches)."""
        status = self.account_status(name)
        if status["mismatch"]:
            raise GoogleError("mismatch", status["message"], True)
        return self._providers[name]

    # --- Connecting ------------------------------------------------------------------------

    def begin_authorization(self, redirect_uri: str | None = None, name: str | None = None) -> str:
        """Start Google sign-in for an existing account name (reconnect) or a new one."""
        redirect_uri = redirect_uri or self.redirect_uri
        with self._lock:
            if name is None:
                name = self.default
            existing = next((entry for entry in self._registry["accounts"] if entry["name"].casefold() == str(name).casefold()), None)
            if existing is not None:
                name, provider = existing["name"], self._providers[existing["name"]]
            else:
                if not valid_name(name):
                    raise GoogleError("invalid", "Name the account with a letter followed by up to 23 letters, "
                                      "digits, '-' or '_' (for example work or bits).")
                if len(self._registry["accounts"]) >= MAX_ACCOUNTS:
                    raise GoogleError("invalid", f"At most {MAX_ACCOUNTS} Google accounts can be connected.")
                provider = self._factory(f"oauth:{name.casefold()}")
            url = provider.begin_authorization(redirect_uri)
            state = re.search(r"[?&]state=([^&]+)", url).group(1)
            self._pending = {key: value for key, value in self._pending.items() if value[1].has_pending(key)}
            self._pending[state] = (name, provider)
        return url  # a new account is added only after a verified sign-in

    def complete_authorization(self, state: str | None, code: str | None, error: str | None) -> dict:
        """Finish a sign-in; returns {"name", "email", "new"}. Refuses mismatched identities."""
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if pending is None:
            # A sign-in started before this change (or an expired one): the default provider decides.
            pending = (self.default, self._providers[self.default])
        name, provider = pending
        is_new = name not in self._providers

        def accept(email: str) -> None:
            with self._lock:
                for entry in self._registry["accounts"]:
                    other = entry["email"] or (self.email(entry["name"]) if entry["name"] != name else None)
                    if entry["name"] != name and other and other.casefold() == email.casefold():
                        refusal = GoogleError("duplicate", f"{email} is already connected as {entry['name']}. "
                                              f"Nothing was changed for {name}.")
                        refusal.revoke_grant = False  # revoking would also disconnect that account
                        raise refusal
                if not is_new:
                    expected = self._entry(name)["email"] or self.email(name)
                    if expected and expected.casefold() != email.casefold():
                        raise GoogleError("mismatch", f"{name} is connected to {expected}, but you signed in as "
                                          f"{email}. Nothing was changed; disconnect {name} first to use a different "
                                          "Google account.")

        provider.complete_authorization(state, code, error, accept_identity=accept)
        email = provider.status().get("account")
        with self._lock:
            if is_new:
                self._registry["accounts"].append({"name": name, "item": f"oauth:{name.casefold()}", "email": email})
                self._providers[name] = provider
            else:
                self._entry(name)["email"] = email
            self._save()
        logger.info("Google account %s connected.", name)  # the name only; never the token or email
        return {"name": name, "email": email, "new": is_new}

    def set_default(self, name: str) -> str:
        with self._lock:
            name = self.resolve(name)
            if not self.account_status(name)["connected"]:
                raise GoogleError("not_connected", f"{name} is not connected, so it cannot be the default.")
            self._registry["default"] = name
            self._save()
        return name

    def disconnect(self, name: str | None = None) -> dict:
        """Forget K.R.I.S.'s connection to an account (revoke and delete its token). The Google
        account itself is untouched. The original account keeps its name, disconnected."""
        with self._lock:
            name = self.resolve(name)
            self._providers[name].disconnect()
            entry = self._entry(name)
            if entry["item"] == LEGACY_ITEM or (name == self.default and not self.multiple):
                entry["email"] = None
                others = [other for other in self.connected() if other != name]
                if name == self._registry["default"] and others:
                    self._registry["default"] = others[0]
            else:
                self._registry["accounts"].remove(entry)
                del self._providers[name]
                if self._registry["default"] == name:
                    self._registry["default"] = self._registry["accounts"][0]["name"]
            self._save()
        return self.status()
