from flask import Blueprint, current_app, request, redirect, url_for, jsonify

scheduler_bp = Blueprint("scheduler", __name__)


@scheduler_bp.route("/scheduler")
def scheduler_view():
    # Its content (schedules, retry queue, recent scans) now lives on
    # System > Tasks. The POST/JSON endpoints below keep their URLs.
    return redirect("/system/tasks", code=301)


PAUSED_KEY = "scheduler_paused"


def _remember_paused(paused: bool) -> None:
    """
    Persist the user's last Pause/Resume so it survives a restart.

    Rule (web.py main): scheduler_autostart decides the very first boot;
    after anyone presses Pause or Resume, their last choice wins. A failed
    write only loses that memory, so it is logged and never blocks the toggle.
    """
    state = current_app.config.get("STATE")
    if not state:
        return
    try:
        state.set_app_state(PAUSED_KEY, "1" if paused else "0")
    except Exception as exc:
        current_app.logger.warning(f"Could not persist scheduler pause: {exc}")


def _toggle() -> tuple[bool, bool, str]:
    """Flip the scheduler. Returns (ok, running_now, message)."""
    scheduler = current_app.config["SCHEDULER"]
    if scheduler.running:
        scheduler.stop()
        _remember_paused(True)
        return True, False, "Scheduler stopped"
    if scheduler.start():
        _remember_paused(False)
        return True, True, "Scheduler started"
    # BUG-11: previous loop still finishing a scan — did not start
    return False, False, "Scheduler still stopping - try again shortly"


@scheduler_bp.route("/scheduler/toggle.json", methods=["POST"])
def toggle_json():
    ok, running, message = _toggle()
    return jsonify({"ok": ok, "running": running, "message": message}), (200 if ok else 409)


@scheduler_bp.route("/scheduler/run.json", methods=["POST"])
def run_json():
    if current_app.config["SCHEDULER"].trigger():
        return jsonify({"ok": True, "message": "Scan started"})
    return jsonify({"ok": False, "message": "A scan is already running"}), 409


@scheduler_bp.route("/scheduler/toggle", methods=["POST"])
def toggle():
    ok, running, message = _toggle()
    if ok:
        suffix = ("?toast=Scheduler+started&level=success" if running
                  else "?toast=Scheduler+stopped&level=info")
    else:
        suffix = "?toast=Scheduler+still+stopping+-+try+again+shortly&level=warning"
    referrer = request.referrer or url_for("dashboard.index")
    # Strip any existing toast params from referrer before appending new ones
    base = referrer.split("?")[0]
    return redirect(base + suffix)


@scheduler_bp.route("/scheduler/run", methods=["POST"])
def run_now():
    scheduler = current_app.config["SCHEDULER"]
    # BUG-12: trigger() itself now refuses when a scan is in flight (the old
    # is_scanning()-then-trigger() pattern was a check-then-act race).
    if scheduler.trigger():
        suffix = "?toast=Scan+triggered&level=success"
    else:
        suffix = "?toast=Scan+already+running&level=warning"
    referrer = request.referrer or url_for("dashboard.index")
    base = referrer.split("?")[0]
    return redirect(base + suffix)


@scheduler_bp.route("/scheduler/status")
def scheduler_status():
    scheduler = current_app.config["SCHEDULER"]
    return jsonify(scheduler.get_status())
