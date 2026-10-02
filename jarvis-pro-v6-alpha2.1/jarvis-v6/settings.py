"""Локальные настройки интерфейса. Не хранит пароли и токены."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "jarvis_settings.json"
DEFAULT_SETTINGS = {"model": "qwen2.5:7b", "voice_mode": "wake", "save_chat_history": "false"}
ALLOWED_VOICE_MODES = {"wake", "toggle", "ptt"}


def load_settings() -> dict[str, str]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            settings.update({key: str(value) for key, value in data.items() if key in DEFAULT_SETTINGS})
    except (OSError, ValueError, TypeError):
        pass
    if settings.get("voice_mode") not in ALLOWED_VOICE_MODES:
        settings["voice_mode"] = DEFAULT_SETTINGS["voice_mode"]
    if settings.get("save_chat_history", "false").casefold() not in {"true", "false"}:
        settings["save_chat_history"] = DEFAULT_SETTINGS["save_chat_history"]
    return settings


def save_settings(**updates: str) -> dict[str, str]:
    settings = load_settings()
    for key, value in updates.items():
        if key in DEFAULT_SETTINGS:
            settings[key] = str(value)
    if settings.get("voice_mode") not in ALLOWED_VOICE_MODES:
        settings["voice_mode"] = DEFAULT_SETTINGS["voice_mode"]
    if settings.get("save_chat_history", "false").casefold() not in {"true", "false"}:
        settings["save_chat_history"] = DEFAULT_SETTINGS["save_chat_history"]
    CONFIG_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings
