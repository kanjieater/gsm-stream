import asyncio
import os
import time
import threading
from collections import deque
from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image

import noise_filter
import speaker_filter
import controller as ctrl_module
from ocr import run_meikiocr_raw

FPS = int(os.environ.get("FPS", "2"))

REPLAY_BUFFER_SECS = int(os.environ.get("REPLAY_BUFFER_SECS", "120"))
AUDIO_SEG_SECS     = 2
AUDIO_SEG_DIR      = "/tmp/gsm_audio"
HQ_FRAME_DIR       = "/tmp/gsm_hq"
HQ_FRAME_FPS       = 1  # must match the fps= value in the ffmpeg HQ output

latest_frame: bytes | None = None

_replay_buffer: deque[tuple[datetime, bytes]] = deque()

_seg_lock        = threading.Lock()
_session_start:  datetime | None = None
_session_id:     int | None      = None
_last_prune_ts:  float           = 0.0


def _new_session() -> tuple[int, str, str]:
    """Reset per-session state, clear old files, return (sid, audio_pattern, hq_pattern)."""
    global _session_start, _session_id
    sid = int(time.time())
    for d in (AUDIO_SEG_DIR, HQ_FRAME_DIR):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
    with _seg_lock:
        _session_id    = sid
        _session_start = datetime.now()
    audio_pattern = os.path.join(AUDIO_SEG_DIR, f"seg_{sid}_%06d.mka")
    hq_pattern    = os.path.join(HQ_FRAME_DIR,   f"hq_{sid}_%06d.jpg")
    return sid, audio_pattern, hq_pattern


def _frame_path(directory: str, prefix: str, sid: int, n: int) -> str:
    return os.path.join(directory, f"{prefix}_{sid}_{n:06d}.jpg")


def _seg_path(sid: int, n: int) -> str:
    return os.path.join(AUDIO_SEG_DIR, f"seg_{sid}_{n:06d}.mka")


def get_frame_near(ts: datetime) -> bytes | None:
    if not _replay_buffer:
        return None
    return min(_replay_buffer, key=lambda f: abs(f[0].timestamp() - ts.timestamp()))[1]


def get_hq_frame_near(ts: datetime) -> str | None:
    """Return path to the highest-quality on-disk frame closest to ts, or None."""
    with _seg_lock:
        start = _session_start
        sid   = _session_id
    if not start or sid is None:
        return None
    n = int((ts - start).total_seconds() * HQ_FRAME_FPS)
    if n < 0:
        return None
    for i in (n, n - 1, n + 1):
        if i < 0:
            continue
        path = _frame_path(HQ_FRAME_DIR, "hq", sid, i)
        if os.path.exists(path):
            return path
    return None


REPLAY_BUILD_SECS = 30


def get_audio_replay_buffer(end_time: datetime, duration_secs: int = REPLAY_BUILD_SECS) -> str | None:
    """Concatenate audio segments covering [end_time − duration_secs, end_time].
    Returns path to a combined .mka temp file, or None if no segments found."""
    with _seg_lock:
        start = _session_start
        sid   = _session_id
    if not start or sid is None:
        return None

    window_start = end_time - timedelta(seconds=duration_secs)
    start_n = max(0, int((window_start - start).total_seconds() / AUDIO_SEG_SECS))
    end_n   = int((end_time - start).total_seconds() / AUDIO_SEG_SECS)

    segments = [p for n in range(start_n, end_n + 1)
                if os.path.exists(p := _seg_path(sid, n))]
    if not segments:
        return None

    import subprocess as _sp, tempfile as _tf
    concat_list = os.path.join(_tf.gettempdir(), f"gsm_concat_{sid}_{end_n}.txt")
    with open(concat_list, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    out = os.path.join(_tf.gettempdir(), f"gsm_replay_{sid}_{end_n}.mka")
    r = _sp.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                  "-c:a", "copy", out],
                capture_output=True, timeout=30)
    return out if r.returncode == 0 and os.path.exists(out) else None


def get_audio_segment_near(ts: datetime) -> str | None:
    with _seg_lock:
        start = _session_start
        sid   = _session_id
    if not start or sid is None:
        return None
    n = int((ts - start).total_seconds() / AUDIO_SEG_SECS)
    if n < 0:
        return None
    for i in (n, n - 1):
        if i < 0:
            continue
        path = _seg_path(sid, i)
        if os.path.exists(path):
            return path
    return None


def _prune_session_files(now: datetime) -> None:
    with _seg_lock:
        start = _session_start
        sid   = _session_id
    if not start or sid is None:
        return
    elapsed = (now - start).total_seconds()
    # Prune audio segments
    keep_segs  = REPLAY_BUFFER_SECS // AUDIO_SEG_SECS
    current_n  = int(elapsed / AUDIO_SEG_SECS)
    for i in range(max(0, current_n - keep_segs)):
        try:
            os.unlink(_seg_path(sid, i))
        except OSError:
            pass
    # Prune HQ frames
    keep_frames  = REPLAY_BUFFER_SECS * HQ_FRAME_FPS
    current_hq_n = int(elapsed * HQ_FRAME_FPS)
    for i in range(max(0, current_hq_n - keep_frames)):
        try:
            os.unlink(_frame_path(HQ_FRAME_DIR, "hq", sid, i))
        except OSError:
            pass


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
    global latest_frame, _last_prune_ts
    loop = asyncio.get_event_loop()
    while True:
        sid, audio_pattern, hq_pattern = _new_session()
        print(f"ffmpeg: connecting to {stream_url}...", flush=True)
        cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-i", stream_url,
            # Output 1: OCR pipe — 2fps, moderate quality (OCR doesn't need max quality)
            "-map", "0:v",
            "-err_detect", "ignore_err",
            "-vf", f"fps={FPS}",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
            # Output 2: HQ screenshot frames — 1fps, max quality, stored to disk
            "-map", "0:v",
            "-vf", f"fps={HQ_FRAME_FPS}",
            "-vcodec", "mjpeg", "-q:v", "1",
            hq_pattern,
            # Output 3: Audio segments — copy codec, 2-second chunks
            "-map", "0:a?", "-c:a", "copy",
            "-f", "segment", "-segment_time", str(AUDIO_SEG_SECS),
            "-reset_timestamps", "1",
            audio_pattern,
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        connected = False
        try:
            async for jpeg in read_frames(proc.stdout):
                if not connected:
                    connected = True
                    print(f"bridge [{stream_url}]: receiving frames", flush=True)
                now          = datetime.now()
                latest_frame = jpeg
                _replay_buffer.append((now, jpeg))
                cutoff = now.timestamp() - REPLAY_BUFFER_SECS
                while _replay_buffer and _replay_buffer[0][0].timestamp() < cutoff:
                    _replay_buffer.popleft()
                if now.timestamp() - _last_prune_ts > 10:
                    _prune_session_files(now)
                    _last_prune_ts = now.timestamp()
                loop.run_in_executor(None, handle_frame_in_thread, jpeg, now)
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
