import configparser
from pathlib import Path

from computer_utilities import UtilitySettings, build_utility_settings
from developer import DeveloperSettings, build_developer_settings
from resource_alerts import ResourceAlertSettings, build_resource_alert_settings
from google_workspace import GoogleSettings, build_google_settings
from voice import VoiceSettings, build_voice_settings


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


def load_think_setting(config_path: Path = CONFIG_PATH) -> bool | None:
    """[ollama] think: off asks the model to answer without its hidden reasoning pass, on asks for
    it, and leaving it out keeps the model's own default."""
    parser = configparser.ConfigParser()
    parser.read(config_path)
    value = parser.get("ollama", "think", fallback="").strip().lower()
    if value in ("on", "true", "yes"):
        return True
    if value in ("off", "false", "no"):
        return False
    if value:
        print(f"Ignoring ollama think = {value!r}: use on or off")
    return None

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


def load_developer_settings(config_path: Path = CONFIG_PATH, home: Path | None = None) -> DeveloperSettings:
    """Read [projects], [project_tests] and [developer]; without [projects], the developer tools are off."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep project labels as written
    if not parser.read(config_path) or not parser.has_section("projects"):
        return DeveloperSettings()
    tests = dict(parser.items("project_tests")) if parser.has_section("project_tests") else {}
    developer = dict(parser.items("developer")) if parser.has_section("developer") else {}
    return build_developer_settings(dict(parser.items("projects")), tests, developer, home)


def load_resource_alert_settings(config_path: Path = CONFIG_PATH) -> ResourceAlertSettings:
    """[resource_alerts]; without the section the defaults apply (on, notifications, no speech)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    values = dict(parser.items("resource_alerts")) if parser.has_section("resource_alerts") else {}
    return build_resource_alert_settings(values)


def load_google_default_account(config_path: Path = CONFIG_PATH) -> str | None:
    """[google] default_account: the local name of the original account (and the first default)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    return (parser.get("google", "default_account", fallback="") or "").strip() or None


def load_ui_control_settings(config_path: Path = CONFIG_PATH):
    """[ui_control] apps: the apps whose named controls K.R.I.S. may inspect and act on (off if unset)."""
    from ui_control import build_ui_control_settings
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    return build_ui_control_settings(parser.get("ui_control", "apps", fallback=""))


def load_automation_limits(config_path: Path = CONFIG_PATH):
    """[automation]: on/off and the bounds of one run (steps, plan changes, retries, seconds)."""
    from automation import build_automation_limits
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    return build_automation_limits(dict(parser.items("automation")) if parser.has_section("automation") else {})


def load_ui_settings(config_path: Path = CONFIG_PATH) -> dict[str, bool]:
    """[ui] switches. tasks_tab: show the Tasks tab (hidden by default; Apple Reminders is primary)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)
    return {"tasks_tab": parser.getboolean("ui", "tasks_tab", fallback=False)}


def load_voice_settings(config_path: Path = CONFIG_PATH) -> VoiceSettings:
    """Read the [voice] section; without it, push-to-talk and spoken replies are off."""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(config_path) or not parser.has_section("voice"):
        return VoiceSettings()
    return build_voice_settings(dict(parser.items("voice")), Path(config_path).resolve().parent)


def load_google_settings(config_path: Path = CONFIG_PATH) -> GoogleSettings:
    """Read the opt-in [google] section; without it, Google Workspace is off."""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(config_path) or not parser.has_section("google"):
        return GoogleSettings()
    return build_google_settings(dict(parser.items("google")))
