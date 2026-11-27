"""Rescore & reorder as a background job; the reorder uses its own pass's scores."""
import copy
import json
import threading
import time

from core import indexer_jobs
from core.config import _parse_config
from core.indexer_scorer import IndexerScorer
from tests.conftest import BASE_CONFIG


class _Prowlarr:
    def __init__(self, priorities):
        self.indexers = [{"id": i, "name": f"ix{i}", "priority": p} for i, p in priorities.items()]
        self.set = {}

    def get_torrent_indexers(self, include_disabled=False):
        return sorted(self.indexers, key=lambda x: x["priority"])

    def set_indexer_priority(self, raw, prio):
        self.set[raw["id"]] = prio
        return True


class _State:
    def __init__(self, cached):
        self.cached, self.app = cached, {}

    def get_cached_scores(self):
        return {i: {"health_score": v} for i, v in self.cached.items()}

    def get_all_indexer_stats(self):
        return []

    def update_last_reorder(self, iid):
        pass

    def get_app_state(self, k):
        return self.app.get(k)

    def set_app_state(self, k, v):
        self.app[k] = v


def _cfg():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["prowlarr"].update({"enabled": True, "url": "http://p", "api_key": "k", "base_priority": 10})
    return _parse_config(raw).prowlarr


def test_reorder_uses_the_scores_from_its_own_pass():
    # Stored scores are stale (another pass wrote them); this pass says ix3 is best.
    prowlarr = _Prowlarr({1: 10, 2: 11, 3: 12})
    state = _State({1: 90, 2: 80, 3: 50})
    scorer = IndexerScorer(prowlarr, state, _cfg())
    scorer.reorder([{"id": 1, "health_score": 60}, {"id": 2, "health_score": 70}, {"id": 3, "health_score": 92}])
    assert prowlarr.set == {3: 10, 1: 12}                 # ix2 already at 11; ix3 first


def test_reorder_without_a_pass_still_uses_stored_scores():
    prowlarr = _Prowlarr({1: 12, 2: 11, 3: 10})
    scorer = IndexerScorer(prowlarr, _State({1: 90, 2: 80, 3: 50}), _cfg())
    scorer.reorder()
    assert prowlarr.set == {1: 10, 3: 12}                 # ix2 already at 11


def test_recover_marks_an_interrupted_job():
    st = _State({})
    st.app[indexer_jobs.STATE_KEY] = json.dumps({"state": "running", "stage": "Scoring"})
    indexer_jobs.recover(st)
    job = indexer_jobs.status(st)
    assert job["state"] == "failed" and "restart" in job["stage"]


def test_one_job_at_a_time(monkeypatch, config_path):
    st = _State({})
    gate = threading.Event()

    def slow_run(self, job):
        with indexer_jobs.SCORING_LOCK:
            gate.wait(5)
            job.update(state="done", stage="Done: 0 indexer(s) moved")
            self._save(job)

    monkeypatch.setattr(indexer_jobs.RescoreJob, "_run", slow_run)
    job = indexer_jobs.RescoreJob(st, config_path)
    assert job.start()[0] is True
    time.sleep(0.1)
    started, msg = job.start()
    assert started is False and "already running" in msg
    gate.set()
    job._thread.join(5)
    assert indexer_jobs.status(st)["state"] == "done"


def test_previous_scores(app):
    state = app.config["STATE"]
    for v in (80.0, 80.0, 85.0, 92.0, 92.0):
        state.record_score_history(7, v)
    prev = state.get_previous_scores()[7]
    assert prev["latest"] == 92.0 and prev["previous"] == 85.0 and prev["previous_at"]


def test_routes_start_and_report(client, config_path, monkeypatch):
    from tests.conftest import read_config, write_config
    data = read_config(config_path)
    data["prowlarr"].update({"enabled": True, "url": "http://p", "api_key": "k"})
    write_config(config_path, data)

    def fake_run(self, job):
        job.update(state="done", stage="Done: 3 indexer(s) moved", moved=3)
        self._save(job)

    monkeypatch.setattr(indexer_jobs.RescoreJob, "_run", fake_run)
    r = client.post("/indexers/rescore-reorder")
    assert r.status_code == 202 and r.get_json()["started"]
    for _ in range(50):
        job = client.get("/indexers/rescore/status").get_json()["job"]
        if job.get("state") == "done":
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["moved"] == 3
