"""App settings that are not secrets: auto-play and which alerts to send.

Stored as JSON in ~/.hedge-fund/settings.json. Secrets (API keys, the
phone-alert topic) stay in .env through tui/keys.py, never here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from hedge_fund.paths import USER_DIR, write_atomic

SETTINGS_PATH = USER_DIR / "settings.json"

ALERT_KINDS = ("insider", "fund_buy", "congress", "hype", "pump", "shield", "big_day", "drawdown", "frozen", "golive")

DEFAULTS: dict = {
    "autoplay": False,
    "alerts": {"phone": False, "kinds": list(ALERT_KINDS)},
}


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    out = deepcopy(DEFAULTS)
    try:
        saved = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return out
    if isinstance(saved, dict):
        if isinstance(saved.get("autoplay"), bool):
            out["autoplay"] = saved["autoplay"]
        alerts = saved.get("alerts")
        if isinstance(alerts, dict):
            if isinstance(alerts.get("phone"), bool):
                out["alerts"]["phone"] = alerts["phone"]
            if isinstance(alerts.get("kinds"), list):
                out["alerts"]["kinds"] = [k for k in alerts["kinds"] if k in ALERT_KINDS]
    return out


def save_settings(update: dict, path: Path = SETTINGS_PATH) -> dict:
    """Merge *update* into the saved settings, validating every field."""
    current = load_settings(path)
    if "autoplay" in update:
        if not isinstance(update["autoplay"], bool):
            raise ValueError("autoplay must be true or false")
        current["autoplay"] = update["autoplay"]
    alerts = update.get("alerts")
    if alerts is not None:
        if not isinstance(alerts, dict):
            raise ValueError("alerts must be an object")
        if "phone" in alerts:
            if not isinstance(alerts["phone"], bool):
                raise ValueError("alerts.phone must be true or false")
            current["alerts"]["phone"] = alerts["phone"]
        if "kinds" in alerts:
            kinds = alerts["kinds"]
            if not isinstance(kinds, list) or any(k not in ALERT_KINDS for k in kinds):
                raise ValueError(f"alert kinds must be from: {', '.join(ALERT_KINDS)}")
            current["alerts"]["kinds"] = [k for k in ALERT_KINDS if k in kinds]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_atomic(Path(path), json.dumps(current, indent=2))
    return current
