"""
설정 관리 — settings.json 또는 .env
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_SETTINGS_FILE = Path(__file__).parent / "settings.json"

_DEFAULTS = {
    "claude_api_key": os.getenv("CLAUDE_API_KEY", ""),
    "claude_model":   os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
}


def load() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            saved = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            merged = dict(_DEFAULTS)
            merged.update(saved)
            return merged
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(settings: dict):
    _SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
