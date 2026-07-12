import asyncio
import os
from datetime import datetime
from io import BytesIO

from PIL import Image

import noise_filter
import speaker_filter
import controller as ctrl_module
from ocr import run_meikiocr_raw

FPS = int(os.environ.get("FPS", "2"))

latest_frame: bytes | None = None


def handle_frame_in_thread(jpeg: bytes, ts: datetime) -> None:
    try:
        pil = Image.open(BytesIO(jpeg))
    except Exception:
        return

    try:
        raw = run_meikiocr_raw(pil)
    except Exception as e:
        print(f"meikiocr error: {e}", flush=True)
        noise_filter.record_empty_frame()
        return

    noise_filter.record_frame(raw, pil.height)
    filtered = noise_filter.filter_meiki_results(raw, pil.height)

    speaker_filter.record_frame(filtered, pil.height, pil.width)
    dialogue, speakers = speaker_filter.filter_speakers(filtered, pil.height, pil.width)
    if speakers:
        print(f"meiki speaker: {', '.join(r['text'].strip() for r in speakers)!r}", flush=True)

    text = "\n".join(r["text"] for r in dialogue).strip()
    if not text:
        return

    ctrl = ctrl_module.get_controller()
    with ctrl_module._ctrl_lock:
        ctrl.handle_ocr_result(
            text=text,
            orig_text=text.split("\n"),
            time=ts,
            img=pil,
        )


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
            yield bytes(buf[start: end + 2])
            del buf[: end + 2]


async def bridge_loop(stream_url: str):
    global latest_frame
    cmd = [
        "ffmpeg", "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", stream_url,
        "-an",
        "-err_detect", "ignore_err",
        "-vf", f"fps={FPS}",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    loop = asyncio.get_event_loop()
    while True:
        print(f"ffmpeg: connecting to {stream_url}...", flush=True)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        connected = False
        try:
            async for jpeg in read_frames(proc.stdout):
                if not connected:
                    connected = True
                    print(f"bridge [{stream_url}]: receiving frames", flush=True)
                latest_frame = jpeg
                loop.run_in_executor(None, handle_frame_in_thread, jpeg, datetime.now())
        except Exception as e:
            print(f"bridge error [{stream_url}]: {e}", flush=True)
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        retry_delay = 5 if connected else 15
        print(f"[{stream_url}] retrying in {retry_delay}s...", flush=True)
        await asyncio.sleep(retry_delay)


async def mjpeg_server():
    async def _handle(reader, writer):
        try:
            await reader.read(4096)
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
                frame = latest_frame
                if frame:
                    writer.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                    await writer.drain()
                await asyncio.sleep(0.5)
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "0.0.0.0", 7276)
    print("MJPEG stream: http://0.0.0.0:7276/", flush=True)
    async with server:
        await server.serve_forever()
