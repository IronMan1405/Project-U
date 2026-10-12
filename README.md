# Project U

A local assistant with a terminal client and a lightweight web chat interface, powered by Ollama.

## Setup

Requires Python 3 and Ollama with a model already installed. If `config.ini` is
not present, create it from the example:

```sh
cp config.ini.example config.ini
```

Set `model` in `config.ini` to a model shown by `ollama list`. The default
configuration uses `gemma4:12b` and the local Ollama endpoint
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
