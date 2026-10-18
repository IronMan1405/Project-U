"""Natural local speech output with Kokoro-82M (ONNX on the CPU, through kokoro-onnx).

The model loads once per process (lazily, or ahead of time with preload()) and stays in
memory. Each call synthesizes one short segment of text (a sentence or two; voice.py splits
replies), so a long reply never blocks one long inference and playback can start after the
first segment. An inference that has started cannot be interrupted; the browser stops
asking for further segments instead.

Pronunciation uses espeak-ng (bundled by espeakng-loader) through one phonemizer backend kept
for the life of the engine: phonemizer.phonemize(), which kokoro-onnx calls by default, builds
a new backend (copying and initializing espeak-ng) on every call, which cost 0.6-1.8 s per
segment here, more than the speech inference itself. espeak-ng ends the whole process
if it cannot find its data, and it silently ignores data paths longer than its internal
buffer, so the path is checked before espeak-ng is touched.
"""

import io
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
# espeak-ng keeps its data directory in a fixed buffer (160 bytes, including the file names
# it appends); longer paths make it fall back to a build-machine path and exit the process.
MAX_ESPEAK_DATA_PATH = 120
# Leave CPU cores for the local language model and the browser (4 was fastest on an M3).
INFERENCE_THREADS = 4
# kokoro-onnx trims silence from each clip, so segments played back to back would run
# together; a short pause is added after each one, as a speaker would leave.
SENTENCE_PAUSE_SECONDS = 0.28
CLAUSE_PAUSE_SECONDS = 0.14


class KokoroUnavailable(RuntimeError):
    """Kokoro cannot be used; the message is safe to show and log (no text or audio)."""


class KokoroSpeech:
    def __init__(self, model_path: Path, voices_path: Path, voice: str, speed: float):
        self.model_path = model_path
        self.voices_path = voices_path
        self.voice = voice
        self.speed = speed
        self.language = "en-gb" if voice.startswith("b") else "en-us"
        self._model = None
        self._phonemizer = None
        self._error: str | None = None
        self._lock = threading.Lock()  # one load and one inference at a time

    def _load(self):
        if self._model is not None:
            return self._model
        if self._error:
            raise KokoroUnavailable(self._error)
        try:
            import espeakng_loader
            import onnxruntime
            from kokoro_onnx import Kokoro
            from kokoro_onnx.config import EspeakConfig
        except ImportError:
            self._error = "The Kokoro speech packages are not installed."
            raise KokoroUnavailable(self._error) from None
        data_path = espeakng_loader.get_data_path()
        if len(str(data_path)) > MAX_ESPEAK_DATA_PATH:
            self._error = "The espeak-ng data path is too long for espeak-ng; move the project to a shorter path."
            raise KokoroUnavailable(self._error)
        try:
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = INFERENCE_THREADS
            session = onnxruntime.InferenceSession(
                str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            model = Kokoro.from_session(session, str(self.voices_path), espeak_config=EspeakConfig(data_path=data_path))
            if self.voice not in model.get_voices():
                self._error = f"The Kokoro voice {self.voice!r} is not in the voices file."
                raise KokoroUnavailable(self._error)
            from phonemizer.backend import EspeakBackend

            # The same options kokoro-onnx passes to phonemizer.phonemize().
            self._phonemizer = EspeakBackend(self.language, preserve_punctuation=True, with_stress=True)
        except KokoroUnavailable:
            raise
        except Exception as error:  # a broken model or voices file, or onnxruntime failing to start
            logger.warning("Kokoro could not load: %s", type(error).__name__)
            self._error = "The Kokoro speech model could not be loaded."
            raise KokoroUnavailable(self._error) from None
        self._model = model
        return model

    def preload(self) -> bool:
        """Load the model and run one tiny synthesis so the first reply is not a cold start."""
        try:
            self.synthesize("Ready.")
        except KokoroUnavailable as error:
            logger.warning("Kokoro speech is unavailable: %s", error)
            return False
        return True

    def synthesize(self, text: str) -> bytes:
        """Return 24 kHz mono 16-bit WAV for one segment of text."""
        with self._lock:
            model = self._load()
            try:
                phonemes = "".join(self._phonemizer.phonemize([text])[0])
                phonemes = "".join(symbol for symbol in phonemes if symbol in model.tokenizer.vocab).strip()
                if not phonemes:
                    raise KokoroUnavailable("There is nothing pronounceable in this text.")
                samples, rate = model.create(phonemes, voice=self.voice, speed=self.speed, is_phonemes=True)
            except KokoroUnavailable:
                raise
            except Exception as error:  # never log the text
                logger.warning("Kokoro synthesis failed: %s", type(error).__name__)
                raise KokoroUnavailable("Kokoro could not synthesize this text.") from None
        import numpy

        ending = text.rstrip("\"')\u201d\u2019] ")[-1:]
        pause = SENTENCE_PAUSE_SECONDS if ending in ".!?\u2026" else CLAUSE_PAUSE_SECONDS if ending in ",;:" else 0.0
        samples = numpy.concatenate([numpy.asarray(samples, dtype=numpy.float32), numpy.zeros(int(rate * pause), dtype=numpy.float32)])
        pcm = (numpy.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(pcm)
        return buffer.getvalue()


_engines: dict[tuple, KokoroSpeech] = {}
_engines_lock = threading.Lock()


def engine(model_path: Path, voices_path: Path, voice: str, speed: float) -> KokoroSpeech:
    """One shared engine per model/voice configuration, so the model loads once per process."""
    key = (str(model_path), str(voices_path), voice, speed)
    with _engines_lock:
        if key not in _engines:
            _engines[key] = KokoroSpeech(model_path, voices_path, voice, speed)
        return _engines[key]
