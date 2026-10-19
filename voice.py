"""Local speech: whisper.cpp transcription, spoken replies (Kokoro-82M, with macOS `say` as
the fallback), and the local check behind the "Kris" wake word.

Audio and text exist only inside a private temporary directory for the duration of one
request and are never logged or stored. External programs run from configured absolute
paths with argument lists (never a shell) and timeouts.
"""

import array
import logging
import math
import re
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path


SAY_COMMAND = Path("/usr/bin/say")
MAX_AUDIO_BYTES = 3 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 2_000
MAX_SPEECH_CHARS = 1_500  # one /api/voice/speak request (one segment, or a whole short text)
# Spoken replies are split into sentence-sized segments (speech_segments) that the browser
# synthesizes one after another while the previous one plays.
MAX_SPOKEN_REPLY_CHARS = 4_000
# Synthesis time grows with segment length, so a first sentence longer than this is split at
# its first clause break (never mid-clause): the first audio then starts sooner.
FIRST_SEGMENT_CHARS = 80
MAX_SEGMENT_CHARS = 280
MIN_SEGMENT_CHARS = 20  # shorter pieces ("Sure.") are joined to the next one
DEFAULT_KOKORO_MODEL = "data/models/kokoro-v1.0.onnx"
DEFAULT_KOKORO_VOICES = "data/models/voices-v1.0.bin"
DEFAULT_KOKORO_VOICE = "af_heart"
KOKORO_VOICE_NAME = re.compile(r"^[a-z]{2}_[a-z]{2,20}$")
MAX_SPEECH_AUDIO_BYTES = 12 * 1024 * 1024
CONVERT_TIMEOUT_SECONDS = 20
TRANSCRIBE_TIMEOUT_SECONDS = 60
SPEAK_TIMEOUT_SECONDS = 60
DEFAULT_MAX_SECONDS = 60
# Hands-free automatic end-of-speech detection (done in the browser; see
# static/voice_activity.js). These are passed to the page through /api/voice/status.
DEFAULT_END_SILENCE_MS = 1200
DEFAULT_MIN_RECORDING_MS = 600
END_SILENCE_MS_RANGE = (500, 4000)
MIN_RECORDING_MS_RANGE = (200, 3000)
NO_SPEECH_TIMEOUT_MS = 6000
# Wake word ("Kris", said like "Chris"). In standby the browser sends only short, isolated
# sounds (at most WAKE_MAX_CLIP_SECONDS) as 16 kHz WAV; whisper.cpp transcribes the clip here
# and only a clip that is just the name (optionally after "hey"/"okay") counts. The text is
# never returned, logged, or kept. "Kris" and "Chris" sound the same, so whisper may write
# either; similar words it writes differently ("crisis", "Christmas", "kiss") do not count.
WAKE_MAX_CLIP_SECONDS = 2.5
MAX_WAKE_AUDIO_BYTES = 128 * 1024  # 16 kHz 16-bit mono: about 4 s, so a 2.5 s clip fits
WAKE_NAMES = frozenset({"kris", "chris", "cris", "kriss", "chriss", "krys"})
_WAKE_LEADING = frozenset({"hey", "hi", "okay", "ok", "oh", "um", "uh", "yo"})
DEFAULT_WAKE_FOLLOW_UP_SECONDS = 8
WAKE_FOLLOW_UP_SECONDS_RANGE = (3, 30)
# Loudness check on the converted 16 kHz mono WAV, before whisper.cpp runs (whisper
# transcribes silence as "you"). A recording counts as speech only if at least
# MIN_SPEECH_SECONDS of 30 ms windows reach SPEECH_LEVEL_DBFS. The threshold sits just
# above a measured quiet-room microphone noise floor (loudest 30 ms window -58.7 dBFS)
# and below the quietest speech whisper still transcribed correctly in that room
# (loudest window -55 dBFS); requiring a duration ignores single clicks.
SPEECH_LEVEL_DBFS = -57.0
MIN_SPEECH_SECONDS = 0.12
LEVEL_WINDOW_SECONDS = 0.03
NO_SPEECH_MESSAGE = "No speech was detected. Hold the button and speak, then release."

# Browser MediaRecorder formats: MIME type -> (file suffix, forced FFmpeg demuxer, magic check).
AUDIO_FORMATS = {
    "audio/webm": (".webm", "matroska", lambda data: data[:4] == b"\x1a\x45\xdf\xa3"),
    "audio/ogg": (".ogg", "ogg", lambda data: data[:4] == b"OggS"),
    "audio/mp4": (".m4a", "mov", lambda data: data[4:8] == b"ftyp"),
    # Wake-word clips, encoded in the page from its standby buffer.
    "audio/wav": (".wav", "wav", lambda data: data[:4] == b"RIFF" and data[8:12] == b"WAVE"),
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
    end_silence_ms: int = DEFAULT_END_SILENCE_MS
    min_recording_ms: int = DEFAULT_MIN_RECORDING_MS
    # How long a conversation started by the wake word waits for you to speak (after the
    # acknowledgment, and after each reply) before returning to wake-word standby.
    wake_follow_up_seconds: int = DEFAULT_WAKE_FOLLOW_UP_SECONDS
    # Kokoro-82M speech (both files present): the natural voice; `say` is the fallback.
    kokoro_model: Path | None = None
    kokoro_voices: Path | None = None
    kokoro_voice: str = DEFAULT_KOKORO_VOICE
    kokoro_speed: float = 1.0

    @property
    def stt_available(self) -> bool:
        return all(
            path is not None and path.is_file()
            for path in (self.ffmpeg, self.whisper_cli, self.whisper_model)
        )

    @property
    def kokoro_configured(self) -> bool:
        return self.kokoro_model is not None and self.kokoro_voices is not None

    @property
    def tts_available(self) -> bool:
        return self.speech_output and (self.kokoro_configured or SAY_COMMAND.is_file())

    @property
    def tts_engine(self) -> str:
        return "kokoro" if self.kokoro_configured else "say"


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
    end_silence_ms = _bounded_ms(values, "end_silence_ms", DEFAULT_END_SILENCE_MS, END_SILENCE_MS_RANGE)
    min_recording_ms = _bounded_ms(values, "min_recording_ms", DEFAULT_MIN_RECORDING_MS, MIN_RECORDING_MS_RANGE)
    if min_recording_ms >= max_seconds * 1000:
        logger.warning("Voice: min_recording_ms must be shorter than max_seconds; using the default.")
        min_recording_ms = min(DEFAULT_MIN_RECORDING_MS, max_seconds * 500)
    try:
        wake_follow_up = int(values.get("wake_follow_up_seconds", DEFAULT_WAKE_FOLLOW_UP_SECONDS))
    except ValueError:
        logger.warning("Voice: wake_follow_up_seconds must be a whole number; using the default.")
        wake_follow_up = DEFAULT_WAKE_FOLLOW_UP_SECONDS
    kokoro_model = kokoro_voices = None
    kokoro_voice = values.get("kokoro_voice", DEFAULT_KOKORO_VOICE).strip() or DEFAULT_KOKORO_VOICE
    if not KOKORO_VOICE_NAME.fullmatch(kokoro_voice):
        logger.warning("Voice: kokoro_voice is not a valid voice name; using %s.", DEFAULT_KOKORO_VOICE)
        kokoro_voice = DEFAULT_KOKORO_VOICE
    try:
        kokoro_speed = min(max(float(values.get("kokoro_speed", "1.0")), 0.5), 2.0)
    except ValueError:
        logger.warning("Voice: kokoro_speed must be a number; using 1.0.")
        kokoro_speed = 1.0
    engine = values.get("tts_engine", "kokoro").strip().lower()
    if engine not in {"kokoro", "say"}:
        logger.warning("Voice: tts_engine must be kokoro or say; using kokoro.")
        engine = "kokoro"
    if engine == "kokoro" and speech_output:
        found = []
        for key, default in (("kokoro_model", DEFAULT_KOKORO_MODEL), ("kokoro_voices", DEFAULT_KOKORO_VOICES)):
            candidate = Path(values.get(key, "").strip() or default).expanduser()
            if not candidate.is_absolute():
                candidate = base_directory / candidate
            found.append(candidate.resolve() if candidate.is_file() else None)
        if all(found):
            kokoro_model, kokoro_voices = found
        else:
            logger.warning("Voice: the Kokoro model or voices file was not found; spoken replies use macOS say.")
    low, high = WAKE_FOLLOW_UP_SECONDS_RANGE
    wake_follow_up = min(max(wake_follow_up, low), high)
    if wake_follow_up >= max_seconds:
        # The recording's hard limit would end it first and upload silence.
        logger.warning("Voice: wake_follow_up_seconds must be shorter than max_seconds; shortening it.")
        wake_follow_up = max(max_seconds - 1, 1)
    return VoiceSettings(
        ffmpeg, whisper_cli, model, voice, language, max_seconds, speech_output,
        end_silence_ms, min_recording_ms, wake_follow_up,
        kokoro_model, kokoro_voices, kokoro_voice, kokoro_speed,
    )


def _bounded_ms(values: dict[str, str], name: str, default: int, bounds: tuple[int, int]) -> int:
    """Read a whole number of milliseconds within bounds; anything else uses the default."""
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Voice: %s must be a whole number of milliseconds; using %d.", name, default)
        return default
    low, high = bounds
    if not low <= value <= high:
        logger.warning("Voice: %s must be between %d and %d; using %d.", name, low, high, default)
        return default
    return value


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


def speech_level(wav_path: Path) -> tuple[float, float]:
    """Return (seconds of 30 ms windows at or above SPEECH_LEVEL_DBFS, loudest window dBFS)
    for a 16-bit mono WAV. Deterministic and standard-library only."""
    try:
        with wave.open(str(wav_path), "rb") as recording:
            if recording.getsampwidth() != 2 or recording.getnchannels() != 1:
                raise VoiceError("Local audio conversion produced unusable audio.")
            rate = recording.getframerate()
            samples = array.array("h", recording.readframes(recording.getnframes()))
    except (wave.Error, EOFError, OSError) as error:
        raise VoiceError("Local audio conversion produced unusable audio.") from error
    if sys.byteorder == "big":
        samples.byteswap()
    size = max(1, int(rate * LEVEL_WINDOW_SECONDS))
    threshold_energy = (10 ** (SPEECH_LEVEL_DBFS / 20) * 32768) ** 2 * size
    loud_windows = 0
    loudest_energy = 0
    for start in range(0, len(samples) - size + 1, size):
        window = samples[start : start + size]
        energy = sum(value * value for value in window)
        loudest_energy = max(loudest_energy, energy)
        if energy >= threshold_energy:
            loud_windows += 1
    loudest_rms = math.sqrt(loudest_energy / size)
    loudest_db = 20 * math.log10(loudest_rms / 32768) if loudest_rms else float("-inf")
    return loud_windows * size / rate, loudest_db


def transcribe(settings: VoiceSettings, audio: bytes, mime_type: str, max_seconds: float | None = None) -> str:
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
                "-f", demuxer, "-i", str(source), "-t", str(max_seconds or settings.max_seconds),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(speech),
            ],
            CONVERT_TIMEOUT_SECONDS,
            "audio conversion",
        )
        speech_seconds, loudest_db = speech_level(speech)
        # Levels only (never audio or text), to help tune SPEECH_LEVEL_DBFS.
        logger.info("Voice level: loudest %.1f dBFS, %.2fs at or above %.0f dBFS", loudest_db, speech_seconds, SPEECH_LEVEL_DBFS)
        if speech_seconds < MIN_SPEECH_SECONDS:
            raise VoiceError(NO_SPEECH_MESSAGE)
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
        raise VoiceError(NO_SPEECH_MESSAGE)
    return text[:MAX_TRANSCRIPT_CHARS]


# Whole-utterance replies to a pending confirmation. Only a transcript that is *just*
# one of these phrases is classified; anything longer (e.g. "no, open the March one") is
# an ordinary message. Only "confirm" approves, and only the action that was read out before
# the recording started (static/voice_loop.js, confirmationDecision). "affirm" (a bare "yes",
# which background audio and whisper produce easily) never approves anything: the page asks
# for "confirm". "decline" acts like the Cancel button, which can never run an action.
# "unclear" is a short reply that mentions approving but is not a clean "confirm" ("don't
# confirm", "confirm or cancel"); the action keeps waiting and the page asks again.
_CONFIRM_PHRASES = frozenset({
    "confirm", "confirmed", "confirm it", "confirm that", "confirm this", "i confirm",
    "approve", "approved", "approve it", "approve that", "approve this", "i approve",
})
_CONFIRM_LEAD = frozenset({"yes", "yeah", "yep", "ok", "okay", "kris", "chris"})
_APPROVAL_WORDS = frozenset({"confirm", "confirmed", "confirming", "approve", "approved", "approving"})
_AFFIRM_PHRASES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead", "go for it",
    "please do", "yes please", "yes do it", "yes go ahead", "sounds good", "correct",
    "that's right", "thats right", "right",
})
_DECLINE_PHRASES = frozenset({
    "no", "nope", "nah", "no thanks", "no thank you", "cancel", "cancel it", "cancel that",
    "cancel this", "don't", "dont", "do not", "don't do it", "dont do it", "do not do it",
    "stop", "never mind", "nevermind", "forget it", "not now", "no don't", "no dont",
    "no cancel", "no cancel it", "no cancel that",
})
_FILLER = frozenset({"please", "thanks", "thank", "you", "um", "uh", "oh", "well", "u"})


def classify_reply(text: str) -> str | None:
    """Classify a transcript as 'confirm', 'affirm', 'decline', 'unclear', or None (ordinary)."""
    words = re.findall(r"[a-z']+", (text or "").casefold().replace("\u2019", "'"))
    if not words or len(words) > 6:
        return None
    phrase = " ".join(words)
    core_words = [word for word in words if word not in _FILLER]
    core = " ".join(core_words) or phrase
    approval = core_words
    while approval and approval[0] in _CONFIRM_LEAD:
        approval = approval[1:]
    if " ".join(approval) in _CONFIRM_PHRASES:
        return "confirm"
    for candidate in (phrase, core):
        if candidate in _DECLINE_PHRASES:
            return "decline"
        if candidate in _AFFIRM_PHRASES:
            return "affirm"
    if _APPROVAL_WORDS & set(words):
        return "unclear"
    return None


def is_wake_phrase(text: str) -> bool:
    """True only for a transcript that is just the name: "Kris", "Chris.", "Hey Kris!"."""
    words = re.findall(r"[a-z]+", (text or "").casefold())
    while words and words[0] in _WAKE_LEADING:
        words = words[1:]
    return len(words) == 1 and words[0] in WAKE_NAMES


def detect_wake_word(settings: VoiceSettings, audio: bytes) -> bool:
    """Transcribe one short standby clip locally and report only whether it was the name."""
    if len(audio) > MAX_WAKE_AUDIO_BYTES:
        raise VoiceError("The wake-word clip is too long.")
    try:
        text = transcribe(settings, audio, "audio/wav", max_seconds=WAKE_MAX_CLIP_SECONDS)
    except VoiceUnavailable:
        raise
    except VoiceError as error:
        if str(error) == NO_SPEECH_MESSAGE:
            return False  # too quiet, or whisper heard nothing
        raise
    return is_wake_phrase(text)


# --- Preparing text for speech -----------------------------------------------------------
# Abbreviations whose full stop does not end a sentence (compared without the final ".").
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc", "e.g", "i.e", "eg", "ie",
    "a.m", "p.m", "no", "nos", "approx", "inc", "ltd", "co", "corp", "dept", "fig", "est", "min",
    "max", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "u.s", "u.k", "e.u", "ph.d", "cf", "al",
})
_BEFORE_NUMBER = frozenset({
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "no", "nos", "fig",
})
_SENTENCE_END = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019)\]]*(?=\s)")
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_DECIMAL = re.compile(r"(?<![\d.,$\u00a3\u20ac])(\d+)\.(\d+)(?![\d.]|\.\d)")
_MARKUP = re.compile(r"[*_`#>|~]+")
_LIST_MARKER = re.compile(r"^\s*(?:[-\u2022\u2013]\s+)")


# Speech-only rewrites. Kokoro (through espeak-ng) pauses at every full stop, comma or colon
# inside a token, so "Dr. Patel", "J. Smith", "7:45 a.m." and "$1,249.99" were spoken with
# breaks ("dollar one, two hundred..."). These are rewritten for the voice only; the text
# shown in the chat is unchanged. Measured with silent-gap checks on the synthesized audio.
# "ada@example.org" paused inside the address; it is read as "ada at example dot org".
_EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
_EMAIL_SYMBOLS = {".": " dot ", "_": " underscore ", "-": " dash ", "+": " plus "}


def _email(match: re.Match) -> str:
    spell = lambda part: "".join(_EMAIL_SYMBOLS.get(char, char) for char in part)
    return f"{spell(match.group(1))} at {spell(match.group(2))}"


_TITLES = {"dr": "Doctor", "mr": "Mister", "mrs": "Missus", "ms": "Miz", "prof": "Professor"}
_TITLE = re.compile(r"\b(Dr|Mr|Mrs|Ms|Prof)\.(?=\s+[A-Z])")
_INITIAL = re.compile(r"(?<![.\w])([A-Z])\.(?=\s+[A-Z][a-z])")
_CLOCK = re.compile(
    r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])"
    r"(?:\s*(a\.m\.|p\.m\.|am\b|pm\b|AM\b|PM\b))?"
)
_MERIDIEM = re.compile(r"(?<![\w.])([aApP])\.[mM]\.(?=\s+[A-Z]|\s*$)|(?<![\w.])([aApP])\.[mM]\.")
_MONEY = re.compile(
    r"([$\u00a3\u20ac])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?!\d)"
    r"(\s+(?:thousand|million|billion|trillion)\b)?"
)
_CURRENCIES = {"$": ("dollar", "dollars", "cent", "cents"), "\u00a3": ("pound", "pounds", "penny", "pence"),
               "\u20ac": ("euro", "euros", "cent", "cents")}
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# Dates: "12 March" is read "twelve March"; people say "the twelfth of March" (day first)
# or "March twelfth" (month first). Abbreviated months are written out.
_MONTHS = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June",
    "jul": "July", "aug": "August", "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}
_MONTH = (r"(January|February|March|April|May|June|July|August|September|October|November|December"
          r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)")
_DAY = r"([1-9]|[12]\d|3[01])"
# An abbreviation's full stop is dropped only when the sentence continues ("Jan. 25", "25 Jan. at").
_ABBREVIATION_STOP = r"(\.(?=\s+[a-z\d]))?"
_DAY_MONTH = re.compile(r"(?<![\d.,:/])(?<!\bthe )\b" + _DAY + r"\s+" + _MONTH + _ABBREVIATION_STOP + r"(?![A-Za-z])")
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"(\.)?\s+" + _DAY + r"(?!\d|[:.]\d|st|nd|rd|th)\b")
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _small_number(value: int) -> str:
    """0-59 in words ("forty-five")."""
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _clock(match: re.Match) -> str:
    """ "7:45 a.m." -> "seven forty-five AM", "9:30" -> "nine thirty", "9:05" -> "nine oh five",
    "14:00" -> "fourteen hundred", "9:00 PM" -> "nine PM"."""
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3)
    suffix = f" {meridiem.replace('.', '').upper()}" if meridiem else ""
    if minute == 0:
        spoken = f"{_small_number(hour)}{suffix}" if suffix or hour <= 12 else f"{_small_number(hour)} hundred"
        return spoken if suffix or hour > 12 else f"{spoken} o'clock"
    minutes = f"oh {_small_number(minute)}" if minute < 10 else _small_number(minute)
    return f"{_small_number(hour)} {minutes}{suffix}"


# A number right after another capitalized word belongs to it ("Terminal 2 March 12", "Gate 3
# May"), except after a weekday or a sentence-opening preposition ("Friday 25 January").
_DATE_LEAD_INS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue", "tues",
    "wed", "thu", "thur", "thurs", "fri", "sat", "sun", "on", "by", "from", "until", "till", "since",
    "before", "after", "due", "until", "to", "and", "or", "between",
})


def _day_month(match: re.Match) -> str:
    before = re.search(r"([A-Za-z]+) $", match.string[: match.start()])
    if before and before.group(1)[0].isupper() and before.group(1).lower() not in _DATE_LEAD_INS:
        return match.group(0)
    return f"the {_ordinal(match.group(1))} of {_month(match.group(2))}"


def _ordinal(day: str) -> str:
    number = int(day)
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _month(name: str) -> str:
    return _MONTHS.get(name.lower(), name)


def _money(match: re.Match) -> str:
    """ "$1,249.99" -> "1249 dollars and 99 cents", "$5 million" -> "5 million dollars"."""
    one, many, cent, cents = _CURRENCIES[match.group(1)]
    whole = match.group(2).replace(",", "")
    fraction, scale = match.group(3), (match.group(4) or "").strip()
    if scale or (fraction and len(fraction) != 2):  # "$2.5 billion", "$2.5": not cents
        return f"{whole}{'.' + fraction if fraction else ''}{' ' + scale if scale else ''} {many}"
    parts = []
    if whole != "0" or not fraction or fraction == "00":
        parts.append(f"{whole} {one if whole == '1' else many}")
    if fraction and fraction != "00":
        parts.append(f"{int(fraction)} {cent if fraction == '01' else cents}")
    return " and ".join(parts)


def speech_text(text: str) -> str:
    """Rewrite one line for the voice only: drop leftover markup and URLs; say titles, initials,
    clock times, a.m./p.m., money and thousands without the internal full stops, colons and
    commas that make Kokoro pause; say dates as "the 12th of March" / "March 12th"; read plain
    decimals as "3 point 5". Version numbers ("1.2.3") are left as written."""
    text = _URL.sub("a link", text)
    text = _EMAIL.sub(_email, text)  # before markup removal, which would drop "_"
    text = _MARKUP.sub(" ", _LIST_MARKER.sub("", text))
    text = _TITLE.sub(lambda match: _TITLES[match.group(1).lower()], text)
    text = _INITIAL.sub(r"\1", text)
    text = _MONEY.sub(_money, text)
    text = _DAY_MONTH.sub(_day_month, text)
    text = _MONTH_DAY.sub(lambda match: f"{_month(match.group(1))} {_ordinal(match.group(3))}", text)
    text = _CLOCK.sub(_clock, text)
    # A bare "a.m." that ends a sentence keeps its full stop as the sentence end.
    text = _MERIDIEM.sub(lambda match: f"{(match.group(1) or match.group(2)).upper()}M"
                         + ("." if match.group(1) else ""), text)
    text = _THOUSANDS.sub("", text)
    text = _DECIMAL.sub(r"\1 point \2", text)
    return " ".join(text.split())


def _sentences(line: str) -> list[str]:
    pieces, start = [], 0
    for match in _SENTENCE_END.finditer(line):
        punctuation = match.group(0)
        token = line[: match.start() + 1].rsplit(None, 1)[-1].rstrip(".").lower()
        following = line[match.end():].lstrip()[:1]
        if following.islower():
            continue  # "approx. five", "wait… then": the sentence goes on
        if punctuation.startswith(".") and len(punctuation.rstrip("\"'\u201d\u2019)]")) == 1:
            # "Dr. Patel", "7:45 a.m. Friday", initials ("J. Smith"): not a sentence end. A month
            # or "No." only continues before a number ("Jan. 25", "No. 5"), else it ends one.
            if token in _BEFORE_NUMBER:
                if following.isdigit():
                    continue
            elif token in _ABBREVIATIONS or re.fullmatch(r"[a-z]", token):
                continue
        pieces.append(line[start:match.end()].strip())
        start = match.end()
    pieces.append(line[start:].strip())
    return [piece for piece in pieces if piece]


def _split_long(text: str, limit: int) -> list[str]:
    """Split at the last clause break (;, :, —, ,) within the limit, else at a space."""
    parts = []
    while len(text) > limit:
        window = text[: limit + 1]
        cut = max(window.rfind(mark) for mark in ("; ", ": ", " \u2014 ", ", "))
        if cut < limit * 0.4:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        else:
            cut += 1  # keep the punctuation with the first part
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def speech_segments(text: str) -> tuple[list[str], bool]:
    """Split a reply into sentence-sized segments for speech; returns (segments, truncated).

    Splits only at sentence ends (not after abbreviations, initials or decimal points) and
    line breaks; a sentence longer than MAX_SEGMENT_CHARS is split at a clause break. The
    first segment is kept short so the first audio arrives sooner, and tiny pieces are joined
    to the next one so the voice does not sound choppy."""
    sentences = []
    for line in (speech_text(raw) for raw in (text or "").splitlines()):
        if not line:
            continue
        if not re.search(r"[.!?\u2026:;,][\"'\u201d\u2019)\]]*$", line):
            line += "."  # a list item or heading still gets a pause after it
        sentences.extend(_sentences(line))
    pieces = [part for sentence in sentences for part in _split_long(sentence, MAX_SEGMENT_CHARS)]
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) < MIN_SEGMENT_CHARS and len(merged[-1]) + 1 + len(piece) <= MAX_SEGMENT_CHARS:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    if merged and len(merged[0]) > FIRST_SEGMENT_CHARS:
        first = merged[0]
        breaks = [first.find(mark, MIN_SEGMENT_CHARS) for mark in ("; ", ": ", " \u2014 ", ", ")]
        cut = min((index for index in breaks if index != -1), default=-1)
        if 0 < cut and len(first) - cut > MIN_SEGMENT_CHARS:
            merged[:1] = [first[: cut + 1].strip(), first[cut + 1:].strip()]
    segments, total, truncated = [], 0, False
    for segment in merged:
        if total + len(segment) > MAX_SPOKEN_REPLY_CHARS:
            truncated = True
            break
        segments.append(segment)
        total += len(segment) + 1
    return segments, truncated


def synthesize(settings: VoiceSettings, text: str) -> tuple[bytes, bool]:
    """Speak one text (normally one segment) as WAV; returns (audio, truncated).

    Uses Kokoro when it is configured and loads; if Kokoro fails, macOS `say` speaks instead
    so a reply is never lost (the failure is logged without the text)."""
    if not settings.tts_available:
        raise VoiceUnavailable("Local speech output is not available.")
    spoken = " ".join((text or "").split())
    if not spoken:
        raise VoiceError("There is no text to speak.")
    truncated = len(spoken) > MAX_SPEECH_CHARS
    if truncated:
        spoken = spoken[:MAX_SPEECH_CHARS].rsplit(" ", 1)[0]
    if settings.kokoro_configured:
        import kokoro_speech

        engine = kokoro_speech.engine(
            settings.kokoro_model, settings.kokoro_voices, settings.kokoro_voice, settings.kokoro_speed
        )
        try:
            return engine.synthesize(speech_text(spoken)), truncated
        except kokoro_speech.KokoroUnavailable as error:
            if not SAY_COMMAND.is_file():
                raise VoiceUnavailable(str(error)) from None
            logger.warning("Kokoro speech failed (%s); using macOS say for this segment.", error)

    with tempfile.TemporaryDirectory(prefix="project-u-voice-") as directory:
        # Text goes through a file, so content such as "-v" is never parsed as an option.
        text_file = Path(directory) / "speech.txt"
        output = Path(directory) / "speech.wav"
        text_file.write_text(spoken, encoding="utf-8")
        voice_arguments = ["-v", settings.say_voice] if settings.say_voice else []  # say fallback
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


def preload_speech(settings: VoiceSettings) -> None:
    """Load Kokoro in the background at server start, so the first reply is not a cold start."""
    if not (settings.speech_output and settings.kokoro_configured):
        return
    import threading

    import kokoro_speech

    engine = kokoro_speech.engine(
        settings.kokoro_model, settings.kokoro_voices, settings.kokoro_voice, settings.kokoro_speed
    )
    threading.Thread(target=engine.preload, name="kokoro-preload", daemon=True).start()
