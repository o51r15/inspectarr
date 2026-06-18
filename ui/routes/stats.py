"""
ui/routes/stats.py — Indexer grab and malicious hit statistics
"""
import sqlite3
from flask import Blueprint, render_template, current_app
from core.config import load_config

stats_bp = Blueprint("stats", __name__)


@stats_bp.route("/stats")
def stats():
    error = None
    indexer_rows = []
    prowlarr_enabled = False

    try:
        config = load_config(current_app.config["CONFIG_PATH"])
        db_path = config.state.db_file
        prowlarr_enabled = config.prowlarr.enabled

        # Read indexer_stats from SQLite directly (read-only view)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM indexer_stats WHERE total_grabs > 0"
                ).fetchall()
            except sqlite3.OperationalError:
                # total_grabs column not yet migrated — show empty page
                rows = []

        db_stats = {r["indexer_id"]: dict(r) for r in rows}

        # Try to enrich with live Prowlarr priority
        prowlarr_map = {}
        if prowlarr_enabled:
            try:
                from core.prowlarr import ProwlarrClient
                prowlarr = ProwlarrClient(
                    config.prowlarr.url,
                    config.prowlarr.api_key,
                )
                for idx in prowlarr.get_torrent_indexers():
                    prowlarr_map[idx["id"]] = idx
            except Exception:
                pass  # Prowlarr unreachable — show stats without priority

        for iid, s in db_stats.items():
            total     = s.get("total_grabs", 0)
            malicious = s.get("malicious_hits", 0)
            pct       = round(malicious / total * 100, 1) if total > 0 else 0.0
            prowlarr_info = prowlarr_map.get(iid, {})
            indexer_rows.append({
                "id":            iid,
                "name":          s["indexer_name"],
                "priority":      prowlarr_info.get("priority", "—"),
                "total_grabs":   total,
                "malicious_hits": malicious,
                "malicious_pct": pct,
            })

        # Sort: most malicious first, then most grabs
        indexer_rows.sort(key=lambda x: (-x["malicious_hits"], -x["total_grabs"]))

    except Exception as exc:
        error = str(exc)

    return render_template(
        "stats.html",
        indexer_rows=indexer_rows,
        prowlarr_enabled=prowlarr_enabled,
        error=error,
    )
