"""
ui/routes/stats.py — Indexer grab and malicious hit statistics
"""
from flask import Blueprint, render_template, current_app
from core.config import load_config

stats_bp = Blueprint("stats", __name__)


def _get_state(cfg):
    """Reuse shared StateManager (IMP-2 pattern)."""
    state = current_app.config.get("STATE")
    if state is not None:
        return state
    from core.state import StateManager
    return StateManager(
        db_path=cfg.state.db_file,
        log_path=cfg.logging.log_file,
        retention_days=cfg.logging.retention_days,
    )


@stats_bp.route("/stats")
def stats():
    error = None
    indexer_rows = []
    prowlarr_enabled = False
    scored_data = {}

    try:
        config = load_config(current_app.config["CONFIG_PATH"])
        prowlarr_enabled = config.prowlarr.enabled
        state = _get_state(config)

        # Read indexer_stats via shared StateManager connection
        with state._lock:
            try:
                rows = state._db.execute("SELECT * FROM indexer_stats").fetchall()
            except Exception:
                rows = []

        db_stats = {r["indexer_id"]: dict(r) for r in rows}

        # Get live scored data if Prowlarr is available
        if prowlarr_enabled:
            try:
                from core.prowlarr import ProwlarrClient
                from core.indexer_scorer import IndexerScorer
                prowlarr = ProwlarrClient(config.prowlarr.url, config.prowlarr.api_key)
                scorer = IndexerScorer(prowlarr, state, config.prowlarr)
                results = scorer.score_all(skip_ai=True)
                scored_data = {r["id"]: r for r in results}
            except Exception:
                pass

        # Build enriched rows — include all known indexers (scored or DB)
        seen_ids = set()
        for iid, s in scored_data.items():
            seen_ids.add(iid)
            db = db_stats.get(iid, {})
            total_grabs = db.get("total_grabs", 0)
            malicious = db.get("malicious_hits", 0)
            pct = round(malicious / total_grabs * 100, 1) if total_grabs > 0 else 0.0
            indexer_rows.append({
                "id": iid,
                "name": s["name"],
                "priority": s.get("priority", "—"),
                "total_grabs": total_grabs,
                "malicious_hits": malicious,
                "malicious_pct": pct,
                "avg_response_ms": s.get("avg_response_ms", 0),
                "success_rate": s.get("success_rate", 0),
                "grab_success": s.get("grab_success", 0),
                "health_score": s.get("health_score"),
                "total_records": s.get("total_records", 0),
                "in_backoff": s.get("in_backoff", False),
                "enabled": s.get("enabled", True),
                "trend": s.get("trend"),
            })

        # Add any DB-only indexers not in scored_data
        for iid, db in db_stats.items():
            if iid in seen_ids:
                continue
            total = db.get("total_grabs", 0)
            malicious = db.get("malicious_hits", 0)
            pct = round(malicious / total * 100, 1) if total > 0 else 0.0
            indexer_rows.append({
                "id": iid,
                "name": db["indexer_name"],
                "priority": "—",
                "total_grabs": total,
                "malicious_hits": malicious,
                "malicious_pct": pct,
                "avg_response_ms": 0,
                "success_rate": 0,
                "grab_success": 0,
                "health_score": db.get("health_score"),
                "total_records": 0,
                "in_backoff": False,
                "enabled": True,
                "trend": None,
            })

        indexer_rows.sort(key=lambda x: (-(x["health_score"] or 0), -x["malicious_hits"]))

    except Exception as exc:
        error = str(exc)

    return render_template(
        "stats.html",
        indexer_rows=indexer_rows,
        prowlarr_enabled=prowlarr_enabled,
        error=error,
    )
