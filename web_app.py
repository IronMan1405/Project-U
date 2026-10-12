import json
import logging
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from config import load_settings
from documents import (
    MAX_ACTIVE_DOCUMENTS,
    MAX_UPLOAD_BYTES,
    DocumentError,
    SelectedDocument,
    document_context_message,
    extract_document,
)
from main import respond_to_message
from storage import (
    DATABASE_PATH,
    RecordNotFound,
    create_note,
    create_task,
    delete_note,
    delete_task,
    get_note,
    initialize_database,
    list_notes,
    list_tasks,
    set_task_completed,
    update_note,
)


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    conversations: dict[str, list[dict[str, str]]] = {}
    conversation_documents: dict[str, SelectedDocument] = {}
    app.config["DATABASE_PATH"] = Path(database_path) if database_path else DATABASE_PATH
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 64 * 1024

    try:
        endpoint, model = load_settings()
        config_error = None
    except (OSError, ValueError, KeyError) as error:
        endpoint, model = None, None
        config_error = str(error)

    try:
        initialize_database(app.config["DATABASE_PATH"])
        app.config["DATABASE_READY"] = True
    except (OSError, sqlite3.Error):
        logging.getLogger(__name__).exception("Could not initialize the local data store")
        app.config["DATABASE_READY"] = False

    @app.errorhandler(sqlite3.Error)
    def handle_database_error(error):
        logging.getLogger(__name__).exception("Local data store operation failed")
        return jsonify(error="Local data store error. Please try again."), 500

    @app.errorhandler(RecordNotFound)
    def handle_missing_record(error):
        return jsonify(error=str(error)), 404

    @app.errorhandler(RequestEntityTooLarge)
    def handle_upload_too_large(error):
        return jsonify(error="Uploads must be 5 MiB or smaller."), 413

    def data_store_unavailable():
        if not app.config["DATABASE_READY"]:
            return jsonify(error="Local data store is unavailable."), 503
        return None

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
            conversation_documents.pop(previous_id, None)

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
            document = conversation_documents.get(conversation_id)
            if document is None:
                response = respond_to_message(endpoint, model, conversation, message.strip())
                return jsonify(response=response)

            context_message, selection = document_context_message(document, message.strip())
            response = respond_to_message(
                endpoint,
                model,
                conversation,
                message.strip(),
                additional_context=[context_message],
            )
            return jsonify(response=response, document_context=selection.mode)
        except DocumentError as error:
            return jsonify(error=str(error)), 422
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

    @app.post("/api/documents")
    def document_upload():
        conversation_id = request.form.get("conversation_id", "")
        if not _valid_conversation_id(conversation_id):
            return jsonify(error="Invalid conversation ID."), 400
        if conversation_id not in conversations:
            return jsonify(error="Conversation not found. Start a new conversation."), 404
        if (
            conversation_id not in conversation_documents
            and len(conversation_documents) >= MAX_ACTIVE_DOCUMENTS
        ):
            return jsonify(error="The local server already has four active document sessions."), 429

        uploaded_file = request.files.get("file")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify(error="Choose a .txt or .pdf file to upload."), 400

        try:
            content = uploaded_file.stream.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                return jsonify(error="Uploads must be 5 MiB or smaller."), 413
            document = extract_document(uploaded_file.filename, content)
        except DocumentError as error:
            return jsonify(error=str(error)), 422
        except OSError:
            return jsonify(error="The selected file could not be read."), 400
        finally:
            uploaded_file.close()

        conversation_reset = conversation_id in conversation_documents
        if conversation_reset:
            conversations[conversation_id].clear()
        conversation_documents[conversation_id] = document
        return jsonify(
            document=document.metadata(),
            conversation_reset=conversation_reset,
        ), 201

    @app.delete("/api/conversations/<conversation_id>/document")
    def document_clear(conversation_id: str):
        if not _valid_conversation_id(conversation_id):
            return jsonify(error="Invalid conversation ID."), 400
        if conversation_id not in conversations:
            return jsonify(error="Conversation not found. Start a new conversation."), 404
        conversation_documents.pop(conversation_id, None)
        return "", 204

    @app.get("/api/tasks")
    def tasks_list():
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        return jsonify(tasks=list_tasks(app.config["DATABASE_PATH"]))

    @app.post("/api/tasks")
    def tasks_create():
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with a task title."), 400
        title = data.get("title")
        description = data.get("description", "")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            return jsonify(error="Title must be between 1 and 200 characters."), 400
        if not isinstance(description, str) or len(description) > 5000:
            return jsonify(error="Description must be 5,000 characters or fewer."), 400
        task = create_task(title.strip(), description, app.config["DATABASE_PATH"])
        return jsonify(task=task), 201

    @app.patch("/api/tasks/<int:task_id>")
    def tasks_update(task_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("completed"), bool):
            return jsonify(error="completed must be a boolean."), 400
        task = set_task_completed(
            task_id, data["completed"], app.config["DATABASE_PATH"]
        )
        return jsonify(task=task)

    @app.delete("/api/tasks/<int:task_id>")
    def tasks_delete(task_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        delete_task(task_id, app.config["DATABASE_PATH"])
        return "", 204

    @app.get("/api/notes")
    def notes_list():
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        return jsonify(notes=list_notes(app.config["DATABASE_PATH"]))

    @app.post("/api/notes")
    def notes_create():
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with a note title and content."), 400
        title = data.get("title")
        content = data.get("content")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            return jsonify(error="Title must be between 1 and 200 characters."), 400
        if not isinstance(content, str) or len(content) > 200000:
            return jsonify(error="Content must be text of 200,000 characters or fewer."), 400
        note = create_note(title.strip(), content, app.config["DATABASE_PATH"])
        return jsonify(note=note), 201

    @app.get("/api/notes/<int:note_id>")
    def notes_read(note_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        return jsonify(note=get_note(note_id, app.config["DATABASE_PATH"]))

    @app.patch("/api/notes/<int:note_id>")
    def notes_update(note_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with a note title and content."), 400
        title = data.get("title")
        content = data.get("content")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            return jsonify(error="Title must be between 1 and 200 characters."), 400
        if not isinstance(content, str) or len(content) > 200000:
            return jsonify(error="Content must be text of 200,000 characters or fewer."), 400
        note = update_note(note_id, title.strip(), content, app.config["DATABASE_PATH"])
        return jsonify(note=note)

    @app.delete("/api/notes/<int:note_id>")
    def notes_delete(note_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        delete_note(note_id, app.config["DATABASE_PATH"])
        return "", 204

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