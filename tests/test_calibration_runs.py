"""
Calibration runs: one model at a time, stored so they survive a restart,
pause / cancel mid-model, overnight start, new models, and the
better-model banner. calibrate() itself is replaced by a script.
"""
import json
from datetime import datetime, timezone

import pytest

from core import calibration_runs as cr
from tests.conftest import read_config, write_config

URL = "http://192.168.1.125:11434"


def _server(config_path, **ollama):
    data = read_config(config_path)
    data["prowlarr"]["ollama"].update({"enabled": True, "url": URL, **ollama})
    write_config(config_path, data)


@pytest.fixture
def runner(app, config_path, monkeypatch):
    _server(config_path, model="current:latest")
    r = app.config["CAL_RUNNER"]
    calls = []

    def fake_calibrate(url, model, n, progress=None):
        calls.append(model)
        if progress:
            progress("Quality test", 0, 4)
        score = {"good:latest": 92.0, "current:latest": 80.0}.get(model, 60.0)
        return {"model": model, "ollama_url": url, "passed": score >= 70, "calibrated": score >= 70,
                "quality_score": score, "total_s": 60.0, "gpu_offload_pct": 100,
                "settings": {"context_window": 8192, "max_indexers_per_call": 20,
                             "think": None, "timeout": 120}}
    import core.calibration
    monkeypatch.setattr(core.calibration, "calibrate", fake_calibrate)
    import core.ollama_models
    monkeypatch.setattr(core.ollama_models, "unload", lambda url, m: calls.append("unload:" + m))
    import core.ollama_registry
    monkeypatch.setattr(core.ollama_registry, "local_digest", lambda url, m: "sha256:x")
    r.calls = calls
    return r


def test_models_run_one_after_another_and_results_are_saved(app, runner):
    st = app.config["STATE"]
    run_id = st.create_calibration_run(URL, ["good:latest", "weak:latest"], kind="all")
    assert runner.step() is True
    assert [c for c in runner.calls if ":" in c and not c.startswith("unload")] == ["good:latest", "weak:latest"]
    run = st.get_calibration_run(run_id)
    assert run["status"] == "done"
    assert [(i["model"], i["status"]) for i in run["items"]] == [("good:latest", "done"), ("weak:latest", "failed")]
    assert st.get_validation("good:latest", URL)["status"] == "supported"
    assert st.get_validation("weak:latest", URL)["status"] == "failed"


def test_models_are_unloaded_unless_in_use(app, runner):
    st = app.config["STATE"]
    st.create_calibration_run(URL, ["good:latest", "current:latest"])
    runner.step()
    assert "unload:good:latest" in runner.calls and "unload:current:latest" not in runner.calls


def test_a_run_for_tonight_waits(app, runner):
    st = app.config["STATE"]
    later = cr.next_hour((datetime.now().hour + 2) % 24)
    st.create_calibration_run(URL, ["good:latest"], start_after=later)
    assert runner.step() is False and runner.calls == []


def test_next_hour_is_in_the_future():
    now = datetime(2026, 11, 26, 3, 30).astimezone()
    at = datetime.fromisoformat(cr.next_hour(1, now))
    assert at > now and (at - now).total_seconds() < 24 * 3600
    assert datetime.fromisoformat(cr.next_hour(5, now)).astimezone().hour == 5


def test_cancel_mid_model_stops_and_keeps_finished_results(app, runner, monkeypatch):
    st = app.config["STATE"]
    run_id = st.create_calibration_run(URL, ["good:latest", "weak:latest", "other:latest"])
    import core.calibration
    real = core.calibration.calibrate

    def cancelling(url, model, n, progress=None):
        if model == "weak:latest":
            runner._request[run_id] = "cancel"    # pressed while this model runs
        return real(url, model, n, progress)
    monkeypatch.setattr(core.calibration, "calibrate", cancelling)
    runner.step()
    run = st.get_calibration_run(run_id)
    assert run["status"] == "cancelled"
    assert [i["status"] for i in run["items"]] == ["done", "cancelled", "cancelled"]
    assert st.get_validation("good:latest", URL) is not None
    assert st.get_validation("weak:latest", URL) is None


def test_pause_then_resume_starts_the_interrupted_model_again(app, runner, monkeypatch):
    st = app.config["STATE"]
    run_id = st.create_calibration_run(URL, ["good:latest", "weak:latest"])
    import core.calibration
    real = core.calibration.calibrate
    seen = []

    def pausing(url, model, n, progress=None):
        seen.append(model)
        if model == "weak:latest" and seen.count("weak:latest") == 1:
            runner._request[run_id] = "pause"
        return real(url, model, n, progress)
    monkeypatch.setattr(core.calibration, "calibrate", pausing)
    runner.step()
    assert st.get_calibration_run(run_id)["status"] == "paused"
    runner.request(run_id, "resume")
    runner.step()
    run = st.get_calibration_run(run_id)
    assert run["status"] == "done" and seen == ["good:latest", "weak:latest", "weak:latest"]


def test_a_restart_carries_on_where_it_was(app, runner):
    st = app.config["STATE"]
    run_id = st.create_calibration_run(URL, ["good:latest", "weak:latest"])
    st.set_calibration_run(run_id, "running")
    st.set_calibration_item(run_id, 0, "done")
    st.set_calibration_item(run_id, 1, "running")                 # killed here
    assert st.recover_calibration_runs() == 1
    runner.step()
    assert runner.calls[0] == "weak:latest"                          # only the unfinished one
    assert st.get_calibration_run(run_id)["status"] == "done"


def test_banner_offers_a_clearly_better_model(app, runner):
    st = app.config["STATE"]
    st.create_calibration_run(URL, ["current:latest", "good:latest"])
    runner.step()
    banner = json.loads(st.get_app_state(cr.BANNER_KEY))
    assert banner["model"] == "good:latest" and banner["current"] == "current:latest"


@pytest.mark.parametrize("cand, cur, better", [
    ({"quality_score": 85, "total_s": 60}, {"quality_score": 80, "total_s": 60}, True),
    ({"quality_score": 85, "total_s": 100}, {"quality_score": 80, "total_s": 60}, False),   # much slower
    ({"quality_score": 81, "total_s": 30}, {"quality_score": 80, "total_s": 60}, True),     # same, faster
    ({"quality_score": 81, "total_s": 55}, {"quality_score": 80, "total_s": 60}, False),
    ({"quality_score": 95, "total_s": 60, "gpu_offload_pct": 70},
     {"quality_score": 80, "total_s": 60, "gpu_offload_pct": 100}, False),                  # off the card
    ({"quality_score": 95, "total_s": 60}, None, False),
])
def test_meaningfully_better(cand, cur, better):
    assert cr.meaningfully_better(cand, cur) is better


def test_new_models_are_queued_for_tonight(app, runner, monkeypatch):
    st = app.config["STATE"]
    import core.ollama_models
    models = [{"name": "a:latest", "can_score": True}]
    monkeypatch.setattr(core.ollama_models, "list_models", lambda url: models)
    runner._maybe_queue_new_models()                      # first look: nothing queued
    assert st.list_calibration_runs() == []
    models.append({"name": "new:latest", "can_score": True})
    models.append({"name": "embed:latest", "can_score": False})
    runner._last_new_check = 0
    runner._maybe_queue_new_models()
    runs = st.list_calibration_runs()
    assert len(runs) == 1 and [i["model"] for i in runs[0]["items"]] == ["new:latest"]
    assert runs[0]["start_after"] > datetime.now(timezone.utc).isoformat()


def test_new_models_setting_off_queues_nothing(app, runner, config_path, monkeypatch):
    _server(config_path, calibrate_new_models=False)
    import core.ollama_models
    monkeypatch.setattr(core.ollama_models, "list_models", lambda url: [{"name": "x", "can_score": True}])
    runner._maybe_queue_new_models()
    assert app.config["STATE"].get_app_state(cr.KNOWN_KEY + URL) is None


# --- routes -----------------------------------------------------------------------

def test_calibrate_button_creates_a_one_model_run(app, client, runner):
    r = client.post("/config/ai/validate", json={"model": "good:latest"})
    assert r.status_code == 200
    runs = app.config["STATE"].list_calibration_runs()
    assert len(runs) == 1 and runs[0]["kind"] == "one"
    assert client.get("/config/ai/validate/status").get_json()["running"] is True
    runner.step()
    assert client.get("/config/ai/validate/status").get_json()["running"] is False


def test_calibrate_all_now_and_tonight(app, client, runner):
    r = client.post("/config/ai/runs", json={"models": ["a", "b"], "when": "tonight"})
    assert r.status_code == 200 and r.get_json()["start_after"]
    d = client.get("/config/ai/runs").get_json()
    assert len(d["runs"]) == 1 and [i["model"] for i in d["runs"][0]["items"]] == ["a", "b"]
    assert client.post("/config/ai/runs", json={"models": []}).status_code == 400


def test_run_actions_and_banner_dismiss(app, client, runner):
    st = app.config["STATE"]
    run_id = st.create_calibration_run(URL, ["a"])
    assert client.post(f"/config/ai/runs/{run_id}/pause").status_code == 200
    assert st.get_calibration_run(run_id)["status"] == "paused"
    client.post(f"/config/ai/runs/{run_id}/resume")
    assert st.get_calibration_run(run_id)["status"] == "waiting"
    client.post(f"/config/ai/runs/{run_id}/cancel")
    assert st.get_calibration_run(run_id)["status"] == "cancelled"
    st.set_app_state(cr.BANNER_KEY, json.dumps({"model": "x"}))
    client.post("/config/ai/banner/dismiss")
    assert client.get("/config/ai/runs").get_json()["banner"] is None
    assert client.post(f"/config/ai/runs/{run_id}/explode").status_code == 400


def test_calibration_refuses_while_ai_is_off(app, client, config_path, runner):
    _server(config_path, enabled=False)
    assert client.post("/config/ai/validate", json={"model": "a"}).status_code == 400
    assert client.post("/config/ai/runs", json={"models": ["a"]}).status_code == 400
