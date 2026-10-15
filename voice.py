"""Local push-to-talk speech: whisper.cpp transcription and macOS `say` speech output.

Audio and text exist only inside a private temporary directory for the duration of one
request and are never logged or stored. External programs run from configured absolute
paths with argument lists (never a shell) and timeouts.
"""

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


SAY_COMMAND = Path("/usr/bin/say")
MAX_AUDIO_BYTES = 3 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 2_000
MAX_SPEECH_CHARS = 1_500
MAX_SPEECH_AUDIO_BYTES = 12 * 1024 * 1024
CONVERT_TIMEOUT_SECONDS = 20
TRANSCRIBE_TIMEOUT_SECONDS = 60
SPEAK_TIMEOUT_SECONDS = 60
DEFAULT_MAX_SECONDS = 60

# Browser MediaRecorder formats: MIME type -> (file suffix, forced FFmpeg demuxer, magic check).
AUDIO_FORMATS = {
    "audio/webm": (".webm", "matroska", lambda data: data[:4] == b"\x1a\x45\xdf\xa3"),
    "audio/ogg": (".ogg", "ogg", lambda data: data[:4] == b"OggS"),
    "audio/mp4": (".m4a", "mov", lambda data: data[4:8] == b"ftyp"),
}
_NON_SPEECH_MARKER = re.compile(r"\[[^\]]*\]")
VOICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()._'-]{0,63}$")
LANGUAGE_CODE = re.compile(r"^[a-z]{2}$")

logger = logging.getLogger(__name__)


class VoiceError(ValueError):
    """The request could not be handled; the message is safe to show the user."""


class VoiceUnavailable(VoiceError):
    pass


class VoiceTimeout(VoiceError):
    pass


@dataclass(frozen=True)
class VoiceSettings:
    ffmpeg: Path | None = None
    whisper_cli: Path | None = None
    whisper_model: Path | None = None
    say_voice: str | None = None
    language: str = "en"
    max_seconds: int = DEFAULT_MAX_SECONDS
    speech_output: bool = False

    @property
    def stt_available(self) -> bool:
        return all(
            path is not None and path.is_file()
            for path in (self.ffmpeg, self.whisper_cli, self.whisper_model)
        )

    @property
    def tts_available(self) -> bool:
        return self.speech_output and SAY_COMMAND.is_file()


def _executable(raw: str, name: str) -> Path | None:
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        logger.warning("Voice: %s must be an absolute path; speech input is disabled.", name)
        return None
    if not path.is_file() or not path.stat().st_mode & 0o111:
        logger.warning("Voice: %s was not found or is not executable; speech input is disabled.", name)
        return None
    return path


def build_voice_settings(values: dict[str, str], base_directory: Path) -> VoiceSettings:
    """Validate a [voice] config section. Problems disable the affected feature, never chat."""
    ffmpeg = _executable(values.get("ffmpeg", ""), "ffmpeg") if values.get("ffmpeg") else None
    whisper_cli = (
        _executable(values.get("whisper_cli", ""), "whisper_cli") if values.get("whisper_cli") else None
    )
    model = None
    if values.get("whisper_model"):
        candidate = Path(values["whisper_model"].strip()).expanduser()
        if not candidate.is_absolute():
            candidate = base_directory / candidate
        if candidate.is_file():
            model = candidate.resolve()
        else:
            logger.warning("Voice: whisper_model was not found; speech input is disabled.")

    voice = values.get("say_voice", "").strip() or None
    if voice is not None and not VOICE_NAME.fullmatch(voice):
        logger.warning("Voice: say_voice is not a valid voice name; using the system voice.")
        voice = None
    language = values.get("language", "en").strip().lower() or "en"
    if not LANGUAGE_CODE.fullmatch(language):
        logger.warning("Voice: language must be a two-letter code; using en.")
        language = "en"
    try:
        max_seconds = int(values.get("max_seconds", DEFAULT_MAX_SECONDS))
    except ValueError:
        max_seconds = DEFAULT_MAX_SECONDS
    max_seconds = min(max(max_seconds, 1), 120)
    speech_output = values.get("speech_output", "on").strip().lower() not in {"off", "no", "false", "0"}
    return VoiceSettings(ffmpeg, whisper_cli, model, voice, language, max_seconds, speech_output)


def _run(arguments: list[str], timeout: int, action: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise VoiceTimeout(f"Local {action} took too long and was stopped.") from error
    except OSError as error:
        raise VoiceUnavailable(f"Local {action} is unavailable.") from error
    if result.returncode != 0:
        # stderr can describe the audio but never contains the transcript; keep it short.
        logger.warning("Voice %s failed (exit %s): %s", action, result.returncode, result.stderr[-300:])
        raise VoiceError(f"Local {action} failed.")
    return result


def audio_format(mime_type: str):
    """Return (file suffix, FFmpeg demuxer, magic-byte check) for an allowed MIME type."""
    base_type = (mime_type or "").split(";", 1)[0].strip().lower()
    if base_type not in AUDIO_FORMATS:
        raise VoiceError("Unsupported recording format; use WebM, Ogg, or MP4 audio.")
    return AUDIO_FORMATS[base_type]


def transcribe(settings: VoiceSettings, audio: bytes, mime_type: str) -> str:
    """Convert one browser recording to 16 kHz mono WAV with FFmpeg and transcribe it."""
    if not settings.stt_available:
        raise VoiceUnavailable("Local speech recognition is not configured.")
    if not audio:
        raise VoiceError("The recording was empty.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("The recording is too large.")
    suffix, demuxer, matches_format = audio_format(mime_type)
    if not matches_format(audio):
        raise VoiceError("The recording does not match its declared format.")

    with tempfile.TemporaryDirectory(prefix="project-u-voice-") as directory:
        source = Path(directory) / f"recording{suffix}"
        speech = Path(directory) / "speech.wav"
        source.write_bytes(audio)
        _run(
            [
                str(settings.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", demuxer, "-i", str(source), "-t", str(settings.max_seconds),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(speech),
            ],
            CONVERT_TIMEOUT_SECONDS,
            "audio conversion",
        )
        result = _run(
            [
                str(settings.whisper_cli), "-m", str(settings.whisper_model), "-f", str(speech),
                "-l", settings.language, "-nt", "-np",
            ],
            TRANSCRIBE_TIMEOUT_SECONDS,
            "transcription",
        )
    text = " ".join(_NON_SPEECH_MARKER.sub(" ", result.stdout).split())
    if not text:
        raise VoiceError("No speech was detected. Hold the button and speak, then release.")
    return text[:MAX_TRANSCRIPT_CHARS]


def synthesize(settings: VoiceSettings, text: str) -> tuple[bytes, bool]:
    """Speak text with macOS `say` into a WAV file; returns (audio, truncated)."""
    if not settings.tts_available:
        raise VoiceUnavailable("Local speech output is not available.")
    spoken = " ".join((text or "").split())
    if not spoken:
        raise VoiceError("There is no text to speak.")
    truncated = len(spoken) > MAX_SPEECH_CHARS
    if truncated:
        spoken = spoken[:MAX_SPEECH_CHARS].rsplit(" ", 1)[0]

    with tempfile.TemporaryDirectory(prefix="project-u-voice-") as directory:
        # Text goes through a file, so content such as "-v" is never parsed as an option.
        text_file = Path(directory) / "speech.txt"
        output = Path(directory) / "speech.wav"
        text_file.write_text(spoken, encoding="utf-8")
        voice_arguments = ["-v", settings.say_voice] if settings.say_voice else []
        _run(
            [
                str(SAY_COMMAND), *voice_arguments, "-f", str(text_file),
                "--file-format=WAVE", "--data-format=LEI16@22050", "-o", str(output),
            ],
            SPEAK_TIMEOUT_SECONDS,
            "speech output",
        )
        audio = output.read_bytes() if output.is_file() else b""
    if audio[:4] != b"RIFF" or len(audio) > MAX_SPEECH_AUDIO_BYTES:
        raise VoiceError("Local speech output produced unusable audio.")
    return audio, truncated
