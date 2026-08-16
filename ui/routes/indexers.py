from flask import Blueprint, render_template, current_app, jsonify, request

indexers_bp = Blueprint("indexers", __name__)


@indexers_bp.route("/indexers", methods=["GET"])
@indexers_bp.route("/indexers/<tab>", methods=["GET"])
def indexers_view(tab=None):
    """Indexer hub page with tabs: Health, Stats, AI Scoring."""
    config_path = current_app.config["CONFIG_PATH"]
    enabled = False
    stats_rows = []
    stats_error = None
    llm_runs = []
    llm_error = None
    active_tab = tab or request.args.get("tab", "health")

    try:
        from core.config import load_config
        cfg = load_config(config_path)
        enabled = cfg.prowlarr.enabled

        if enabled:
            # --- Stats data ---
            try:
                from .stats import _get_state as stats_get_state, _build_stats_rows
                state = stats_get_state(cfg)
                stats_rows = _build_stats_rows(cfg, state)
            except Exception as exc:
                stats_error = str(exc)

            # --- LLM Logs data ---
            try:
                state = current_app.config.get("STATE")
                if state:
                    llm_runs = state.get_llm_scoring_runs(limit=50)
            except Exception as exc:
                llm_error = str(exc)

    except Exception:
        enabled = False

    return render_template(
        "indexers.html",
        prowlarr_enabled=enabled,
        active_tab=active_tab,
        stats_rows=stats_rows,
        stats_error=stats_error,
        llm_runs=llm_runs,
        llm_error=llm_error,
    )


@indexers_bp.route("/indexers/rescore-reorder", methods=["POST"])
def indexers_rescore_reorder():
    """Rescore all indexers and reorder in Prowlarr (no sync to apps)."""
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
        state    = _get_state(cfg)
        scorer   = IndexerScorer(prowlarr, state, cfg.prowlarr)
        scorer.score_all(skip_ai=False)
        changed  = scorer.reorder()
        return jsonify({
            "ok": True, "changed": changed,
            "msg": f"{changed} indexer(s) rescored and reordered.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/sync-only", methods=["POST"])
def indexers_sync_only():
    """Sync current Prowlarr indexer order to all connected apps."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        synced = prowlarr.sync_to_apps()
        return jsonify({
            "ok": True, "synced": synced,
            "msg": "Sync dispatched to connected apps." if synced
                   else "Sync to apps failed — check Prowlarr logs.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/sync", methods=["POST"])
def indexers_sync():
    """Legacy: Reorder + sync combined (kept for backward compat)."""
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
        state    = _get_state(cfg)
        scorer   = IndexerScorer(prowlarr, state, cfg.prowlarr)
        changed  = scorer.reorder()
        synced   = prowlarr.sync_to_apps()
        return jsonify({
            "ok": True, "changed": changed, "synced": synced,
            "msg": (f"{changed} indexer(s) reordered, sync dispatched."
                    if synced else
                    f"{changed} indexer(s) reordered, sync failed."),
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/apply-priorities", methods=["POST"])
def indexers_apply_priorities():
    """Apply manual priority changes from the UI table."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient

        data = request.get_json(silent=True) or {}
        changes = data.get("changes", {})
        if not changes:
            return jsonify({"ok": False, "msg": "No changes to apply"}), 400

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400

        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        all_indexers = prowlarr.get_torrent_indexers(include_disabled=True)
        idx_map = {i["id"]: i for i in all_indexers}

        applied = 0
        for iid_str, new_prio in changes.items():
            iid = int(iid_str)
            raw = idx_map.get(iid)
            if raw and prowlarr.set_indexer_priority(raw, int(new_prio)):
                applied += 1

        return jsonify({
            "ok": True, "applied": applied,
            "msg": f"{applied} priority change(s) applied.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/history", methods=["GET"])
def indexers_history():
    """Return score history for the health analytics chart."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from .config import _get_state

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400

        state = _get_state(cfg)
        rows = state.get_score_history_all()
        datasets = {}
        for r in rows:
            name = r["indexer_name"]
            if name not in datasets:
                datasets[name] = []
            datasets[name].append({"x": r["scored_at"], "y": r["health_score"]})
        return jsonify({"ok": True, "datasets": datasets})
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

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr is not enabled"}), 400

        state = _get_state(cfg)
        state.reset_indexer_stats(int(indexer_id))
        return jsonify({"ok": True, "msg": f"Stats reset for indexer {indexer_id}"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/scoring-status", methods=["GET"])
def indexers_scoring_status():
    """Return the scoring/reorder schedule status."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from .config import _get_state
        from datetime import datetime, timezone, timedelta

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr not enabled"}), 400

        state = _get_state(cfg)
        scheduler = current_app.config.get("SCHEDULER")
        interval_h = cfg.prowlarr.reorder_interval_hours

        last_reorder_iso = state.get_app_state("last_prowlarr_reorder")
        last_reorder = None
        next_reorder = None
        if last_reorder_iso:
            try:
                last_reorder = last_reorder_iso
                last_dt = datetime.fromisoformat(last_reorder_iso)
                next_dt = last_dt + timedelta(hours=interval_h)
                next_reorder = next_dt.isoformat()
            except ValueError:
                pass

        scheduler_running = scheduler.running if scheduler else False
        return jsonify({
            "ok": True,
            "scheduler_running": scheduler_running,
            "interval_hours": interval_h,
            "last_reorder": last_reorder,
            "next_reorder": next_reorder,
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@indexers_bp.route("/indexers/trigger-scoring", methods=["POST"])
def indexers_trigger_scoring():
    """Manually trigger a full score + reorder + sync cycle."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        from .config import _get_state
        from datetime import datetime, timezone

        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "msg": "Prowlarr not enabled"}), 400

        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state = _get_state(cfg)
        scorer = IndexerScorer(prowlarr, state, cfg.prowlarr)
        scored = scorer.score_all(skip_ai=False)
        changed = scorer.reorder()
        synced = prowlarr.sync_to_apps()

        # Update the last reorder timestamp
        now = datetime.now(timezone.utc)
        state.set_app_state("last_prowlarr_reorder", now.isoformat())
        scheduler = current_app.config.get("SCHEDULER")
        if scheduler:
            scheduler.last_reorder = now

        return jsonify({
            "ok": True, "changed": changed, "synced": synced,
            "msg": f"Scored, {changed} reordered, sync {'sent' if synced else 'failed'}.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
