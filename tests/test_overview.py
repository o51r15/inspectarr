"""
ui/overview.py decides the sentence a newcomer reads first, and the outcome
label on every History row. One test per branch, plus the orderings that
matter (an outage beats everything; paused beats held).
"""
import pytest

from ui.overview import compute_health, outcome_label, risk_tone

RUNNING_OK = {"running": True, "last_result": {"error": None}}


def test_protected_when_running_clean_automatic():
    h = compute_health(RUNNING_OK, "automatic", 0, [])
    assert (h.tone, h.title) == ("ok", "Protected")


def test_service_down_wins_over_everything():
    status = {"running": False, "last_result": {"error": "boom"}}
    h = compute_health(status, "monitor", 5, ["Sonarr", "qBittorrent"])
    assert h.tone == "danger"
    assert h.title == "Can't reach Sonarr, qBittorrent"


def test_last_scan_error_beats_paused():
    h = compute_health({"running": False, "last_result": {"error": "qbit timeout"}}, "automatic", 0, [])
    assert h.tone == "danger" and "failed" in h.title
    assert "qbit timeout" in h.detail


def test_paused_beats_held():
    h = compute_health({"running": False}, "automatic", 3, [])
    assert h.tone == "warn" and h.title.startswith("Paused")
    assert h.action == "toggle-scheduler"


@pytest.mark.parametrize("n,phrase", [(1, "1 release is"), (4, "4 releases are")])
def test_held_wording(n, phrase):
    h = compute_health(RUNNING_OK, "quarantine", n, [])
    assert h.tone == "warn" and h.title.startswith(phrase)
    assert h.href == "/activity/review"


def test_held_beats_monitor_mode():
    assert compute_health(RUNNING_OK, "monitor", 2, []).href == "/activity/review"


def test_monitor_mode_is_info_not_protected():
    h = compute_health(RUNNING_OK, "monitor", 0, [])
    assert h.tone == "info" and "Protected" not in h.title


def test_quarantine_mode():
    h = compute_health(RUNNING_OK, "quarantine", 0, [])
    assert h.tone == "ok" and "review" in h.title


def test_no_status_at_all_reads_as_paused():
    assert compute_health(None, "automatic", 0, []).title.startswith("Paused")


# ---- outcome_label: shapes taken from core/scanner.py and the live DB ----

@pytest.mark.parametrize("row,label,tone", [
    ({"action": "deleted", "decision": "remediate"}, "Removed and blocklisted", "danger"),
    ({"action": "quarantined", "decision": "quarantine"}, "Held for review", "warn"),
    ({"action": "dry_run"}, "Dry run — no action taken", "violet"),
    ({"action": "failed", "decision": "remediate"}, "Action failed — will retry", "danger"),
    ({"action": "recorded", "decision": "record",
      "action_detail": "held by operating_mode=monitor (would have been remediate)"},
     "Would have been removed", "info"),
    ({"action": "recorded", "decision": "record",
      "action_detail": "held by operating_mode=monitor (would have been quarantine)"},
     "Would have been held", "info"),
    ({"action": "recorded", "decision": "record", "action_detail": None},
     "Recorded — below your threshold", "neutral"),
    ({"action": "something_new"}, "Recorded", "neutral"),
    ({}, "Recorded", "neutral"),
])
def test_outcome_label(row, label, tone):
    assert outcome_label(row) == (label, tone)


def test_risk_tone():
    assert risk_tone("CRITICAL") == "danger"
    assert risk_tone("medium") == "warn"
    assert risk_tone(None) == "neutral"
