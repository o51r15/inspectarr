"""
ui/routes/activity.py — Activity: what Inspectarr found and what it did.

  /activity                     -> Needs review if anything is held, else History
  /activity/review              held releases waiting for a decision
  /activity/history             every inspection, newest first, paged
  /activity/inspection/<id>     JSON: one inspection plus the reasons it fired

Review actions still POST to /quarantine/action (unchanged endpoint), so the
logic that pauses, resumes and deletes is not touched by this page.
"""
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request

from ui.overview import outcome_label, risk_tone

activity_bp = Blueprint("activity", __name__)

PAGE_SIZE = 50
FILTERS = {
    "all": None,
    "removed": "deleted",
    "held": "quarantined",
    "recorded": "recorded",
    "failed": "failed",
}


def _state():
    return current_app.config.get("STATE")


@activity_bp.route("/activity")
def activity_root():
    state = _state()
    held = state.count_quarantine_held() if state else 0
    return redirect("/activity/review" if held else "/activity/history")


@activity_bp.route("/activity/review")
def review():
    state = _state()
    held = state.get_quarantine("held") if state else []
    for q in held:
        q["risk_tone"] = risk_tone(q.get("risk_level"))
    recent = [q for q in (state.get_quarantine(None, limit=50) if state else [])
              if q.get("status") != "held"][:15]
    return render_template("activity_review.html", held=held, recent=recent,
                           mode=current_app.config["READ_OPERATING_MODE"]())


@activity_bp.route("/activity/history")
def history():
    state = _state()
    flt = request.args.get("show", "all")
    if flt not in FILTERS:
        flt = "all"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    action = FILTERS[flt]
    total = state.count_inspections(action=action) if state else 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = state.get_inspections(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                                 action=action) if state else []
    for r in rows:
        r["outcome"], r["outcome_tone"] = outcome_label(r)
        r["risk_tone"] = risk_tone(r.get("risk_level"))
    return render_template("activity_history.html", rows=rows, total=total,
                           page=page, pages=pages, show=flt, filters=list(FILTERS))


@activity_bp.route("/activity/inspection/<inspection_id>")
def inspection_detail(inspection_id):
    # Inspection ids are uuid4 hex; anything else is not ours to look up.
    if not inspection_id or len(inspection_id) > 64 or not inspection_id.replace("-", "").isalnum():
        abort(404)
    state = _state()
    if not state:
        return jsonify({"ok": False, "message": "State unavailable"}), 503
    row = state.get_inspection(inspection_id)
    if not row:
        return jsonify({"ok": False, "message": "Not found"}), 404
    reasons = state.get_inspection_reasons(inspection_id)
    label, tone = outcome_label(row)
    return jsonify({
        "ok": True,
        "outcome": label, "outcome_tone": tone,
        "action_detail": row.get("action_detail"),
        "indexer": row.get("indexer"), "arr_app": row.get("arr_app"),
        "category": row.get("category"), "file_count": row.get("file_count"),
        "risk_score": row.get("risk_score"),
        "reasons": [{"signal": r.get("signal"), "detail": r.get("detail"),
                     "file_path": r.get("file_path"), "severity": r.get("severity")}
                    for r in reasons],
    })
