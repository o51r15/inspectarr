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
            from datetime import datetime, timezone, timedelta
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


@scheduler_bp.route("/scheduler/toggle", methods=["POST"])
def toggle():
    scheduler = current_app.config["SCHEDULER"]
    if scheduler.running:
        scheduler.stop()
        suffix = "?toast=Scheduler+stopped&level=info"
    else:
        if scheduler.start():
            suffix = "?toast=Scheduler+started&level=success"
        else:
            # BUG-11: previous loop still finishing a scan — did not start
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
