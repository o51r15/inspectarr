from flask import Blueprint, render_template, current_app, request, redirect, url_for, jsonify

scheduler_bp = Blueprint("scheduler", __name__)


@scheduler_bp.route("/scheduler")
def scheduler_view():
    scheduler = current_app.config["SCHEDULER"]
    status    = scheduler.get_status()
    try:
        from core.config import load_config
        config   = load_config(current_app.config["CONFIG_PATH"])
        interval = config.poll_interval_seconds
    except Exception:
        interval = 300
    return render_template("scheduler.html", status=status, interval=interval)


@scheduler_bp.route("/scheduler/toggle", methods=["POST"])
def toggle():
    scheduler = current_app.config["SCHEDULER"]
    if scheduler.running:
        scheduler.stop()
    else:
        scheduler.start()
    return redirect(request.referrer or url_for("dashboard.index"))


@scheduler_bp.route("/scheduler/run", methods=["POST"])
def run_now():
    scheduler = current_app.config["SCHEDULER"]
    if not scheduler.is_scanning():
        scheduler.trigger()
    return redirect(request.referrer or url_for("dashboard.index"))


@scheduler_bp.route("/scheduler/status")
def scheduler_status():
    scheduler = current_app.config["SCHEDULER"]
    return jsonify(scheduler.get_status())
