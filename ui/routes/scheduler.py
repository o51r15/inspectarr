from flask import Blueprint, render_template, current_app, request, redirect, url_for, jsonify

scheduler_bp = Blueprint("scheduler", __name__)


@scheduler_bp.route("/scheduler")
def scheduler_view():
    scheduler = current_app.config["SCHEDULER"]
    status    = scheduler.get_status()
    try:
        from core.config import load_config
        config   = load_config(current_app.config["CONFIG_PATH"])
        interval = config.scanning.polling.interval_seconds
    except Exception:
        interval = 300

    # Prowlarr scoring schedule info
    scoring_info = None
    try:
        from core.config import load_config as _lc
        cfg = _lc(current_app.config["CONFIG_PATH"])
        if cfg.prowlarr.enabled:
            from ui.routes.config import _get_state
            from datetime import datetime, timedelta
            state = _get_state(cfg)
            last_iso = state.get_app_state("last_prowlarr_reorder")
            last_reorder = None
            next_reorder = None
            if last_iso:
                try:
                    last_reorder = last_iso[:19].replace("T", " ")
                    last_dt = datetime.fromisoformat(last_iso)
                    next_dt = last_dt + timedelta(hours=cfg.prowlarr.reorder_interval_hours)
                    next_reorder = next_dt.isoformat()[:19].replace("T", " ")
                except ValueError:
                    pass
            scoring_info = {
                "interval_hours": cfg.prowlarr.reorder_interval_hours,
                "last_reorder": last_reorder,
                "next_reorder": next_reorder,
                "auto_manage": cfg.prowlarr.auto_manage.enabled,
            }
    except Exception:
        pass

    # Retry queue entries
    retry_entries = []
    try:
        from core.config import load_config as _lc2
        cfg2 = _lc2(current_app.config["CONFIG_PATH"])
        from ui.routes.config import _get_state as _gs2
        state2 = _gs2(cfg2)
        retry_entries = state2.get_all_unresolved_retries()
    except Exception:
        pass

    return render_template("scheduler.html", status=status, interval=interval,
                           scoring_info=scoring_info, retry_entries=retry_entries)


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
