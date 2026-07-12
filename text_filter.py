import re

_BLANK_LINE = "ＢＬＡＮＫ＿ＬＩＮＥ"

# Fullwidth ASCII variants (U+FF01–U+FF5E: ！ through ～) with optional spaces.
# Publisher watermarks (e.g. ＯＩＤＥＡＦＡＣＴＯＲＹ) are fullwidth Latin with no kana/kanji.
_FULLWIDTH_ONLY = re.compile(r'^[！-～\s]+$')
_HAS_JP = re.compile(r'[぀-ヿ一-鿿㐀-䶿豈-﫿]')

# Sentence-ending punctuation used in both speaker and dialogue detection.
_SENT_PUNCT = re.compile(r'[。！？、…]')
_TRAILING_FULLWIDTH = re.compile(r'[！-～]+$')

MAX_SPEAKER_LEN = 12


def is_watermark(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_FULLWIDTH_ONLY.match(t)) and not bool(_HAS_JP.search(t))


def strip_owocr_artifacts(msg: str) -> str:
    """Remove BLANK_LINE region separators, fullwidth-only watermark lines, and trailing fullwidth chars."""
    lines = msg.split("\n")
    clean: list[str] = []
    for line in lines:
        if line.strip() == _BLANK_LINE:
            continue
        if not is_watermark(line):
            clean.append(_TRAILING_FULLWIDTH.sub('', line).rstrip())
    return "\n".join(clean).strip()


def looks_like_dialogue(line: str) -> bool:
    """True if line looks like dialogue rather than a speaker name or label."""
    if not line:
        return False
    if line[0] in '「『':
        return True
    if _SENT_PUNCT.search(line):
        return True
    if len(line) > 15:  # last resort: clearly too long to be a name
        return True
    return False


def is_likely_speaker_name(lines: list[str], index: int) -> bool:
    """Text-only heuristic used as fallback for glens output (no bbox data)."""
    line = lines[index].strip()
    if not line or len(line) > MAX_SPEAKER_LEN:
        return False
    if _SENT_PUNCT.search(line):
        return False
    next_idx = index + 1
    while next_idx < len(lines) and not lines[next_idx].strip():
        next_idx += 1
    if next_idx >= len(lines):
        return False
    return looks_like_dialogue(lines[next_idx].strip())


def split_speaker(text: str) -> tuple[str | None, str]:
    """Split speaker name from dialogue if structurally detectable (glens fallback).

    Checks both leading (name before dialogue) and trailing (name after dialogue)
    layouts since games differ — e.g. Steins;Gate appends the name at the end.
    """
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None, text
    # Speaker before dialogue
    if is_likely_speaker_name(lines, 0):
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    # Speaker after dialogue
    last = lines[-1].strip()
    if last and len(last) <= MAX_SPEAKER_LEN and not _SENT_PUNCT.search(last):
        if looks_like_dialogue(lines[-2].strip()):
            return last, "\n".join(lines[:-1]).strip()
    return None, text
