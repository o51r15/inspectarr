#!/usr/bin/env python3
"""
inspectarr — web UI entry point

Starts the Flask application and the background scheduler daemon.
The core (core/) is never modified by the UI layer.

Usage:
    python3 web.py
    python3 web.py --config /path/to/config.yaml

CLI runs (no UI) still use:
    python3 inspectarr.py --dry-run
    python3 inspectarr.py
"""
import argparse
import atexit
import os
import secrets
import sys
from datetime import datetime

from flask import Flask, Response
from ui.auth import check_auth, read_auth_block
from ui.scheduler import Scheduler
from ui.routes.dashboard import dashboard_bp
from ui.routes.config import config_bp
from ui.routes.logs import logs_bp
from ui.routes.scheduler import scheduler_bp
from ui.routes.indexers import indexers_bp
from ui.routes.torrents import torrents_bp
from ui.routes.stats import stats_bp
from ui.routes.system import system_bp
from ui.routes.webhooks import webhooks_bp
from ui.routes.llm_logs import llm_logs_bp
from ui.routes.api import api_bp
from ui.routes.quarantine import quarantine_bp
from ui.routes.activity import activity_bp
from ui.routes.notify import notify_bp


# The one inline script allowed by the CSP: the theme picker in base.html's
# <head>, which must run before first paint. tests/test_csp.py recomputes
# this hash from base.html, so editing that script without updating the hash
# fails the build instead of silently breaking theming.
THEME_SCRIPT_HASH = "sha256-PPoTuvlWA0XNVpQ6rJx1Ht/QDS3rc3k6k+EMCxYxZtY="
# Enforced since every template passed tests/test_templates_lint.py and an
# enforced sweep of every page logged zero violations. Escape hatch if a
# path was missed: set INSPECTARR_CSP=report-only on the container and the
# policy only reports (browser console) instead of blocking.
CSP_ENFORCE = os.environ.get("INSPECTARR_CSP", "enforce").strip().lower() != "report-only"
CSP = ("default-src 'self'; "
       f"script-src 'self' '{THEME_SCRIPT_HASH}'; "
       "style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
       "form-action 'self'; object-src 'none'")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="inspectarr web UI")
    p.add_argument("--config", default="config.yaml",
                   help="Path to config file (default: config.yaml)")
    return p.parse_args()


def create_app(config_path: str) -> Flask:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "ui", "templates"),
        static_folder=os.path.join(base_dir, "ui", "static"),
    )
    app.config["CONFIG_PATH"] = config_path
    # L-01: set SECRET_KEY for session signing (env override or random per-restart)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
    # SEC-8: cap request body size to 1 MB to prevent memory-spike attacks
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
    # L-02: security response headers
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Phase 1.13: report-only until every template is migrated off inline
        # style/script; then CSP_ENFORCE flips and violations are blocked.
        header = ("Content-Security-Policy" if CSP_ENFORCE
                  else "Content-Security-Policy-Report-Only")
        response.headers[header] = CSP
        return response

    app.config["SCHEDULER"]   = Scheduler(config_path)
    # IMP-2: share the scheduler's StateManager with all routes so requests
    # reuse one SQLite connection instead of opening one per request (BUG-09).
    # May be None if the config/DB was unavailable at startup — routes fall
    # back to a fresh instance in that case.
    app.config["STATE"] = app.config["SCHEDULER"]._state
    # Model calibration runs (Settings -> AI). Created here, started by
    # main() -- or by the first request that needs it -- never in tests.
    from core.calibration_runs import CalibrationRunner
    app.config["CAL_RUNNER"] = (CalibrationRunner(app.config["STATE"], config_path)
                                if app.config["STATE"] else None)
    # The old single prowlarr.ollama.system_prompt setting becomes a stored
    # prompt (or nothing, if it was the Default) and leaves config.yaml.
    if app.config["STATE"]:
        from core.scoring_prompts import migrate_config_prompt
        migrate_config_prompt(app.config["STATE"], config_path)
    # Old notification settings (urls + notify_on) become places to send to.
    from core.notify_targets import migrate_config as migrate_notify_targets
    migrate_notify_targets(config_path)
    # L-03: register explicit close so SQLite connection is cleaned up on exit
    if app.config["STATE"]:
        atexit.register(app.config["STATE"].close)

    def _read_operating_mode() -> str:
        """
        Current operating mode, for the persistent banner.

        Reads the file rather than the parsed config because Settings saves
        take effect on the next scan without a restart, and a banner that
        lagged a restart behind reality would be worse than no banner.

        Fails to "automatic" on any error: that is the state in which the
        banner is hidden, so a transient read failure shows nothing rather
        than flashing a false "Monitor" claim on every page.

        Reads through the config parse cache (ROADMAP item 14), so this is
        a dictionary lookup and a stat rather than the second full YAML parse
        per render it used to be.
        """
        try:
            from core.config import load_raw_config
            raw = load_raw_config(config_path) or {}
            mode = str((raw.get("remediation") or {}).get(
                "operating_mode") or "automatic").lower().strip()
            from core import severity as _sev
            return mode if _sev.is_valid_mode(mode) else "automatic"
        except Exception:
            return "automatic"

    app.config["READ_OPERATING_MODE"] = _read_operating_mode

    @app.context_processor
    def inject_globals():
        from core import __version__
        from flask import request
        from ui.nav import NAV, active_key, active_subnav
        auth = read_auth_block(config_path)
        state = app.config.get("STATE")
        held = state.count_quarantine_held() if state else 0
        tabs, tab_active = active_subnav(request.path)
        return {
            "auth_enabled": auth.get("enabled", False),
            "app_version": __version__,
            "operating_mode": _read_operating_mode(),
            "nav": NAV,
            "nav_active": active_key(request.path),
            "section_tabs": tabs,
            "section_tab_active": tab_active,
            "nav_counts": {"held": held},
            "scheduler_running": bool(app.config["SCHEDULER"].running),
        }

    @app.template_filter("datetimeformat")
    def datetimeformat(value):
        """Convert a Unix timestamp (int) to a readable date string."""
        try:
            if not value or int(value) <= 0:
                return "—"
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "—"

    @app.template_filter("duration")
    def duration(seconds):
        """Seconds -> '2 h 14 min' / '45 s'. Anything unusable -> '—'."""
        try:
            s = int(seconds)
        except (TypeError, ValueError):
            return "—"
        if s <= 0:
            return "—"
        d, rem = divmod(s, 86400)
        h, rem = divmod(rem, 3600)
        m, sec = divmod(rem, 60)
        if d:
            return f"{d} d {h} h"
        if h:
            return f"{h} h {m} min"
        if m:
            return f"{m} min"
        return f"{sec} s"

    @app.route("/logout")
    def logout():
        # A template rather than an inline <style> string, so the CSP's
        # style-src 'self' does not strip it once enforced.
        from flask import render_template
        return Response(
            render_template("logged_out.html"),
            401,
            {"WWW-Authenticate": 'Basic realm="Inspectarr"', "Content-Type": "text/html"},
        )

    @app.before_request
    def enforce_auth():
        return check_auth(config_path)

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(scheduler_bp)
    app.register_blueprint(indexers_bp)
    app.register_blueprint(torrents_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(llm_logs_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(quarantine_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(notify_bp)

    return app


def main():
    args = parse_args()

    config_path = os.environ.get("INSPECTARR_CONFIG", args.config)

    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        print("       Copy config.example.yaml to config.yaml and fill in your values.")
        sys.exit(1)

    # Read port from config — fall back to 8585 if config is broken
    port = 8585
    scheduler_autostart = False
    try:
        from core.config import load_config
        config = load_config(config_path)
        port   = config.web.port
        scheduler_autostart = config.web.scheduler_autostart
    except Exception as e:
        print(f"WARNING: Could not read config ({e}), using defaults")
        config = None

    # L-21: warn if auth is disabled
    if config and not config.web.auth.enabled:
        print("WARNING: Authentication is disabled — anyone with network access can control Inspectarr")

    # L-07: warn on default password
    if config and config.web.auth.enabled:
        pw = config.web.auth.password
        if pw == "changeme" or (not pw.startswith(("pbkdf2:", "scrypt:")) and pw == "changeme"):
            print("WARNING: Using default password 'changeme' — change it in Settings")

    app = create_app(config_path)

    print(f"inspectarr web UI → http://0.0.0.0:{port}")
    print(f"Config: {config_path}")

    # Phase 2.5: scheduler_autostart decides the first boot only. Once someone
    # presses Pause or Resume, their last choice is stored and wins, so a
    # restart neither silently stops protection nor silently resumes it.
    state = app.config.get("STATE")
    paused = None
    try:
        paused = state.get_app_state("scheduler_paused") if state else None
    except Exception as exc:
        print(f"WARNING: could not read saved pause state ({exc})")
    if paused == "1":
        print("Scheduler is PAUSED — it was paused from the UI before this restart. "
              "Resume it from Overview.")
    elif paused == "0" or (paused is None and scheduler_autostart):
        app.config["SCHEDULER"].start()
        why = ("resumed from the UI before this restart" if paused == "0"
               else "scheduler_autostart: true")
        print(f"Scheduler started ({why})")
    else:
        print("Scheduler is OFF at startup (scheduler_autostart: false) — "
              "start it from Overview.")

    # Calibration runs carry on after a restart, and new models are noticed.
    if app.config.get("CAL_RUNNER"):
        app.config["CAL_RUNNER"].start()

    # "Stopped" / "stopped unexpectedly" messages (core/lifecycle.py). The
    # start check sends over the network, so it runs beside startup, not in it.
    import signal
    import threading
    from core import lifecycle
    threading.Thread(target=lifecycle.on_start, args=(state, config_path),
                     name="inspectarr-start-check", daemon=True).start()

    def _stop(signum, _frame):
        why = {getattr(signal, "SIGTERM", None): "it was asked to stop",
               getattr(signal, "SIGINT", None): "stopped from the keyboard"}.get(signum, "")
        print(f"Stopping ({why})…")
        lifecycle.on_stop(state, config_path, why)
        raise SystemExit(0)

    for sig in ("SIGTERM", "SIGINT"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _stop)

    # Use waitress (production WSGI) if available, fall back to Flask dev server
    try:
        from waitress import serve
        print("Server: waitress (production)")
        serve(app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        print("WARNING: waitress not installed — using Flask dev server (not recommended for production)")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
