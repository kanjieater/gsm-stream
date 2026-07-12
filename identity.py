"""
Game identity detection.

Priority: runtime override (web UI preset) → GAME_NAME env var → GSM profile name.
"""
import os

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
