"""
Frequency-based UI noise suppression.

meikiocr returns SHORT per-region strings; glens returns LONG concatenated strings.
They can't share a history (length mismatch kills Levenshtein similarity), so each
pass has its own rolling window.

Histories are cached per game identity so switching games restores a warm filter
instantly instead of requiring another warm-up period.

Suppression uses a scored approach — short text alone is never sufficient:
  frequency > SUPPRESS_THRESH  → +3
  len(text) <= 4               → +1
  suppress when score >= UI_SCORE_THRESH (default 3)
"""
import os
import threading
from collections import deque

import identity

try:
    from rapidfuzz.distance import Levenshtein as _Lev
    _FUZZY = True
except ImportError:
    _FUZZY = False

FREQ_WINDOW      = int(os.environ.get("FREQ_WINDOW",          "60"))
SUPPRESS_THRESH  = float(os.environ.get("SUPPRESS_THRESH",    "0.8"))
FUZZY_RATIO      = float(os.environ.get("FUZZY_RATIO",        "0.85"))
WARM_UP_ENTRIES  = int(os.environ.get("WARM_UP_ENTRIES",       "3"))
UI_SCORE_THRESH  = int(os.environ.get("UI_SCORE_THRESH",       "3"))

# --- meikiocr history (short per-region strings keyed by (text, vertical_bucket)) ---
_frame_history: deque[frozenset] = deque(maxlen=FREQ_WINDOW)
_freq_lock = threading.Lock()

# --- glens history (full output lines from the second pass) ---
_glens_history: deque[frozenset] = deque(maxlen=FREQ_WINDOW)
_glens_lock = threading.Lock()

# --- per-game cache ---
_game_cache: dict[str, tuple[list, list]] = {}
_current_game: str = ""
_cache_lock = threading.Lock()


def _check_game_change() -> None:
    global _current_game
    game = identity.active_profile()
    if not game or game == _current_game:
        return
    with _cache_lock:
        if game == _current_game:
            return
        if _current_game:
            with _freq_lock:
                frame_snap = list(_frame_history)
            with _glens_lock:
                glens_snap = list(_glens_history)
            _game_cache[_current_game] = (frame_snap, glens_snap)
        if game in _game_cache:
            frame_snap, glens_snap = _game_cache[game]
            label = f"loaded cache ({len(frame_snap)}f/{len(glens_snap)}g entries)"
        else:
            frame_snap, glens_snap = [], []
            label = "fresh"
        with _freq_lock:
            _frame_history.clear()
            _frame_history.extend(frame_snap)
        with _glens_lock:
            _glens_history.clear()
            _glens_history.extend(glens_snap)
        _current_game = game
        print(f"noise_filter: game → {game!r} ({label})", flush=True)


def vertical_bucket(chars: list, img_h: int) -> str:
    if not chars or not img_h:
        return "unknown"
    ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
    cy = (min(ys) + max(ys)) / 2 / img_h
    return "top" if cy < 0.33 else ("bottom" if cy >= 0.67 else "middle")


def record_frame(results: list, img_h: int) -> None:
    _check_game_change()
    keys = frozenset(
        (r["text"].strip(), vertical_bucket(r.get("chars", []), img_h))
        for r in results if r.get("text", "").strip()
    )
    with _freq_lock:
        _frame_history.append(keys)


def record_empty_frame() -> None:
    _check_game_change()
    with _freq_lock:
        _frame_history.append(frozenset())


def _freq_score_meiki(text: str, bucket: str) -> float:
    """Returns 0.0–1.0 frequency of (text, bucket) in frame history. Caller holds _freq_lock."""
    n = len(_frame_history)
    if n < WARM_UP_ENTRIES:
        return 0.0
    def hit(frame):
        if _FUZZY:
            return any(k[1] == bucket and _Lev.normalized_similarity(text, k[0]) >= FUZZY_RATIO for k in frame)
        return (text, bucket) in frame
    return sum(hit(f) for f in _frame_history) / n


def is_suppressed(text: str, bucket: str) -> bool:
    t = text.strip()
    with _freq_lock:
        freq = _freq_score_meiki(t, bucket)
    score = 0
    if freq > SUPPRESS_THRESH:
        score += 3
    if len(t) <= 4:
        score += 1
    return score >= UI_SCORE_THRESH


def filter_meiki_results(results: list, img_h: int) -> list:
    return [
        r for r in results
        if not is_suppressed(r["text"].strip(), vertical_bucket(r.get("chars", []), img_h))
    ]


def record_glens(text: str) -> None:
    _check_game_change()
    lines = frozenset(l.strip() for l in text.split("\n") if l.strip())
    with _glens_lock:
        _glens_history.append(lines)


def is_glens_suppressed(line: str) -> bool:
    key = line.strip()
    if not key:
        return False
    with _glens_lock:
        n = len(_glens_history)
        if n < WARM_UP_ENTRIES:
            return False
        def hit(frame_lines):
            if _FUZZY:
                return any(_Lev.normalized_similarity(key, l) >= FUZZY_RATIO for l in frame_lines)
            return key in frame_lines
        freq = sum(hit(f) for f in _glens_history) / n
    score = 0
    if freq > SUPPRESS_THRESH:
        score += 3
    if len(key) <= 4:
        score += 1
    return score >= UI_SCORE_THRESH
