"""
The graded quality test: expected ranges come from the scoring prompt's
points table, and grading rewards being in range, ranking in the right
order, and giving real reasons.
"""
from core import model_quality as q, model_validator


def _answer(fn, reason="Reliable, fast, and nothing suspicious seen."):
    return {c["row"]["indexer_id"]: {"health_score": fn(c), "reasoning": reason}
            for c in q.CASES}


def _mid(c):
    return sum(c["expect"]) / 2


# --- the points table -------------------------------------------------------------

def test_table_matches_the_prompt():
    assert q.table_points(99.8, 210, 0, False) == (100, 100)
    assert q.table_points(99.6, 4200, 0, False) == (80, 80)        # 3-5 s: 5 pts
    assert q.table_points(99.7, 250, 1, False) == (92, 92)         # -8 per hit
    assert q.table_points(99.7, 250, 4, False) == (75, 75)         # trust floors at 0
    assert q.table_points(99.6, 300, 0, True) == (90, 90)          # backoff: no availability
    assert q.table_points(70, 6000, 3, True) == (1, 11)            # <80%: 0-10 pts


def test_the_test_is_balanced():
    lows = [c for c in q.CASES if c["expect"][1] < 40]
    highs = [c for c in q.CASES if c["expect"][0] >= 80]
    assert lows and len(highs) >= 3
    assert len({c["row"]["indexer_id"] for c in q.CASES}) == len(q.CASES)


def test_rows_are_not_sent_best_to_worst():
    ids = [r["indexer_id"] for r in q.quality_indexers()]
    assert sorted(ids) == sorted(c["row"]["indexer_id"] for c in q.CASES)
    mids = {c["row"]["indexer_id"]: _mid(c) for c in q.CASES}
    sent = [mids[i] for i in ids]
    assert sent != sorted(sent) and sent != sorted(sent, reverse=True)


def test_names_do_not_give_the_answer_away():
    for row in q.quality_indexers():
        assert row["name"].startswith("quality-test-")


def test_rows_carry_every_field_production_sends():
    from core.llm_client import SHORT_KEYS
    for row in q.quality_indexers():
        assert set(SHORT_KEYS) <= set(row), set(SHORT_KEYS) - set(row)


# --- grading ----------------------------------------------------------------------

def test_perfect_answer_scores_100_and_passes():
    g = q.grade(_answer(_mid))
    assert g["score"] == 100 and g["passed"] and g["complete"]


def test_reversed_answer_fails():
    g = q.grade(_answer(lambda c: 100 - _mid(c)))
    assert g["score"] < 30 and not g["passed"]


def test_same_score_for_everything_fails():
    # A model that does not read the data must not pass on lucky ranges.
    g = q.grade(_answer(lambda c: 80))
    assert g["order"] == 0 and not g["passed"]


def test_gpt_oss_low_pattern_is_marked_down():
    # Measured: right order, but the worst indexer at 60 ("fine").
    g = q.grade(_answer(lambda c: 60 + 0.4 * _mid(c)))
    failing = next(x for x in g["cases"] if x["case"].startswith("failing"))
    assert failing["points"] == 0 and "expected 0-19" in failing["note"]
    assert g["order"] > 0.9 and g["score"] < 100


def test_near_miss_gets_half_marks():
    case = q.CASES[0]
    lo = case["expect"][0]
    answer = _answer(_mid)
    answer[case["row"]["indexer_id"]]["health_score"] = lo - 5
    g = q.grade(answer)
    assert g["cases"][0]["points"] == 0.5


def test_a_missing_indexer_fails_however_good_the_rest():
    answer = _answer(_mid)
    answer.pop(q.CASES[3]["row"]["indexer_id"])
    g = q.grade(answer)
    assert g["missing"] == 1 and not g["complete"] and not g["passed"]
    assert g["score"] > 90           # the grade still says how good the rest was


def test_invented_indexer_fails():
    answer = _answer(_mid)
    answer[123456] = {"health_score": 50, "reasoning": "An indexer nobody asked about."}
    assert not q.grade(answer)["passed"]


def test_echoed_or_empty_reasons_lose_marks():
    g = q.grade(_answer(_mid, reason='{"indexer_id": 9301, "ms": 210}'))
    assert g["reasons_pct"] == 0 and g["score"] == 85


def test_string_ids_and_scores_are_read():
    answer = {str(k): {"health_score": str(int(v["health_score"])), "reasoning": v["reasoning"]}
              for k, v in _answer(_mid).items()}
    assert q.grade(answer)["complete"]


def test_nothing_returned_scores_zero():
    g = q.grade({})
    assert g["score"] == 0 and g["missing"] == len(q.CASES)


def test_rank_correlation():
    assert q.rank_correlation([1, 2, 3], [10, 20, 30]) == 1
    assert q.rank_correlation([1, 2, 3], [30, 20, 10]) == -1
    assert q.rank_correlation([5, 5, 5], [1, 2, 3]) == 0


# --- wired into Calibrate -----------------------------------------------------------



def test_quality_test_uses_the_built_in_prompt_and_one_call(monkeypatch):
    seen = {}

    def fake(rows, url, model, timeout, custom_prompt="", context_window=0,
             max_indexers_per_call=0, think=None, stats=None):
        seen.update(prompt=custom_prompt, batch=max_indexers_per_call, n=len(rows))
        return {}
    monkeypatch.setattr(q, "ollama_score_indexers", fake)
    out = q.run_quality_test("http://x", "m", 60, 8192)
    from core.llm_client import SYSTEM_PROMPT
    assert seen == {"prompt": SYSTEM_PROMPT, "batch": len(q.CASES), "n": len(q.CASES)}
    assert not out["passed"] and "nothing" in out["detail"]


def test_a_model_that_repeats_the_reference_is_labelled():
    copy = {c["row"]["indexer_id"]: {"health_score": c["row"]["deterministic_health_score"],
                                     "reasoning": "Matches the reference health score."}
            for c in q.CASES}
    g = q.grade(copy)
    assert g["follows_reference"] and g["reference_gap"] < 1


def test_a_model_that_judges_is_not_labelled():
    g = q.grade(_answer(_mid))
    assert not g["follows_reference"]


def test_pass_mark_is_70():
    assert q.PASS_SCORE == 70
