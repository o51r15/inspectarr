"""
ui/routes/webhooks.py — Webhook endpoints for event-driven scanning

POST /webhook/sonarr and /webhook/radarr receive grab/download events
from *arr apps. After a configurable delay (to let qBit connect to the
swarm and start downloading), a single-torrent scan is triggered.

Auth via shared secret in the X-Webhook-Secret header only (H-02).
Can run alongside polling or as the sole scan trigger.
"""
import logging
import threading
import hmac

from flask import Blueprint, request, jsonify, current_app

webhooks_bp = Blueprint("webhooks", __name__)
log = logging.getLogger("inspectarr")


def _check_secret(config_path: str) -> bool:
    """Validate the webhook secret. Returns True if valid or no secret configured."""
    from core.config import load_config
    cfg = load_config(config_path)
    secret = cfg.scanning.webhooks.secret
    if not secret:
        return True  # no secret configured = open (user's choice)
    provided = request.headers.get("X-Webhook-Secret", "")
    if not provided:
        log.warning("Webhook secret missing — rejected (use X-Webhook-Secret header)")
        return False
    return hmac.compare_digest(secret, provided)


def _delayed_scan(config_path: str, delay: int, event_data: dict):
    """Wait `delay` seconds, then trigger a scan."""
    import time
    source = event_data.get("source", "webhook")
    log.info(f"Webhook scan scheduled — waiting {delay}s for torrent to initialize ({source})")
    time.sleep(delay)

    scheduler = current_app.config.get("SCHEDULER") if current_app else None
    if scheduler:
        ok = scheduler.trigger()
        if ok:
            log.info(f"Webhook-triggered scan started ({source})")
        else:
            log.info(f"Webhook scan skipped — another scan already in progress ({source})")
    else:
        # Fallback: run scanner directly
        try:
            from core.config import load_config
            from core.scanner import Scanner
            cfg = load_config(config_path)
            scanner = Scanner(cfg)
            scanner.prepare()
            scanner.run_scan()
            log.info(f"Webhook-triggered scan completed ({source})")
        except Exception as exc:
            log.error(f"Webhook scan failed: {exc}")


@webhooks_bp.route("/webhook/sonarr", methods=["POST"])
def webhook_sonarr():
    return _handle_webhook("sonarr")


@webhooks_bp.route("/webhook/radarr", methods=["POST"])
def webhook_radarr():
    return _handle_webhook("radarr")


@webhooks_bp.route("/webhook/lidarr", methods=["POST"])
def webhook_lidarr():
    return _handle_webhook("lidarr")


def _handle_webhook(source: str):
    """Common handler for all *arr webhook endpoints."""
    config_path = current_app.config["CONFIG_PATH"]

    # Check if webhooks are enabled
    try:
        from core.config import load_config
        cfg = load_config(config_path)
        if not cfg.scanning.webhooks.enabled:
            return jsonify({"ok": False, "message": "Webhooks are not enabled"}), 403
        delay = cfg.scanning.webhooks.scan_delay_seconds
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500

    # Validate secret
    if not _check_secret(config_path):
        return jsonify({"ok": False, "message": "Invalid webhook secret"}), 401

    body = request.get_json(silent=True) or {}
    event_type = body.get("eventType", "unknown")
    log.info(f"Webhook received from {source}: eventType={event_type}")

    # Only trigger scan on grab/download events (not test/rename/etc)
    scan_events = {"Grab", "Download", "Test"}
    if event_type not in scan_events:
        return jsonify({"ok": True, "message": f"Event '{event_type}' ignored — no scan triggered"})

    if event_type == "Test":
        return jsonify({"ok": True, "message": "Webhook test successful"})

    # Fire delayed scan in background thread
    # We need app context for the scheduler reference
    app = current_app._get_current_object()
    def run_with_context():
        with app.app_context():
            _delayed_scan(config_path, delay, {"source": source, "event": event_type})

    t = threading.Thread(target=run_with_context, daemon=True, name=f"webhook-{source}")
    t.start()

    return jsonify({
        "ok": True,
        "message": f"Scan scheduled in {delay}s",
        "delay_seconds": delay,
    })
