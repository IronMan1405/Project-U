"""Natural voice: Kokoro speech output, sentence segmentation, the say fallback, and /api/voice/plan.

Everything except LocalKokoroTests uses fakes (no model is loaded). LocalKokoroTests
(VOICE_SMOKE=1) runs the real Kokoro model and checks the audio with whisper.cpp; that checks
intelligibility, not how natural the voice sounds, which needs listening.
"""
import io
import os
import re
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy

import config
import kokoro_speech
import voice
import web_app
from computer_utilities import UtilitySettings
from voice import VoiceError, VoiceSettings, VoiceUnavailable


def words(text):
    return re.findall(r"[a-z0-9$]+", text.lower())


class SegmentationTests(unittest.TestCase):
    def segments(self, text):
        return voice.speech_segments(text)[0]

    def test_splits_at_sentence_ends_only(self):
        self.assertEqual(
            self.segments("Your meeting with Dr. Patel is at 7:45 a.m. on Friday. Should I move it? Yes!"),
            ["Your meeting with Dr. Patel is at 7:45 a.m. on Friday.", "Should I move it? Yes!"],
        )

    def test_abbreviations_initials_and_numbers_are_not_sentence_ends(self):
        for text in (
            "I spoke to J. Smith about it yesterday afternoon.",
            "It takes approx. five minutes, e.g. while the kettle boils.",
            "The report is due on Jan. 25 at the latest, I think.",
            "Item No. 5 is the one that failed this morning.",
            "It costs $1,249.99 and version 1.2.3 fixes the bug.",
            "Wait… then it failed again after the restart.",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(self.segments(text)), 1)

    def test_month_or_number_abbreviation_at_a_sentence_end_still_ends_it(self):
        self.assertEqual(
            self.segments("The review is on 25 Jan. It went well overall."),
            ["The review is on 25 Jan.", "It went well overall."],
        )

    def test_numbers_are_kept_readable(self):
        text = " ".join(self.segments("It rose 3.5 percent to $1,249.99 in v1.2.3 at 7:45."))
        self.assertIn("3 point 5 percent", text)
        for kept in ("$1,249.99", "v1.2.3", "7:45"):
            self.assertIn(kept, text)

    def test_markup_urls_and_list_items_are_spoken_cleanly(self):
        segments = self.segments("**Today**\n- Call `John`\n- See https://example.com/a?b=c\n# Done")
        # Each line gets its full stop; short lines are joined so the voice does not sound choppy.
        self.assertEqual(segments, ["Today. Call John. See a link.", "Done."])

    def test_long_sentences_split_at_clause_breaks_and_the_first_segment_is_short(self):
        clause = "the quarterly numbers came in higher than expected across every region"
        text = f"Overall, {clause}; {clause}, and {clause}, while {clause}."
        segments = self.segments(text)
        self.assertEqual(segments[0], "Overall, the quarterly numbers came in higher than expected across every region;")
        self.assertTrue(all(len(segment) <= voice.MAX_SEGMENT_CHARS for segment in segments))
        self.assertEqual(words(" ".join(segments)), words(text))  # nothing lost or reordered

    def test_a_long_first_sentence_splits_at_its_first_clause_break_only(self):
        self.assertEqual(
            self.segments("Your next meeting is the design review with John, at 2:30 PM on Friday, 25 January."),
            ["Your next meeting is the design review with John,", "at 2:30 PM on Friday, 25 January."],
        )
        no_break = "Your next meeting is the design review with John at half past two on Friday afternoon."
        self.assertEqual(self.segments(no_break), [no_break])  # never cut mid-clause

    def test_tiny_pieces_join_the_next_one(self):
        self.assertEqual(self.segments("Sure. I've added it to your tasks."), ["Sure. I've added it to your tasks."])

    def test_no_words_are_lost_across_a_realistic_reply(self):
        reply = (
            "Here's your morning briefing.\n\nCalendar: the design review with John at 2:30 PM, "
            "and a call with Dr. Patel at 4 p.m. about the Q3 budget.\nTasks: Prepare slides; "
            "Water the plants.\nEmail: 3 unread, including one from Ada about the API outage (HTTP 503). "
            "Would you like me to read it?"
        )
        segments = self.segments(reply)
        self.assertGreater(len(segments), 2)
        self.assertEqual(words(" ".join(segments)), words(reply))

    def test_very_long_replies_are_truncated_at_a_segment_boundary(self):
        segments, truncated = voice.speech_segments("This sentence is about twenty words long, give or take a few. " * 200)
        self.assertTrue(truncated)
        self.assertLessEqual(sum(len(s) + 1 for s in segments), voice.MAX_SPOKEN_REPLY_CHARS + 1)
        self.assertTrue(segments[-1].endswith("."))

    def test_empty_or_markup_only_text_has_no_segments(self):
        for text in ("", "   ", "\n\n", "** __ ``"):
            self.assertEqual(voice.speech_segments(text), ([], False))


class EngineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def place_models(self):
        models = self.base / "data" / "models"
        models.mkdir(parents=True)
        (models / "kokoro-v1.0.onnx").write_bytes(b"model")
        (models / "voices-v1.0.bin").write_bytes(b"voices")
        return models

    def test_kokoro_is_used_when_the_default_files_exist(self):
        models = self.place_models()
        settings = voice.build_voice_settings({}, self.base)
        self.assertEqual(settings.tts_engine, "kokoro")
        self.assertEqual(settings.kokoro_model, models / "kokoro-v1.0.onnx")
        self.assertEqual((settings.kokoro_voice, settings.kokoro_speed), ("af_heart", 1.0))
        self.assertTrue(settings.tts_available)

    def test_missing_files_fall_back_to_say_with_a_warning(self):
        with self.assertLogs("voice", "WARNING"):
            settings = voice.build_voice_settings({}, self.base)
        self.assertEqual(settings.tts_engine, "say")

    def test_say_can_be_chosen_and_speech_can_be_off(self):
        self.place_models()
        self.assertEqual(voice.build_voice_settings({"tts_engine": "say"}, self.base).tts_engine, "say")
        off = voice.build_voice_settings({"speech_output": "off"}, self.base)
        self.assertFalse(off.kokoro_configured)
        self.assertFalse(off.tts_available)

    def test_invalid_voice_speed_and_engine_fall_back(self):
        self.place_models()
        with self.assertLogs("voice", "WARNING"):
            settings = voice.build_voice_settings(
                {"kokoro_voice": "../evil", "kokoro_speed": "fast", "tts_engine": "cloud"}, self.base
            )
        self.assertEqual((settings.kokoro_voice, settings.kokoro_speed, settings.tts_engine), ("af_heart", 1.0, "kokoro"))
        self.assertEqual(voice.build_voice_settings({"kokoro_speed": "9"}, self.base).kokoro_speed, 2.0)
        self.assertEqual(voice.build_voice_settings({"kokoro_voice": "bf_emma"}, self.base).kokoro_voice, "bf_emma")


def kokoro_settings(directory):
    model, voices = Path(directory) / "m.onnx", Path(directory) / "v.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")
    return VoiceSettings(speech_output=True, kokoro_model=model, kokoro_voices=voices)


class SynthesizeDispatchTests(unittest.TestCase):
    def test_kokoro_gets_the_speech_ready_text(self):
        engine = Mock()
        engine.synthesize.return_value = b"RIFF-wav"
        with tempfile.TemporaryDirectory() as directory, patch.object(kokoro_speech, "engine", return_value=engine):
            audio, truncated = voice.synthesize(kokoro_settings(directory), "  It rose 3.5   percent. ")
        self.assertEqual((audio, truncated), (b"RIFF-wav", False))
        engine.synthesize.assert_called_once_with("It rose 3 point 5 percent.")

    def test_kokoro_failure_falls_back_to_say(self):
        engine = Mock()
        engine.synthesize.side_effect = kokoro_speech.KokoroUnavailable("The Kokoro speech model could not be loaded.")

        def run(arguments, **kwargs):
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"RIFF" + b"\x00" * 40)
            return Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory, patch.object(kokoro_speech, "engine", return_value=engine), \
                patch.object(voice.subprocess, "run", side_effect=run) as say, \
                patch.object(voice, "SAY_COMMAND", Path("/bin/sh")), self.assertLogs("voice", "WARNING") as logs:
            audio, _ = voice.synthesize(kokoro_settings(directory), "Private reply text")
        self.assertEqual(audio[:4], b"RIFF")
        say.assert_called_once()
        self.assertNotIn("Private reply text", "\n".join(logs.output))

    def test_kokoro_failure_without_say_is_unavailable(self):
        engine = Mock()
        engine.synthesize.side_effect = kokoro_speech.KokoroUnavailable("The Kokoro speech packages are not installed.")
        with tempfile.TemporaryDirectory() as directory, patch.object(kokoro_speech, "engine", return_value=engine), \
                patch.object(voice, "SAY_COMMAND", Path("/nonexistent/say")):
            with self.assertRaises(VoiceUnavailable):
                voice.synthesize(kokoro_settings(directory), "Hello")

    def test_preload_runs_only_for_kokoro_on_a_background_thread(self):
        with patch.object(kokoro_speech, "engine") as engine, patch("threading.Thread") as thread:
            voice.preload_speech(VoiceSettings(speech_output=True))
            engine.assert_not_called()
            with tempfile.TemporaryDirectory() as directory:
                voice.preload_speech(kokoro_settings(directory))
        thread.assert_called_once_with(target=engine.return_value.preload, name="kokoro-preload", daemon=True)
        thread.return_value.start.assert_called_once()


class FakeKokoro:
    def __init__(self, voices=("af_heart",), fail=False):
        self.voices, self.fail, self.calls = list(voices), fail, []
        self.tokenizer = Mock(vocab=set("abcdefghijklmnopqrstuvwxyzəˈ .,?!"))

    def get_voices(self):
        return self.voices

    def create(self, text, voice, speed, is_phonemes=False):
        self.calls.append((text, voice, speed, is_phonemes))
        if self.fail:
            raise ValueError("phonemes too long")
        return numpy.array([0.0, 0.5, -0.5, 2.0], dtype=numpy.float32), 24000


class KokoroEngineTests(unittest.TestCase):
    """KokoroSpeech with the packages replaced by fakes (nothing is loaded)."""

    def modules(self, fake, data_path="/short/espeak-ng-data"):
        kokoro_onnx = types.ModuleType("kokoro_onnx")
        kokoro_onnx.Kokoro = Mock(from_session=Mock(return_value=fake))
        config_module = types.ModuleType("kokoro_onnx.config")
        config_module.EspeakConfig = Mock()
        onnxruntime = types.ModuleType("onnxruntime")
        onnxruntime.SessionOptions = Mock
        onnxruntime.InferenceSession = Mock()
        loader = types.ModuleType("espeakng_loader")
        loader.get_data_path = lambda: data_path
        backend = types.ModuleType("phonemizer.backend")
        # Stands in for espeak-ng: "phonemes" are the lower-cased text plus a symbol the model lacks.
        backend.EspeakBackend = Mock(side_effect=lambda language, **options: Mock(
            language=language, options=options, phonemize=lambda texts: [texts[0].lower() + "§"]))
        self.backend = backend.EspeakBackend
        return patch.dict(sys.modules, {
            "kokoro_onnx": kokoro_onnx, "kokoro_onnx.config": config_module,
            "onnxruntime": onnxruntime, "espeakng_loader": loader, "phonemizer.backend": backend,
        }), onnxruntime

    def engine(self, voice_name="af_heart"):
        return kokoro_speech.KokoroSpeech(Path("/m.onnx"), Path("/v.bin"), voice_name, 1.0)

    def test_returns_24khz_16bit_wav_and_loads_once(self):
        fake = FakeKokoro()
        patcher, onnxruntime = self.modules(fake)
        with patcher:
            speech = self.engine()
            first = speech.synthesize("Hello.")
            speech.synthesize("Again.")
        with wave.open(io.BytesIO(first)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            frames = numpy.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        self.assertEqual(frames[:4].tolist(), [0, 16383, -16383, 32767])  # clipped, not wrapped
        # A sentence end is followed by a natural pause of silence.
        self.assertEqual(len(frames), 4 + int(24000 * kokoro_speech.SENTENCE_PAUSE_SECONDS))
        self.assertFalse(frames[4:].any())
        self.assertEqual(onnxruntime.InferenceSession.call_count, 1)
        self.assertEqual(self.backend.call_count, 1)  # one espeak backend, reused for every segment
        self.backend.assert_called_once_with("en-us", preserve_punctuation=True, with_stress=True)
        # Phonemes the model has no symbol for are dropped, as kokoro-onnx does.
        self.assertEqual(fake.calls, [("hello.", "af_heart", 1.0, True), ("again.", "af_heart", 1.0, True)])

    def test_british_voices_use_british_pronunciation(self):
        fake = FakeKokoro(voices=("bf_emma",))
        patcher, _ = self.modules(fake)
        with patcher:
            self.engine("bf_emma").synthesize("Schedule.")
        self.assertEqual(self.backend.call_args.args, ("en-gb",))

    def test_a_long_espeak_path_is_refused_before_espeak_runs(self):
        patcher, onnxruntime = self.modules(FakeKokoro(), data_path="/" + "x" * 200)
        with patcher, self.assertRaises(kokoro_speech.KokoroUnavailable):
            self.engine().synthesize("Hello.")
        onnxruntime.InferenceSession.assert_not_called()

    def test_unknown_voice_and_failures_are_reported_without_the_text(self):
        patcher, _ = self.modules(FakeKokoro(voices=("am_adam",)))
        with patcher, self.assertRaises(kokoro_speech.KokoroUnavailable) as raised:
            self.engine().synthesize("Hello.")
        self.assertIn("voice", str(raised.exception))
        patcher, _ = self.modules(FakeKokoro(fail=True))
        with patcher, self.assertLogs("kokoro_speech", "WARNING") as logs, \
                self.assertRaises(kokoro_speech.KokoroUnavailable):
            self.engine().synthesize("Secret meeting notes")
        self.assertNotIn("Secret", "\n".join(logs.output))

    def test_pause_length_follows_the_punctuation(self):
        patcher, _ = self.modules(FakeKokoro())
        lengths = {}
        with patcher:
            speech = self.engine()
            for text in ("Is it ready?", "First, second,", "No punctuation"):
                with wave.open(io.BytesIO(speech.synthesize(text))) as audio:
                    lengths[text] = audio.getnframes() - 4
        self.assertEqual(lengths, {
            "Is it ready?": int(24000 * kokoro_speech.SENTENCE_PAUSE_SECONDS),
            "First, second,": int(24000 * kokoro_speech.CLAUSE_PAUSE_SECONDS),
            "No punctuation": 0,
        })

    def test_text_with_nothing_pronounceable_is_refused(self):
        patcher, _ = self.modules(FakeKokoro())
        with patcher, self.assertRaises(kokoro_speech.KokoroUnavailable):
            self.engine().synthesize("§§§")

    def test_missing_packages_are_unavailable(self):
        with patch.dict(sys.modules, {"kokoro_onnx": None}), self.assertRaises(kokoro_speech.KokoroUnavailable):
            self.engine().synthesize("Hello.")

    def test_preload_reports_failure_instead_of_raising(self):
        with patch.dict(sys.modules, {"kokoro_onnx": None}), self.assertLogs("kokoro_speech", "WARNING"):
            self.assertFalse(self.engine().preload())

    def test_one_shared_engine_per_configuration(self):
        first = kokoro_speech.engine(Path("/a"), Path("/b"), "af_heart", 1.0)
        self.assertIs(first, kokoro_speech.engine(Path("/a"), Path("/b"), "af_heart", 1.0))
        self.assertIsNot(first, kokoro_speech.engine(Path("/a"), Path("/b"), "am_adam", 1.0))


class PlanApiTests(unittest.TestCase):
    def client(self, settings):
        with tempfile.TemporaryDirectory() as directory:
            return web_app.create_app(Path(directory) / "p.sqlite3", utility_settings=UtilitySettings(),
                                      voice_settings=settings).test_client()

    def test_plan_returns_segments_without_synthesizing(self):
        with patch.object(voice, "SAY_COMMAND", Path("/bin/sh")), patch.object(voice, "synthesize") as synthesize:
            response = self.client(VoiceSettings(speech_output=True)).post(
                "/api/voice/plan", json={"text": "Sure. Your meeting with Dr. Patel is at 3. Shall I move it?"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "segments": ["Sure. Your meeting with Dr. Patel is at 3.", "Shall I move it?"], "truncated": False,
        })
        synthesize.assert_not_called()

    def test_plan_rejects_bad_input_and_reports_no_engine(self):
        with patch.object(voice, "SAY_COMMAND", Path("/bin/sh")):
            client = self.client(VoiceSettings(speech_output=True))
            self.assertEqual(client.post("/api/voice/plan", json={}).status_code, 400)
            self.assertEqual(client.post("/api/voice/plan", json={"text": "x" * 20001}).status_code, 400)
            self.assertEqual(client.post("/api/voice/plan", json={"text": "** __"}).status_code, 422)
        self.assertEqual(self.client(VoiceSettings()).post("/api/voice/plan", json={"text": "Hi."}).status_code, 503)

    def test_status_names_the_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            status = self.client(kokoro_settings(directory)).get("/api/voice/status").get_json()
        self.assertEqual(status["speech_engine"], "kokoro")


@unittest.skipUnless(os.environ.get("VOICE_SMOKE") == "1", "set VOICE_SMOKE=1 to run the real Kokoro model")
class LocalKokoroTests(unittest.TestCase):
    """The real Kokoro model, checked by transcribing its audio with whisper.cpp."""

    def test_segments_are_spoken_intelligibly(self):
        settings = config.load_voice_settings()
        if not (settings.kokoro_configured and settings.stt_available):
            self.skipTest("the Kokoro model files and whisper.cpp are required")
        segments, _ = voice.speech_segments(
            "Your next meeting is with Dr. Patel at 2:30 PM on Friday. It rose 3.5 percent. Should I move it?"
        )
        heard = []
        for segment in segments:
            audio, _ = voice.synthesize(settings, segment)
            with wave.open(io.BytesIO(audio)) as clip:
                self.assertEqual(clip.getframerate(), kokoro_speech.SAMPLE_RATE)
                self.assertGreater(clip.getnframes() / clip.getframerate(), 0.5)
            heard.append(voice.transcribe(settings, audio, "audio/wav").lower())
        text = " ".join(heard)
        for expected in ("meeting", "patel", "friday", "3.5", "move"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
