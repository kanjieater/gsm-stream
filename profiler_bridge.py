"""
Dialogue Layout Profiler bridge — observation-only, fire-and-forget.

Sends meiki OCR frames to a long-lived Rust profiler subprocess so it can
learn per-game dialogue layouts. Predictions are logged but not yet used to
crop OCR regions.

Enable with: LAYOUT_PROFILER_ENABLED=1

The profiler runs inside Docker (image must already be pulled):

    docker run --rm -i -v <profiler_dir>:/work -w /work rust:1.79-slim \\
        cargo run -q -p dialogue-layout-profiler-cli -- serve ...

Files written by the profiler:
    <profiler_dir>/profiles/<safe-game-id>.layout.json   — durable learned profile
    <profiler_dir>/logs/<safe-game-id>.profiler.debug.jsonl  — request/response log
"""
import atexit
import json
import os
import queue
import select
import subprocess
import threading
import time
import uuid

import identity

ENABLED = os.environ.get("LAYOUT_PROFILER_ENABLED", "0") == "1"
# Controls whether filter_to_ocr_regions() actually filters.
# Toggle at runtime via the /profiler-filter endpoint.
_filter_enabled: bool = os.environ.get("LAYOUT_PROFILER_FILTER", "1") == "1"

PROFILER_DIR = os.environ.get("LAYOUT_PROFILER_DIR", "/dialogue-layout-profiler")
# If set, run this binary directly instead of via `docker run`.
# Inside the gsm-stream container, set this to the mounted binary path.
PROFILER_BIN = os.environ.get("LAYOUT_PROFILER_BIN", "")

# Per-response read timeout. If cargo needs to compile on first run set this
# to 120+. After the binary is cached subsequent responses land in <100ms.
RESPONSE_TIMEOUT = float(os.environ.get("LAYOUT_PROFILER_TIMEOUT", "2.0"))
QUEUE_MAX = int(os.environ.get("LAYOUT_PROFILER_QUEUE", "50"))
FLUSH_EVERY = int(os.environ.get("LAYOUT_PROFILER_FLUSH_EVERY", "100"))

# --- module-level state ---
_lock = threading.Lock()
_bridge: "_ProfilerBridge | None" = None
_current_game: str = ""

# Latest prediction exposed for debug rendering — written by worker thread,
# read by Flask thread. GIL makes simple assignment safe enough here.
_latest_overlay: dict | None = None


# ---------------------------------------------------------------------------

class _ProfilerBridge:
    """Owns one profiler subprocess for a single game identity."""

    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        self.safe_id = _safe_id(game_id)
        self._disabled = False
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue[str] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stats: dict = {
            "frames_sent": 0,
            "modes": {},
            "errors": 0,
            "timeouts": 0,
        }

    # --- public interface ---

    def start(self) -> bool:
        profiles_dir = os.path.join(PROFILER_DIR, "profiles")
        logs_dir = os.path.join(PROFILER_DIR, "logs")
        os.makedirs(profiles_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)

        profile_path = os.path.join(PROFILER_DIR, "profiles", f"{self.safe_id}.layout.json")
        log_path = os.path.join(PROFILER_DIR, "logs", f"{self.safe_id}.profiler.debug.jsonl")

        if PROFILER_BIN:
            cmd = [
                PROFILER_BIN,
                "serve",
                "--profile", profile_path,
                "--profile-id", self.safe_id,
                "--flush-every", str(FLUSH_EVERY),
                "--debug-log", log_path,
            ]
        else:
            # Docker fallback — requires Docker socket mounted in the container.
            cmd = [
                "docker", "run", "--rm", "-i",
                "-v", f"{PROFILER_DIR}:/work",
                "-w", "/work",
                "rust:1.79-slim",
                "cargo", "run", "-q", "-p", "dialogue-layout-profiler-cli", "--",
                "serve",
                "--profile", f"/work/profiles/{self.safe_id}.layout.json",
                "--profile-id", self.safe_id,
                "--flush-every", str(FLUSH_EVERY),
                "--debug-log", f"/work/logs/{self.safe_id}.profiler.debug.jsonl",
            ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # suppress cargo compile noise
                bufsize=0,
            )
        except Exception as exc:
            print(f"[profiler] failed to launch: {exc}", flush=True)
            self._disabled = True
            return False

        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name=f"profiler-{self.safe_id}",
        )
        self._thread.start()
        print(
            f"[profiler] started for {self.game_id!r} "
            f"| profile: profiles/{self.safe_id}.layout.json "
            f"| log: logs/{self.safe_id}.profiler.debug.jsonl",
            flush=True,
        )
        return True

    def observe(self, raw_results: list, width: int, height: int, ts: float,
                on_prediction=None) -> None:
        if self._disabled:
            return
        frame = _build_frame(raw_results, width, height, ts)
        msg = json.dumps({"type": "observe_frame", "frame": frame}, ensure_ascii=False)
        try:
            self._q.put_nowait((msg, width, height, on_prediction))
        except queue.Full:
            pass  # profiler is behind; drop rather than stall OCR

    _SHUTDOWN = object()  # sentinel

    def shutdown(self) -> None:
        if self._proc is None or self._disabled:
            return
        try:
            self._q.put_nowait(self._SHUTDOWN)
        except queue.Full:
            self._kill()
            return
        if self._thread:
            self._thread.join(timeout=8.0)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # --- internal ---

    def _worker(self) -> None:
        try:
            self._run_loop()
        except Exception as exc:
            print(f"[profiler] worker error: {exc}", flush=True)
        finally:
            self._disabled = True
            self._kill()

    def _run_loop(self) -> None:
        assert self._proc
        stdin = self._proc.stdin
        stdout = self._proc.stdout

        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                if self._proc.poll() is not None:
                    print("[profiler] process exited unexpectedly", flush=True)
                    return
                continue

            if item is _ProfilerBridge._SHUTDOWN:
                self._graceful_shutdown(stdin, stdout)
                return

            msg, frame_width, frame_height, on_prediction = item
            try:
                stdin.write((msg + "\n").encode())
                stdin.flush()
            except Exception as exc:
                print(f"[profiler] write error: {exc}", flush=True)
                self._stats["errors"] += 1
                return

            self._stats["frames_sent"] += 1

            line = self._read_line(stdout)
            if line is None:
                if self._proc.poll() is not None:
                    print("[profiler] process died while awaiting response", flush=True)
                else:
                    print("[profiler] response timeout — disabling", flush=True)
                    self._stats["timeouts"] += 1
                return

            self._handle_response(line, frame_width, frame_height, on_prediction)

    def _read_line(self, stdout, timeout: float = RESPONSE_TIMEOUT) -> str | None:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([stdout], [], [], min(remaining, 0.25))
            except (ValueError, OSError):
                return None
            if not ready:
                if self._proc.poll() is not None:
                    return None
                continue
            chunk = stdout.read(1)
            if not chunk:
                return None
            if chunk == b"\n":
                return buf.decode(errors="replace")
            buf += chunk

    def _handle_response(self, line: str, frame_width: int, frame_height: int,
                         on_prediction) -> None:
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            self._stats["errors"] += 1
            print(f"[profiler] invalid JSON response: {line[:120]}", flush=True)
            return
        rtype = resp.get("type")
        if rtype == "prediction":
            pred = resp.get("prediction", {})
            mode = pred.get("mode", "unknown")
            m = self._stats["modes"]
            m[mode] = m.get(mode, 0) + 1
            if on_prediction:
                try:
                    on_prediction(pred, frame_width, frame_height)
                except Exception:
                    pass
        elif rtype == "error":
            self._stats["errors"] += 1
            print(f"[profiler] error: {resp.get('message', resp)}", flush=True)

    def _graceful_shutdown(self, stdin, stdout) -> None:
        try:
            stdin.write(b'{"type":"shutdown"}\n')
            stdin.flush()
        except Exception:
            self._kill()
            return
        ack = self._read_line(stdout, timeout=5.0)
        if ack:
            try:
                resp = json.loads(ack)
                if resp.get("type") == "shutdown_ack":
                    print("[profiler] clean shutdown_ack received", flush=True)
                    return
            except json.JSONDecodeError:
                pass
        print("[profiler] no shutdown_ack — killing process", flush=True)
        self._kill()

    def _kill(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Frame conversion helpers
# ---------------------------------------------------------------------------

def _safe_id(game_id: str) -> str:
    """Return a filesystem-safe, length-bounded identifier for game_id."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in game_id)
    safe = safe.strip("_")[:64]
    return safe or "default"


def _build_frame(results: list, width: int, height: int, ts: float) -> dict:
    """Convert raw meiki OCR results into a profiler FrameObservation dict."""
    regions = []
    for i, r in enumerate(results):
        chars = r.get("chars", [])
        if chars:
            xs = [c["bbox"][0] for c in chars] + [c["bbox"][2] for c in chars]
            ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
            bbox = [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))]
            raw_confs = [c["conf"] for c in chars if "conf" in c]
            confidence = sum(raw_confs) / len(raw_confs) if raw_confs else 1.0
        elif "bbox" in r:
            # Fallback: meiki sometimes includes a top-level bbox
            bbox = [float(x) for x in r["bbox"]]
            confidence = float(r.get("confidence", r.get("conf", 1.0)))
        else:
            continue  # no position info — skip

        char_obs = [
            {
                "char": c["char"],
                "bbox": [float(x) for x in c["bbox"]],
                "confidence": float(c.get("conf", 1.0)),
            }
            for c in chars
        ]

        regions.append({
            "id": f"r{i}",
            "kind_hint": "text",
            "bbox": bbox,
            "text": r.get("text", ""),
            "confidence": float(confidence),
            "is_vertical": bool(r.get("is_vertical", False)),
            "chars": char_obs,
        })

    return {
        "frame_id": uuid.uuid4().hex[:8],
        "timestamp_ms": ts * 1000.0,
        "frame": {"width": width, "height": height},
        "regions": regions,
        "ui_regions": [],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _result_bbox(result: dict) -> list | None:
    chars = result.get("chars", [])
    if chars:
        xs = [c["bbox"][0] for c in chars] + [c["bbox"][2] for c in chars]
        ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
        return [min(xs), min(ys), max(xs), max(ys)]
    if "bbox" in result:
        return [float(x) for x in result["bbox"]]
    return None


def _center_in_box(result_bbox: list, region_bbox: list) -> bool:
    cx = (result_bbox[0] + result_bbox[2]) / 2
    cy = (result_bbox[1] + result_bbox[3]) / 2
    return (region_bbox[0] <= cx <= region_bbox[2] and
            region_bbox[1] <= cy <= region_bbox[3])


def filter_to_ocr_regions(meiki_results: list) -> list:
    """Keep only results whose center falls inside a profiler-predicted ocr_region.

    Passes through unchanged when filtering is disabled or no prediction exists.
    """
    if not _filter_enabled:
        return meiki_results
    overlay = _latest_overlay
    if not overlay:
        return meiki_results
    pred = overlay.get("prediction", {})
    if pred.get("mode") != "established":
        return meiki_results
    ocr_regions = pred.get("ocr_regions", [])
    if not ocr_regions:
        return meiki_results

    kept = []
    for result in meiki_results:
        bbox = _result_bbox(result)
        if bbox is None:
            kept.append(result)
            continue
        for region in ocr_regions:
            rbbox = region.get("bbox") if isinstance(region, dict) else region
            if rbbox and _center_in_box(bbox, rbbox):
                kept.append(result)
                break
    return kept


def crop_to_ocr_regions(pil_image):
    """Crop to the union of established ocr_regions, then black out ignore_regions within.

    Returns the original image when filtering is disabled, no prediction exists,
    or mode isn't established yet.
    """
    if not _filter_enabled:
        return pil_image
    overlay = _latest_overlay
    if not overlay:
        return pil_image
    pred = overlay.get("prediction", {})
    if pred.get("mode") != "established":
        return pil_image
    ocr_regions = pred.get("ocr_regions", [])
    if not ocr_regions:
        return pil_image

    def _bbox(region):
        return region.get("bbox") if isinstance(region, dict) else region

    bboxes = [b for b in (_bbox(r) for r in ocr_regions) if b]
    if not bboxes:
        return pil_image

    w, h = pil_image.size
    x1 = max(0, int(min(b[0] for b in bboxes)))
    y1 = max(0, int(min(b[1] for b in bboxes)))
    x2 = min(w, int(max(b[2] for b in bboxes)))
    y2 = min(h, int(max(b[3] for b in bboxes)))
    if x2 <= x1 or y2 <= y1:
        return pil_image

    cropped = pil_image.crop((x1, y1, x2, y2)).copy()

    # Black out ignore_regions that overlap the crop area.
    from PIL import ImageDraw
    draw = ImageDraw.Draw(cropped)
    for region in pred.get("ignore_regions", []):
        rb = _bbox(region)
        if not rb:
            continue
        # Translate to crop-local coordinates.
        rx1 = max(0, int(rb[0]) - x1)
        ry1 = max(0, int(rb[1]) - y1)
        rx2 = min(x2 - x1, int(rb[2]) - x1)
        ry2 = min(y2 - y1, int(rb[3]) - y1)
        if rx2 > rx1 and ry2 > ry1:
            draw.rectangle([rx1, ry1, rx2, ry2], fill=0)

    print(f"[profiler] crop→lens: ({x1},{y1},{x2},{y2}) from {w}x{h}", flush=True)
    return cropped


def set_filter_enabled(enabled: bool) -> None:
    global _filter_enabled
    _filter_enabled = enabled
    print(f"[profiler] region filter {'enabled' if enabled else 'disabled'}", flush=True)


def is_filter_enabled() -> bool:
    return _filter_enabled


def observe(raw_results: list, width: int, height: int, ts: float) -> None:
    """Feed a raw meiki frame to the profiler. Fire-and-forget; never raises."""
    if not ENABLED:
        return
    _ensure_bridge()
    b = _bridge
    if b is not None:
        b.observe(raw_results, width, height, ts, _update_overlay)


def _update_overlay(prediction: dict, frame_width: int, frame_height: int) -> None:
    global _latest_overlay
    _latest_overlay = {
        "prediction": prediction,
        "frame_width": frame_width,
        "frame_height": frame_height,
    }


def get_debug_overlay() -> dict | None:
    """Return the latest prediction + frame dims for the browser debug overlay."""
    return _latest_overlay


def shutdown() -> None:
    """Graceful shutdown — sends shutdown to the profiler and waits for ack."""
    if not ENABLED:
        return
    with _lock:
        b = _bridge
    if b is not None:
        b.shutdown()
        _log_stats(b)


def _ensure_bridge() -> None:
    global _bridge, _current_game
    game = identity.active_profile()
    with _lock:
        if game == _current_game and _bridge is not None and not _bridge._disabled:
            return
        if _bridge is not None:
            _log_stats(_bridge)
            _bridge.shutdown()
        if not game:
            _bridge = None
            _current_game = ""
            return
        _current_game = game
        b = _ProfilerBridge(game)
        _bridge = b if b.start() else None


def _log_stats(b: "_ProfilerBridge") -> None:
    s = b.stats
    profile_path = os.path.join(PROFILER_DIR, "profiles", f"{b.safe_id}.layout.json")
    log_path = os.path.join(PROFILER_DIR, "logs", f"{b.safe_id}.profiler.debug.jsonl")
    print(
        f"[profiler] session end {b.game_id!r} — "
        f"frames={s['frames_sent']} "
        f"modes={s['modes']} "
        f"errors={s['errors']} "
        f"timeouts={s['timeouts']} "
        f"| profile={profile_path} "
        f"| log={log_path}",
        flush=True,
    )


atexit.register(shutdown)
