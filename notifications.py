"""macOS notifications for K.R.I.S. (timer endings), through AppleScript's `display notification`.

The script is fixed; the title and message are passed as arguments, never as code. macOS shows
the notification under the app that runs K.R.I.S. (for example Terminal) and may ask once for
permission to show notifications; if notifications are off for that app, nothing appears and
K.R.I.S. still shows the alert in its own page.
"""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

OSASCRIPT = Path("/usr/bin/osascript")
TIMEOUT_SECONDS = 10
MAX_CHARS = 200
SCRIPT = """on run argv
  display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"
end run
"""


def notify(title: str, message: str) -> bool:
    """Show one notification; returns False (and logs only that it failed) if it could not."""
    if sys.platform != "darwin" or not OSASCRIPT.is_file():
        return False
    try:
        result = subprocess.run(
            [str(OSASCRIPT), "-", title[:MAX_CHARS], message[:MAX_CHARS]],
            input=SCRIPT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Could not show a macOS notification.")
        return False
    if result.returncode != 0:
        logger.warning("Could not show a macOS notification (osascript exit %s).", result.returncode)
        return False
    return True
