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
        return response

    app.config["SCHEDULER"]   = Scheduler(config_path)
    # IMP-2: share the scheduler's StateManager with all routes so requests
    # reuse one SQLite connection instead of opening one per request (BUG-09).
    # May be None if the config/DB was unavailable at startup — routes fall
    # back to a fresh instance in that case.
    app.config["STATE"] = app.config["SCHEDULER"]._state
    # L-03: register explicit close so SQLite connection is cleaned up on exit
    if app.config["STATE"]:
        atexit.register(app.config["STATE"].close)

    @app.context_processor
    def inject_globals():
        from core import __version__
        auth = read_auth_block(config_path)
        return {
            "auth_enabled": auth.get("enabled", False),
            "app_version": __version__,
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

    @app.route("/logout")
    def logout():
        return Response(
            """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Logged out — inspectarr</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#0f1117;color:#e2e8f0}
.box{text-align:center;padding:40px}.box a{color:#60a5fa;text-decoration:none}
.box a:hover{text-decoration:underline}</style></head>
<body><div class="box"><h2>Logged out</h2><p><a href="/">Log back in</a></p></div></body></html>""",
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

    if scheduler_autostart:
        app.config["SCHEDULER"].start()
        print(f"inspectarr web UI → http://0.0.0.0:{port}")
        print(f"Config: {config_path}")
        print("Scheduler started automatically (scheduler_autostart: true)")
    else:
        print(f"inspectarr web UI → http://0.0.0.0:{port}")
        print(f"Config: {config_path}")
        print("Scheduler is OFF at startup — enable it from the dashboard.")

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
