"""
Calibrate: thinking x context x batch search, the settings it picks, and
"Use this model" applying them. No real Ollama: quality runs and batch
probes are replaced with scripted results.
"""
import json

import pytest

from core import calibration as cal, model_validator
from core.model_quality import PASS_SCORE
from tests.conftest import read_config, write_config

URL = "http://192.168.1.125:11434"


# --- the pure parts --------------------------------------------------------------

def test_even_splits():
    assert cal.even_splits(37) == [37, 19, 13, 10, 8, 7, 6, 5]
    assert cal.even_splits(3) == [3]
    assert cal.even_splits(120)[0] == 40          # 120 in one call is over the cap
    assert all(b <= cal.MAX_BATCH for b in cal.even_splits(200))


@pytest.mark.parametrize("worst, expected", [(40, 80), (10, 60), (1000, 900), (None, 300)])
def test_timeout_is_twice_the_slowest_call_within_limits(worst, expected):
    assert cal.suggested_timeout(worst) == expected


def test_thinking_options():
    assert cal.thinking_options({"thinking": "none"}) == [None]
    assert cal.thinking_options({"thinking": "toggle"}) == [False, True]
    assert cal.thinking_options({"thinking": "levels", "think_levels": ["low", "medium", "high"]}) \
        == ["low", "medium", "high"]
    assert cal.thinking_options({"thinking": "levels", "think_levels": ["off", "low"]}) == [False, "low"]
    assert cal.thinking_options(None) == [None]


def test_thinking_needs_a_bigger_context_but_never_past_the_model():
    assert cal.context_for_thinking(True, 131072) == 16384
    assert cal.context_for_thinking(False, 131072) == 8192
    assert cal.context_for_thinking("low", 8192) == 8192


def _t(think, avg, spi, complete=True):
    return {"think": think, "avg": avg, "sec_per_indexer": spi, "complete": complete}


def test_best_quality_wins_and_a_near_tie_goes_to_the_faster():
    # 97 vs 88: quality wins even though slower
    assert cal.Calibration.choose_thinking([_t("low", 68, 0.6), _t("medium", 97, 4.6)])["think"] == "medium"
    # 91 vs 90: within 2 points, the faster wins
    assert cal.Calibration.choose_thinking([_t(False, 90, 0.5), _t(True, 91, 2.3)])["think"] is False
    # an incomplete setting never wins, however it scored
    assert cal.Calibration.choose_thinking([_t(False, 70, 0.5), _t(True, 0, 9, complete=False)])["think"] is False
    assert cal.Calibration.choose_thinking([_t(True, 0, 9, complete=False)]) is None


def _f(ctx, total, gpu=100):
    return {"context_window": ctx, "total_s": total, "gpu_offload_pct": gpu, "batch": 10, "calls": 4}


def test_setting_choice_prefers_the_card_then_speed_then_less_memory():
    assert cal.Calibration.choose_setting([_f(8192, 60), _f(16384, 30, gpu=80)])["context_window"] == 8192
    assert cal.Calibration.choose_setting([_f(8192, 60), _f(16384, 30)])["context_window"] == 16384
    assert cal.Calibration.choose_setting([_f(8192, 31), _f(16384, 30)])["context_window"] == 8192
    assert cal.Calibration.choose_setting([]) is None


def test_token_model_uses_measured_numbers():
    from core.model_quality import quality_indexers
    rows = quality_indexers()
    stats = [{"n": 14, "prompt_tokens": 3000, "answer_tokens": 2800}]
    tm = cal.TokenModel(stats, rows)
    assert tm.answer_per_indexer == 200
    assert tm.need(rows) > 3000 + 14 * 200


# --- the whole run, scripted ---------------------------------------------------------

class Script:
    """Scripted Ollama: quality scores per think value, and a batch limit
    above which the model drops indexers."""

    def __init__(self, monkeypatch, profile, quality, drops_above=99, gpu=100):
        self.calls = []
        monkeypatch.setattr(cal, "model_profile", lambda url, m: profile)
        monkeypatch.setattr(model_validator, "_warmup",
                            lambda url, m, ctx=8192: {"gpu_offload_pct": gpu, "tok_per_s": 40.0})

        def quality_test(url, model, timeout, ctx, think=None, stats=None):
            score, complete = quality[think]
            self.calls.append(("quality", think, ctx))
            if stats is not None:
                stats.append({"n": 14, "prompt_tokens": 2400, "answer_tokens": 14 * 60,
                              "seconds": 5.0, "done_reason": "stop" if complete else "length"})
            return {"score": score, "complete": complete, "sec_per_indexer": 0.5 if not think else 2.0,
                    "follows_reference": False, "detail": f"{score}/100"}
        monkeypatch.setattr(cal, "run_quality_test", quality_test)

        def probe(self_, ctx, think, batch):
            self.calls.append(("probe", ctx, batch))
            ok = batch <= drops_above
            calls = -(-self_.n // batch)
            return {"context_window": ctx, "batch": batch, "elapsed_s": 3.0 * calls,
                    "worst_call_s": 3.0, "calls": calls, "scored": self_.n if ok else self_.n - 2,
                    "missing": 0 if ok else 2, "invented": 0, "cut_off": False, "passed": ok}
        monkeypatch.setattr(cal.Calibration, "probe", probe)


TOGGLE = {"max_context": 131072, "thinking": "toggle", "think_levels": [], "can_score": True}


def test_calibrate_picks_thinking_on_and_the_largest_reliable_even_split(monkeypatch):
    Script(monkeypatch, TOGGLE, {False: (65.0, True), True: (93.0, True)}, drops_above=19)
    out = cal.calibrate(URL, "gemma4:latest", indexer_count=37)
    assert out["passed"] and out["calibrated"]
    s = out["settings"]
    assert s["think"] is True and s["max_indexers_per_call"] == 19
    # Found at 16K (thinking's starting size), then confirmed at 8K: the
    # model, not the room, was the limit, so the smaller context wins.
    assert s["context_window"] == 8192 and 60 <= s["timeout"] <= 900
    assert out["quality_score"] == 93.0 and out["quality_runs"] == [93.0, 93.0]
    assert "dropped or invented indexers above 19" in out["explanation"]
    assert [t["name"] for t in out["tests"]] == ["Quality", "Best settings"]


def test_each_quality_setting_runs_twice(monkeypatch):
    sc = Script(monkeypatch, TOGGLE, {False: (80.0, True), True: (81.0, True)})
    cal.calibrate(URL, "m", indexer_count=10)
    assert [c for c in sc.calls if c[0] == "quality"] == [
        ("quality", False, 8192), ("quality", False, 8192),
        ("quality", True, 16384), ("quality", True, 16384)]


def test_a_level_that_does_not_finish_stops_the_climb(monkeypatch):
    levels = {"max_context": 131072, "thinking": "levels",
              "think_levels": ["low", "medium", "high"], "can_score": True}
    sc = Script(monkeypatch, levels, {"low": (68.0, True), "medium": (0.0, False), "high": (99.0, True)})
    out = cal.calibrate(URL, "gpt-oss:20b", indexer_count=10)
    assert ("quality", "high", 16384) not in sc.calls
    assert not out["passed"] and out["quality_score"] == 68.0   # low is under the pass mark


def test_low_quality_stops_before_the_batch_search(monkeypatch):
    sc = Script(monkeypatch, {"max_context": 8192, "thinking": "none", "can_score": True},
                {None: (PASS_SCORE - 10, True)})
    out = cal.calibrate(URL, "weak", indexer_count=20)
    assert not out["passed"] and not out["calibrated"]
    assert not any(c[0] == "probe" for c in sc.calls)
    assert out["tests"][0]["name"] == "Quality" and not out["tests"][0]["passed"]


def test_nothing_finishes_is_explained(monkeypatch):
    Script(monkeypatch, TOGGLE, {False: (0.0, False), True: (0.0, False)})
    out = cal.calibrate(URL, "lfm3", indexer_count=20)
    assert not out["passed"] and "ran out of room" in out["tests"][0]["detail"]


def test_embedding_or_decision_model_is_refused_without_calls(monkeypatch):
    sc = Script(monkeypatch, {"max_context": 4096, "thinking": "none", "can_score": False}, {})
    out = cal.calibrate(URL, "d1:latest", indexer_count=20)
    assert not out["passed"] and "cannot write answers" in out["error"]
    assert sc.calls == []


def test_room_limited_tries_the_next_context_up(monkeypatch):
    sc = Script(monkeypatch, {"max_context": 131072, "thinking": "none", "can_score": True},
                {None: (85.0, True)})
    # Make each indexer expensive so 8K only fits small batches.
    monkeypatch.setattr(cal.TokenModel, "need", lambda self, rows: 600 * len(rows))
    out = cal.calibrate(URL, "m", indexer_count=37)
    contexts = {c[1] for c in sc.calls if c[0] == "probe"}
    assert 16384 in contexts
    assert out["settings"]["context_window"] == 16384


def test_never_goes_past_the_models_own_limit(monkeypatch):
    sc = Script(monkeypatch, {"max_context": 8192, "thinking": "none", "can_score": True},
                {None: (85.0, True)})
    monkeypatch.setattr(cal.TokenModel, "need", lambda self, rows: 600 * len(rows))
    cal.calibrate(URL, "m", indexer_count=37)
    assert all(c[1] <= 8192 for c in sc.calls if c[0] == "probe")


def test_validate_model_deep_is_calibrate(monkeypatch):
    seen = {}
    monkeypatch.setattr(cal, "calibrate", lambda url, model, n, progress=None:
                        seen.update(url=url, model=model, n=n) or {"passed": True})
    out = model_validator.validate_model(URL, "m", indexer_count=33, calibration="deep")
    assert out == {"passed": True} and seen == {"url": URL, "model": "m", "n": 33}


# --- applying it ---------------------------------------------------------------------

def _enable_ai(config_path, **ollama):
    data = read_config(config_path)
    data["prowlarr"]["ollama"].update({"enabled": True, "url": URL, **ollama})
    write_config(config_path, data)


def _record(app, model, settings, calibrated=True):
    state = app.config["STATE"]
    state.save_validation(model, {"passed": True, "calibrated": calibrated,
                                  "max_safe_batch": settings.get("max_indexers_per_call"),
                                  "settings": settings}, ollama_url=URL)


def test_use_this_model_applies_the_calibrated_settings(app, client, config_path, monkeypatch):
    _enable_ai(config_path, context_window=16384, model="old")
    _record(app, "gemma4:latest", {"context_window": 16384, "max_indexers_per_call": 19,
                                   "think": True, "timeout": 120})
    r = client.post("/config/ai/model", json={"model": "gemma4:latest"})
    assert r.status_code == 200 and r.get_json()["applied"]["calibrated"]
    o = read_config(config_path)["prowlarr"]["ollama"]
    assert (o["model"], o["context_window"], o["max_indexers_per_call"], o["timeout"], o["think"]) \
        == ("gemma4:latest", 16384, 19, 120, True)


def test_use_this_model_without_calibration_resets_context_and_thinking(app, client, config_path, monkeypatch):
    import core.ollama_models as om
    monkeypatch.setattr(om, "model_profile", lambda url, m: {"max_context": 4096})
    _enable_ai(config_path, context_window=16384, think="medium", model="old")
    r = client.post("/config/ai/model", json={"model": "tiny", "force": True})
    assert r.status_code == 200
    o = read_config(config_path)["prowlarr"]["ollama"]
    assert o["context_window"] == 4096 and o.get("think") is None
    from core.config import _as_think
    assert _as_think(o.get("think")) is None              # null = recommended


def test_calibration_endpoint_marks_a_changed_context_as_stale(app, client, config_path):
    _enable_ai(config_path, model="gemma4:latest", context_window=8192)
    _record(app, "gemma4:latest", {"context_window": 16384, "max_indexers_per_call": 19,
                                   "think": True, "timeout": 120})
    body = client.get("/config/ai/calibration").get_json()
    assert body["stale"] and not body["calibrated"] and body["max_safe_batch"] is None
    _enable_ai(config_path, context_window=16384)
    body = client.get("/config/ai/calibration").get_json()
    assert body["calibrated"] and body["max_safe_batch"] == 19


def test_validations_list_carries_quality_and_settings(app, client, config_path):
    _enable_ai(config_path)
    app.config["STATE"].save_validation("gemma4:latest", {
        "passed": True, "calibrated": True, "quality_score": 91.0, "quality_runs": [96.4, 85.6],
        "settings": {"context_window": 16384, "max_indexers_per_call": 19, "think": True,
                     "timeout": 120}, "total_s": 64.0, "explanation": "Thinking on..."},
        ollama_url=URL)
    v = client.get("/config/ai/validations").get_json()["validations"][0]
    assert v["quality_score"] == 91.0 and v["settings"]["think"] is True
    assert v["total_s"] == 64.0 and v["explanation"].startswith("Thinking on")
