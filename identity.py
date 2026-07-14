"""
Game identity detection.

Priority: runtime override (web UI preset) → GAME_NAME env var → GSM profile name.
"""
import os
import yaml as _yaml

_PROFILES_PATH = os.environ.get("PROFILES_CONFIG", "/profiles.yml")

_runtime_game: str = ""


def set_runtime_game(name: str) -> None:
    """Set the active game identity from an external signal (e.g. texthooker preset switch)."""
    global _runtime_game
    _runtime_game = name.strip()


def active_profile() -> str:
    if _runtime_game:
        return _runtime_game
    env = os.environ.get("GAME_NAME", "").strip()
    if env:
        return env
    try:
        from GameSentenceMiner.util.config.configuration import config_instance
        if config_instance:
            return config_instance.current_profile or ""
    except Exception:
        pass
    return ""


def get_ocr_strip_pattern(game_name: str) -> str:
    if not game_name:
        return ""
    try:
        with open(_PROFILES_PATH) as f:
            data = _yaml.safe_load(f) or {}
        return (data.get("game_overrides") or {}).get(game_name, {}).get("ocr_strip", "")
    except Exception:
        return ""
