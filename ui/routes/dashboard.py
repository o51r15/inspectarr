"""
ui/routes/dashboard.py — Overview (the home page) and its live status JSON.

The blueprint keeps its old name ("dashboard") and endpoint ("index") so
existing url_for("dashboard.index") calls keep working; only the page changed.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, render_template

from ui.overview import compute_health, outcome_label, risk_tone

dashboard_bp = Blueprint("dashboard", __name__)

RECENT_LIMIT = 8
WINDOW_DAYS = 7


def _mode() -> str:
    reader = current_app.config.get("READ_OPERATING_MODE")
    return reader() if reader else "automatic"


def _overview_numbers(state) -> dict:
    """Counts for the three tiles. Each read fails to 0, never to an error page."""
    if not state:
        return {"held": 0, "caught": 0, "removed": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    return {
        "held": state.count_quarantine_held(),
        "caught": state.count_inspections(since_iso=since),
        "removed": state.count_inspections(action="deleted", since_iso=since),
    }


@dashboard_bp.route("/")
def index():
    scheduler = current_app.config["SCHEDULER"]
    state = current_app.config.get("STATE")
    status = scheduler.get_status()
    mode = _mode()
    numbers = _overview_numbers(state)

    recent = []
    for row in (state.get_inspections(limit=RECENT_LIMIT) if state else []):
        label, tone = outcome_label(row)
        row["outcome"], row["outcome_tone"] = label, tone
        row["risk_tone"] = risk_tone(row.get("risk_level"))
        recent.append(row)

    # Connections are checked by the browser after render (overview.js calls
    # /system/status/data), so a slow service never delays this page. The
    # hero is rendered here assuming nothing is down and is replaced if the
    # check finds otherwise.
    health = compute_health(status, mode, numbers["held"], [])
    return render_template(
        "overview.html", status=status, mode=mode, health=health,
        numbers=numbers, window_days=WINDOW_DAYS, recent=recent,
        page_data={"mode": mode, "held": numbers["held"],
                   "running": bool(status.get("running"))},
    )


@dashboard_bp.route("/status")
def status_json():
    """Polled by the Overview page. Keys are only ever added, never removed."""
    scheduler = current_app.config["SCHEDULER"]
    state = current_app.config.get("STATE")
    data = scheduler.get_status()
    data["held"] = state.count_quarantine_held() if state else 0
    data["mode"] = _mode()
    return jsonify(data)
