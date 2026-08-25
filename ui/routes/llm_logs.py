"""
ui/routes/llm_logs.py — System → LLM Logs

Shows AI scoring report (current run reasoning per indexer) and
historical score trend data.
"""
from flask import Blueprint, redirect, current_app, jsonify

llm_logs_bp = Blueprint("llm_logs", __name__)


@llm_logs_bp.route("/system/llm-logs")
def llm_logs_page():
    """Redirect to Indexers hub AI Scoring tab."""
    return redirect("/indexers/ai-scoring", code=301)


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
