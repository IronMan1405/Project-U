import configparser
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config.ini"


def load_settings() -> tuple[str, str]:
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH.name}. Copy config.ini.example to config.ini and configure it."
        )

    endpoint = parser.get("ollama", "endpoint").strip().rstrip("/")
    model = parser.get("ollama", "model").strip()
    if not endpoint or not model:
        raise ValueError("The Ollama endpoint and model must both be configured.")

    return endpoint, model