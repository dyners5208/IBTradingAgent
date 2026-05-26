"""
alert_log.py — Persistent alert log for critical trading events.

Alerts are written to alerts.json in the project root so they survive
process restarts and can be reviewed retrospectively.
"""

import json
import os
from datetime import datetime

_ALERTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "alerts.json",
)
_MAX_ALERTS = 10_000


def log_alert(level: str, message: str, context: dict | None = None) -> None:
    """
    Append an alert to alerts.json.

    level   : "CRITICAL" | "WARNING" | "INFO"
    message : short human-readable description
    context : optional dict of extra details (serialised to strings)
    """
    entry: dict = {
        "timestamp": datetime.now().isoformat(),
        "level":     level,
        "message":   message,
    }
    if context:
        entry["context"] = {str(k): str(v) for k, v in context.items()}

    alerts: list = []
    if os.path.exists(_ALERTS_PATH):
        try:
            with open(_ALERTS_PATH, "r", encoding="utf-8") as f:
                alerts = json.load(f)
        except Exception:
            alerts = []

    alerts.append(entry)
    if len(alerts) > _MAX_ALERTS:
        alerts = alerts[-_MAX_ALERTS:]
    with open(_ALERTS_PATH, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2, default=str)

    prefix = "!!" if level == "CRITICAL" else ("!" if level == "WARNING" else "")
    print(f"  [{level}]{prefix} {message}")
