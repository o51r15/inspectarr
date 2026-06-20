"""
ui/routes/system.py — System → Status page

Mirrors the Servarr System → Status screen: about/version, scheduler state,
storage usage, and live connection health for every configured service.
Connection checks run concurrently and are fetched async by the page so the
initial render is instant.
"""
import os
import sys
import shutil
import platform
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, render_template, current_app, jsonify

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


def _check_one(name: str, cfg) -> dict:
    """Run a single connection test. Returns {name, configured, ok}."""
    try:
        if name == "qBittorrent":
            from core.qbit import QBittorrentClient
            c = QBittorrentClient(cfg.qbittorrent.url, cfg.qbittorrent.username, cfg.qbittorrent.password)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Sonarr":
            if not cfg.arrs.sonarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.sonarr import SonarrClient
            c = SonarrClient(cfg.arrs.sonarr.url, cfg.arrs.sonarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Radarr":
            if not cfg.arrs.radarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.radarr import RadarrClient
            c = RadarrClient(cfg.arrs.radarr.url, cfg.arrs.radarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Lidarr":
            if not cfg.arrs.lidarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.lidarr import LidarrClient
            c = LidarrClient(cfg.arrs.lidarr.url, cfg.arrs.lidarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Prowlarr":
            if not cfg.prowlarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.prowlarr import ProwlarrClient
            c = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
    except Exception:
        return {"name": name, "configured": True, "ok": False}
    return {"name": name, "configured": False, "ok": False}


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

    names = ["qBittorrent", "Sonarr", "Radarr", "Lidarr", "Prowlarr"]
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(lambda n: _check_one(n, cfg), names))

    return jsonify({"ok": True, "connections": results})
