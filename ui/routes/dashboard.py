from flask import Blueprint, render_template, current_app

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    scheduler = current_app.config["SCHEDULER"]
    status    = scheduler.get_status()
    try:
        from core.config import load_config
        cfg = load_config(current_app.config["CONFIG_PATH"])
        retention_days = cfg.logging.retention_days
    except Exception:
        retention_days = 30
    state = current_app.config.get("STATE")
    flagged_history = []
    if state:
        try:
            flagged_history = state.get_flagged_history(limit=50)
        except Exception:
            pass
    return render_template("dashboard.html", status=status,
                           retention_days=retention_days,
                           flagged_history=flagged_history)


@dashboard_bp.route("/status")
def status_json():
    """Polled by JS every 5s to update the dashboard live."""
    from flask import jsonify
    scheduler = current_app.config["SCHEDULER"]
    return jsonify(scheduler.get_status())
