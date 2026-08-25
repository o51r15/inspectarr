"""
ui/routes/quarantine.py — the review queue.

A held torrent is paused and waiting on a person. This page exists so that
decision can be made; without it quarantine would be a trap that silently
accumulates paused torrents with no way out short of SQL.

Three outcomes, deliberately distinct:
  release   — resume it, this was a false positive
  remediate — blocklist in the arr and delete, the hold confirmed the catch
  keep      — leave it paused but stop asking
"""
import logging

from flask import Blueprint, render_template, current_app, request, jsonify

from ui.routes._utils import safe_error

log = logging.getLogger("inspectarr")

quarantine_bp = Blueprint("quarantine", __name__)


def _clients():
    """Config plus a torrent client, or (None, None, error) on failure."""
    from core.config import load_config
    from core.torrent_client import build_torrent_client
    cfg = load_config(current_app.config["CONFIG_PATH"])
    return cfg, build_torrent_client(cfg)


@quarantine_bp.route("/quarantine")
def quarantine_page():
    state = current_app.config.get("STATE")
    held = state.get_quarantine("held") if state else []
    recent = [q for q in (state.get_quarantine(None, limit=50) if state else [])
              if q.get("status") != "held"][:25]
    return render_template("quarantine.html", held=held, recent=recent)


@quarantine_bp.route("/quarantine/data")
def quarantine_data():
    state = current_app.config.get("STATE")
    if not state:
        return jsonify({"ok": False, "held": []})
    return jsonify({"ok": True, "held": state.get_quarantine("held")})


def _still_in_client(client, hash_) -> bool | None:
    """
    Is this torrent still known to the torrent client?

    Needed because the clients do not reliably report a miss: qBittorrent
    answers 200 OK to a resume for a hash it has never heard of, so
    resume_torrent() returns True and a hold would be resolved as "released"
    for a torrent that is simply gone. Checking first turns that silent lie
    into an accurate outcome.

    Returns None if the question cannot be answered -- callers then proceed
    rather than blocking a decision on a failed lookup.
    """
    try:
        found = client.get_all_torrents(hash_)
        return bool(found)
    except Exception as exc:
        log.debug(f"Could not confirm {hash_} in client: {exc}")
        return None


@quarantine_bp.route("/quarantine/action", methods=["POST"])
def quarantine_action():
    """
    Resolve a hold.

    Each branch resolves the row only after the underlying operation
    succeeded. Marking a hold "released" when the resume actually failed
    would drop the torrent out of the queue while leaving it paused forever
    — the one outcome worse than asking twice.
    """
    data = request.get_json(silent=True) or {}
    hash_ = (data.get("hash") or "").strip()
    action = (data.get("action") or "").strip().lower()
    if not hash_ or action not in ("release", "remediate", "keep"):
        return jsonify({"ok": False, "message": "Bad request"}), 400

    state = current_app.config.get("STATE")
    if not state:
        return jsonify({"ok": False, "message": "State unavailable"}), 503
    entry = state.get_quarantine_entry(hash_)
    if not entry or entry.get("status") != "held":
        return jsonify({"ok": False, "message": "Not currently held"}), 404

    name = entry.get("torrent_name") or hash_
    try:
        if action == "keep":
            state.resolve_quarantine(hash_, "kept",
                                     "kept paused by user decision")
            state.write_log({
                "level": "ACTION", "event": "quarantine_kept",
                "inspection_id": entry.get("inspection_id"),
                "torrent_name": name, "hash": hash_,
            })
            return jsonify({"ok": True, "message": f"{name} left paused"})

        cfg, client = _clients()

        if action == "release":
            present = _still_in_client(client, hash_)
            if present is False:
                # Gone from the client entirely. Resolving as "released"
                # would claim we resumed something that no longer exists.
                state.resolve_quarantine(
                    hash_, "released",
                    "torrent no longer present in the client")
                state.write_log({
                    "level": "WARNING", "event": "quarantine_vanished",
                    "inspection_id": entry.get("inspection_id"),
                    "torrent_name": name, "hash": hash_,
                })
                return jsonify({
                    "ok": True,
                    "message": f"{name} is no longer in the torrent client — "
                               f"hold cleared",
                })
            resumed = False
            try:
                resumed = bool(client.resume_torrent(hash_))
            except Exception as exc:
                return jsonify({"ok": False,
                                "message": f"Could not resume: {safe_error(exc)}"}), 502
            if not resumed:
                return jsonify({"ok": False,
                                "message": "Torrent client refused to resume — "
                                           "still held"}), 502
            state.resolve_quarantine(hash_, "released",
                                     "released by user; resumed")
            state.write_log({
                "level": "ACTION", "event": "quarantine_released",
                "inspection_id": entry.get("inspection_id"),
                "torrent_name": name, "hash": hash_,
            })
            return jsonify({"ok": True, "message": f"{name} released"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500

    # action == "remediate": confirm the catch — blocklist, then delete.
    try:
        cfg, client = _clients()
        present = _still_in_client(client, hash_)
        if present is False:
            state.resolve_quarantine(
                hash_, "remediated",
                "torrent already absent from the client")
            return jsonify({
                "ok": True,
                "message": f"{name} was already gone from the torrent client — "
                           f"hold cleared",
            })
        from core.scanner import _build_arr_client
        arr_ok = False
        app_name = entry.get("arr_app") or "sonarr"
        try:
            arr_ok = bool(_build_arr_client(app_name, cfg).blocklist(hash_))
        except Exception as exc:
            log.warning(f"Quarantine remediate: arr blocklist failed: {exc}")

        deleted = False
        try:
            deleted = bool(client.delete_torrent(hash_, delete_files=True))
        except Exception as exc:
            return jsonify({
                "ok": False,
                "message": f"Blocklist {'ok' if arr_ok else 'failed'}, but "
                           f"delete failed: {safe_error(exc)} — still held",
            }), 502
        if not deleted:
            return jsonify({
                "ok": False,
                "message": "Torrent client refused to delete — still held",
            }), 502

        # Only now is the hold genuinely resolved.
        state.record_action(hash_, name, entry.get("category"),
                            entry.get("rule_name"), "deleted", arr_ok, True)
        state.resolve_quarantine(
            hash_, "remediated",
            f"remediated by user (blocklist {'ok' if arr_ok else 'failed'})")
        state.write_log({
            "level": "ACTION", "event": "quarantine_remediated",
            "inspection_id": entry.get("inspection_id"),
            "torrent_name": name, "hash": hash_,
            "category": entry.get("category"), "rule": entry.get("rule_name"),
            "arr": app_name, "arr_blocklisted": arr_ok, "qbit_deleted": True,
            "bad_files": entry.get("bad_files") or [],
        })
        # Indexer attribution and the replacement watch. Missing here until
        # now, which mattered because in `operating_mode: quarantine` every
        # remediation goes through this button -- so the Indexers tab showed
        # nothing, indistinguishable from having found no bad releases.
        #
        # Deliberately NOT gated on the operating mode: a person deciding is
        # the point of this queue, and capping it would make quarantine mode
        # a queue you cannot empty.
        try:
            from core.scanner import Scanner
            Scanner(cfg, state).record_rejection(hash_, name, app_name)
        except Exception as exc:
            # Bookkeeping must never fail a deletion that already happened.
            log.warning(f"Quarantine remediate: bookkeeping failed: {exc}")

        msg = f"{name} deleted"
        if not arr_ok:
            msg += " (arr blocklist failed — check the Events log)"
        return jsonify({"ok": True, "message": msg})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500
