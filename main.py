import json
import sqlite3
import sys
import urllib.error

from assistant_runtime import AssistantRuntime, ask_ollama
from config import load_settings, load_utility_settings
from storage import initialize_database
from tool_registry import ToolRegistry


def _confirm_in_terminal(summary: str) -> bool:
    try:
        answer = input(f"U requests: {summary}\nConfirm? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("Confirmation cancelled.")
        return False
    return answer in {"y", "yes"}


def _terminal_activity(activity: dict) -> None:
    if activity.get("status") == "running":
        print(f"U: {activity.get('summary', 'Using a tool')}...", flush=True)


def main() -> int:
    try:
        endpoint, model = load_settings()
    except (OSError, ValueError, KeyError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    try:
        initialize_database()
    except (OSError, sqlite3.Error) as error:
        print(f"Local task/note storage is unavailable: {error}", file=sys.stderr)

    print(f"U is ready (model: {model}). Type /quit to exit.")
    runtime = AssistantRuntime(
        endpoint,
        model,
        tool_registry=ToolRegistry(utilities=load_utility_settings()),
        model_client=ask_ollama,
        activity_callback=_terminal_activity,
    )
    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if message.lower() in {"/quit", "/exit"}:
            print("Goodbye.")
            return 0
        if not message:
            continue

        try:
            result = runtime.run_turn(message, confirm_callback=_confirm_in_terminal)
            for activity in result.activities:
                print(f"[{activity['status']}] {activity['summary']}")
            if result.pending_confirmation:
                print("Action is awaiting confirmation.", file=sys.stderr)
            elif result.response:
                print(f"U: {result.response}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            try:
                detail = json.loads(detail).get("error", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            print(f"Ollama request failed (HTTP {error.code}): {detail}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            print(
                f"Could not connect to Ollama at {endpoint}: {reason}. "
                "Check that Ollama is running and try again.",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, ValueError) as error:
            print(f"Could not read Ollama's response: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())