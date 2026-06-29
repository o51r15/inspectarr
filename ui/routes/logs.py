import json
import os
from flask import Blueprint, render_template, current_app, request, jsonify, redirect, url_for

logs_bp = Blueprint("logs", __name__)

PAGE_SIZE = 100


def _read_log_page(log_path: str, page: int, level_filter: str) -> tuple[list, int]:
    """
    Read log entries from the JSON Lines file.
    Returns (entries_for_page, total_matching_count), newest first.
    """
    if not os.path.exists(log_path):
        return [], 0

    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if level_filter and level_filter != "ALL":
                    if entry.get("level") != level_filter:
                        continue
                entries.append(entry)
            except json.JSONDecodeError:
                pass

    entries.reverse()   # newest first
    total  = len(entries)
    start  = (page - 1) * PAGE_SIZE
    end    = start + PAGE_SIZE
    return entries[start:end], total


@logs_bp.route("/logs")
@logs_bp.route("/system/events")
def logs_view():
    config_path  = current_app.config["CONFIG_PATH"]
    log_path     = _get_log_path(config_path)
    page         = int(request.args.get("page", 1))
    level_filter = request.args.get("level", "ALL")

    entries, total = _read_log_page(log_path, page, level_filter)
    total_pages    = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template("logs.html",
        entries=entries,
        page=page,
        total_pages=total_pages,
        total=total,
        level_filter=level_filter,
        levels=["ALL", "ACTION", "ERROR", "DRY_RUN", "INFO", "DEBUG"],
    )


@logs_bp.route("/logs/data")
def logs_data():
    """JSON endpoint polled by auto-refresh."""
    config_path  = current_app.config["CONFIG_PATH"]
    log_path     = _get_log_path(config_path)
    level_filter = request.args.get("level", "ALL")
    entries, total = _read_log_page(log_path, 1, level_filter)
    return jsonify({"entries": entries, "total": total})


@logs_bp.route("/logs/clear", methods=["POST"])
def logs_clear():
    config_path = current_app.config["CONFIG_PATH"]
    log_path    = _get_log_path(config_path)
    if os.path.exists(log_path):
        with open(log_path, "w"):   # BUG-07: use context manager, not bare open()
            pass
    return redirect(url_for("logs.logs_view"))


def _get_log_path(config_path: str) -> str:
    try:
        from core.config import load_config
        config = load_config(config_path)
        return config.logging.log_file
    except Exception:
        return "./data/inspectarr.log.json"
