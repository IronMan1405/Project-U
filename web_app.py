import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

import voice
from config import load_settings, load_utility_settings, load_voice_settings
from computer_utilities import UtilitySettings
from voice import VoiceError, VoiceSettings, VoiceTimeout, VoiceUnavailable
from documents import (
    MAX_ACTIVE_DOCUMENTS,
    MAX_UPLOAD_BYTES,
    DocumentError,
    SelectedDocument,
    document_context_message,
    extract_document,
)
from assistant_runtime import AssistantRuntime, RuntimeBusyError, TurnResult
from tool_registry import ToolRegistry
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
    update_note,
    update_task,
    validate_due_at,
)


def create_app(
    database_path: str | Path | None = None,
    utility_settings: UtilitySettings | None = None,
    voice_settings: VoiceSettings | None = None,
) -> Flask:
    app = Flask(__name__)
    conversations: dict[str, AssistantRuntime] = {}
    conversation_documents: dict[str, SelectedDocument] = {}
    conversation_activity: dict[str, dict] = {}
    app.config["DATABASE_PATH"] = Path(database_path) if database_path else DATABASE_PATH
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 64 * 1024

    try:
        endpoint, model = load_settings()
        config_error = None
    except (OSError, ValueError, KeyError) as error:
        endpoint, model = None, None
        config_error = str(error)
    if utility_settings is None:
        utility_settings = load_utility_settings()
    if voice_settings is None:
        voice_settings = load_voice_settings()
    # One transcription and one synthesis at a time keep local CPU/GPU use bounded.
    transcription_slot = threading.Semaphore(1)
    speech_slot = threading.Semaphore(1)

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

    @app.errorhandler(RuntimeBusyError)
    def handle_runtime_busy(error):
        return jsonify(error=str(error)), 409

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
            conversation_activity.pop(previous_id, None)

        conversation_id = str(uuid4())
        if config_error is None:
            def update_activity(activity: dict, key: str = conversation_id) -> None:
                conversation_activity[key] = activity

            conversations[conversation_id] = AssistantRuntime(
                endpoint,
                model,
                database_path=app.config["DATABASE_PATH"],
                tool_registry=ToolRegistry(
                    app.config["DATABASE_PATH"], utilities=utility_settings
                ),
                activity_callback=update_activity,
            )
            conversation_activity[conversation_id] = {
                "status": "idle",
                "tool": None,
                "summary": "",
            }
        return jsonify(conversation_id=conversation_id)

    @app.get("/api/conversations/<conversation_id>/activity")
    def conversation_activity_status(conversation_id: str):
        if not _valid_conversation_id(conversation_id):
            return jsonify(error="Invalid conversation ID."), 400
        if conversation_id not in conversations:
            return jsonify(error="Conversation not found. Start a new conversation."), 404
        return jsonify(activity=conversation_activity.get(conversation_id, {"status": "idle"}))

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

        runtime = conversations.get(conversation_id)
        if runtime is None:
            return jsonify(error="Conversation not found. Start a new conversation."), 404

        try:
            document = conversation_documents.get(conversation_id)
            additional_context = []
            document_selection = None
            if document is not None:
                context_message, document_selection = document_context_message(
                    document, message.strip()
                )
                additional_context.append(context_message)
            result = runtime.run_turn(
                message.strip(), additional_context=additional_context
            )
            return jsonify(_serialize_turn_result(result, document_selection))
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
        finally:
            if not runtime.has_pending_confirmation:
                conversation_activity[conversation_id] = {
                    "status": "idle",
                    "tool": None,
                    "summary": "",
                }

    def voice_error_response(error: VoiceError):
        if isinstance(error, VoiceUnavailable):
            return jsonify(error=str(error)), 503
        if isinstance(error, VoiceTimeout):
            return jsonify(error=str(error)), 504
        return jsonify(error=str(error)), 422

    @app.get("/api/voice/status")
    def voice_status():
        return jsonify(
            speech_input=voice_settings.stt_available,
            speech_output=voice_settings.tts_available,
            max_seconds=voice_settings.max_seconds,
        )

    @app.post("/api/voice/transcribe")
    def voice_transcribe():
        # Returns text only; the browser shows it for review and the user sends it
        # through /api/chat, so tool intent checks and confirmations are unchanged.
        if not voice_settings.stt_available:
            return jsonify(error="Local speech recognition is not configured."), 503
        recording = request.files.get("audio")
        if recording is None:
            return jsonify(error="Send the recording as the audio field."), 400
        try:
            audio = recording.stream.read(voice.MAX_AUDIO_BYTES + 1)
        except OSError:
            return jsonify(error="The recording could not be read."), 400
        finally:
            recording.close()
        if len(audio) > voice.MAX_AUDIO_BYTES:
            return jsonify(error="The recording is too large; keep it under a minute."), 413
        if not transcription_slot.acquire(blocking=False):
            return jsonify(error="Another recording is being transcribed. Try again shortly."), 429
        try:
            text = voice.transcribe(voice_settings, audio, recording.content_type or "")
        except VoiceError as error:
            return voice_error_response(error)
        finally:
            transcription_slot.release()
        return jsonify(text=text)

    @app.post("/api/voice/speak")
    def voice_speak():
        if not voice_settings.tts_available:
            return jsonify(error="Local speech output is not available."), 503
        data = request.get_json(silent=True)
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            return jsonify(error="Send text of 1 to 20,000 characters to speak."), 400
        if not speech_slot.acquire(blocking=False):
            return jsonify(error="Speech output is busy. Try again shortly."), 429
        try:
            audio, truncated = voice.synthesize(voice_settings, text)
        except VoiceError as error:
            return voice_error_response(error)
        finally:
            speech_slot.release()
        return Response(
            audio,
            mimetype="audio/wav",
            headers={"Cache-Control": "no-store", "X-Speech-Truncated": "1" if truncated else "0"},
        )

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
            try:
                conversations[conversation_id].reset_history()
            except RuntimeBusyError as error:
                return jsonify(error=str(error)), 409
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

    @app.post("/api/confirm")
    def confirm_action():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with the confirmation details."), 400
        conversation_id = data.get("conversation_id")
        confirmation_id = data.get("confirmation_id")
        approved = data.get("approved")
        if not _valid_conversation_id(conversation_id):
            return jsonify(error="Invalid conversation ID."), 400
        if not isinstance(confirmation_id, str) or not confirmation_id:
            return jsonify(error="Invalid confirmation ID."), 400
        if not isinstance(approved, bool):
            return jsonify(error="approved must be a boolean."), 400
        runtime = conversations.get(conversation_id)
        if runtime is None:
            return jsonify(error="Conversation not found. Start a new conversation."), 404
        try:
            result = runtime.resolve_confirmation(confirmation_id, approved)
            return jsonify(_serialize_turn_result(result))
        except RuntimeBusyError as error:
            return jsonify(error=str(error)), 409
        finally:
            if not runtime.has_pending_confirmation:
                conversation_activity[conversation_id] = {
                    "status": "idle",
                    "tool": None,
                    "summary": "",
                }

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
        due_at = data.get("due_at")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            return jsonify(error="Title must be between 1 and 200 characters."), 400
        if not isinstance(description, str) or len(description) > 5000:
            return jsonify(error="Description must be 5,000 characters or fewer."), 400
        try:
            due_at = validate_due_at(due_at)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        task = create_task(
            title.strip(), description, app.config["DATABASE_PATH"], due_at=due_at
        )
        return jsonify(task=task), 201

    @app.patch("/api/tasks/<int:task_id>")
    def tasks_update(task_id: int):
        unavailable = data_store_unavailable()
        if unavailable:
            return unavailable
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not ({"completed", "due_at"} & data.keys()):
            return jsonify(error="Send completed and/or due_at to update a task."), 400
        changes = {}
        if "completed" in data:
            if not isinstance(data["completed"], bool):
                return jsonify(error="completed must be a boolean."), 400
            changes["completed"] = data["completed"]
        if "due_at" in data:
            try:
                changes["due_at"] = validate_due_at(data["due_at"])
            except ValueError as error:
                return jsonify(error=str(error)), 400
        task = update_task(task_id, app.config["DATABASE_PATH"], **changes)
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


def _serialize_turn_result(result: TurnResult, document_selection=None) -> dict:
    activities = list(result.activities)
    if document_selection is not None:
        if document_selection.mode == "full":
            summary = "Used the full extracted document text."
        else:
            summary = "Used selected document excerpts; the full document was not considered."
        activities.insert(
            0,
            {"tool": "document_context", "status": "context", "summary": summary},
        )
    return {
        "response": result.response,
        "activities": activities,
        "pending_confirmation": result.pending_confirmation,
        "document_context": document_selection.mode if document_selection is not None else None,
    }


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, threaded=True)
