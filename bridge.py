"""
Bridges SysDVR RTSP → owocr WebSocket (7331) → GSM texthooker page (7275).
GSM's own web server serves the texthooker UI and handles all WebSocket clients.
"""
import asyncio
import hashlib
import os
import re
import sys
import uuid
from datetime import datetime

import websockets

SWITCH_HOST = os.environ.get("SWITCH_HOST", "192.168.1.145")
# SysDVR RTSP server is on port 6666 (from source: #define RTSP_PORT 6666)
SWITCH_STREAM = os.environ.get("SWITCH_STREAM", f"rtsp://{SWITCH_HOST}:6666")
OWOCR_WS = "ws://127.0.0.1:7331"
FPS = int(os.environ.get("FPS", "2"))

# Filters: skip lines matching copyright watermarks (handles fullwidth chars too)
_WATERMARK_RE = re.compile(
    r"HuneX|COMFORT|Game\s*Source|Licensed\s*to|©|＠|copyright",
    re.IGNORECASE,
)
_BLANK_LINE = "ＢＬＡＮＫ＿ＬＩＮＥ"

# Only keep lines that contain at least one CJK character or meaningful content
_HAS_CJK = re.compile(r"[　-鿿＀-￯]")


def start_gsm_web_server():
    from GameSentenceMiner.util.config.configuration import get_config
    from GameSentenceMiner.web.texthooking_page import app, start_web_server
    from flask import request, jsonify

    # Bind to all interfaces so NPM/external clients can reach the container
    get_config().advanced.localhost_bind_address = "0.0.0.0"

    # When accessed through an HTTPS reverse proxy, return port 0 so the
    # Svelte frontend uses window.location.port (the proxy port) instead of
    # the raw container port 7275, which the browser can't reach directly.
    @app.route("/get_websocket_port", methods=["GET"])
    def _patched_ws_port():
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        if forwarded_proto == "https":
            return jsonify({"port": 0}), 200
        return jsonify({"port": 7275}), 200

    start_web_server()


_last_frame_hash = ""
_last_text = ""


def _clean_ocr(raw: str) -> str:
    """Extract only meaningful CJK lines, strip watermarks and noise."""
    lines = [l.strip() for l in raw.splitlines()]
    kept = []
    for l in lines:
        if not l or l == _BLANK_LINE:
            continue
        if _WATERMARK_RE.search(l):
            continue
        if not _HAS_CJK.search(l):
            continue
        kept.append(l)
    return "\n".join(kept).strip()


async def push_text(text: str):
    from GameSentenceMiner.gametext import GameLine
    from GameSentenceMiner.web.texthooking_page import add_event_to_texthooker
    line = GameLine(
        id=str(uuid.uuid4()),
        text=text,
        time=datetime.now(),
        prev=None,
        next=None,
    )
    await add_event_to_texthooker(line)


async def read_frames(stdout):
    buf = bytearray()
    while True:
        chunk = await stdout.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                buf.clear()
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                del buf[:start]
                break
            yield bytes(buf[start : end + 2])
            del buf[: end + 2]


async def bridge_loop():
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", SWITCH_STREAM,
        "-an",
        "-vf", f"fps={FPS}",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]

    while True:
        print(f"ffmpeg: connecting to {SWITCH_STREAM}...", flush=True)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        connected = False
        try:
            async with websockets.connect(OWOCR_WS, max_size=100_000_000) as ws:
                print("bridge: connected to owocr :7331", flush=True)

                async def receive_loop():
                    global _last_text
                    async for msg in ws:
                        if msg in ("True", "False"):
                            continue
                        clean = _clean_ocr(msg)
                        if not clean or clean == _last_text:
                            continue
                        _last_text = clean
                        print(f"OCR: {clean}", flush=True)
                        await push_text(clean)

                recv_task = asyncio.create_task(receive_loop())
                frame_count = 0
                async for jpeg in read_frames(proc.stdout):
                    if not connected:
                        connected = True
                        print("bridge: receiving frames", flush=True)
                    frame_count += 1
                    if frame_count % 10 == 0:
                        with open("/tmp/latest_frame.jpg", "wb") as f:
                            f.write(jpeg)
                    # Skip OCR if frame is identical to the last one sent
                    global _last_frame_hash
                    h = hashlib.md5(jpeg).hexdigest()
                    if h == _last_frame_hash:
                        continue
                    _last_frame_hash = h
                    await ws.send(jpeg)
                recv_task.cancel()

        except Exception as e:
            print(f"bridge error: {e}", flush=True)
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

        # SysDVR only accepts one connection at a time — give it time to reset
        retry_delay = 5 if connected else 15
        print(f"retrying in {retry_delay}s...", flush=True)
        await asyncio.sleep(retry_delay)


async def main():
    import threading
    threading.Thread(target=start_gsm_web_server, daemon=True).start()
    print("GSM web server starting on :7275", flush=True)
    await bridge_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
