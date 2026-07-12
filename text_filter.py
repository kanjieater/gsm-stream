import re

_BLANK_LINE = "ＢＬＡＮＫ＿ＬＩＮＥ"

# Fullwidth ASCII variants (U+FF01–U+FF5E: ！ through ～) with optional spaces.
# Publisher watermarks (e.g. ＯＩＤＥＡＦＡＣＴＯＲＹ) are fullwidth Latin with no kana/kanji.
_FULLWIDTH_ONLY = re.compile(r'^[！-～\s]+$')
_HAS_JP = re.compile(r'[぀-ヿ一-鿿㐀-䶿豈-﫿]')


def is_watermark(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_FULLWIDTH_ONLY.match(t)) and not bool(_HAS_JP.search(t))


def strip_owocr_artifacts(msg: str) -> str:
    """Remove BLANK_LINE region separators and fullwidth-only watermark lines from owocr output."""
    lines = msg.split("\n")
    clean: list[str] = []
    for line in lines:
        if line.strip() == _BLANK_LINE:
            continue
        if not is_watermark(line):
            clean.append(line)
    return "\n".join(clean).strip()


_SPEAKER_NO_PUNCT = re.compile(r'[。！？、…]')
_DIALOGUE_PUNCT   = re.compile(r'[。！？、…]')
MAX_SPEAKER_LEN   = 12


def _looks_like_dialogue(line: str) -> bool:
    if not line:
        return False
    if line[0] in '「『':
        return True
    if _DIALOGUE_PUNCT.search(line):
        return True
    if len(line) > 15:  # last resort: clearly too long to be a name
        return True
    return False


def is_likely_speaker_name(lines: list[str], index: int) -> bool:
    line = lines[index].strip()
    if not line or len(line) > MAX_SPEAKER_LEN:
        return False
    if _SPEAKER_NO_PUNCT.search(line):
        return False
    next_idx = index + 1
    while next_idx < len(lines) and not lines[next_idx].strip():
        next_idx += 1
    if next_idx >= len(lines):
        return False
    return _looks_like_dialogue(lines[next_idx].strip())


def split_speaker(text: str) -> tuple[str | None, str]:
    """Split leading speaker name from dialogue if structurally detectable."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) >= 2 and is_likely_speaker_name(lines, 0):
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    return None, text
