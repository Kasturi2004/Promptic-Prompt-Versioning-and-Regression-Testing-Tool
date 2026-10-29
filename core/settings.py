"""
core/settings.py
Simple persistent app settings, stored as JSON.
Kept separate from the SQLite database since these are lightweight
app-level preferences, not relational data.
"""

import json
import os

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "settings.json")

DEFAULTS = {
    "max_concurrent_requests": 2,
}


def load_settings() -> dict:
    """Return current settings, falling back to defaults for any missing keys."""
    if not os.path.exists(SETTINGS_PATH):
        return DEFAULTS.copy()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return DEFAULTS.copy()
        merged = DEFAULTS.copy()
        value = data.get("max_concurrent_requests", DEFAULTS["max_concurrent_requests"])
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            value = DEFAULTS["max_concurrent_requests"]
        merged["max_concurrent_requests"] = value
        return merged
    except (json.JSONDecodeError, OSError):
        return DEFAULTS.copy()


def save_settings(settings: dict, actor_username: str | None = None) -> None:
    """Persist global app settings; only the persisted administrator may write."""
    from core.auth import is_admin

    if not actor_username or not is_admin(actor_username):
        raise PermissionError("Only the administrator can change application settings.")
    if not isinstance(settings, dict):
        raise ValueError("Settings must be an object.")
    value = settings.get("max_concurrent_requests", DEFAULTS["max_concurrent_requests"])
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError("max_concurrent_requests must be between 1 and 5.")
    settings = {**DEFAULTS, **settings, "max_concurrent_requests": value}
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
