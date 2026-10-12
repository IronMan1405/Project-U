# Project U

A minimal terminal chat client for a locally running Ollama model.

## Setup

Requires Python 3 and Ollama with a model already installed. If `config.ini` is
not present, create it from the example:

```sh
cp config.ini.example config.ini
```

Set `model` in `config.ini` to a model shown by `ollama list`. The default
configuration uses `gemma4:12b` and the local Ollama endpoint
`http://127.0.0.1:11434`. Make sure Ollama is running before starting U.

## Start

From the project directory, run:

```sh
python3 main.py
```

Enter a message to chat. Type `/quit` or `/exit` to leave.
