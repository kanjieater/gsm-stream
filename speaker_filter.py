"""
Positional speaker name classifier for meikiocr results.

Learns which screen regions consistently contain short, no-punctuation text
adjacent to dialogue. Speaker names in VN dialogue boxes exhibit this pattern
without requiring any game-specific configuration.

Hard gate: any sentence punctuation (。！？、…) → score 0 immediately.
This protects lines like ザ・ゾンビ？ from being misclassified.

Scoring (threshold SPEAKER_SCORE_THRESH, default 9):
  +3  short text (≤ SPEAKER_MAX_LEN, default 10 chars)
  +2  no sentence punctuation (always awarded when gate is passed)
  +3  position stable across recent frames OR cached as speaker region
  +2  dialogue text present in the same frame
  +2  positioned in a different vertical band than dialogue

Per-game region cache:
  _RegionProfile tracks each grid cell's classification history:
    - confidence = speaker_hits / total_obs  (how often this cell was speaker)
    - text_diversity = distinct_texts / total_obs  (low = nameplate, high = dialogue)

  A cached region boosts scoring only when confidence is high AND diversity is low.
  High diversity = dialogue text at a consistent screen position → NOT a speaker region.
  This guards against a character name mentioned in dialogue at a stable position
  eventually being mistaken for a nameplate.

  Cache accelerates warm-up when returning to the same game; it never permanently
  blacklists text strings — only layout behavior is stored.
"""
import dataclasses
import os
import threading
from collections import deque

import identity
import text_filter

SPEAKER_WINDOW       = int(os.environ.get("SPEAKER_WINDOW",        "30"))
SPEAKER_POS_THRESH   = float(os.environ.get("SPEAKER_POS_THRESH",   "0.5"))
SPEAKER_SCORE_THRESH = int(os.environ.get("SPEAKER_SCORE_THRESH",   "9"))
SPEAKER_WARM_UP      = int(os.environ.get("SPEAKER_WARM_UP",        "3"))
SPEAKER_MAX_LEN      = int(os.environ.get("SPEAKER_MAX_LEN",        "10"))

CACHE_CONF_THRESH    = float(os.environ.get("SPEAKER_CACHE_CONF",          "0.7"))
CACHE_DIVERSITY_MAX  = float(os.environ.get("SPEAKER_CACHE_DIVERSITY_MAX",  "0.5"))
CACHE_MIN_OBS        = int(os.environ.get("SPEAKER_CACHE_MIN_OBS",         "5"))
CACHE_MAX_TEXTS      = int(os.environ.get("SPEAKER_CACHE_MAX_TEXTS",       "50"))

_GRID = 10  # discretize image into _GRID x _GRID position cells

_region_history: deque[frozenset] = deque(maxlen=SPEAKER_WINDOW)
_region_lock = threading.Lock()


@dataclasses.dataclass
class _RegionProfile:
    """Layout knowledge for a single grid cell, accumulated across frames."""
    total_obs: int = 0
    speaker_hits: int = 0
    seen_texts: set = dataclasses.field(default_factory=set)

    @property
    def confidence(self) -> float:
        if self.total_obs < CACHE_MIN_OBS:
            return 0.0
        return self.speaker_hits / self.total_obs

    @property
    def text_diversity(self) -> float:
        """Low = nameplate (same few names). High = dialogue at consistent position."""
        if not self.total_obs:
            return 0.0
        return len(self.seen_texts) / min(self.total_obs, CACHE_MAX_TEXTS)

    def record(self, text: str, is_speaker: bool) -> None:
        self.total_obs += 1
        if is_speaker:
            self.speaker_hits += 1
        if len(self.seen_texts) < CACHE_MAX_TEXTS:
            self.seen_texts.add(text.strip())


# Per-game persistent layout cache: game_name → {cell → _RegionProfile}
_game_speaker_profiles: dict[str, dict] = {}
_active_profiles: dict = {}
_last_game: str = ""
_profile_lock = threading.Lock()


def _check_game_change() -> None:
    global _active_profiles, _last_game
    current = identity.active_profile()
    with _profile_lock:
        if current == _last_game:
            return
        _game_speaker_profiles[_last_game] = dict(_active_profiles)
        _active_profiles = dict(_game_speaker_profiles.get(current, {}))
        _last_game = current
        print(f"speaker_filter: profile → {current!r}", flush=True)


def _center(result: dict, img_h: int, img_w: int) -> tuple[float, float]:
    chars = result.get("chars", [])
    if not chars or not img_h or not img_w:
        return (-1.0, -1.0)
    xs = [c["bbox"][0] for c in chars] + [c["bbox"][2] for c in chars]
    ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
    return ((min(xs) + max(xs)) / 2 / img_w, (min(ys) + max(ys)) / 2 / img_h)


def _cell(cx: float, cy: float) -> tuple[int, int]:
    if cx < 0:
        return (-1, -1)
    return (min(int(cx * _GRID), _GRID - 1), min(int(cy * _GRID), _GRID - 1))


def _is_candidate(text: str) -> bool:
    t = text.strip()
    return bool(t) and len(t) <= SPEAKER_MAX_LEN and not text_filter._SENT_PUNCT.search(t)


def record_frame(results: list, img_h: int, img_w: int) -> None:
    """Record positions of short no-punct results from this (noise-filtered) frame."""
    cells = frozenset(
        _cell(*_center(r, img_h, img_w))
        for r in results
        if _is_candidate(r.get("text", ""))
    )
    with _region_lock:
        _region_history.append(cells)


def _pos_stable(cell: tuple[int, int]) -> bool:
    if cell[0] < 0:
        return False
    with _region_lock:
        n = len(_region_history)
        if n < SPEAKER_WARM_UP:
            return False
        return sum(cell in f for f in _region_history) / n >= SPEAKER_POS_THRESH


def _cache_confident(cell: tuple[int, int]) -> bool:
    """True if the per-game cache indicates this region reliably contains speaker names.

    Requires high confidence AND low text diversity. High diversity means this
    cell sees many different strings → likely dialogue at a consistent position,
    not a nameplate.
    """
    with _profile_lock:
        prof = _active_profiles.get(cell)
    if not prof:
        return False
    return prof.confidence >= CACHE_CONF_THRESH and prof.text_diversity <= CACHE_DIVERSITY_MAX


def _record_decision(cell: tuple[int, int], text: str, is_speaker: bool) -> None:
    if cell[0] < 0:
        return
    with _profile_lock:
        prof = _active_profiles.get(cell)
        if prof is None:
            prof = _RegionProfile()
            _active_profiles[cell] = prof
        prof.record(text, is_speaker)


def score(result: dict, img_h: int, img_w: int, all_results: list) -> int:
    text = result.get("text", "").strip()
    if not text or text_filter._SENT_PUNCT.search(text):
        return 0  # hard gate

    s = 0
    if len(text) <= SPEAKER_MAX_LEN:
        s += 3
    s += 2  # passed no-punctuation gate

    cx, cy = _center(result, img_h, img_w)
    cell = _cell(cx, cy)
    if _pos_stable(cell) or _cache_confident(cell):
        s += 3

    dialogue_others = [
        r for r in all_results
        if r is not result and text_filter.looks_like_dialogue(r.get("text", "").strip())
    ]
    if dialogue_others:
        s += 2  # dialogue present in frame
        my_row = cell[1]
        for r in dialogue_others:
            _, other_cy = _center(r, img_h, img_w)
            if _cell(0.0, other_cy)[1] != my_row:
                s += 2  # speaker is in a different vertical band than dialogue
                break

    return s


def filter_speakers(results: list, img_h: int, img_w: int) -> tuple[list, list]:
    """Return (dialogue_results, speaker_results) partitioned by score threshold."""
    _check_game_change()
    dialogue, speakers = [], []
    for r in results:
        text = r.get("text", "").strip()
        cx, cy = _center(r, img_h, img_w)
        cell = _cell(cx, cy)

        # Fast path: once a region is proven (rolling window stable or cached),
        # skip adjacency scoring — no need to see dialogue in this specific frame.
        if _is_candidate(text) and (_pos_stable(cell) or _cache_confident(cell)):
            _record_decision(cell, text, True)
            speakers.append(r)
            continue

        s = score(r, img_h, img_w, results)
        is_speaker = s >= SPEAKER_SCORE_THRESH
        _record_decision(cell, text, is_speaker)
        (speakers if is_speaker else dialogue).append(r)
    return dialogue, speakers