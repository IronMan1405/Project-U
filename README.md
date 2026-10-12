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

To verify, confirm the header reports the configured model as ready, then send
`What is 17 × 23?` followed by `What is 17 × 24? Find the difference between the
previous and current results.` U should use the previous answer and return
`391`, `408`, and `17`. The health check is also available at
<http://127.0.0.1:5000/api/health>.
