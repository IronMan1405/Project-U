# Project U

A local assistant with a terminal client and a lightweight web chat interface, powered by Ollama.

## Setup

Requires Python 3 and Ollama with a model already installed. If `config.ini` is
not present, create it from the example:

```sh
cp config.ini.example config.ini
```

Set `model` in `config.ini` to a model shown by `ollama list`. The default
configuration uses `gemma4:e4b` and the local Ollama endpoint
`http://127.0.0.1:11434`. Make sure Ollama is running before starting U.

Run the automated profile and client tests with:

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
python3 main.py
```

Enter a message to chat. Type `/quit` or `/exit` to leave.

## Web interface

Install the web dependency and start the local server:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python web_app.py
```

Open <http://127.0.0.1:5000>. The server binds to localhost and uses the same
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
deletes its tasks and notes. These operations are explicit UI/API actions and
are not performed automatically by the model.

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
protocol. U can list/create/complete/reopen/delete tasks; list/read/create/edit/
delete notes; report local time, battery, memory, and disk status; and request a
website opening in the default browser. Task and note actions use the existing
SQLite storage layer. No model-generated shell commands or code are executed.

The runtime allows at most four tool executions per user turn. Read-only tools
run directly. A task/note write runs directly only when the user message clearly
requests it; ambiguous writes, every deletion, and every website opening pause
for explicit approval. Web users get Confirm/Cancel controls; the terminal asks
for a yes/no response. Tool errors and unavailable system readings are reported
as such rather than guessed.

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
