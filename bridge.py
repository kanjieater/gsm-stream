"""
Bridges SysDVR RTSP → owocr WebSocket (7331) → GSM texthooker page (7275).

Text intake goes through GSM's full pipeline:
  owocr (glens, first-pass) → TwoPassOCRControllerV2 (stabilization, 2+ matching frames)
  → gametext.handle_new_text_event (cross-source dedup, text processing, Anki, texthooker)
"""
import asyncio
import os
import sys
import threading
from collections import deque
from datetime import datetime
from io import BytesIO

# Tell GSM we're acting as the Electron host: skip OBS auto-launch, pystray tray icon,
# and other desktop-only behaviour that doesn't apply in a headless container.
os.environ.setdefault("GSM_ELECTRON", "1")

import websockets
from PIL import Image

SWITCH_HOST = os.environ.get("SWITCH_HOST", "192.168.1.145")
SWITCH_STREAM = os.environ.get("SWITCH_STREAM", f"rtsp://{SWITCH_HOST}:6666")
OWOCR_WS = "ws://127.0.0.1:7331"
FPS = int(os.environ.get("FPS", "2"))

# PIL images for the last few frames sent to owocr, so handle_ocr_result has an img
_pil_deque: deque = deque(maxlen=3)
_ocr_busy: bool = False  # True while owocr is processing a frame; gates sends to prevent backlog
_latest_frame: bytes | None = None  # most recent raw JPEG; served by /debug/stream

# Set in main() so the controller's sync send_result callback can schedule
# coroutines back onto our event loop
_bridge_loop: asyncio.AbstractEventLoop | None = None


def _make_controller():
    from GameSentenceMiner.ocr.gsm_ocr import (
        TwoPassConfig,
        TwoPassOCRControllerV2,
        SecondPassResult,
    )
    from GameSentenceMiner.owocr.owocr.ocr_runtime import TextFiltering

    cfg = TwoPassConfig(
        two_pass_enabled=True,
        ocr1_engine="glens",
        ocr2_engine="glens",
        language="ja",
        duplicate_threshold=80,
        change_threshold=20,
    )
    filtering = TextFiltering(lang="ja")

    def _send(text, time, *, response_dict=None, source=None):
        if not text or not _bridge_loop:
            return
        from GameSentenceMiner import gametext
        print(f"OCR stable: {text!r}", flush=True)
        asyncio.run_coroutine_threadsafe(
            gametext.handle_new_text_event(
                text,
                time,
                source="ocr",
                source_display_name="GSM OCR",
            ),
            _bridge_loop,
        )

    def _second_ocr(img, last_result, filtering, engine, **kw):
        # First pass is already glens — return its text as the confirmed result
        text = "".join(item for item in (last_result or []) if item)
        return SecondPassResult(text=text, orig_text=last_result or [])

    return TwoPassOCRControllerV2(
        config=cfg,
        filtering=filtering,
        send_result=_send,
        run_second_ocr=_second_ocr,
    )


_controller: "TwoPassOCRControllerV2 | None" = None


def get_controller():
    global _controller
    if _controller is None:
        _controller = _make_controller()
    return _controller


async def mjpeg_server():
    """Minimal MJPEG server on :7276 — bypasses GSM's proxying gateway."""
    async def _handle(reader, writer):
        try:
            await reader.read(4096)  # consume the HTTP request
        except Exception:
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        try:
            while True:
                frame = _latest_frame
                if frame:
                    writer.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                    )
                    await writer.drain()
                await asyncio.sleep(0.5)
        except (ConnectionResetError, BrokenPipeError, Exception):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "0.0.0.0", 7276)
    print("MJPEG stream: http://192.168.1.21:7276/", flush=True)
    async with server:
        await server.serve_forever()


def start_gsm_web_server():
    from GameSentenceMiner.util.config.configuration import get_config
    from GameSentenceMiner.web.texthooking_page import app, start_web_server
    from flask import request, jsonify

    get_config().advanced.localhost_bind_address = "0.0.0.0"

    @app.route("/get_websocket_port", methods=["GET"])
    def _patched_ws_port():
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        if forwarded_proto == "https":
            return jsonify({"port": 0}), 200
        return jsonify({"port": 7275}), 200

    start_web_server()


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
                ctrl = get_controller()

                async def receive_loop():
                    global _ocr_busy
                    try:
                        async for msg in ws:
                            if msg in ("True", "False"):
                                continue
                            _ocr_busy = False  # owocr finished — next frame can be sent
                            img = _pil_deque[-1] if _pil_deque else None
                            try:
                                ctrl.handle_ocr_result(
                                    text=msg,
                                    orig_text=[msg],
                                    time=datetime.now(),
                                    img=img,
                                )
                            except Exception as exc:
                                import traceback
                                print(f"handle_ocr_result error: {exc}", flush=True)
                                traceback.print_exc()
                    except Exception as exc:
                        import traceback
                        print(f"receive_loop crashed: {exc}", flush=True)
                        traceback.print_exc()

                recv_task = asyncio.create_task(receive_loop())
                frame_count = 0
                async for jpeg in read_frames(proc.stdout):
                    if not connected:
                        connected = True
                        print("bridge: receiving frames", flush=True)
                    frame_count += 1
                    global _latest_frame, _ocr_busy
                    _latest_frame = jpeg
                    # Skip if owocr is still processing the previous frame.
                    # Without this gate, frames pile up faster than Google Lens can
                    # process them (~1.4s/frame), causing ever-growing backlog and
                    # 30s+ delays. Skipping here means the controller always sees
                    # real, fresh OCR results — no replayed or faked frames.
                    if _ocr_busy:
                        continue
                    _ocr_busy = True
                    try:
                        _pil_deque.append(Image.open(BytesIO(jpeg)))
                    except Exception:
                        pass
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

        retry_delay = 5 if connected else 15
        print(f"retrying in {retry_delay}s...", flush=True)
        await asyncio.sleep(retry_delay)


async def main():
    global _bridge_loop
    _bridge_loop = asyncio.get_event_loop()

    # Import gametext early so its internal multiplex WS server starts before
    # start_web_server() creates the 7275 gateway that proxies to it.
    import GameSentenceMiner.gametext  # noqa: F401 — side-effect import

    threading.Thread(target=start_gsm_web_server, daemon=True).start()
    print("GSM web server starting on :7275", flush=True)

    # Warm up the controller (imports + instantiation) before the bridge loop
    get_controller()

    await asyncio.gather(mjpeg_server(), bridge_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
