"""
ui/routes/system.py — System → Status page

Mirrors the Servarr System → Status screen: about/version, scheduler state,
storage usage, and live connection health for every configured service.
Connection checks run concurrently and are fetched async by the page so the
initial render is instant.
"""
import os
import shutil
import platform
from flask import Blueprint, render_template, current_app, jsonify

# ROADMAP item 13: the checks live in core/ so the CLI can use them too.
from core.connections import check_all, check_connection

# Kept so anything still referring to the old private name keeps working.
_check_one = check_connection

system_bp = Blueprint("system", __name__)


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


@system_bp.route("/system/status")
def system_status():
    config_path = current_app.config["CONFIG_PATH"]
    scheduler   = current_app.config["SCHEDULER"]

    info = {
        "python_version": platform.python_version(),
        "platform":       platform.system() + " " + platform.release(),
        "scheduler":      scheduler.get_status(),
    }

    # Storage — DB + log sizes, disk usage on the data volume
    storage = {}
    try:
        from core.config import load_config
        cfg = load_config(config_path)
        db_path  = cfg.state.db_file
        log_path = cfg.logging.log_file
        storage["db_path"]  = db_path
        storage["log_path"] = log_path
        storage["db_size"]  = _fmt_bytes(os.path.getsize(db_path))  if os.path.exists(db_path)  else "—"
        storage["log_size"] = _fmt_bytes(os.path.getsize(log_path)) if os.path.exists(log_path) else "—"
        # Disk usage on the directory holding the DB (the data volume)
        data_dir = os.path.dirname(os.path.abspath(db_path)) or "."
        usage = shutil.disk_usage(data_dir)
        storage["disk_total"] = _fmt_bytes(usage.total)
        storage["disk_used"]  = _fmt_bytes(usage.used)
        storage["disk_free"]  = _fmt_bytes(usage.free)
        storage["disk_pct"]   = round(usage.used / usage.total * 100, 1) if usage.total else 0
    except Exception as exc:
        storage["error"] = str(exc)

    return render_template("system_status.html", info=info, storage=storage)


@system_bp.route("/system/status/data")
def system_status_data():
    """
    Run all connection checks concurrently and return JSON.
    Concurrency keeps total time near a single timeout rather than the sum of
    five sequential timeouts when services are unreachable.
    """
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        cfg = load_config(config_path)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc), "connections": []})

    return jsonify({"ok": True, "connections": check_all(cfg)})


@system_bp.route("/system/tasks")
def system_tasks():
    """System → Tasks: show scheduled internal jobs and their state."""
    config_path = current_app.config["CONFIG_PATH"]
    scheduler   = current_app.config["SCHEDULER"]
    status      = scheduler.get_status()

    tasks = []
    try:
        from core.config import load_config
        cfg = load_config(config_path)

        # 1. Torrent Scan
        tasks.append({
            "name":          "Torrent Scan",
            "interval":      f"Every {cfg.poll_interval_seconds}s",
            "last_execution": status.get("last_run"),
            "next_execution": status.get("next_run"),
            "state":         "running" if status.get("scanning") else
                             ("queued" if status.get("running") else "idle"),
        })

        # 2. Retry Processing
        tasks.append({
            "name":          "Retry Processing",
            "interval":      f"Before each scan (every {cfg.retry.interval_seconds}s between attempts)"
                             if cfg.retry.enabled else "Disabled",
            "last_execution": status.get("last_run"),  # runs at scan time
            "next_execution": status.get("next_run") if cfg.retry.enabled else None,
            "state":         "idle" if cfg.retry.enabled else "disabled",
        })

        # 3. Prowlarr Auto-Reorder
        prowlarr_enabled = cfg.prowlarr.enabled
        last_reorder = None
        if scheduler.last_reorder:
            last_reorder = scheduler.last_reorder.isoformat()
        tasks.append({
            "name":          "Prowlarr Auto-Reorder",
            "interval":      f"Every {cfg.prowlarr.reorder_interval_hours}h"
                             if prowlarr_enabled else "Disabled",
            "last_execution": last_reorder,
            "next_execution": None,  # calculated relative to last_reorder
            "state":         "idle" if prowlarr_enabled else "disabled",
        })

        # 4. Log & State Pruning
        tasks.append({
            "name":          "Log & State Pruning",
            "interval":      "On startup / before each scan",
            "last_execution": status.get("last_run"),
            "next_execution": status.get("next_run"),
            "state":         "idle",
        })

    except Exception as exc:
        return render_template("system_tasks.html", tasks=[], error=str(exc))

    return render_template("system_tasks.html", tasks=tasks, error=None)


@system_bp.route("/system/updates")
def system_updates():
    """System → Updates: show current version and check GitHub for newer releases."""
    return render_template("system_updates.html")


@system_bp.route("/system/updates/check")
def system_updates_check():
    """Fetch the latest release from GitHub and compare to the running version."""
    import requests
    import re
    from core import __version__
    current = f"v{__version__}"
    try:
        resp = requests.get(
            "https://api.github.com/repos/o51r15/inspectarr/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 404:
            return jsonify({"ok": True, "current": current, "latest": current,
                            "up_to_date": True, "message": "No releases published yet"})
        if resp.status_code == 403:
            return jsonify({"ok": False, "current": current,
                            "message": "GitHub API rate limit exceeded — try again later"})
        resp.raise_for_status()
        data = resp.json()
        latest = data.get("tag_name", current)
        published = data.get("published_at", "")
        body = data.get("body", "")
        html_url = data.get("html_url", "")

        def _ver_tuple(v):
            return tuple(int(x) for x in re.findall(r"\d+", v))

        up_to_date = _ver_tuple(latest) <= _ver_tuple(current)
        return jsonify({
            "ok": True,
            "current": current,
            "latest": latest,
            "up_to_date": up_to_date,
            "published": published,
            "release_notes": body[:2000],
            "url": html_url,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc), "current": current})
