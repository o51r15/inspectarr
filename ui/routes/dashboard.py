from flask import Blueprint, render_template, current_app

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    scheduler = current_app.config["SCHEDULER"]
    status    = scheduler.get_status()
    return render_template("dashboard.html", status=status)


@dashboard_bp.route("/status")
def status_json():
    """Polled by JS every 5s to update the dashboard live."""
    from flask import jsonify
    scheduler = current_app.config["SCHEDULER"]
    return jsonify(scheduler.get_status())
