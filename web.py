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
import os
import sys

from flask import Flask
from ui.scheduler import Scheduler
from ui.routes.dashboard import dashboard_bp
from ui.routes.config import config_bp
from ui.routes.logs import logs_bp
from ui.routes.scheduler import scheduler_bp


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
    app.config["SCHEDULER"]   = Scheduler(config_path)

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(scheduler_bp)

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

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
