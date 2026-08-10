"""
ui/routes/llm_logs.py — System → LLM Logs

Shows AI scoring report (current run reasoning per indexer) and
historical score trend data.
"""
from flask import Blueprint, render_template, current_app, jsonify

llm_logs_bp = Blueprint("llm_logs", __name__)


@llm_logs_bp.route("/system/llm-logs")
def llm_logs_page():
    """Render the LLM Logs page."""
    state = current_app.config.get("STATE")
    error = None
    runs = []
    if state:
        try:
            runs = state.get_llm_scoring_runs(limit=50)
        except Exception as exc:
            error = str(exc)
    return render_template("llm_logs.html", runs=runs, error=error)


@llm_logs_bp.route("/api/llm-logs/history/<int:indexer_id>")
def llm_history_api(indexer_id):
    """Return AI score history for one indexer (JSON)."""
    state = current_app.config.get("STATE")
    if not state:
        return jsonify({"error": "no state"}), 500
    try:
        history = state.get_llm_score_history_for_indexer(indexer_id, limit=30)
        return jsonify(history)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
