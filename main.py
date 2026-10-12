import json
import sys
import urllib.error
import urllib.request

from config import load_settings


def ask_ollama(endpoint: str, model: str, message: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)

    try:
        return result["message"]["content"]
    except (KeyError, TypeError) as error:
        raise ValueError("Ollama returned an unexpected response.") from error


def main() -> int:
    try:
        endpoint, model = load_settings()
    except (OSError, ValueError, KeyError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    print(f"U is ready (model: {model}). Type /quit to exit.")
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
            print(f"U: {ask_ollama(endpoint, model, message)}")
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