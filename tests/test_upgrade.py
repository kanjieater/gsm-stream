"""Offline integration contracts against the installed GSM (no production mounts).

Run in a disposable container with --network none. Lens/Anki are test doubles;
these tests do NOT establish OCR accuracy, audio quality or end-to-end mining.
"""
import asyncio
from datetime import datetime
import os
import threading
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GSM_ELECTRON", "1")
os.environ["PROFILES_CONFIG"] = "/nonexistent-test-profiles.yml"

import bridge
import controller
import stream
from PIL import Image
from GameSentenceMiner import anki, gametext
from GameSentenceMiner.util.config.configuration import get_config


class UpgradeContracts(unittest.TestCase):
    def test_background_startup_returns_with_blocking_anki_monitor(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def blocking_monitor():
            entered.set()
            release.wait(5)

        def start():
            try:
                bridge._start_gsm_background_services()
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        with patch.object(anki, "start_monitoring_anki", blocking_monitor), \
             patch("watchdog.observers.Observer"), \
             patch("GameSentenceMiner.vad.vad_processor.init"), \
             patch.object(bridge, "_patch_vad_similarity"), \
             patch.object(bridge, "_patch_vad_verification"):
            worker = threading.Thread(target=start, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "Anki worker never started")
                self.assertTrue(returned.wait(2), "Anki loop blocked bridge startup")
                self.assertEqual(errors, [])
            finally:
                release.set()
                worker.join(3)

    def test_inactive_stream_and_disable_recording_gate(self):
        original_replay = __import__("GameSentenceMiner.obs", fromlist=["save_replay_buffer"]).save_replay_buffer
        original_gate = anki._is_anki_polling_allowed
        obs = __import__("GameSentenceMiner.obs", fromlist=["save_replay_buffer"])
        try:
            bridge._patch_gsm_replay()
            with patch.object(stream, "is_stream_active", return_value=False):
                self.assertFalse(anki._is_anki_polling_allowed())
            with patch.object(stream, "is_stream_active", return_value=True), \
                 patch.object(get_config().obs, "disable_recording", True):
                self.assertFalse(anki._is_anki_polling_allowed())
            with patch.object(stream, "is_stream_active", return_value=True), \
                 patch.object(get_config().obs, "disable_recording", False):
                self.assertTrue(anki._is_anki_polling_allowed())
        finally:
            anki._is_anki_polling_allowed = original_gate
            obs.save_replay_buffer = original_replay

    def test_custom_web_routes(self):
        client = bridge._flask_app.test_client()
        with patch.object(stream, "latest_frame", None):
            self.assertEqual(client.get("/frame").status_code, 204)
        with patch.object(stream, "latest_frame", b"jpeg-fixture"):
            result = client.get("/frame")
            self.assertEqual(result.data, b"jpeg-fixture")
            self.assertEqual(result.mimetype, "image/jpeg")
        for route in ("/header-ui.js", "/bridge-sync.js"):
            result = client.get(route)
            self.assertEqual(result.status_code, 200)
            self.assertIn("javascript", result.mimetype)

    def test_text_normalization_patch(self):
        from GameSentenceMiner.util import text_log
        normalize, score = text_log.normalize_text_for_comparison, text_log._match_score
        try:
            bridge._patch_gsm_text_normalization()
            self.assertEqual(text_log.normalize_text_for_comparison("月[つき]の王[おう]"),
                             normalize("月の王"))
            self.assertEqual(text_log._match_score("別の行\n月の王", "月[つき]の王[おう]"), 100)
        finally:
            text_log.normalize_text_for_comparison, text_log._match_score = normalize, score

    def test_stable_frames_call_lens_once_and_deliver_text(self):
        async def exercise():
            controller.bridge_loop = asyncio.get_running_loop()
            text = "今日はとてもいい天気ですね。"
            lens = AsyncMock(return_value=text)
            intake = AsyncMock()
            with patch.object(controller, "call_glens", lens), \
                 patch.object(gametext, "handle_new_text_event", intake), \
                 patch.object(controller.noise_filter, "record_glens"), \
                 patch.object(controller.noise_filter, "is_glens_suppressed", return_value=False), \
                 patch.object(controller.identity, "get_ocr_strip_pattern", return_value=""), \
                 patch.object(controller.text_filter, "split_speaker", return_value=("", text)):
                ctrl = controller.make_controller()
                image = Image.new("RGB", (640, 360), "white")
                stamp = datetime.now()
                await asyncio.to_thread(ctrl.handle_ocr_result, text, [text], stamp, image)
                self.assertEqual(lens.await_count, 0, "Lens ran on an unstable frame")
                for _ in range(5):
                    await asyncio.to_thread(ctrl.handle_ocr_result, text, [text], stamp, image)
                await asyncio.sleep(0)  # drain queued intake coroutine, not a timing assumption
                self.assertEqual(lens.await_count, 1)
                self.assertEqual(intake.await_count, 1)
                self.assertEqual(intake.call_args.args[0], text)
                self.assertEqual(intake.call_args.kwargs["source"], "ocr")
            controller.bridge_loop = None
        asyncio.run(exercise())

    @unittest.skipUnless(os.environ.get("EXPECT_GSM_NATIVE") == "1", "candidate only")
    def test_candidate_native_extension_loads(self):
        from GameSentenceMiner import _native
        from GameSentenceMiner.native.runtime import NativeMode, get_native_mode
        self.assertTrue(callable(_native.filter_ocr_text))
        self.assertEqual(get_native_mode("ocr"), NativeMode.NATIVE)


if __name__ == "__main__":
    unittest.main()
