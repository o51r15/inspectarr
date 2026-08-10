"""
ui/routes/torrents.py — Torrent management page

Provides a quick-look dashboard of all qBittorrent torrents with controls
for category changes, pause/resume, and delete. Also a per-torrent detail
view with tracker and file information.
"""
import re

from flask import Blueprint, render_template, request, jsonify, current_app, abort
from core.config import load_config
from core.torrent_client import build_torrent_client, AbstractTorrentClient, TorrentClientError

_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")

torrents_bp = Blueprint("torrents", __name__)


def _client_from_config() -> AbstractTorrentClient:
    config = load_config(current_app.config["CONFIG_PATH"])
    return build_torrent_client(config)


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

PAGE_SIZE = 50


@torrents_bp.route("/torrents")
def torrents():
    error = None
    torrent_list = []
    categories = []
    try:
        client = _client_from_config()
        torrent_list = client.get_all_torrents()
        cat_map = client.get_categories()
        categories = sorted(
            name for name in cat_map.keys() if name
        )
    except Exception as exc:
        error = str(exc)

    # Server-side pagination
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    total = len(torrent_list)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * PAGE_SIZE
    paged = torrent_list[start:start + PAGE_SIZE]

    return render_template(
        "torrents.html",
        torrents=paged,
        categories=categories,
        error=error,
        page=page,
        total_pages=total_pages,
        total=total,
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
        client = _client_from_config()
        matches = client.get_all_torrents(hash=hash)
        if not matches:
            return render_template("torrent_detail.html", error="Torrent not found.", torrent=None)
        torrent = matches[0]
        properties = client.get_torrent_properties(hash)
        trackers = client.get_torrent_trackers(hash)
        files = client.get_torrent_files(hash)
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
        client = _client_from_config()
        ok = client.set_torrent_category(hash, category)
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
        client = _client_from_config()
        ok = client.pause_torrent(hash)
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
        client = _client_from_config()
        ok = client.resume_torrent(hash)
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
        client = _client_from_config()
        ok = client.delete_torrent(hash, delete_files=True)
        return jsonify({"ok": ok, "msg": "Deleted" if ok else "Delete failed"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
