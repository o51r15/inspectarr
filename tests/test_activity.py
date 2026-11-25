"""Overview and Activity against a real (temporary) database."""
import uuid

import pytest


def _state(app):
    st = app.config.get("STATE")
    if st is None:
        pytest.skip("no StateManager in this app")
    return st


def _inspect(st, **kw):
    row = {"inspection_id": str(uuid.uuid4()), "torrent_hash": uuid.uuid4().hex.upper(),
           "torrent_name": "Show.S01E01.1080p", "rule_name": "TV", "category": "tv-sonarr",
           "action": "deleted", "decision": "remediate", "operating_mode": "automatic",
           "risk_level": "CRITICAL", "indexer": "TestIdx"}
    row.update(kw)
    assert st.record_inspection(row, [{"signal": "bad_extension", "detail": ".exe in a TV release",
                                       "file_path": "Show/setup.exe", "severity": "CRITICAL"}])
    return row


def test_overview_empty_state(client):
    html = client.get("/").get_data(as_text=True)
    assert "Nothing caught yet" in html
    assert "Paused" in html          # scheduler is not started in tests


def test_overview_lists_recent_catch(app, client):
    st = _state(app)
    _inspect(st, torrent_name="Evil.Release.S01")
    html = client.get("/").get_data(as_text=True)
    assert "Evil.Release.S01" in html
    assert "Removed and blocklisted" in html


def test_held_count_shows_in_nav_and_hero(app, client):
    st = _state(app)
    st.add_quarantine({"hash": "A" * 40, "torrent_name": "Held.One", "rule_name": "TV",
                       "risk_level": "MEDIUM", "bad_files": ["x.scr"], "paused_ok": 1})
    assert st.count_quarantine_held() == 1
    html = client.get("/activity/review").get_data(as_text=True)
    assert "Held.One" in html
    assert 'class="pill"' in html
    assert client.get("/activity").headers["Location"].endswith("/activity/review")


def test_activity_root_goes_to_history_when_nothing_held(client):
    assert client.get("/activity").headers["Location"].endswith("/activity/history")


def test_history_paging_and_filters(app, client):
    st = _state(app)
    for i in range(3):
        _inspect(st, torrent_name=f"Del.{i}")
    _inspect(st, torrent_name="Mon.1", action="recorded", decision="record",
             action_detail="held by operating_mode=monitor (would have been remediate)")
    html = client.get("/activity/history").get_data(as_text=True)
    assert "4 catches" in html and "Would have been removed" in html
    html = client.get("/activity/history?show=recorded").get_data(as_text=True)
    assert "Mon.1" in html and "Del.0" not in html
    # Unknown filter and silly page numbers fall back instead of erroring.
    assert client.get("/activity/history?show=%27;drop&page=-5").status_code == 200
    assert client.get("/activity/history?page=999").status_code == 200


def test_inspection_detail_json(app, client):
    st = _state(app)
    row = _inspect(st)
    d = client.get(f"/activity/inspection/{row['inspection_id']}").get_json()
    assert d["ok"] and d["outcome"] == "Removed and blocklisted"
    assert d["reasons"][0]["signal"] == "bad_extension"


@pytest.mark.parametrize("bad", ["nope", "../../etc", "x" * 80])
def test_inspection_detail_rejects_unknown(client, bad):
    assert client.get(f"/activity/inspection/{bad}").status_code == 404


def test_status_json_gains_held_and_mode(client):
    d = client.get("/status").get_json()
    assert "held" in d and "mode" in d and "running" in d


def test_pause_is_remembered(app, client, monkeypatch):
    st = _state(app)
    sched = app.config["SCHEDULER"]
    # Stand-ins: the real start() launches a scan thread.
    def fake_start():
        sched.running = True
        return True
    def fake_stop():
        sched.running = False
    monkeypatch.setattr(sched, "start", fake_start)
    monkeypatch.setattr(sched, "stop", fake_stop)
    r = client.post("/scheduler/toggle.json")
    assert r.status_code == 200 and r.get_json()["running"] is True
    assert st.get_app_state("scheduler_paused") == "0"
    r = client.post("/scheduler/toggle.json")
    assert r.get_json()["running"] is False
    assert st.get_app_state("scheduler_paused") == "1"


def test_run_now_reports_truthfully(app, client, monkeypatch):
    sched = app.config["SCHEDULER"]
    monkeypatch.setattr(sched, "trigger", lambda: False)
    r = client.post("/scheduler/run.json")
    assert r.status_code == 409 and r.get_json()["ok"] is False
