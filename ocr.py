import os

import numpy as np
import websockets
from PIL import Image

from text_filter import is_watermark

OWOCR_WS = os.environ.get("OWOCR_WS", "ws://127.0.0.1:7331")


def run_meikiocr_raw(pil: Image.Image) -> list:
    """Run meikiocr and return raw result dicts (with text + char bboxes).

    Filters out empty results and fullwidth-only watermark lines.
    """
    from GameSentenceMiner.ocr import SharedMeikiOCRModel
    results = SharedMeikiOCRModel.get_model().run_ocr(
        np.array(pil.convert("RGB")), punct_conf_factor=0.2
    )
    return [r for r in results if r.get("text") and not is_watermark(r["text"])]


async def call_glens(jpeg: bytes) -> str:
    """Send a JPEG to the owocr glens subprocess and return its raw text output."""
    async with websockets.connect(OWOCR_WS, max_size=100_000_000) as ws:
        await ws.send(jpeg)
        async for msg in ws:
            if msg in ("True", "False"):
                continue
            return msg
    return ""
