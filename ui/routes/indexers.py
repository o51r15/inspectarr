from flask import Blueprint, render_template, current_app, jsonify, request

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
        from .config import _get_state

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = _get_state(cfg)   # IMP-2: shared app-wide StateManager
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


@indexers_bp.route("/indexers/reset", methods=["POST"])
def indexers_reset():
    """Reset grab count, malicious hits, and cached scores for one indexer."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from .config import _get_state

        data = request.get_json(silent=True) or {}
        indexer_id = data.get("indexer_id")
        if indexer_id is None:
            return jsonify({"ok": False, "msg": "indexer_id is required"}), 400

        cfg   = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400

        state = _get_state(cfg)
        state.reset_indexer_stats(int(indexer_id))
        return jsonify({"ok": True, "msg": f"Stats reset for indexer {indexer_id}"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
