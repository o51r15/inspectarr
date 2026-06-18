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


@indexers_bp.route("/indexers/sync", methods=["POST"])
def indexers_sync():
    """
    Reorder indexers in Prowlarr by health score, then trigger
    ApplicationIndexerSync to push the updated order to all connected apps
    (Sonarr, Radarr, Whisparr, etc.).
    """
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        from core.state import StateManager

        cfg      = load_config(config_path)
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = StateManager(
            db_path=cfg.state.db_file,
            log_path=cfg.logging.log_file,
            retention_days=cfg.logging.retention_days,
        )
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        changed = scorer.reorder()

        synced = prowlarr.sync_to_apps()

        return jsonify({
            "ok":      True,
            "changed": changed,
            "synced":  synced,
            "msg": (
                f"{changed} indexer(s) reordered, sync dispatched to connected apps."
                if synced else
                f"{changed} indexer(s) reordered, but sync to apps failed — check Prowlarr logs."
            ),
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
