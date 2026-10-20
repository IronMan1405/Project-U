"""Narrow Mac controls: output volume, mute, basic Music/Spotify playback, and app status.

Each operation runs one fixed AppleScript with `/usr/bin/osascript -`; values (a volume level,
an application name from the approved list) are passed as arguments, never as code. Every
change is read back before it is reported.

Volume and mute use AppleScript's Standard Additions and need no permission. Playback control
sends Apple events to Music or Spotify, so the first use makes macOS ask whether the app that
runs K.R.I.S. may control that player (Privacy & Security > Automation); until it is allowed,
playback commands fail with a clear message. A player that is not running is never launched.
Other media (a video in a browser, for example) cannot be controlled: macOS has no public,
permission-free interface for the system-wide media keys.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

OSASCRIPT = Path("/usr/bin/osascript")
TIMEOUT_SECONDS = 10
FIELD = "\x1f"

# Players with a documented AppleScript playback interface: name -> bundle identifier.
PLAYERS = {"Music": "com.apple.Music", "Spotify": "com.spotify.client"}
MEDIA_ACTIONS = ("play", "pause", "toggle", "next", "previous")

GET_VOLUME_SCRIPT = """
on run argv
  set v to get volume settings
  return ((output volume of v) as text) & (character id 31) & ((output muted of v) as text)
end run
"""

SET_VOLUME_SCRIPT = """
on run argv
  set volume output volume ((item 1 of argv) as integer)
  set v to get volume settings
  return ((output volume of v) as text) & (character id 31) & ((output muted of v) as text)
end run
"""

SET_MUTED_SCRIPT = """
on run argv
  set volume output muted ((item 1 of argv) is "true")
  set v to get volume settings
  return ((output volume of v) as text) & (character id 31) & ((output muted of v) as text)
end run
"""

# `is running` is answered by macOS without contacting (or launching) the application.
RUNNING_SCRIPT = """
on run argv
  return (application (item 1 of argv) is running) as text
end run
"""

# Playback: the player is addressed by bundle ID, only when already running, and the result is
# the player state afterwards plus the current track. The command is chosen from a fixed list.
# AppleScript reads a player's terms ("playpause", "current track") when the script is compiled,
# so the bundle ID is written into the script, always from PLAYERS, never from a request.
MEDIA_SCRIPT = """
on run argv
  set command to item 1 of argv
  if not (application id "{bundle}" is running) then return "not_running"
  tell application id "{bundle}"
    if command is "play" then
      play
    else if command is "pause" then
      pause
    else if command is "toggle" then
      playpause
    else if command is "next" then
      next track
    else if command is "previous" then
      previous track
    end if
    delay 0.3
    set playerState to (player state as text)
    set trackName to ""
    set trackArtist to ""
    try
      set trackName to name of current track
      set trackArtist to artist of current track
    end try
  end tell
  return playerState & (character id 31) & trackName & (character id 31) & trackArtist
end run
"""

STATUS_SCRIPT = """
on run argv
  if not (application id "{bundle}" is running) then return "not_running"
  tell application id "{bundle}"
    set playerState to (player state as text)
    set trackName to ""
    set trackArtist to ""
    try
      set trackName to name of current track
      set trackArtist to artist of current track
    end try
  end tell
  return playerState & (character id 31) & trackName & (character id 31) & trackArtist
end run
"""

_PERMISSION_HELP = (
    "K.R.I.S. is not allowed to control {app}. In System Settings > Privacy & Security > Automation, "
    "allow the app that runs K.R.I.S. (for example Terminal) to control {app}, then try again."
)


class MacControlError(Exception):
    """kind: unavailable, denied, invalid, not_running, timeout or failed; message is safe to show."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def script_error(stderr: str | None) -> str:
    """A short, safe summary of an osascript failure: AppleScript's message and error number
    (e.g. "Can't get kind. (-1728)"), without the script's line and column offsets, capped."""
    text = " ".join((stderr or "").split())
    match = re.search(r"execution error: (.*?)\s*\((-?\d+)\)\s*$", text)
    if match:
        text = f"{match.group(1)} ({match.group(2)})"
    else:
        text = re.sub(r"^\d+:\d+: ", "", text)  # a compile error's position prefix
    return (text[:160] + "...") if len(text) > 160 else (text or "no details")


def _run_osascript(script: str, arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(OSASCRIPT), "-", *arguments], input=script, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, check=False)


class MacControl:
    def __init__(self, runner: Callable[[str, list[str]], subprocess.CompletedProcess] | None = None):
        self.runner = runner or _run_osascript

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and OSASCRIPT.is_file()

    def _call(self, script: str, arguments: list[str], app: str | None = None) -> str:
        if self.runner is _run_osascript and not self.available():
            raise MacControlError("unavailable", "Mac controls are only available on macOS.")
        try:
            result = self.runner(script, arguments)
        except subprocess.TimeoutExpired:
            raise MacControlError("timeout", "macOS did not respond in time.") from None
        except OSError:
            raise MacControlError("unavailable", "macOS scripting could not be started.") from None
        if result.returncode == 0:
            return result.stdout.rstrip("\r\n")  # not strip(): it would also remove trailing \x1f separators
        error = (result.stderr or "").lower()
        if "-1743" in error or "not allowed" in error or "not authorized" in error:
            raise MacControlError("denied", _PERMISSION_HELP.format(app=app or "that application"))
        if "-600" in error or "-609" in error:
            raise MacControlError("not_running", f"{app or 'The application'} is not running.")
        raise MacControlError("failed", f"{app or 'macOS'} reported an error: {script_error(result.stderr)}.")

    @staticmethod
    def _volume_state(output: str) -> dict:
        level, _, muted = output.partition(FIELD)
        if level == "missing value":
            # e.g. an HDMI or digital output whose volume is set on the device itself
            raise MacControlError("unavailable", "The current sound output does not have a volume macOS can set.")
        try:
            volume = int(level)
        except ValueError:
            raise MacControlError("failed", "macOS returned a volume K.R.I.S. could not read.") from None
        return {"volume": volume, "muted": muted == "true"}

    def volume(self) -> dict:
        return self._volume_state(self._call(GET_VOLUME_SCRIPT, []))

    def set_volume(self, level: int) -> dict:
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
            raise MacControlError("invalid", "Volume must be a whole number from 0 to 100.")
        state = self._volume_state(self._call(SET_VOLUME_SCRIPT, [str(level)]))
        # macOS stores volume in steps, so the read-back can differ from the request by a point or two.
        if abs(state["volume"] - level) > 3:
            raise MacControlError("failed", f"macOS reports the volume at {state['volume']}% instead of {level}%.")
        return state

    def set_muted(self, muted: bool) -> dict:
        if not isinstance(muted, bool):
            raise MacControlError("invalid", "Say whether to mute or unmute.")
        state = self._volume_state(self._call(SET_MUTED_SCRIPT, ["true" if muted else "false"]))
        if state["muted"] != muted:
            raise MacControlError("failed", f"macOS did not {'mute' if muted else 'unmute'} the sound.")
        return state

    def is_running(self, application: str) -> bool:
        return self._call(RUNNING_SCRIPT, [application], application) == "true"

    @staticmethod
    def _player_state(output: str, player: str) -> dict:
        if output == "not_running":
            raise MacControlError("not_running", f"{player} is not running, so there is nothing to control.")
        state, _, rest = output.partition(FIELD)
        track, _, artist = rest.partition(FIELD)
        return {"player": player, "state": state, "track": track or None, "artist": artist or None}

    def media(self, player: str, action: str) -> dict:
        if player not in PLAYERS:
            raise MacControlError("invalid", f"Playback control supports {', '.join(PLAYERS)}.")
        if action not in MEDIA_ACTIONS:
            raise MacControlError("invalid", f"Playback actions are {', '.join(MEDIA_ACTIONS)}.")
        script = MEDIA_SCRIPT.replace("{bundle}", PLAYERS[player])
        result = self._player_state(self._call(script, [action], player), player)
        expected = {"play": "playing", "pause": "paused"}.get(action)
        if expected and result["state"] != expected:
            raise MacControlError("failed", f"{player} reports it is {result['state']}, not {expected}.")
        return result

    def now_playing(self, player: str) -> dict:
        if player not in PLAYERS:
            raise MacControlError("invalid", f"Playback status supports {', '.join(PLAYERS)}.")
        script = STATUS_SCRIPT.replace("{bundle}", PLAYERS[player])
        return self._player_state(self._call(script, [], player), player)

    def running_players(self) -> list[str]:
        return [player for player in PLAYERS if self.is_running(player)]


# --- Apple Music library playback -----------------------------------------------------------
# Music's scripting dictionary can search the user's own library ("search library playlist 1
# for ...", the same as its Search field) and play a specific track or playlist; it has no
# Apple Music catalogue search. So only songs and playlists already in the library (including
# Apple Music songs added to it) can be played by name. Items are identified by Music's
# persistent ID, and playback is verified by reading the current track or playlist back.

MAX_MUSIC_RESULTS = 15
_PERSISTENT_ID = re.compile(r"^[0-9A-F]{16}$")

MUSIC_SEARCH_SCRIPT = """
on run argv
  set q to item 1 of argv
  set output to {}
  tell application id "com.apple.Music"
    set found to search library playlist 1 for q
    if found is missing value then set found to {}
    set n to count of found
    if n > 15 then set n to 15
    repeat with i from 1 to n
      set t to item i of found
      set end of output to "track" & (character id 31) & (persistent ID of t) & (character id 31) & (name of t) & (character id 31) & (artist of t) & (character id 31) & (album of t)
    end repeat
    set lists to (user playlists whose name contains q and smart is false)
    repeat with p in lists
      set end of output to "playlist" & (character id 31) & (persistent ID of p) & (character id 31) & (name of p) & (character id 31) & "" & (character id 31) & ""
    end repeat
  end tell
  set AppleScript's text item delimiters to (character id 30)
  return output as text
end run
"""

MUSIC_PLAY_SCRIPT = """
on run argv
  -- Not "kind" or "st": both are reserved AppleScript terms ("kind" cannot be set; "st" is
  -- the ordinal suffix in "1st"), and using them made this script fail.
  set {itemKind, itemId} to argv
  tell application id "com.apple.Music"
    if itemKind is "track" then
      play (first track of library playlist 1 whose persistent ID is itemId)
    else
      play (first user playlist whose persistent ID is itemId)
    end if
    set waited to 0
    repeat while player state is not playing and waited < 6
      delay 0.5
      set waited to waited + 1
    end repeat
    set playerState to (player state as text)
    set trackId to ""
    set trackName to ""
    set trackArtist to ""
    set listId to ""
    try
      set trackId to persistent ID of current track
      set trackName to name of current track
      set trackArtist to artist of current track
    end try
    try
      set listId to persistent ID of current playlist
    end try
  end tell
  return playerState & (character id 31) & trackId & (character id 31) & trackName & (character id 31) & trackArtist & (character id 31) & listId
end run
"""


def _music_call(control: "MacControl", script: str, arguments: list[str]) -> str:
    return control._call(script, arguments, "Music")


def music_search(control: "MacControl", query: str) -> list[dict]:
    """Songs and playlists in the user's Music library matching the query (Music's own search)."""
    query = " ".join((query or "").split())
    if not query or len(query) > 200:
        raise MacControlError("invalid", "Give a song, artist, album or playlist name of up to 200 characters.")
    output = _music_call(control, MUSIC_SEARCH_SCRIPT, [query])
    items = []
    for record in output.split("\x1e"):
        fields = record.split(FIELD)
        if len(fields) == 5 and _PERSISTENT_ID.fullmatch(fields[1]):
            kind, pid, name, artist, album = fields
            items.append({"kind": kind, "item_id": f"{kind}:{pid}", "name": name, "artist": artist or None,
                          "album": album or None})
    return items


def music_play(control: "MacControl", item_id: str) -> dict:
    """Play one library track or playlist by its item ID and verify that it is what plays."""
    kind, _, pid = (item_id or "").partition(":")
    if kind not in ("track", "playlist") or not _PERSISTENT_ID.fullmatch(pid):
        raise MacControlError("invalid", "That is not a Music item from find_music.")
    output = _music_call(control, MUSIC_PLAY_SCRIPT, [kind, pid])
    state, track_id, name, artist, list_id = (output.split(FIELD) + [""] * 5)[:5]
    playing = state == "playing"
    matched = track_id == pid if kind == "track" else list_id == pid
    if not (playing and matched):
        raise MacControlError("failed", f"Music did not start that {kind}: it reports {state or 'no state'}"
                              + (f" with \"{name}\"" if name else "") + ". Check Music; an Apple Music song may need "
                              "to download or your subscription may need attention.")
    return {"state": state, "track": name or None, "artist": artist or None, "kind": kind}
