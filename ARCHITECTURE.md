# Project U — Architecture

## 1. Purpose

U is a lightweight, personal desktop assistant designed to help with everyday computing, studying, productivity, and engineering projects.

**Project deadline:** October 21, 2026  
**Development approach:** AI-assisted development using GitHub Copilot  
**Design principle:** Useful, lightweight, local-first, and maintainable.

U is a personal utility, not an autonomous AI agent or a general-purpose operating system.

## 2. Technology Stack

- **Language:** Python
- **LLM runtime:** Ollama
- **LLM:** Existing locally installed model
- **Database:** SQLite
- **Interface:** Local Flask web UI at `http://127.0.0.1:5000` plus a terminal client (`main.py`)
- **Version control:** Git and GitHub

Prefer existing capabilities and minimal dependencies. Do not introduce additional frameworks without a clear need.

## 3. Core Architecture

### Assistant Core
Coordinates user input, context, model requests, and tool execution. `assistant_runtime.py` keeps one in-memory history per conversation (user messages, tool requests, tool results, and replies); it is session-scoped, never persisted, and lost when the server or terminal client restarts. Each model request is ordered profile, fixed rules (`SYSTEM_RULES`), earlier turns, then the per-request date/time and document context, then the current turn, so Ollama can reuse its prompt cache for the unchanged prefix; the per-request context is never stored. Before each turn, history over `MAX_HISTORY_CHARS` (48,000 characters) is trimmed toward `HISTORY_TRIM_TARGET_CHARS` (28,800) by dropping the oldest complete turns (cut only at user messages, so a tool request and its result stay together; the newest turn is always kept), and stored tool requests omit the model's hidden "thinking".

Confirmation outcomes are deterministic. A declined action, and every tool call queued after it in the same request, gets a "not run" result and the turn ends with fixed text from `_cancel`, without asking the model; if earlier actions in that request already ran, the text says so. A new message while a confirmation is pending performs that cancellation and starts the new turn under the same per-conversation lock as `resolve_confirmation`, so the old confirmation ID is invalid afterwards and the two can never interleave; the cancellation text is returned as `TurnResult.notice`. Confirm and Cancel remain the other ways to resolve a pending action.

### LLM Client
Communicates with the local Ollama API. Keeps model configuration separate from application logic.

### Context
Loads the user-editable `profile.json` before each model request and provides its validated preferences as separate system context alongside the in-memory conversation. The web client can also attach one explicitly uploaded `.txt` or `.pdf` to a conversation. `documents.py` validates and extracts uploads locally in memory; question-relevant page/paragraph excerpts are passed as separate untrusted document context through the shared Ollama request path, capped at 12,000 characters. Replacing the selected document resets that conversation's history. Profile values are user data, not instructions or authorization. Missing or invalid profiles use generic defaults.

### Memory
`storage.py` owns the SQLite schema and parameterized CRUD operations for tasks and notes. Tasks have an optional `due_at` (`YYYY-MM-DD` or a UTC `YYYY-MM-DD HH:MM:SSZ` timestamp), validated by `storage.validate_due_at` and shared by the API, tool schemas, and UI; older databases gain the column on startup. `update_task` changes only the fields it is given, so completing a task never alters its due date. The assistant tools accept due times as local wall-clock values (`YYYY-MM-DD HH:MM`, or with an explicit offset); `storage.normalize_due_at` converts them to UTC in Python using the system time zone and rejects times that a daylight-saving change skips or repeats, so the model never does timezone arithmetic. The web API itself accepts only the canonical formats. The default database is `data/project_u.sqlite3`, created automatically and kept local. Flask exposes explicit task and note APIs used by the web interface; chat changes these records only through the registered tools in `tool_registry.py`.

### Tools
Provides narrowly scoped functions for approved operations, such as managing tasks, reading selected files, and opening applications. `computer_utilities.py` holds the approved computer utilities: `config.py` loads allowlisted folders and macOS apps from `config.ini`, and `tool_registry.py` offers `find_files` (bounded, read-only file-name search; a multi-word search with no results is retried once with recognized file-type words such as "spreadsheets" turned into an extension filter, within the same entry limit), `open_file` (confirmed, path-contained, document types only), and `open_application` (confirmed, allowlist only) only when they are configured. Launches use the fixed `/usr/bin/open` executable with argument lists, never a shell.

### UI
Provides a single, consistent interface for interacting with U, presented in the web UI as the K.R.I.S. command center. `templates/index.html` lays out a CSS grid under a header with the centred wordmark: a left column with three boxes (an always-visible System panel with CPU, memory, GPU and thermal rows that say "Not connected" and, beneath a divider, a live clock and date (`updateClock()`, once a second); a switchable Inbox / Calendar panel with honest "not connected" states; and Alerts), a centre column that holds only the energy core and its status, a right column with the workspace panel (Chat / Tasks / Notes / Files), a read-only console, and a switchable Activity / Utilities panel, and the command bar (composer, microphone, voice controls, errors) across the bottom. The two side columns share one width (`--side-column`), so the centre column and the core sit on the page's horizontal centre. Below 1180 px the core and the left boxes share one column; at 820 px and below everything stacks into a single scrolling column with a sticky command bar. Every element ID that `static/app.js` already used is kept. The Chat, Tasks and Notes view panels moved unchanged into the workspace panel, and the document controls moved into a new `#files-view` panel, so `switchView()` works as before: it hides every `.view-panel` except `#<name>-view` and loads tasks or notes on demand. Because the core is outside those panels, navigation never replaces it. `static/style.css` defines design tokens on `:root` (colours, spacing, borders, typography), and the core is an inline SVG animated only with CSS (no 3D engine and no new dependencies), styled by its `data-state`. A final "HUD visual layer" section in the stylesheet restyles the existing panels (hairline borders, four corner brackets from one pseudo-element, `data-section` identifiers, tab and readout typography) and adds a fixed, inert `.hud-frame` around the viewport. The core sits inside `.core-instrument`, a second decorative SVG (tick ring, segmented arcs, an orbit marker, an amber attention arc) that takes its size from the existing breakpoints. `renderCore()` copies the core's state onto it, and the LINK, MODE and LAST EVENT readouts are filled from the health check, the Hands-free toggle and `logActivity()`; none of this feeds back into behaviour.

The command-center panels only display facts the page already has and never change behaviour. `renderVoiceView()` also calls `renderCore()`, which takes the state from `VoiceLoop.viewState()` (not limited to voice, so typed requests still show Thinking). It shows Waiting for confirmation while the Confirm/Cancel panel is open, and Offline when `/api/health` fails and nothing else is happening. `renderAlerts()` rebuilds the alert list from the health result, the visible error message, the confirmation panel (watched with a `MutationObserver`, since several code paths hide it) and the loop's `notice`. `logActivity()` appends timestamped lines (tool activity, core state changes, errors, model status changes) to an in-page list capped at 60 entries. The model status appears in the header and the core's LINK readout, both from `/api/health`; there is no hardware telemetry API. The live "thinking" status from the runtime still says "U is thinking...", so the browser shows it as "K.R.I.S. is thinking..." instead. `showChatView()` returns to the transcript when a message is sent or a confirmation appears, so the persistent command bar never sends into a hidden view. The two side panels marked `[data-tabs]` use `setupPanelTabs()`, a standard ARIA tab list (`aria-selected`, roving `tabindex`, arrow keys, Home and End) that only shows and hides panes; the alert count beside the Alerts heading comes from `renderAlerts()`.

### Voice
`voice.py` adds optional, local push-to-talk. `POST /api/voice/transcribe` converts a browser recording with FFmpeg, measures its loudness with `speech_level` (standard library only; recordings with less than `MIN_SPEECH_SECONDS` at or above `SPEECH_LEVEL_DBFS` are rejected as "No speech was detected" without running whisper.cpp, because whisper transcribes silence as "you"), and otherwise transcribes it with whisper.cpp (`ggml-base.en`). It returns text plus a deterministic `reply_kind` (`affirm`, `decline`, or null) from `classify_reply`, which matches only a whole short reply such as "yes" or "no". The server never acts on `reply_kind`; the browser uses it while a confirmation is pending: `affirm` is not sent (approval stays button-only) and `decline` calls the Cancel path. Otherwise the transcript is placed in the message box for review, or, in the opt-in Hands-free mode, sent automatically through the normal `/api/chat` path, so tool rules and confirmations are unchanged. `POST /api/voice/speak` turns replies, confirmation prompts and cancellation messages into WAV audio with macOS `say` when spoken replies or Hands-free are on. In Hands-free mode the browser also detects the end of speech: `static/voice_activity.js` is a small DOM-free state machine (tested with `node --test tests/js/`) fed one level per 50 ms from a Web Audio `AnalyserNode` on the same `MediaStream` the `MediaRecorder` records. It measures a per-recording noise floor, recognises speech onset and a trailing silence of `end_silence_ms` (after `min_recording_ms`), and discards recordings with no speech within `NO_SPEECH_TIMEOUT_MS` without uploading; `max_seconds` remains the hard cap. The timings come from `[voice]` via `GET /api/voice/status`. In `static/app.js`, every way a recording ends goes through `stopRecording(reason)` (first reason wins) and `teardownRecording()`, which stops all tracks, the analyser, and timers; a session counter ignores late callbacks, and hidden pages, page exit, and an ended microphone track discard the recording. The transcribe and speak endpoints run fixed executables with argument lists and timeouts, keep audio and text only in a temporary directory for the request, and are configured by the optional `[voice]` section in `config.ini`.

The Hands-free listening loop is owned by `static/voice_loop.js`, a DOM-free reducer (also tested with `node --test tests/js/`) with the states `off`, `listening`, `transcribing`, `thinking`, `speaking`, and `awaiting_confirmation`. `static/app.js` only reports events to it (`dispatchLoop`) — the user's tap, recordings uploaded or discarded, transcript outcomes, chat and confirmation results tagged with a turn number, playback ending or failing, Hands-free off, page hidden, typed messages — and carries out the actions it returns (`start_listening`, `stop_listening`, `stop_playback`, `notify`). Only a tap starts the loop; it resumes listening only from `speaking` (when that turn's playback ends) or from `thinking` (an unspoken reply), never while awaiting a confirmation or while a request is in flight; events from an earlier turn or the wrong state are ignored, and every failure or two consecutive silences return it to `off` with a message. `speakReply()` returns a promise (true when playback ends, false on failure, null when stopped or replaced), Confirm and Cancel are disabled while a recording is being transcribed, and a hands-free transcript that arrives while another request is running is kept in the message box instead of being submitted.

The voice indicator above the message box (Listening, Transcribing, Thinking, Speaking, Waiting for confirmation, Hands-free paused, Error) is derived, never stored: `VoiceLoop.viewState(loop, facts)` returns the state from the loop while Hands-free runs, and otherwise from read-only page facts (recording, transcribing, a request in flight, a spoken reply in progress, an error showing), so Review mode is covered without a second state machine. The loop keeps a `notice` (kind `paused` for silence or a busy page, `error` for failures) for the paused/error display, cleared by the next tap or by switching Hands-free off. `renderVoiceView()` in `static/app.js` updates the indicator — a polite, atomic `role="status"` region whose text changes only when the state changes — and shows the Stop Speaking button only while a spoken reply is being prepared or played. Stop Speaking dispatches `stop_speaking`: the loop always returns `stop_playback`, and only from `speaking` (a hands-free reply) also moves to `listening` with a single `start_listening`; the cancelled playback promise then resolves after the loop has left `speaking`, so its stale `playback_finished` is ignored. In Review mode, during a spoken confirmation prompt, or when nothing is playing, it only stops (or ignores) playback and never starts listening.

Barge-in reuses the existing paths rather than adding a state machine. While a spoken reply plays the mic stays enabled (labelled "Interrupt and talk" in Hands-free); `micPressStart()` dispatches `start` (the loop moves from `speaking` or `awaiting_confirmation` to `listening` with no actions) and then calls `startRecording()`, which calls `stopReplyAudio()` before opening the microphone, so the tap itself is the only recording start and the interrupted playback's promise later resolves as a stale `playback_finished` that the loop ignores. A failed start dispatches `listen_failed` (loop `off` with an error notice, no retry). The loop carries a `confirmationPending` flag, set in `awaiting_confirmation` and kept only through `listening` and `transcribing`, so a recording discarded as silent after interrupting a confirmation prompt returns to `awaiting_confirmation` instead of asking to listen again; transcripts still go through the existing `reply_kind` rules (affirm guarded, decline → Cancel, anything else → normal chat). In Review mode the loop is off, so pressing the mic only stops the audio and records while held. Because a guard or cancellation message can follow an interrupted synthesis immediately, `speakReply()` retries `POST /api/voice/speak` on 429 (the server's one-synthesis-at-a-time limit) up to 12 times at 250 ms intervals while the request is still current. Gemma generation is not interruptible: the mic is disabled while a chat request is in flight.

## 4. Safety and Privacy

- Prefer local processing and local storage.
- Never execute arbitrary shell commands generated by the LLM.
- Speech recognition and speech output run locally; recordings and transcripts are never stored or logged (only per-recording loudness numbers are logged, at INFO level).
- State-changing requests (POST, PATCH, DELETE) whose `Origin` is not U's own page, or that are marked `Sec-Fetch-Site: cross-site`, are refused with 403 before any work is done, so other websites cannot trigger uploads, transcription, speech, or tool actions; requests without an `Origin` (curl, the terminal, tests) are unaffected.
- Validate tool arguments before execution.
- Require explicit user confirmation for consequential actions.
- Restrict file access to user-approved locations.
- Never store API keys, passwords, or secrets in source control.
- Do not send personal files to external services without explicit user approval.
- Keep the local SQLite database out of version control.
- Task and note changes come from explicit UI/API requests or validated tool calls; model-requested writes run directly only for clear user requests and otherwise require confirmation.
- Only the Confirm button (or answering "y"/"yes" at the terminal's confirmation prompt) approves an action; cancelling, or sending a new message while one is pending, never runs it or anything queued after it.
- Uploaded documents are processed locally; request parsing may temporarily spool upload data, but no file is retained after the request. Extracted text is conversation-scoped memory only and is never added to profile or SQLite storage.
- Limit document uploads and extracted context; scanned PDFs are not OCR'd and large documents may be represented only by selected excerpts.
- The web server binds to `127.0.0.1` and accepts only the `127.0.0.1` and `localhost` Host names (Flask `TRUSTED_HOSTS`), so pages on other hostnames, including DNS-rebinding pages, cannot read data or approve actions.

## 5. Development Rules

- Implement one small feature at a time.
- Keep modules simple and responsibilities clearly separated.
- Test each feature before adding the next.
- Avoid unnecessary abstractions, agent frameworks, and dependencies.
- Do not add features outside the agreed MVP.
- Record deferred ideas in FUTURE.md rather than implementing them.
- Commit working milestones to Git.

## 6. Initial Milestone (historical, completed)

This section records the first milestone's scope and is kept for history; sections 2–4 describe the current architecture. The first milestone was to build a minimal Python application that:

1. Starts from the project directory.
2. Accepts a text message.
3. sends the message to the configured local Ollama model.
4. Displays the model's response.
5. Handles connection errors gracefully.
6. Exits cleanly.

Do not implement memory, tools, voice, web search, or a graphical interface in this milestone.

## 7. Definition of Done

U must be usable, reasonably reliable, and documented by October 21, 2026. New features are frozen once the final integration phase begins. Unfinished stretch goals are deferred rather than allowed to delay release.