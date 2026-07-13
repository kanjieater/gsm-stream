import asyncio
import threading
from io import BytesIO

from PIL import Image

import noise_filter
import text_filter
from ocr import call_glens

_ctrl_lock = threading.Lock()
_controller = None

# Set by bridge.main() before any frames arrive
bridge_loop: asyncio.AbstractEventLoop | None = None


def make_controller():
    from GameSentenceMiner.ocr.gsm_ocr import TwoPassConfig, TwoPassOCRControllerV2, SecondPassResult
    from GameSentenceMiner.owocr.owocr.ocr_runtime import TextFiltering

    stable_frames = int(__import__("os").environ.get("STABLE_FRAMES", "3"))

    cfg = TwoPassConfig(
        two_pass_enabled=True,
        ocr1_engine="meikiocr",
        ocr2_engine="glens",
        language="ja",
        duplicate_threshold=80,
        change_threshold=20,
    )
    filtering = TextFiltering(lang="ja")

    def _send(text, ts, *, response_dict=None, source=None):
        if not text or not bridge_loop:
            return
        from GameSentenceMiner import gametext
        speaker, dialogue = text_filter.split_speaker(text)
        if speaker:
            print(f"OCR speaker: {speaker!r}", flush=True)
        out = dialogue or text
        print(f"OCR stable: {out!r}", flush=True)
        # Send each line as a separate GSM event so get_matching_line() can find
        # an exact match. Joining all lines into one GameLine causes fuzz.ratio()
        # to score ~40% vs a short card sentence, losing to older exact matches.
        lines = [l for l in out.split("\n") if l.strip()]
        for line in lines:
            asyncio.run_coroutine_threadsafe(
                gametext.handle_new_text_event(
                    line, ts, source="ocr", source_display_name="GSM OCR"
                ),
                bridge_loop,
            )

    def _second_ocr(img, last_result, filtering, engine, **kw):
        if img is None:
            text = "".join(item for item in (last_result or []) if item)
            return SecondPassResult(text=text, orig_text=last_result or [])
        try:
            jpeg_buf = BytesIO()
            img.save(jpeg_buf, format="JPEG")
            raw = asyncio.run_coroutine_threadsafe(
                call_glens(jpeg_buf.getvalue()), bridge_loop
            ).result(timeout=12)
            clean = text_filter.strip_owocr_artifacts(raw) if raw else ""
            if clean:
                noise_filter.record_glens(clean)
                filtered = [
                    l for l in clean.split("\n")
                    if not text_filter.is_watermark(l) and not noise_filter.is_glens_suppressed(l)
                ]
                clean = "\n".join(filtered).strip()
            if not clean:
                clean = "".join(item for item in (last_result or []) if item)
            return SecondPassResult(text=clean, orig_text=clean.split("\n"))
        except Exception as e:
            print(f"glens second pass error: {e}", flush=True)
            text = "".join(item for item in (last_result or []) if item)
            return SecondPassResult(text=text, orig_text=last_result or [])

    return TwoPassOCRControllerV2(
        config=cfg,
        filtering=filtering,
        send_result=_send,
        run_second_ocr=_second_ocr,
        stable_frame_count=stable_frames,
    )


def get_controller():
    global _controller
    if _controller is None:
        _controller = make_controller()
    return _controller
