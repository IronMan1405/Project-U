import json
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from flask import Flask, jsonify, render_template, request

from config import load_settings
from main import respond_to_message


def create_app() -> Flask:
    app = Flask(__name__)
    conversations: dict[str, list[dict[str, str]]] = {}

    try:
        endpoint, model = load_settings()
        config_error = None
    except (OSError, ValueError, KeyError) as error:
        endpoint, model = None, None
        config_error = str(error)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        if config_error:
            return jsonify(connected=False, error=config_error), 503

        try:
            with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=3) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            reason = getattr(error, "reason", error)
            return jsonify(connected=False, error=f"Ollama is unavailable: {reason}"), 503

        available_models = {
            entry.get("name") or entry.get("model")
            for entry in result.get("models", [])
            if isinstance(entry, dict)
        }
        if model not in available_models:
            return jsonify(
                connected=False,
                error=f"Configured model '{model}' is not available in Ollama.",
            ), 503

        return jsonify(connected=True, model=model)

    @app.post("/api/conversations")
    def new_conversation():
        data = request.get_json(silent=True)
        previous_id = data.get("previous_id") if isinstance(data, dict) else None
        if previous_id is not None:
            if not _valid_conversation_id(previous_id):
                return jsonify(error="Invalid conversation ID."), 400
            conversations.pop(previous_id, None)

        conversation_id = str(uuid4())
        conversations[conversation_id] = []
        return jsonify(conversation_id=conversation_id)

    @app.post("/api/chat")
    def chat():
        if config_error:
            return jsonify(error=config_error), 503

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with a message and conversation ID."), 400

        message = data.get("message")
        conversation_id = data.get("conversation_id")
        if not isinstance(message, str) or not message.strip():
            return jsonify(error="Message must not be empty."), 400
        if len(message) > 20000:
            return jsonify(error="Message must be 20,000 characters or fewer."), 400
        if not _valid_conversation_id(conversation_id):
            return jsonify(error="Invalid conversation ID."), 400

        conversation = conversations.get(conversation_id)
        if conversation is None:
            return jsonify(error="Conversation not found. Start a new conversation."), 404

        try:
            response = respond_to_message(endpoint, model, conversation, message.strip())
            return jsonify(response=response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            try:
                detail = json.loads(detail).get("error", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            return jsonify(error=f"Ollama request failed (HTTP {error.code}): {detail}"), 502
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            return jsonify(error=f"Could not connect to Ollama: {reason}"), 503
        except (json.JSONDecodeError, ValueError) as error:
            return jsonify(error=f"Could not read Ollama's response: {error}"), 502

    return app


def _valid_conversation_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, threaded=True)