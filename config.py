import configparser
from pathlib import Path

from computer_utilities import UtilitySettings, build_utility_settings


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

def load_utility_settings(
    config_path: Path = CONFIG_PATH, home: Path | None = None
) -> UtilitySettings:
    """Read approved folders and applications; a missing section disables those tools."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep folder labels as written
    if not parser.read(config_path):
        return UtilitySettings()
    folders = dict(parser.items("utility_folders")) if parser.has_section("utility_folders") else {}
    apps = parser.get("utilities", "allowed_apps", fallback="").split(",")
    return build_utility_settings(folders, apps, home)
