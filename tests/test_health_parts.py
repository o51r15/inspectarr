"""C-3: the health score's named parts; C-5: the AI can't disable an indexer alone."""
import copy

import pytest

from core.config import _parse_config
from core.indexer_scorer import IndexerScorer, compute_health_score, health_parts, parts_for_display
from tests.conftest import BASE_CONFIG


def _cfg(**auto):
    raw = copy.deepcopy(BASE_CONFIG)
    raw["prowlarr"].update({"enabled": True, "url": "http://p", "api_key": "k"})
    raw["prowlarr"]["auto_manage"] = {"enabled": True, "disable_threshold": 75,
                                      "consecutive_runs": 1, "cooldown_hours": 24, **auto}
    return _parse_config(raw).prowlarr


@pytest.mark.parametrize("args", [
    (420, 0.01, 0.0, 1.0, False, None),
    (1900, 0.12, 0.05, 0.95, False, -2.5),
    (3200, 0.39, 0.0, 0.70, True, 1.0),
    (0, 0.0, 0.0, 1.0, False, None),
    (30000, 1.0, 1.0, 0.0, True, -10),            # floors at 0
])
def test_parts_add_up_to_the_score(args):
    cfg = _cfg()
    *signals, trend = args
    p = health_parts(*signals, cfg, trend)
    raw = sum(p[k]["score"] * p[k]["weight"] for k in ("speed", "searches", "downloads", "safety"))
    raw += p["paused"] + p["trend"]
    assert compute_health_score(*signals, cfg, trend) == round(max(0.0, min(100.0, raw)), 1)


def test_score_is_unchanged_by_the_refactor():
    """The formula as it was before parts existed, kept here verbatim."""
    import math
    from core.indexer_scorer import WORST_RESPONSE_MS
    cfg = _cfg()
    s = cfg.scoring
    for avg, fr, mr, gr, backoff, trend in [(420, .01, 0, 1, False, None), (1900, .12, .05, .95, True, -2.5)]:
        rt = max(0.0, 100.0 * (1.0 - math.log1p(avg) / math.log1p(WORST_RESPONSE_MS)))
        old = (rt * s.response_time_weight + (1 - fr) * 100 * s.failure_rate_weight
               + max(0.0, 100 - mr * 100 * s.malicious_penalty_per_hit) * s.malicious_weight
               + gr * 100 * s.grab_success_weight)
        old -= s.backoff_penalty if backoff else 0
        old += trend or 0
        assert compute_health_score(avg, fr, mr, gr, backoff, cfg, trend) == round(max(0, min(100, old)), 1)


def test_parts_for_display_is_plain():
    d = parts_for_display(health_parts(1900, 0.12, 0.05, 0.95, True, _cfg(), -2.46))
    assert set(d) == {"speed", "searches", "downloads", "safety", "paused", "trend"}
    assert all(isinstance(d[k]["score"], int) and isinstance(d[k]["weight_pct"], int)
               for k in ("speed", "searches", "downloads", "safety"))
    # Shares of the total, so they add up to 100 (give or take rounding) even
    # though this config's weights sum to 1.15.
    assert 99 <= sum(d[k]["weight_pct"] for k in ("speed", "searches", "downloads", "safety")) <= 101
    assert d["paused"] < 0 and d["trend"] == -2.5


# --- C-5: auto-disable --------------------------------------------------------------

class _Prowlarr:
    def __init__(self):
        self.changes = []

    def set_indexer_enabled(self, raw, on):
        self.changes.append((raw["id"], on))
        return True


class _State:
    def __init__(self):
        self.low, self.auto = {}, {}

    def get_auto_manage_state(self, iid):
        return self.auto.get(iid, {})

    def increment_consecutive_low(self, iid):
        self.low[iid] = self.low.get(iid, 0) + 1
        return self.low[iid]

    def reset_consecutive_low(self, iid):
        self.low[iid] = 0

    def mark_auto_disabled(self, iid):
        self.auto[iid] = {"disabled_by_auto": True}


def _row(iid, health, rule, ai):
    return {"id": iid, "name": f"ix{iid}", "health_score": health, "rule_score": rule,
            "ai_scored": ai, "has_enough_data": True, "_raw": {"id": iid}}


def test_the_ai_alone_cannot_disable_an_indexer():
    prowlarr, state = _Prowlarr(), _State()
    scorer = IndexerScorer(prowlarr, state, _cfg())
    out = scorer.auto_manage([
        _row(1, 60, 82, True),     # AI low, rules fine  -> kept
        _row(2, 60, 70, True),     # AI low, rules low   -> disabled
        _row(3, 60, 60, False),    # rules only, low     -> disabled (unchanged behaviour)
        _row(4, 90, 60, True),     # AI high, rules low  -> kept (score is above the line)
    ])
    assert out["disabled"] == ["ix2", "ix3"]
    assert prowlarr.changes == [(2, False), (3, False)]
    assert state.low.get(1) == 0 and state.low.get(4) == 0          # counters reset, not grown


def test_ai_clamp_is_recorded_for_the_page():
    """_apply_ai_scoring keeps what the AI wanted when the limit held it back."""
    import inspect
    src = inspect.getsource(IndexerScorer._apply_ai_scoring)
    assert 's["ai_wanted"] = raw' in src
