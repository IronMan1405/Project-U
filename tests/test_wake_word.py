"""v1.2 wake word ("Kris"): the local server-side check and its endpoint.

The browser state machine and burst detector are tested in tests/js/wake_word.test.js. Apart
from the VOICE_SMOKE test (synthetic macOS `say` speech through the real FFmpeg and whisper.cpp),
these use fakes: they do not measure acoustic accuracy with a real microphone.
"""
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import voice
# Imported as a module, not by class name, so unittest does not discover VoiceApiTests twice.
import tests.test_voice as voice_tests
from voice import VoiceError, VoiceUnavailable


def wav_bytes(directory, seconds=0.8):
    return voice_tests.write_wav(Path(directory) / "clip.wav", [(seconds, -30, "tone")]).read_bytes()


class WakePhraseTests(unittest.TestCase):
    def test_just_the_name_wakes(self):
        for text in ("Kris", " Chris.", "Kris!", "Hey Kris.", "Okay, Chris?", "Hey, hey Kris", "Kriss", "Um, Chris."):
            with self.subTest(text=text):
                self.assertTrue(voice.is_wake_phrase(text))

    def test_anything_else_does_not(self):
        for text in (
            "", "Christmas", "crisis", "Kiss", "Cross", "Crisp", "Christopher", "Christ", "this",
            "I called Chris yesterday.", "Chris is coming over", "Kris, what's on my calendar?",
            "Chris Chris", "Hey", "Kris and Chris", "[BLANK_AUDIO]",
        ):
            with self.subTest(text=text):
                self.assertFalse(voice.is_wake_phrase(text))


class WakeDetectionTests(voice_tests.VoiceTestCase):
    def fake_run(self, transcript):
        def run(arguments, **kwargs):
            if arguments[0] == str(self.ffmpeg):
                voice_tests.write_wav(Path(arguments[-1]), [(0.8, -30, "tone")])
            return Mock(returncode=0, stdout=transcript, stderr="")
        return run

    def test_clip_is_converted_with_the_wav_demuxer_and_a_short_limit(self):
        calls = []
        run = self.fake_run(" Chris.\n")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            voice.subprocess, "run", side_effect=lambda a, **k: calls.append(a) or run(a, **k)
        ):
            self.assertTrue(voice.detect_wake_word(self.settings, wav_bytes(directory)))
        convert = calls[0]
        self.assertEqual(convert[convert.index("-f") + 1], "wav")
        self.assertEqual(convert[convert.index("-t") + 1], str(voice.WAKE_MAX_CLIP_SECONDS))

    def test_other_words_and_silence_are_not_the_wake_word(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = wav_bytes(directory)
            with patch.object(voice.subprocess, "run", side_effect=self.fake_run(" Christmas.\n")):
                self.assertFalse(voice.detect_wake_word(self.settings, clip))
            with patch.object(voice, "transcribe", side_effect=VoiceError(voice.NO_SPEECH_MESSAGE)):
                self.assertFalse(voice.detect_wake_word(self.settings, clip))

    def test_bad_clips_and_failures_raise(self):
        with self.assertRaises(VoiceError):
            voice.detect_wake_word(self.settings, b"RIFF" + b"\x00" * voice.MAX_WAKE_AUDIO_BYTES)
        with self.assertRaises(VoiceError):  # not a WAV
            voice.detect_wake_word(self.settings, b"\x1a\x45\xdf\xa3" + b"\x00" * 60)
        with self.assertRaises(VoiceUnavailable):
            voice.detect_wake_word(voice.VoiceSettings(), b"RIFF\x00\x00\x00\x00WAVE")

    def test_follow_up_timeout_is_configurable_and_bounded(self):
        build = lambda value: voice.build_voice_settings({"wake_follow_up_seconds": value}, self.base)
        self.assertEqual(voice.build_voice_settings({}, self.base).wake_follow_up_seconds, 8)
        self.assertEqual(build("12").wake_follow_up_seconds, 12)
        self.assertEqual(build("1").wake_follow_up_seconds, 3)
        self.assertEqual(build("600").wake_follow_up_seconds, 30)
        with self.assertLogs("voice", "WARNING"):
            self.assertEqual(build("soon").wake_follow_up_seconds, 8)
        with self.assertLogs("voice", "WARNING"):  # must end before the recording's hard limit
            short = voice.build_voice_settings({"max_seconds": "8", "wake_follow_up_seconds": "8"}, self.base)
        self.assertEqual(short.wake_follow_up_seconds, 7)


class WakeApiTests(voice_tests.VoiceTestCase):
    client = voice_tests.VoiceApiTests.client

    def wake(self, client, data, mime="audio/wav"):
        return client.post(
            "/api/voice/wake", data={"audio": (io.BytesIO(data), "wake.wav", mime)},
            content_type="multipart/form-data",
        )

    def test_returns_only_a_yes_or_no_never_the_transcript(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as directory:
            clip = wav_bytes(directory)
        for transcript, expected in ((" Kris.", True), (" I called Chris yesterday.", False)):
            with self.subTest(transcript=transcript), patch.object(voice, "transcribe", return_value=transcript), \
                    self.assertNoLogs(level="INFO"):
                response = self.wake(client, clip)
            self.assertEqual((response.status_code, response.get_json()), (200, {"wake": expected}))
            self.assertNotIn(transcript.strip(" ."), response.get_data(as_text=True))

    def test_limits_errors_and_busy(self):
        client = self.client()
        self.assertEqual(client.post("/api/voice/wake", data={}, content_type="multipart/form-data").status_code, 400)
        self.assertEqual(self.wake(client, b"RIFF" + b"\x00" * voice.MAX_WAKE_AUDIO_BYTES).status_code, 413)
        self.assertEqual(self.wake(self.client(voice.VoiceSettings()), b"RIFF").status_code, 503)
        self.assertEqual(self.wake(client, b"<html>", "text/html").status_code, 422)
        with patch.object(web_app_slot(client), "acquire", return_value=False):
            busy = self.wake(client, b"RIFF\x00\x00\x00\x00WAVE")
        self.assertEqual((busy.status_code, busy.get_json()), (429, {"wake": False, "busy": True}))

    def test_wake_endpoint_has_the_same_host_and_origin_checks(self):
        client = self.client()
        with patch.object(voice, "detect_wake_word", return_value=True) as detect:
            foreign = client.post(
                "/api/voice/wake", data={"audio": (io.BytesIO(b"RIFF"), "w.wav", "audio/wav")},
                content_type="multipart/form-data", headers={"Origin": "https://example.com"},
            )
        self.assertEqual(foreign.status_code, 403)
        detect.assert_not_called()


def web_app_slot(client):
    """The shared one-at-a-time transcription lock inside the app's wake endpoint."""
    view = client.application.view_functions["voice_wake"]
    cells = dict(zip(view.__code__.co_freevars, (cell.cell_contents for cell in view.__closure__)))
    return cells["transcription_slot"]


@unittest.skipUnless(os.environ.get("VOICE_SMOKE") == "1", "set VOICE_SMOKE=1 for the local speech round trip")
class LocalWakeWordTests(unittest.TestCase):
    """Synthetic speech (macOS `say`, 16 kHz WAV like the page sends) through the real FFmpeg and
    whisper.cpp. This checks the pipeline and the name matching; it is not a microphone test."""

    def test_isolated_name_wakes_and_other_speech_does_not(self):
        settings = voice.build_voice_settings(
            {"ffmpeg": "/opt/homebrew/bin/ffmpeg", "whisper_cli": "/opt/homebrew/bin/whisper-cli",
             "whisper_model": "data/models/ggml-base.en.bin"},
            Path(config.CONFIG_PATH).parent,
        )
        if not settings.stt_available or not voice.SAY_COMMAND.is_file():
            self.skipTest("whisper.cpp, the base.en model, FFmpeg, and say are required")
        cases = {"Kris": True, "Hey Kris": True, "Christmas": False, "crisis": False,
                 "I called Chris yesterday": False, "What is on my calendar today": False}
        with tempfile.TemporaryDirectory() as directory:
            for text, expected in cases.items():
                path = Path(directory) / "clip.wav"
                subprocess.run([str(voice.SAY_COMMAND), "-o", str(path), "--data-format=LEI16@16000", text],
                               check=True, timeout=30)
                with self.subTest(text=text):
                    self.assertEqual(voice.detect_wake_word(settings, path.read_bytes()), expected)


if __name__ == "__main__":
    unittest.main()
