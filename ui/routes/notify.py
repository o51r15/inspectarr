"""
ui/routes/notify.py -- Settings -> Notifications: the places to send to.

Each place is saved straight to config.yaml (notifications.apprise.targets)
by these endpoints, not by the Settings form. Secret fields never go back to
the browser: a place is shown by a safe description (notify_targets.describe).
"""
import logging
import threading

from flask import Blueprint, Response, current_app, jsonify, request

from core import config_store
from core import notify_targets as nt

log = logging.getLogger("inspectarr")
notify_bp = Blueprint("notify", __name__)

TEST_TITLE = "Inspectarr test message"
TEST_BODY = ("If you can read this, Inspectarr can reach you here. You'll get a message "
             "when it catches a bad release.")
_write_lock = threading.Lock()          # one config write at a time from this page


def _config_path() -> str:
    return current_app.config["CONFIG_PATH"]


def _state():
    return current_app.config.get("STATE")


def _err(message: str, code: int = 400):
    return jsonify({"ok": False, "message": message}), code


def _apprise_node(doc):
    """notifications.apprise in the loaded document, created if missing, with
    an old-shape config converted first so there is only one way to read it."""
    data = doc.data
    if data.get("notifications") is None:
        data["notifications"] = {}
    if data["notifications"].get("apprise") is None:
        data["notifications"]["apprise"] = {}
    app = data["notifications"]["apprise"]
    if "targets" not in app:
        nt.convert_legacy_node(data["notifications"])
    if app["targets"] is None:
        app["targets"] = []
    return app


def _view(t: dict, status: dict) -> dict:
    s = status.get(int(t["id"])) or {}
    return {"id": int(t["id"]), "service": t["service"],
            "service_label": nt.SERVICES.get(t["service"], {}).get("label", t["service"]),
            "description": nt.describe(t["service"], t["fields"]),
            "events": {k: k in (t.get("events") or []) for k in nt.EVENT_KEYS},
            "enabled": bool(t.get("enabled", True)), "usable": bool(t.get("url")),
            "last_ok": s.get("last_ok"), "last_error": s.get("last_error"),
            "subscribe": nt.subscribe_link(t["service"], t["fields"])}


def _targets():
    from core.config import load_raw_config
    raw = load_raw_config(_config_path()) or {}
    notif = raw.get("notifications") or {}
    return nt.parse_targets(notif.get("apprise") or {}, bool((notif.get("summary") or {}).get("enabled")))


def _checked_url(service: str, fields: dict):
    """(url, None) or (None, error message)."""
    if service not in nt.SERVICES:
        return None, "Pick one of the listed services."
    try:
        url = nt.build_url(service, fields)
    except nt.NotifyError as exc:
        return None, str(exc)
    if not nt.valid_url(url):
        return None, "Inspectarr couldn't use those details. Check them and try again."
    return url, None


def _clean_fields(service: str, fields) -> dict:
    allowed = {f["key"] for f in nt.SERVICES.get(service, {}).get("fields", [])}
    return {k: v.strip() if isinstance(v, str) else v
            for k, v in (fields or {}).items() if k in allowed and v not in (None, "")}


@notify_bp.route("/config/notify", methods=["GET"])
def notify_list():
    state = _state()
    status = state.get_notify_status() if state else {}
    return jsonify({"ok": True, "targets": [_view(t, status) for t in _targets()],
                    "events": [{"key": k, "label": label} for k, label in nt.EVENTS],
                    "default_events": nt.DEFAULT_EVENTS,
                    "services": [{"id": sid, "label": s["label"], "blurb": s["blurb"],
                                  "fields": [{k: v for k, v in f.items() if k != "secret"} | {
                                      "type": f.get("type") or ("password" if f.get("secret") else "text")}
                                             for f in s["fields"]]}
                                 for sid, s in nt.SERVICES.items()]})


@notify_bp.route("/config/notify/targets", methods=["POST"])
def notify_add():
    body = request.get_json(silent=True) or {}
    service = str(body.get("service") or "")
    fields = _clean_fields(service, body.get("fields"))
    url, problem = _checked_url(service, fields)
    if problem:
        return _err(problem)
    events = [e for e in (body.get("events") or []) if e in nt.EVENT_KEYS]
    with _write_lock:
        doc = config_store.load_document(_config_path())
        app = _apprise_node(doc)
        new_id = max([int(t.get("id") or 0) for t in app["targets"]] + [0]) + 1
        app["targets"].append({"id": new_id, "service": service, "fields": fields,
                               "events": events, "enabled": True})
        config_store.save_document(_config_path(), doc)
    log.info(f"Notifications: added {nt.describe(service, fields)}")
    return jsonify({"ok": True, "id": new_id})


@notify_bp.route("/config/notify/targets/<int:target_id>", methods=["POST"])
def notify_update(target_id):
    """Change what a place is sent ({events: {key: bool}}) or pause it ({enabled})."""
    body = request.get_json(silent=True) or {}
    with _write_lock:
        doc = config_store.load_document(_config_path())
        app = _apprise_node(doc)
        target = next((t for t in app["targets"] if int(t.get("id") or 0) == target_id), None)
        if target is None:
            return _err("That place to send to isn't known any more.", 404)
        if isinstance(body.get("events"), dict):
            current = [e for e in (target.get("events") or []) if e in nt.EVENT_KEYS]
            for key, on in body["events"].items():
                if key not in nt.EVENT_KEYS:
                    continue
                if on and key not in current:
                    current.append(key)
                elif not on and key in current:
                    current.remove(key)
            target["events"] = [k for k in nt.EVENT_KEYS if k in current]
        if "enabled" in body:
            target["enabled"] = bool(body["enabled"])
        config_store.save_document(_config_path(), doc)
    return jsonify({"ok": True})


@notify_bp.route("/config/notify/targets/<int:target_id>/delete", methods=["POST"])
def notify_delete(target_id):
    with _write_lock:
        doc = config_store.load_document(_config_path())
        app = _apprise_node(doc)
        keep = [t for t in app["targets"] if int(t.get("id") or 0) != target_id]
        if len(keep) == len(app["targets"]):
            return _err("That place to send to isn't known any more.", 404)
        app["targets"] = keep
        config_store.save_document(_config_path(), doc)
    if _state():
        _state().clear_notify_status(target_id)
    return jsonify({"ok": True})


@notify_bp.route("/config/notify/targets/<int:target_id>/test", methods=["POST"])
def notify_test_saved(target_id):
    target = next((t for t in _targets() if int(t["id"]) == target_id), None)
    if target is None:
        return _err("That place to send to isn't known any more.", 404)
    if not target.get("url"):
        return _err("This place's details are incomplete. Remove it and add it again.")
    ok, problem = nt.send_now(target["url"], TEST_TITLE, TEST_BODY)
    if _state():
        _state().set_notify_status(target_id, ok, problem)
    return jsonify({"ok": ok, "problem": problem})


@notify_bp.route("/config/notify/test", methods=["POST"])
def notify_test_unsaved():
    """The Send test button while adding a place (nothing saved yet)."""
    body = request.get_json(silent=True) or {}
    service = str(body.get("service") or "")
    url, problem = _checked_url(service, _clean_fields(service, body.get("fields")))
    if problem:
        return jsonify({"ok": False, "problem": problem})
    ok, problem = nt.send_now(url, TEST_TITLE, TEST_BODY)
    return jsonify({"ok": ok, "problem": problem})


@notify_bp.route("/config/notify/ntfy-topic", methods=["GET"])
def notify_ntfy_topic():
    return jsonify({"ok": True, "topic": nt.new_ntfy_topic()})


@notify_bp.route("/config/notify/qr", methods=["GET"])
def notify_qr():
    text = request.args.get("text", "")
    if not text.startswith(("http://", "https://")) or len(text) > 300:
        return _err("Only links can be turned into a QR code.")
    return Response(nt.qr_svg(text), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})
