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
                "running": scheduler.is_running(),
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

        scheduler.run_now()
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
