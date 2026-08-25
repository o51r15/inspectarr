"""
ui/routes/api.py — JSON API endpoints for external integrations

GET  /api/status  — scheduler state, last run, config summary
POST /api/scan    — trigger a manual scan
GET  /api/logs    — recent log entries with optional level filter
"""
from flask import Blueprint, current_app, request, jsonify
from ui.routes._utils import safe_error

api_bp = Blueprint("api", __name__)


@api_bp.route("/api/status", methods=["GET"])
def api_status():
    """Return scheduler status, last run info, and high-level config summary."""
    try:
        scheduler = current_app.config.get("SCHEDULER")
        if not scheduler:
            return jsonify({"ok": False, "message": "Scheduler not initialized"}), 503

        from core.config import load_config
        config_path = current_app.config["CONFIG_PATH"]
        cfg = load_config(config_path)

        state = current_app.config.get("STATE")
        last_run = None
        if state:
            runs = state.get_recent_runs(limit=1)
            if runs:
                last_run = runs[0]

        return jsonify({
            "ok": True,
            "scheduler": {
                "running": scheduler.running,
                "interval_seconds": cfg.scanning.polling.interval_seconds,
                "polling_enabled": cfg.scanning.polling.enabled,
                "webhooks_enabled": cfg.scanning.webhooks.enabled,
            },
            "config": {
                "torrent_client": cfg.torrent_client,
                "dry_run": cfg.dry_run,
                "rules_count": len(cfg.rules),
                "prowlarr_enabled": cfg.prowlarr.enabled,
                "notifications_enabled": cfg.notifications.apprise.enabled,
            },
            "last_run": last_run,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500


@api_bp.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a manual scan. Returns immediately — scan runs in background."""
    try:
        scheduler = current_app.config.get("SCHEDULER")
        if not scheduler:
            return jsonify({"ok": False, "message": "Scheduler not initialized"}), 503

        scheduler.trigger()
        return jsonify({"ok": True, "message": "Scan triggered"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500


@api_bp.route("/api/logs", methods=["GET"])
def api_logs():
    """
    Return recent log entries as JSON.

    Query params:
      level  — filter by log level (DEBUG, INFO, WARNING, ERROR)
      limit  — max entries to return (default 100, max 500)
    """
    import json as _json
    import os

    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        cfg = load_config(config_path)
        log_path = cfg.logging.log_file

        level_filter = request.args.get("level", "").upper()
        limit = min(int(request.args.get("limit", 100)), 500)

        if not os.path.exists(log_path):
            return jsonify({"ok": True, "entries": [], "total": 0})

        entries = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if level_filter and entry.get("level", "").upper() != level_filter:
                    continue
                entries.append(entry)

        # Return the most recent entries
        entries = entries[-limit:]
        return jsonify({"ok": True, "entries": entries, "total": len(entries)})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500


@api_bp.route("/api/health", methods=["GET"])
def api_health():
    """
    Liveness / readiness probe.

    Fast by default: makes NO outbound network calls, so it is safe for a
    Docker HEALTHCHECK polling every 30s. It reports Inspectarr's own health
    -- the process, its database, and its scheduler thread -- deliberately
    NOT the health of Sonarr/Prowlarr/etc. A probe that failed because a
    downstream service was down would restart a perfectly healthy container.

    Pass ?deps=1 to additionally run live connection checks against every
    configured service. That path does make outbound calls, is slow when a
    service is unreachable, and still requires authentication. Do not use it
    in a HEALTHCHECK.

    Status: 200 when healthy, 503 when a core component has failed.
    """
    from core import __version__

    checks = {}
    healthy = True

    # --- Database -----------------------------------------------------
    state = current_app.config.get("STATE")
    if state is None:
        checks["database"] = "unavailable"
        healthy = False
    elif state.ping():
        checks["database"] = "ok"
    else:
        checks["database"] = "error"
        healthy = False

    # --- Scheduler ----------------------------------------------------
    # "stopped" is a legitimate configuration: the scheduler ships disabled
    # by default and webhook-only deployments never start it. The one state
    # that genuinely means unhealthy is the flag claiming it runs while the
    # thread is gone -- that means the loop died without cleaning up.
    scheduler = current_app.config.get("SCHEDULER")
    last_scan = None
    if scheduler is None:
        checks["scheduler"] = "unavailable"
        healthy = False
    else:
        try:
            last_scan = scheduler.last_run
            thread = getattr(scheduler, "_thread", None)
            thread_alive = bool(thread is not None and thread.is_alive())
            if scheduler.running and not thread_alive:
                checks["scheduler"] = "died"
                healthy = False
            elif not scheduler.running:
                checks["scheduler"] = "stopped"
            elif scheduler.is_scanning():
                checks["scheduler"] = "scanning"
            else:
                checks["scheduler"] = "running"
        except Exception:
            checks["scheduler"] = "error"
            healthy = False

    payload = {
        "status":    "healthy" if healthy else "unhealthy",
        "version":   __version__,
        "checks":    checks,
        "last_scan": last_scan,
    }

    # --- Optional dependency checks (?deps=1) -------------------------
    # Auth is re-checked here because check_auth() exempts the bare probe so
    # Docker can reach it without credentials. This branch reaches out to the
    # network, so it does not get that exemption.
    if request.args.get("deps"):
        from ui.auth import check_auth
        denied = check_auth(current_app.config["CONFIG_PATH"])
        if denied is not None:
            return denied
        try:
            from core.connections import check_all
            from core.config import load_config

            cfg = load_config(current_app.config["CONFIG_PATH"])
            payload["dependencies"] = check_all(cfg)
        except Exception as exc:
            # A dependency-check failure is reported, not raised: the core
            # health verdict above stands on its own.
            payload["dependencies"] = []
            payload["dependencies_error"] = safe_error(exc)

    return jsonify(payload), (200 if healthy else 503)
