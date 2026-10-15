# Project U

A local assistant with a terminal client and a lightweight web chat interface, powered by Ollama.

## Setup

Requires macOS, Python 3.11 or newer (the test suite is run with Python 3.14),
and Ollama with a model already installed. From the project directory, create
the virtual environment, install the dependencies, and create `config.ini` from
the example:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.ini.example config.ini
```

Set `model` in `config.ini` to a model shown by `ollama list`. The default
configuration uses `gemma4:e4b` and the local Ollama endpoint
`http://127.0.0.1:11434`. Make sure Ollama is running before starting U.

The example configuration also turns on the approved computer utilities (for
`~/Documents` and the Calendar and Notes apps) and voice; see those sections
below to adjust or remove them. Its tool paths assume Homebrew on Apple Silicon
(`/opt/homebrew`); on an Intel Mac, Homebrew installs to `/usr/local`, so check
`command -v ffmpeg whisper-cli` and update the `[voice]` paths.

Run the automated tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Profile and preferences

U reads `profile.json` from the project directory before each model request, so
edits take effect on the next response in either client. Create it from
`profile.example.json` and edit the JSON values:

```sh
cp profile.example.json profile.json
```

The supported fields are `response_style` and `explanation_detail` (short text
values), plus `interests` (a list of short strings). If the file is missing,
malformed, unreadable, or contains invalid values, U logs a warning when
applicable and uses generic defaults. The local `profile.json` is excluded from
Git. Profile values are treated as preferences, not instructions or
authorization.

## Terminal

From the project directory, run:

```sh
.venv/bin/python main.py
```

Enter a message to chat. Type `/quit` or `/exit` to leave.

## Web interface

After the setup above, start the local server:

```sh
.venv/bin/python web_app.py
```

Open <http://127.0.0.1:5000> (or <http://localhost:5000>). The server binds to
127.0.0.1 and rejects requests addressed to any other host name with HTTP 400, so
web pages on other sites cannot use its API. It uses the same
`config.ini` and Ollama model as the terminal client. Conversation history is
kept in server memory for each open conversation and cleared when a new one is
started or the server stops. The status indicator checks Ollama and the
configured model. Stop the server with `Ctrl+C` in its terminal.

## Tasks and notes

Use the Tasks and Notes tabs in the web interface to create, edit, complete,
reopen, and delete records. Notes preserve their text and newlines. Records are
stored locally in `data/project_u.sqlite3`; the directory and schema are created
automatically the first time U starts. Data persists across restarts and is
excluded from Git. Removing the database file while U is stopped permanently
deletes its tasks and notes. In chat, U can also manage tasks and notes, but only
through the validated assistant tools described under "Assistant tools"; the
model has no other way to change records.

Tasks can have an optional due date. `due_at` is either a calendar date
(`YYYY-MM-DD`) or a UTC time (`YYYY-MM-DD HH:MM:SSZ`, optional fractional
seconds); any other format, or an impossible date, is rejected with a 400.
In the Tasks tab, pick a date (and optionally a time, entered in local time)
when adding a task, or use "Set due"/"Edit due" on an existing task to change
or remove it. Incomplete tasks past their due date are marked "Overdue".
`PATCH /api/tasks/<id>` accepts `completed`, `due_at`, or both; `"due_at": null`
clears the due date, and a field left out of the request is left unchanged.
Databases created before due dates existed gain the column automatically.

In chat, the assistant passes due times as your local wall-clock time
(`YYYY-MM-DD HH:MM`, 24-hour), or with an explicit offset such as `+05:30` only
when you name another time zone. Python converts them to the stored UTC form
using this Mac's time-zone rules, including daylight saving; a local time that a
daylight-saving change skips or repeats is rejected rather than guessed. The
confirmation and tool result show the stored time converted back to local time,
so what U reports is what was saved.

## Selected documents

In Chat, choose and upload one `.txt` (UTF-8) or `.pdf` file to ask questions
about it. Extraction runs locally with `pypdf` (the only new dependency).
Flask may transiently spool multipart input while parsing it; no uploaded file
is retained after the request. Extracted text is held only in memory for the
active conversation and is never written to disk, SQLite, or the profile.
Clearing the attachment returns to
ordinary chat. Replacing an
attached document resets that conversation's history to keep document sources
separate. Starting a new conversation discards its attachment.

Uploads are limited to 5 MiB, PDFs to 200 pages, and decoded PDF streams to
500,000 bytes each. Extraction is capped at 100,000 characters, with a
12,000-character document-context budget per model request. Small documents
that fit are supplied in full. Larger documents use
simple keyword-ranked, page-labeled paragraph excerpts; this is not semantic
search, and U does not claim the full document was considered. If extraction
hits its limit, the remainder is unavailable. Scanned/image-only PDFs are not
OCR'd; encrypted, malformed, empty, unsupported, or textless files are rejected
with an explanation.

## Assistant tools

Terminal and web chat share `assistant_runtime.py` and Ollama's native tool-call
protocol. U can list/create/complete/reopen/delete tasks and set, change, or clear a
task's due date; list/read/create/edit/
delete notes; report local time, battery, memory, and disk status; request a
website opening in the default browser; and, when configured, search approved
folders by file name, open documents from them, and open approved applications. Task and note actions use the existing
SQLite storage layer. No model-generated shell commands or code are executed.

The runtime allows at most four tool executions per user turn. Read-only tools
run directly. A task/note write runs directly only when the user message clearly
requests it; ambiguous writes, every deletion, and every website opening pause
for explicit approval. Web users get Confirm/Cancel controls; the terminal asks
for a yes/no response. Tool errors and unavailable system readings are reported
as such rather than guessed. Each model request includes the current local date
and time so U can resolve dates such as "tomorrow" or "Friday" into local
dates and times (U converts them to UTC itself); when the day itself is unclear,
U is instructed to ask rather than guess.

### Conversations

Within a conversation, U sees the earlier messages and tool results, so
follow-ups such as "open the second one", "summarize that", or "actually make it
6pm" can refer to what it found or did before. History is scoped to one
conversation and kept only in memory: it is gone when you start a new
conversation, reload the page, or stop or restart U (the server or the terminal
client). The conversation itself is never saved to disk (tasks and notes that U
creates or changes during it are).

- **History limit.** Before each new message, if the stored conversation is
  larger than about 48,000 characters, U drops the oldest complete turns until it
  is at or below about 28,800 characters (a turn is your message plus U's tool
  requests, their results, and the reply). A tool request is never separated from
  its result, and the newest turn is always kept, even if it alone is larger than
  the limit; U no longer sees what was dropped. The model's hidden reasoning
  ("thinking") is not stored in the history.
- **Prompt order.** Each request sends the parts that do not change first (your
  profile, U's fixed instructions, and the earlier turns), then the parts that
  change with every message (the current date/time and any document excerpts),
  then the current turn. The changing parts are not stored in the history. This
  keeps the start of each request identical to the previous one, so Ollama can
  reuse its cached processing of the earlier turns instead of re-reading the whole
  conversation. How much time that saves depends on the conversation length and
  the Mac; the first message after Ollama starts or reloads the model (Ollama
  unloads an idle model after a few minutes by default), and the first one after
  the history limit drops old turns, are processed in full.
- **Cancelling.** When you click Cancel (or answer anything but "y"/"yes" in the
  terminal), the declined action is not run, and neither is any action U had
  queued after it in the same request. U replies with a fixed message rather than
  a model-written one, for example "Cancelled: Delete task 3. It was not run. 1
  other requested action was not run either." If earlier actions in the same
  request had already run, the reply says so and points to the activity list,
  which shows them.
- **Changing your mind.** While an action waits for confirmation in the web
  interface, Confirm and Cancel stay available, and you can also keep typing (or
  use push-to-talk). Sending a new message first cancels the waiting action
  exactly as Cancel would (it and anything queued after it never run), shows the
  cancellation message, and then answers the new message. The old confirmation
  becomes invalid, so a late Confirm for it is refused. If you had already
  clicked Confirm and the action is running, a new message is refused as busy
  until it finishes. Only the Confirm button approves an action: a typed (or
  spoken) "yes" is a new message, so it cancels the waiting action instead.

### Approved computer utilities

These tools are controlled by `config.ini`; restart U after editing it.
`config.ini.example` enables `find_files`/`open_file` for `~/Documents` and
`open_application` for Calendar and Notes, so copying it turns them on. Remove
the `[utility_folders]` section (or empty `allowed_apps`) to turn the matching
tools off; without them, the tools are not offered to the model.

- `[utility_folders]` lists `Label = folder` pairs. Folders must exist inside
  your home folder; your whole home folder, `~/Library`, and hidden folders are
  refused, and invalid entries are skipped with a warning.
- `[utilities] allowed_apps` is a comma-separated list of macOS app names.

`find_files` matches file names (never contents) in those folders and their
subfolders only. Every query word must appear in the name, ignoring case, spaces,
underscores, punctuation, and camelCase/PascalCase, so "performance sheet" finds
`PerformanceSheet.pdf`, `performance_sheet.pdf`, and `Performance Sheet.pdf`. An
optional `file_type` (e.g. `pdf`) filters by extension.

If a search of several words finds nothing and some of the words only describe a
kind of file, it is retried once with the remaining words, limited to these
extensions:

| Words | Extensions |
|---|---|
| spreadsheet, spreadsheets | `.xlsx` `.xls` `.csv` `.numbers` |
| pdf, pdfs | `.pdf` |
| document, documents, doc, docs | `.docx` `.doc` `.pdf` `.txt` `.md` `.rtf` `.pages` |
| image(s), photo(s), picture(s) | `.png` `.jpg` `.jpeg` `.heic` `.gif` |
| presentation(s), slides, slideshow | `.pptx` `.ppt` `.key` |

For example, "budget spreadsheets" finds `Budget 2026.xlsx`. The retry searches
the same approved folders with the same rules, and uses only what is left of the
same 5,000-entry limit (so both searches together never examine more). It does
not run when the first search found anything, when `file_type` was given, when
every word is a file-type word, or when the limit is already used up; the result
says when it retried. The model may name only
an approved root (folder label), never a path or subfolder; subfolders such as
`Desktop/Projects/` are reached only by searching their approved root. Hidden
items, symbolic links, and app/system packages are skipped; the search goes at most
four levels deep, examines at most 5,000 entries in total (shared in turns across
roots so no root is starved), and returns at most 20 matches as
`Label/relative/path`. Results state the words searched and name any root the
entry limit cut short. It is read-only and runs without confirmation.

`open_file` opens one of those `Label/relative/path` values in its default macOS
app after you confirm. Every path component is checked without following
symbolic links; hidden items, package contents, directories, `.`/`..`, and paths
outside the folder are refused, and only `.pdf .txt .md .csv .docx .xlsx .pptx
.png .jpg .jpeg` files open. The checks run again after you confirm, just
before opening.

`open_application` opens an app from `allowed_apps` (exact name) after you
confirm. Both open tools run the fixed `/usr/bin/open` executable with an
argument list, never a shell, and are macOS-only.

### Voice (push-to-talk)

Voice is optional and fully local: speech is transcribed by
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) and replies are spoken by
macOS `say`. No audio or text leaves the machine. One-time setup (the only step
that uses the network):

```sh
brew install ffmpeg whisper.cpp
mkdir -p data/models
curl -fL --proto '=https' --tlsv1.2 -o data/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Compare `shasum -a 256 data/models/ggml-base.en.bin` with the SHA-256 shown for
the file on Hugging Face. `config.ini.example` already contains the `[voice]`
section (copy it into an existing `config.ini` if needed), then restart U. The
example paths are for Homebrew on Apple Silicon; on an Intel Mac use the paths
from `command -v ffmpeg whisper-cli` (usually `/usr/local/bin`). If FFmpeg,
whisper.cpp, or the model is missing, U logs a warning and hides push-to-talk;
without the `[voice]` section, all voice controls are hidden. Typed chat is
unchanged either way.

In Chat, hold the microphone button (or hold Space/Enter on it) while speaking
and release to stop; recordings are capped at `max_seconds` (60 by default).
FFmpeg converts the browser recording to 16 kHz mono WAV and whisper.cpp
transcribes it. Voice requests always go through the same chat path, tool
rules, and Confirm/Cancel buttons as typed ones.

- **Review mode (default).** The transcript is placed in the message box for you
  to review and edit; nothing is sent until you press Enter.
- **Hands-free (opt-in, off by default).** Tick "Hands-free" to send each
  transcript automatically when you release the button, and to hear the reply,
  any confirmation prompt, and any error for that exchange spoken aloud, even if
  "Speak replies" is off. If you had already typed something in the message box,
  the transcript is added to it for review instead of being sent. The setting is
  not remembered; reloading the page turns it off.

**Confirmations by voice.** Only the Confirm button approves an action. While an
action waits for confirmation:
- saying just "yes", "okay", "go ahead", or a similar short approval does not
  approve it and is not sent; U asks you to press Confirm instead;
- saying just "no", "cancel", "never mind", or a similar short refusal acts
  exactly like the Cancel button, so the action is not run;
- saying anything longer, such as "no, open the March one instead", is an
  ordinary new message, which cancels the waiting action (see "Changing your
  mind") and is then answered.

Only a short reply on its own is treated this way; the classification is a fixed
list of phrases, not the model's judgement.

**Silent recordings.** whisper.cpp tends to transcribe silence as "you", so
before running it U measures the converted recording. Unless at least 0.12
seconds of it reaches −57 dBFS (measured in 30 ms windows), U skips whisper.cpp
and reports "No speech was detected" in both modes; nothing is sent. The
threshold was chosen from a quiet room's microphone noise floor (loudest window
about −59 dBFS) and the quietest speech whisper.cpp still transcribed correctly in
that room (about −55 dBFS). Brief noises such as typing can still pass the check;
whisper.cpp then usually returns nothing, which is reported the same way. If your
quiet speech is rejected, lower `SPEECH_LEVEL_DBFS` in `voice.py`; U logs each
recording's measured level (numbers only) at INFO level to help with tuning.

"Speak replies" (off by default) reads each final reply aloud with `say`, using
`say_voice` if set (list voices with `say -v '?'`). When speech is on, the
confirmation prompt and cancellation messages are spoken too. Long replies are
spoken up to about 1,500 characters. Turning the toggle off, turning
Hands-free off, recording, or starting a new conversation stops playback.

Recordings are limited to 3 MiB and WebM, Ogg, or MP4 audio; conversion,
transcription, and speech each have a timeout, and one transcription runs at a
time. Audio and text exist only in a private temporary directory during a
request and are deleted afterwards; they are never logged or stored (only the
loudness numbers above are logged). Requests that change anything, including
uploading a recording, are refused (HTTP 403) when they come from another
website, so other pages cannot make U transcribe or speak. To run the
local speech round-trip test (no microphone needed):

```sh
VOICE_SMOKE=1 .venv/bin/python -m unittest tests.test_voice -v
```

Assistant responses render Markdown with locally served `markdown-it` 15.0.2
and DOMPurify 3.4.16 bundles in `static/vendor/`. Raw HTML is disabled, parsed
output is sanitized against an allowlist, and unsafe URL schemes are removed.
Safe HTTP(S) links open in a new tab with `noopener noreferrer`. The browser
check covers headings, emphasis, nested lists, links, code, tables, blockquotes,
horizontal rules, multiline text, malformed responses, and raw-HTML/script
injection. The sampled live model response did not contain LaTeX-style math.
Math is not rendered; delimiters remain ordinary text, since a math engine
would add disproportionate assets for this milestone.

To verify, confirm the header reports the configured model as ready, then send
`What is 17 × 23?` followed by `What is 17 × 24? Find the difference between the
previous and current results.` U should use the previous answer and return
`391`, `408`, and `17`. The health check is also available at
<http://127.0.0.1:5000/api/health>.
