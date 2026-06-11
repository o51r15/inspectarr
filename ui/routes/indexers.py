from flask import Blueprint, render_template, current_app, jsonify

indexers_bp = Blueprint("indexers", __name__)


@indexers_bp.route("/indexers", methods=["GET"])
def indexers_view():
    """Standalone Indexer Health page (Prowlarr scoring + reorder)."""
    config_path = current_app.config["CONFIG_PATH"]
    enabled = False
    try:
        from core.config import load_config
        cfg = load_config(config_path)
        enabled = cfg.prowlarr.enabled
    except Exception:
        enabled = False
    return render_template("indexers.html", prowlarr_enabled=enabled)
