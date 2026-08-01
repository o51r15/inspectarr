"""
ui/routes/torrents.py — Torrent management page

Provides a quick-look dashboard of all qBittorrent torrents with controls
for category changes, pause/resume, and delete. Also a per-torrent detail
view with tracker and file information.
"""
import re

from flask import Blueprint, render_template, request, jsonify, current_app, abort
from core.config import load_config
from core.qbit import QBittorrentClient, QBittorrentError

_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")

torrents_bp = Blueprint("torrents", __name__)


def _qbit_from_config() -> QBittorrentClient:
    config = load_config(current_app.config["CONFIG_PATH"])
    return QBittorrentClient(
        config.qbittorrent.url,
        config.qbittorrent.username,
        config.qbittorrent.password,
    )


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

@torrents_bp.route("/torrents")
def torrents():
    error = None
    torrent_list = []
    categories = []
    try:
        qbit = _qbit_from_config()
        torrent_list = qbit.get_all_torrents()
        cat_map = qbit.get_categories()
        # Extract category names, skip the empty-string uncategorized key
        categories = sorted(
            name for name in cat_map.keys() if name
        )
    except Exception as exc:
        error = str(exc)
    return render_template(
        "torrents.html",
        torrents=torrent_list,
        categories=categories,
        error=error,
    )


@torrents_bp.route("/torrents/<hash>")
def torrent_detail(hash: str):
    # SEC-7: validate hash is a 40-hex-char infohash before passing to qBit
    if not _HASH_RE.match(hash):
        abort(400, description="Invalid torrent hash format")
    error = None
    torrent = None
    properties = {}
    trackers = []
    files = []
    try:
        qbit = _qbit_from_config()
        matches = qbit.get_all_torrents(hash=hash)
        if not matches:
            return render_template("torrent_detail.html", error="Torrent not found.", torrent=None)
        torrent = matches[0]
        properties = qbit.get_torrent_properties(hash)
        trackers = qbit.get_torrent_trackers(hash)
        files = qbit.get_torrent_files(hash)
        # Sort files by name for consistent display
        files.sort(key=lambda f: f.get("name", ""))
        # Filter out tier-separator tracker entries (url starts with ** or is blank)
        trackers = [t for t in trackers if t.get("url", "").startswith("http")]
    except Exception as exc:
        error = str(exc)
    return render_template(
        "torrent_detail.html",
        torrent=torrent,
        properties=properties,
        trackers=trackers,
        files=files,
        error=error,
    )


# ------------------------------------------------------------------
# AJAX actions
# ------------------------------------------------------------------

@torrents_bp.route("/torrents/set-category", methods=["POST"])
def set_category():
    data = request.get_json(silent=True) or {}
    hash = data.get("hash", "").strip()
    category = data.get("category", "")   # empty string = uncategorize
    if not hash or not _HASH_RE.match(hash):
        return jsonify({"ok": False, "msg": "Invalid torrent hash"}), 400
    try:
        qbit = _qbit_from_config()
        ok = qbit.set_torrent_category(hash, category)
        return jsonify({"ok": ok, "msg": "Category updated" if ok else "qBittorrent rejected the request"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@torrents_bp.route("/torrents/pause", methods=["POST"])
def pause():
    data = request.get_json(silent=True) or {}
    hash = data.get("hash", "").strip()
    if not hash or not _HASH_RE.match(hash):
        return jsonify({"ok": False, "msg": "Invalid torrent hash"}), 400
    try:
        qbit = _qbit_from_config()
        ok = qbit.pause_torrent(hash)
        return jsonify({"ok": ok, "msg": "Paused" if ok else "Pause failed"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@torrents_bp.route("/torrents/resume", methods=["POST"])
def resume():
    data = request.get_json(silent=True) or {}
    hash = data.get("hash", "").strip()
    if not hash or not _HASH_RE.match(hash):
        return jsonify({"ok": False, "msg": "Invalid torrent hash"}), 400
    try:
        qbit = _qbit_from_config()
        ok = qbit.resume_torrent(hash)
        return jsonify({"ok": ok, "msg": "Resumed" if ok else "Resume failed"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@torrents_bp.route("/torrents/delete", methods=["POST"])
def delete():
    data = request.get_json(silent=True) or {}
    hash = data.get("hash", "").strip()
    if not hash or not _HASH_RE.match(hash):
        return jsonify({"ok": False, "msg": "Invalid torrent hash"}), 400
    try:
        qbit = _qbit_from_config()
        ok = qbit.delete_torrent(hash, delete_files=True)
        return jsonify({"ok": ok, "msg": "Deleted" if ok else "Delete failed"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
