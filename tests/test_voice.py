import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import io
import math
import random
import struct
import threading
import wave

import assistant_runtime
import config
import voice
import web_app
from computer_utilities import UtilitySettings
from voice import VoiceError, VoiceSettings, VoiceTimeout, VoiceUnavailable


WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60


def write_wav(path, segments, rate=16000):
    """Write a 16-bit mono WAV from (seconds, dBFS or None for silence, kind) segments."""
    rng = random.Random(1234)
    frames = []
    for seconds, level, kind in segments:
        amplitude = 0 if level is None else 32768 * 10 ** (level / 20)
        for index in range(int(seconds * rate)):
            if kind == "tone":  # sine RMS is peak / sqrt(2)
                value = amplitude * math.sqrt(2) * math.sin(2 * math.pi * 220 * index / rate)
            else:  # uniform noise RMS is peak / sqrt(3)
                value = amplitude * math.sqrt(3) * rng.uniform(-1, 1)
            frames.append(max(-32768, min(32767, int(value))))
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return path
OGG = b"OggS" + b"\x00" * 60
MP4 = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 60


class VoiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.ffmpeg = self.executable("ffmpeg")
        self.whisper = self.executable("whisper-cli")
        self.model = self.base / "models" / "ggml-base.en.bin"
        self.model.parent.mkdir()
        self.model.write_bytes(b"model")
        self.settings = VoiceSettings(
            self.ffmpeg, self.whisper, self.model, "Samantha", "en", 60, True
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def executable(self, name):
        path = self.base / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return path


class VoiceSettingsTests(VoiceTestCase):
    def write_config(self, text):
        path = self.base / "config.ini"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_config_or_section_turns_voice_off(self):
        self.assertEqual(config.load_voice_settings(self.base / "absent.ini"), VoiceSettings())
        path = self.write_config("[ollama]\nendpoint = x\nmodel = y\n")
        settings = config.load_voice_settings(path)
        self.assertFalse(settings.stt_available)
        self.assertFalse(settings.tts_available)

    def test_valid_section_with_relative_model_path(self):
        path = self.write_config(
            "[voice]\n"
            f"ffmpeg = {self.ffmpeg}\nwhisper_cli = {self.whisper}\n"
            "whisper_model = models/ggml-base.en.bin\nlanguage = EN\nmax_seconds = 500\n"
            "say_voice = Samantha\n"
        )
        settings = config.load_voice_settings(path)
        self.assertTrue(settings.stt_available)
        self.assertEqual(settings.whisper_model, self.model)
        self.assertEqual(settings.language, "en")
        self.assertEqual(settings.max_seconds, 120)
        self.assertEqual(settings.say_voice, "Samantha")
        self.assertTrue(settings.speech_output)

    def test_invalid_entries_disable_only_what_they_affect(self):
        not_executable = self.base / "bin" / "plain"
        not_executable.write_text("x", encoding="utf-8")
        cases = {
            "relative ffmpeg": {"ffmpeg": "ffmpeg"},
            "missing whisper": {"whisper_cli": str(self.base / "nope")},
            "not executable": {"whisper_cli": str(not_executable)},
            "missing model": {"whisper_model": "models/absent.bin"},
        }
        for label, override in cases.items():
            values = {
                "ffmpeg": str(self.ffmpeg),
                "whisper_cli": str(self.whisper),
                "whisper_model": str(self.model),
                **override,
            }
            with self.subTest(case=label), self.assertLogs("voice", level="WARNING"):
                self.assertFalse(voice.build_voice_settings(values, self.base).stt_available)

        with self.assertLogs("voice", level="WARNING"):
            odd = voice.build_voice_settings(
                {"say_voice": "-o /tmp/x", "language": "english", "max_seconds": "abc"}, self.base
            )
        self.assertIsNone(odd.say_voice)
        self.assertEqual(odd.language, "en")
        self.assertEqual(odd.max_seconds, voice.DEFAULT_MAX_SECONDS)
        self.assertFalse(
            voice.build_voice_settings({"speech_output": "off"}, self.base).tts_available
        )


class ReplyKindTests(unittest.TestCase):
    def test_short_affirmations_and_declines_are_classified(self):
        for text in ("Yes.", "yes", "YES!!!", "Yeah.", "Okay.", "OK", "Sure.", "Go ahead.",
                     "Do it.", "Yes, please.", "Um, yes please.", "Please do.", "Sounds good."):
            with self.subTest(text=text):
                self.assertEqual(voice.classify_reply(text), "affirm")
        for text in ("No.", "Nope.", "No, thank you.", "No thanks.", "Cancel.", "Cancel that.", "Don't.",
                     "Don\u2019t do it.", "Stop.", "Never mind.", "Forget it.", "No, cancel it.", "Not now."):
            with self.subTest(text=text):
                self.assertEqual(voice.classify_reply(text), "decline")

    def test_only_explicit_approval_words_are_a_spoken_confirmation(self):
        for text in ("Confirm.", "confirm", "Confirmed.", "Yes, confirm.", "Okay, confirm it.", "I confirm.",
                     "Approve.", "Approve it, please.", "Kris, confirm.", "Um, confirm."):
            with self.subTest(text=text):
                self.assertEqual(voice.classify_reply(text), "confirm")
        # Mentions approval but is not a clean "confirm": never approval, never a new message.
        for text in ("Don't confirm.", "Confirm or cancel?", "Do not approve it.", "Confirm the other one",
                     "No, don't approve that."):
            with self.subTest(text=text):
                self.assertEqual(voice.classify_reply(text), "unclear")
        # Longer speech that happens to contain the word is an ordinary message.
        self.assertIsNone(voice.classify_reply("Can you confirm what time the meeting is tomorrow?"))

    def test_anything_more_than_a_bare_reply_is_an_ordinary_message(self):
        for text in (
            "No, open the March one instead.", "Yes, and also create a task.", "Yes delete task 3 and 4",
            "Cancel my dentist appointment task", "Don't forget to buy milk", "Stop the music in Music app",
            "Okay, what's next on my list?", "Delete task 3", "Thank you.", "Thanks.", "", "   ", "...",
            "no no no no no no no",
        ):
            with self.subTest(text=text):
                self.assertIsNone(voice.classify_reply(text))


class EndOfSpeechSettingsTests(VoiceTestCase):
    def test_defaults_when_not_configured(self):
        settings = voice.build_voice_settings({}, self.base)
        self.assertEqual((settings.end_silence_ms, settings.min_recording_ms), (1200, 600))
        self.assertEqual(settings.max_seconds, 60)
        self.assertEqual(VoiceSettings().end_silence_ms, 1200)
        self.assertEqual(voice.NO_SPEECH_TIMEOUT_MS, 6000)

    def test_valid_overrides_from_config(self):
        path = self.base / "config.ini"
        path.write_text(
            "[voice]\nend_silence_ms = 2000\nmin_recording_ms = 900\nmax_seconds = 45\n", encoding="utf-8"
        )
        settings = config.load_voice_settings(path)
        self.assertEqual((settings.end_silence_ms, settings.min_recording_ms, settings.max_seconds), (2000, 900, 45))
        for values, expected in (
            ({"end_silence_ms": "500", "min_recording_ms": "200"}, (500, 200)),
            ({"end_silence_ms": "4000", "min_recording_ms": "3000"}, (4000, 3000)),
            ({"end_silence_ms": " 1500 "}, (1500, 600)),
        ):
            with self.subTest(values=values):
                settings = voice.build_voice_settings(values, self.base)
                self.assertEqual((settings.end_silence_ms, settings.min_recording_ms), expected)

    def test_invalid_values_fall_back_to_defaults_with_a_warning(self):
        for values in (
            {"end_silence_ms": "fast"},
            {"end_silence_ms": "1.5"},
            {"end_silence_ms": "499"},
            {"end_silence_ms": "4001"},
            {"end_silence_ms": "-1200"},
            {"min_recording_ms": "199"},
            {"min_recording_ms": "3001"},
            {"min_recording_ms": "lots"},
        ):
            with self.subTest(values=values), self.assertLogs("voice", level="WARNING"):
                settings = voice.build_voice_settings(values, self.base)
                self.assertEqual((settings.end_silence_ms, settings.min_recording_ms), (1200, 600))

    def test_min_recording_must_be_shorter_than_the_maximum(self):
        with self.assertLogs("voice", level="WARNING"):
            settings = voice.build_voice_settings({"max_seconds": "1", "min_recording_ms": "2500"}, self.base)
        self.assertEqual(settings.max_seconds, 1)
        self.assertLess(settings.min_recording_ms, 1000)


class TranscribeTests(VoiceTestCase):
    def fake_run(self, transcript=" Please create a task.\n", fail_step=None, timeout_step=None, converted=None):
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            step = "convert" if arguments[0] == str(self.ffmpeg) else "transcribe"
            if step == timeout_step:
                raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
            if step == fail_step:
                return Mock(returncode=1, stdout="", stderr="decoder error")
            if step == "convert":
                write_wav(Path(arguments[-1]), converted or [(1.0, -30, "tone")])
                return Mock(returncode=0, stdout="", stderr="")
            return Mock(returncode=0, stdout=transcript, stderr="")

        return run, calls

    def test_converts_and_transcribes_with_fixed_argument_lists(self):
        run, calls = self.fake_run()
        with patch.object(voice.subprocess, "run", side_effect=run):
            text = voice.transcribe(self.settings, WEBM, "audio/webm;codecs=opus")

        self.assertEqual(text, "Please create a task.")
        (convert, convert_kwargs), (whisper, whisper_kwargs) = calls
        directory = Path(convert[convert.index("-i") + 1]).parent
        self.assertEqual(
            convert,
            [
                str(self.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "matroska", "-i", str(directory / "recording.webm"), "-t", "60",
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y",
                str(directory / "speech.wav"),
            ],
        )
        self.assertEqual(
            whisper,
            [
                str(self.whisper), "-m", str(self.model), "-f", str(directory / "speech.wav"),
                "-l", "en", "-nt", "-np",
            ],
        )
        for kwargs, timeout in (
            (convert_kwargs, voice.CONVERT_TIMEOUT_SECONDS),
            (whisper_kwargs, voice.TRANSCRIBE_TIMEOUT_SECONDS),
        ):
            self.assertEqual(kwargs["timeout"], timeout)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(directory.exists())

    def test_each_browser_format_uses_its_demuxer(self):
        for data, mime, demuxer in (
            (WEBM, "audio/webm", "matroska"),
            (OGG, "audio/ogg;codecs=opus", "ogg"),
            (MP4, "audio/mp4", "mov"),
        ):
            run, calls = self.fake_run()
            with self.subTest(mime=mime), patch.object(voice.subprocess, "run", side_effect=run):
                voice.transcribe(self.settings, data, mime)
                self.assertEqual(calls[0][0][calls[0][0].index("-f") + 1], demuxer)

    def test_temporary_files_are_removed_after_failures_and_timeouts(self):
        for kwargs, error in (
            ({"fail_step": "convert"}, VoiceError),
            ({"fail_step": "transcribe"}, VoiceError),
            ({"timeout_step": "convert"}, VoiceTimeout),
            ({"timeout_step": "transcribe"}, VoiceTimeout),
            ({"transcript": " [BLANK_AUDIO]\n"}, VoiceError),
        ):
            run, calls = self.fake_run(**kwargs)
            with self.subTest(**kwargs), patch.object(voice.subprocess, "run", side_effect=run):
                with self.assertRaises(error) as raised:
                    voice.transcribe(self.settings, WEBM, "audio/webm")
                self.assertNotIn("decoder error", str(raised.exception))
                source = Path(calls[0][0][calls[0][0].index("-i") + 1])
                self.assertFalse(source.parent.exists())

    def test_rejects_bad_uploads_before_running_anything(self):
        run = Mock()
        cases = [
            (b"", "audio/webm", VoiceError),
            (WEBM + b"\x00" * voice.MAX_AUDIO_BYTES, "audio/webm", VoiceError),
            (WEBM, "audio/wav", VoiceError),
            (WEBM, "video/webm", VoiceError),
            (WEBM, "", VoiceError),
            (OGG, "audio/webm", VoiceError),
            (b"<html>", "audio/mp4", VoiceError),
        ]
        with patch.object(voice.subprocess, "run", run):
            for data, mime, error in cases:
                with self.subTest(mime=mime, size=len(data)), self.assertRaises(error):
                    voice.transcribe(self.settings, data, mime)
            with self.assertRaises(VoiceUnavailable):
                voice.transcribe(VoiceSettings(), WEBM, "audio/webm")
        run.assert_not_called()

    def test_transcript_is_normalized_and_bounded(self):
        run, _ = self.fake_run(" [Music] Open   the\nCalendar app. [BLANK_AUDIO]\n")
        with patch.object(voice.subprocess, "run", side_effect=run):
            self.assertEqual(voice.transcribe(self.settings, WEBM, "audio/webm"), "Open the Calendar app.")
        run, _ = self.fake_run("word " * 1000)
        with patch.object(voice.subprocess, "run", side_effect=run):
            text = voice.transcribe(self.settings, WEBM, "audio/webm")
        self.assertLessEqual(len(text), voice.MAX_TRANSCRIPT_CHARS)

    def test_silent_recordings_are_rejected_before_whisper_runs(self):
        quiet_room = [(2.0, -66, "noise")]
        for label, converted in (
            ("digital silence", [(2.0, None, "noise")]),
            ("quiet-room noise floor", quiet_room),
            ("single click in a quiet room", [(0.5, -66, "noise"), (0.03, -20, "noise"), (1.0, -66, "noise")]),
            ("speech just below the threshold", [(1.5, -58, "tone")]),
        ):
            run, calls = self.fake_run(transcript=" you\n", converted=converted)
            with self.subTest(case=label), patch.object(voice.subprocess, "run", side_effect=run):
                with self.assertRaises(VoiceError) as raised:
                    voice.transcribe(self.settings, WEBM, "audio/webm")
                self.assertEqual(str(raised.exception), voice.NO_SPEECH_MESSAGE)
                self.assertEqual(len(calls), 1)  # FFmpeg ran; whisper.cpp did not
                self.assertFalse(Path(calls[0][0][-1]).parent.exists())

    def test_speech_level_audio_reaches_whisper(self):
        for label, converted in (
            ("normal speech level", [(1.5, -25, "tone")]),
            ("quiet speech", [(0.5, -66, "noise"), (1.0, -50, "noise"), (0.5, -66, "noise")]),
            ("very quiet but sustained", [(1.0, -56, "tone")]),
            ("short word", [(0.2, -40, "noise")]),
        ):
            run, calls = self.fake_run(converted=converted)
            with self.subTest(case=label), patch.object(voice.subprocess, "run", side_effect=run):
                self.assertEqual(voice.transcribe(self.settings, WEBM, "audio/webm"), "Please create a task.")
                self.assertEqual(len(calls), 2)

    def test_speech_level_measurement_is_deterministic(self):
        path = Path(self.temp_dir.name) / "level.wav"
        write_wav(path, [(1.0, None, "noise"), (0.6, -30, "tone"), (1.0, None, "noise")])
        seconds, loudest = voice.speech_level(path)
        self.assertAlmostEqual(seconds, 0.6, delta=0.06)
        self.assertAlmostEqual(loudest, -30, delta=0.5)
        self.assertEqual(voice.speech_level(path), (seconds, loudest))
        write_wav(path, [(1.0, None, "noise")])
        self.assertEqual(voice.speech_level(path), (0.0, float("-inf")))
        path.write_bytes(b"RIFF....not a wav")
        with self.assertRaises(VoiceError):
            voice.speech_level(path)
        with wave.open(str(path), "wb") as stereo:
            stereo.setnchannels(2); stereo.setsampwidth(2); stereo.setframerate(16000)
            stereo.writeframes(b"\x00\x00" * 3200)
        with self.assertRaises(VoiceError):
            voice.speech_level(path)

    def test_missing_program_reports_unavailable(self):
        with patch.object(voice.subprocess, "run", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(VoiceUnavailable):
                voice.transcribe(self.settings, WEBM, "audio/webm")


class SynthesizeTests(VoiceTestCase):
    def fake_say(self, audio=b"RIFF" + b"\x00" * 40, returncode=0):
        calls = []

        def run(arguments, **kwargs):
            text_file = Path(arguments[arguments.index("-f") + 1])
            calls.append((arguments, kwargs, text_file.read_text(encoding="utf-8")))
            if returncode == 0:
                Path(arguments[arguments.index("-o") + 1]).write_bytes(audio)
            return Mock(returncode=returncode, stdout="", stderr="say failed")

        return run, calls

    def test_text_is_passed_through_a_file_never_as_arguments(self):
        run, calls = self.fake_say()
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(voice.subprocess, "run", side_effect=run):
            audio, truncated = voice.synthesize(self.settings, "-v Evil -o /tmp/pwn.wav   hello\nthere")
        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertFalse(truncated)
        arguments, kwargs, spoken = calls[0]
        directory = Path(arguments[arguments.index("-f") + 1]).parent
        self.assertEqual(
            arguments,
            [
                str(self.ffmpeg), "-v", "Samantha", "-f", str(directory / "speech.txt"),
                "--file-format=WAVE", "--data-format=LEI16@22050", "-o", str(directory / "speech.wav"),
            ],
        )
        self.assertEqual(spoken, "-v Evil -o /tmp/pwn.wav hello there")
        self.assertEqual(kwargs["timeout"], voice.SPEAK_TIMEOUT_SECONDS)
        self.assertNotIn("shell", kwargs)
        self.assertFalse(directory.exists())

    def test_default_voice_long_text_and_failures(self):
        settings = VoiceSettings(speech_output=True)
        run, calls = self.fake_say()
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(voice.subprocess, "run", side_effect=run):
            _, truncated = voice.synthesize(settings, "word " * 1000)
        self.assertTrue(truncated)
        self.assertNotIn("-v", calls[0][0])
        self.assertLessEqual(len(calls[0][2]), voice.MAX_SPEECH_CHARS)

        with patch.object(voice, "SAY_COMMAND", self.ffmpeg):
            for text in ("", "   \n"):
                with self.subTest(text=text), self.assertRaises(VoiceError):
                    voice.synthesize(settings, text)
            for kwargs in ({"returncode": 1}, {"audio": b"not audio"}):
                run, _ = self.fake_say(**kwargs)
                with self.subTest(**kwargs), patch.object(voice.subprocess, "run", side_effect=run):
                    with self.assertRaises(VoiceError) as raised:
                        voice.synthesize(settings, "hello")
                    self.assertNotIn("say failed", str(raised.exception))
            with self.assertRaises(VoiceUnavailable):
                voice.synthesize(VoiceSettings(speech_output=False), "hello")


class VoiceApiTests(VoiceTestCase):
    def client(self, settings=None):
        app = web_app.create_app(
            self.base / "api.sqlite3",
            utility_settings=UtilitySettings(),
            voice_settings=self.settings if settings is None else settings,
        )
        return app.test_client()

    def upload(self, client, data=WEBM, mime="audio/webm;codecs=opus"):
        return client.post(
            "/api/voice/transcribe",
            data={"audio": (io.BytesIO(data), "recording.webm", mime)},
            content_type="multipart/form-data",
        )

    def test_status_reflects_configuration(self):
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg):
            on = self.client().get("/api/voice/status").get_json()
        timing = {"end_silence_ms": 1200, "min_recording_ms": 600, "no_speech_timeout_ms": 6000,
                  "wake_follow_up_ms": 8000, "wake_max_clip_ms": 2500}
        self.assertEqual(on, {"speech_input": True, "speech_output": True, "max_seconds": 60, "wake_word": True,
                              "speech_engine": "say", **timing})
        off = self.client(VoiceSettings()).get("/api/voice/status").get_json()
        self.assertEqual(off, {"speech_input": False, "speech_output": False, "max_seconds": 60, "wake_word": False,
                               "speech_engine": None, **timing})
        custom = VoiceSettings(self.ffmpeg, self.whisper, self.model, None, "en", 30, True, 2000, 900)
        self.assertEqual(
            {key: self.client(custom).get("/api/voice/status").get_json()[key]
             for key in ("max_seconds", "end_silence_ms", "min_recording_ms")},
            {"max_seconds": 30, "end_silence_ms": 2000, "min_recording_ms": 900},
        )
        disabled = self.client(VoiceSettings())
        self.assertEqual(self.upload(disabled).status_code, 503)
        self.assertEqual(disabled.post("/api/voice/speak", json={"text": "hi"}).status_code, 503)

    def test_transcription_returns_text_only_and_never_runs_the_assistant(self):
        model = Mock()
        transcribe = Mock(return_value="Please delete task 3")
        client = self.client()
        with patch.object(voice, "transcribe", transcribe), patch.object(assistant_runtime, "ask_ollama", model):
            conversation_id = client.post("/api/conversations", json={}).get_json()["conversation_id"]
            response = self.upload(client)
            activity = client.get(f"/api/conversations/{conversation_id}/activity").get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"text": "Please delete task 3", "reply_kind": None})
        transcribe.assert_called_once_with(self.settings, WEBM, "audio/webm;codecs=opus")
        model.assert_not_called()
        self.assertEqual(activity["activity"]["status"], "idle")

    def test_transcription_errors_map_to_clear_status_codes(self):
        client = self.client()
        self.assertEqual(
            client.post("/api/voice/transcribe", data={}, content_type="multipart/form-data").status_code, 400
        )
        too_large = self.upload(client, WEBM + b"\x00" * voice.MAX_AUDIO_BYTES)
        self.assertEqual(too_large.status_code, 413)
        for error, status in (
            (VoiceError("No speech was detected."), 422),
            (VoiceUnavailable("Local transcription is unavailable."), 503),
            (VoiceTimeout("Local transcription took too long and was stopped."), 504),
        ):
            with self.subTest(status=status), patch.object(voice, "transcribe", side_effect=error):
                response = self.upload(client)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json(), {"error": str(error)})
        # The real format checks run inside voice.transcribe and surface as 422.
        self.assertEqual(self.upload(client, b"<html>", "text/html").status_code, 422)

    def test_silent_recording_returns_no_speech_from_the_endpoint(self):
        calls = []

        def run(arguments, **kwargs):
            calls.append(arguments[0])
            write_wav(Path(arguments[-1]), [(1.5, -66, "noise")])
            return Mock(returncode=0, stdout="", stderr="")

        with patch.object(voice.subprocess, "run", side_effect=run):
            response = self.upload(self.client())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json(), {"error": voice.NO_SPEECH_MESSAGE})
        self.assertEqual(calls, [str(self.ffmpeg)])

    def test_only_one_transcription_runs_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def slow_transcribe(*args):
            started.set()
            release.wait(5)
            return "first"

        client = self.client()
        results = {}
        with patch.object(voice, "transcribe", side_effect=slow_transcribe):
            worker = threading.Thread(target=lambda: results.setdefault("first", self.upload(client)))
            worker.start()
            self.assertTrue(started.wait(5))
            second = self.upload(client)
            release.set()
            worker.join(5)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(results["first"].get_json(), {"text": "first", "reply_kind": None})
        with patch.object(voice, "transcribe", return_value="again"):
            self.assertEqual(self.upload(client).status_code, 200)

    def test_reply_kind_is_returned_with_the_transcript(self):
        client = self.client()
        for text, kind in (("Yes.", "affirm"), ("Confirm.", "confirm"), ("No, thank you.", "decline"),
                           ("Open my notes", None)):
            with self.subTest(text=text), patch.object(voice, "transcribe", return_value=text):
                self.assertEqual(self.upload(client).get_json(), {"text": text, "reply_kind": kind})

    def test_only_one_speech_synthesis_runs_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def slow_synthesize(*args):
            started.set()
            release.wait(5)
            return b"RIFF....WAVE", False

        client = self.client()
        results = {}
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(
            voice, "synthesize", side_effect=slow_synthesize
        ) as synthesize:
            worker = threading.Thread(
                target=lambda: results.setdefault("first", client.post("/api/voice/speak", json={"text": "one"}))
            )
            worker.start()
            self.assertTrue(started.wait(5))
            second = client.post("/api/voice/speak", json={"text": "two"})
            release.set()
            worker.join(5)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json(), {"error": "Speech output is busy. Try again shortly."})
        self.assertEqual(results["first"].status_code, 200)
        self.assertEqual(synthesize.call_count, 1)
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(
            voice, "synthesize", return_value=(b"RIFF", False)
        ):
            self.assertEqual(client.post("/api/voice/speak", json={"text": "again"}).status_code, 200)

    def test_speak_returns_uncached_wav(self):
        client = self.client()
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(
            voice, "synthesize", return_value=(b"RIFF....WAVE", True)
        ) as synthesize:
            response = client.post("/api/voice/speak", json={"text": "Task created."})
            for body in ({}, {"text": ""}, {"text": 5}, {"text": "x" * 20001}):
                with self.subTest(body=body):
                    self.assertEqual(client.post("/api/voice/speak", json=body).status_code, 400)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertEqual(response.data, b"RIFF....WAVE")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Speech-Truncated"], "1")
        synthesize.assert_called_once_with(self.settings, "Task created.")
        with patch.object(voice, "SAY_COMMAND", self.ffmpeg), patch.object(
            voice, "synthesize", side_effect=VoiceError("Local speech output failed.")
        ):
            failed = client.post("/api/voice/speak", json={"text": "hi"})
        self.assertEqual((failed.status_code, failed.get_json()), (422, {"error": "Local speech output failed."}))


@unittest.skipUnless(os.environ.get("VOICE_SMOKE") == "1", "set VOICE_SMOKE=1 for the local speech round trip")
class LocalSpeechRoundTripTests(unittest.TestCase):
    """say -> WAV -> (FFmpeg to WebM/Opus, like a browser) -> FFmpeg -> whisper.cpp. Fully local."""

    def test_spoken_request_round_trips_through_local_tools(self):
        settings = config.load_voice_settings()
        if not settings.stt_available:
            # Fall back to the documented Homebrew install and project model location.
            settings = voice.build_voice_settings(
                {
                    "ffmpeg": "/opt/homebrew/bin/ffmpeg",
                    "whisper_cli": "/opt/homebrew/bin/whisper-cli",
                    "whisper_model": "data/models/ggml-base.en.bin",
                },
                Path(config.CONFIG_PATH).parent,
            )
        if not (settings.stt_available and settings.tts_available):
            self.skipTest("whisper.cpp, the base.en model, FFmpeg, and say are required")
        speech, _ = voice.synthesize(settings, "Please create a task to water the plants tomorrow.")
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "reply.wav"
            webm = Path(directory) / "browser.webm"
            wav.write_bytes(speech)
            subprocess.run(
                [str(settings.ffmpeg), "-nostdin", "-loglevel", "error", "-i", str(wav),
                 "-c:a", "libopus", str(webm)],
                check=True, timeout=30,
            )
            recording = webm.read_bytes()
        text = voice.transcribe(settings, recording, "audio/webm;codecs=opus").lower()
        for word in ("create", "task", "water", "plants", "tomorrow"):
            self.assertIn(word, text)

        # A silent browser-style recording is rejected before whisper.cpp runs.
        with tempfile.TemporaryDirectory() as directory:
            silent = Path(directory) / "silent.webm"
            subprocess.run(
                [str(settings.ffmpeg), "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                 "anullsrc=r=48000:cl=mono", "-t", "2", "-c:a", "libopus", str(silent)],
                check=True, timeout=30,
            )
            with self.assertRaises(VoiceError) as raised:
                voice.transcribe(settings, silent.read_bytes(), "audio/webm;codecs=opus")
            self.assertEqual(str(raised.exception), voice.NO_SPEECH_MESSAGE)

        # Same round trip through the HTTP endpoints the browser uses.
        with tempfile.TemporaryDirectory() as directory:
            client = web_app.create_app(
                Path(directory) / "smoke.sqlite3", utility_settings=UtilitySettings(), voice_settings=settings
            ).test_client()
            spoken = client.post("/api/voice/speak", json={"text": "Open the Calendar app."})
            self.assertEqual((spoken.status_code, spoken.mimetype), (200, "audio/wav"))
            wav = Path(directory) / "reply.wav"
            webm = Path(directory) / "browser.webm"
            wav.write_bytes(spoken.data)
            subprocess.run(
                [str(settings.ffmpeg), "-nostdin", "-loglevel", "error", "-i", str(wav),
                 "-c:a", "libopus", str(webm)],
                check=True, timeout=30,
            )
            response = client.post(
                "/api/voice/transcribe",
                data={"audio": (io.BytesIO(webm.read_bytes()), "r.webm", "audio/webm;codecs=opus")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("calendar", response.get_json()["text"].lower())


if __name__ == "__main__":
    unittest.main()
