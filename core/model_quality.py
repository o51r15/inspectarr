"""
core/model_quality.py -- how well does a model score indexers? (0-100)

The scoring prompt (llm_client.SYSTEM_PROMPT) is a fixed points table:

    reliability 40 | speed 25 | trust 25 | availability 10

so for a test indexer Python can work out the score a model SHOULD give,
and grade what it actually gives. That makes the grade objective, the way
DockLlama's eight scenarios are, instead of "did it answer at all".

Fourteen test indexers, each built to test one rule of the table: spotless,
slow, one malicious hit, in backoff, failing logins, too little data to
judge, and so on. Ten follow the table exactly and get a tight range around
the table's answer. Four test the prompt's judgement rules ("penalize
heavily", "pull toward 50", "do not heavily penalize") and get a wider range,
because the prompt itself leaves room there.

Grade, out of 100:

    accuracy 60  each indexer: inside its range = full marks, within 10
                 points of it = half, otherwise none
    order    25  do the scores rank the indexers in the right order?
                 (rank correlation with the middle of each range)
    reasons  15  a real one-sentence reason for each, not empty and not
                 a copy of the input

An indexer missing from the answer scores nothing, and the test fails: a
model that drops indexers cannot be trusted with a real run however good the
rest is.

Always graded with the BUILT-IN prompt, whatever the user has customised:
the ranges come from its table, and scores must stay comparable between
models and over time.
"""
from __future__ import annotations

import logging
import time

from .llm_client import SYSTEM_PROMPT, ollama_score_indexers

log = logging.getLogger("inspectarr")

TOLERANCE = 8          # either side of the table's answer, for exact cases
NEAR = 10              # "near miss" distance for half marks
PASS_SCORE = 70        # below this a model is not fit to score indexers
# A model whose scores sit this close to Inspectarr's own (non-AI) score on
# average is mostly copying the reference it is given, not judging. Measured:
# llama3-groq-tool-use and mistral:7b returned it almost to the decimal.
FOLLOWS_REFERENCE_WITHIN = 4.0
CASE_ID_BASE = 9300

WEIGHT_ACCURACY, WEIGHT_ORDER, WEIGHT_REASONS = 60, 25, 15


# --- the points table, in Python ----------------------------------------------

def table_points(succ: float, ms: float, malicious: int, in_backoff: bool) -> tuple[int, int]:
    """(lowest, highest) score the prompt's points table gives.

    A range rather than one number only because the table says "<80%:
    0-10pts" for reliability; every other line is exact.
    """
    if succ >= 99:
        rel = (40, 40)
    elif succ >= 95:
        rel = (35, 35)
    elif succ >= 90:
        rel = (25, 25)
    elif succ >= 80:
        rel = (15, 15)
    else:
        rel = (0, 10)
    if ms < 500:
        perf = 25
    elif ms < 1000:
        perf = 20
    elif ms < 2000:
        perf = 15
    elif ms < 3000:
        perf = 10
    elif ms <= 5000:
        perf = 5
    else:
        perf = 0
    trust = max(0, 25 - 8 * malicious)
    avail = 0 if in_backoff else 10
    rest = perf + trust + avail
    return rel[0] + rest, rel[1] + rest


# --- the test indexers --------------------------------------------------------

def _indexer(n: int, succ: float, ms: int, malicious: int = 0, backoff: bool = False,
             queries: int = 3000, grabs: int = 400, grab_success: float | None = None,
             auth: int = 40, auth_failed: int = 0, rss: int = 900) -> dict:
    """One test indexer with every field a real one carries, consistent
    with the headline numbers (failed counts follow the success rate)."""
    gs = succ if grab_success is None else grab_success
    failed_q = round(queries * (100 - succ) / 100)
    failed_g = round(grabs * (100 - gs) / 100)
    row = {
        "indexer_id": CASE_ID_BASE + n,
        # A neutral name: "spotless" or "failing" would hand the model the
        # answer, and a real indexer's name says nothing about its health.
        "name": f"quality-test-{n:02d}",
        "avg_response_ms": ms,
        "weighted_success_rate_pct": succ,
        "grab_success_pct": gs,
        "total_queries": queries, "failed_queries": failed_q,
        "total_grabs": grabs, "failed_grabs": failed_g,
        "total_rss_queries": rss, "failed_rss_queries": round(rss * (100 - succ) / 100),
        "total_auth_queries": auth, "failed_auth_queries": auth_failed,
        "malicious_hits": malicious,
        "malicious_rate_pct": round(100.0 * malicious / grabs, 2) if grabs else 0.0,
        "in_backoff": backoff, "total_records": queries,
        "priority": 25, "trend": 0.0,
    }
    row["deterministic_health_score"] = _deterministic(row)
    return row


def _deterministic(row: dict) -> float | None:
    """What Inspectarr's own (non-AI) scorer would say, so the test input
    looks exactly like production input, reference score included."""
    try:
        from .config import ProwlarrConfig
        from .indexer_scorer import compute_health_score
        grabs = row["total_grabs"] or 1
        return compute_health_score(
            row["avg_response_ms"], 1 - row["weighted_success_rate_pct"] / 100,
            row["malicious_hits"] / grabs, row["grab_success_pct"] / 100,
            row["in_backoff"], ProwlarrConfig())
    except Exception as exc:       # never let the reference break the test
        log.debug(f"deterministic reference unavailable: {exc}")
        return None


def _exact(n, label, **kw):
    """A case the table decides exactly: its answer, plus or minus TOLERANCE."""
    row = _indexer(n, **kw)
    lo, hi = table_points(kw["succ"], kw["ms"], kw.get("malicious", 0), kw.get("backoff", False))
    return {"row": row, "label": label, "expect": (max(0, lo - TOLERANCE), min(100, hi + TOLERANCE))}


def _judged(n, label, lo, hi, **kw):
    """A case where the prompt asks for judgement: a wider, fixed range."""
    return {"row": _indexer(n, **kw), "label": label, "expect": (lo, hi)}


CASES = [
    _exact(1, "spotless", succ=99.8, ms=210),
    _exact(2, "reliable but slow (4.2 s)", succ=99.6, ms=4200),
    _exact(3, "reliable but crawling (6.5 s)", succ=99.5, ms=6500),
    _exact(4, "one malicious hit", succ=99.7, ms=250, malicious=1),
    _exact(5, "four malicious hits", succ=99.7, ms=250, malicious=4),
    _exact(6, "in backoff", succ=99.6, ms=300, backoff=True),
    _exact(7, "good (96.5%, 0.8 s)", succ=96.5, ms=800),
    _exact(8, "middling (92%, 1.5 s)", succ=92.0, ms=1500),
    _exact(9, "weak (85%, 2.5 s)", succ=85.0, ms=2500),
    _exact(10, "failing, slow, malicious, in backoff", succ=70.0, ms=6000,
           malicious=3, backoff=True),
    # Table says 95; "auth failures -- penalize heavily" says well below.
    _judged(11, "half its logins fail", 45, 85, succ=97.0, ms=400,
            auth=40, auth_failed=20),
    # Table says 100 on query success; "grab failures weigh more heavily".
    _judged(12, "grabs fail 40% of the time", 50, 90, succ=99.2, ms=350,
            grab_success=60.0),
    # Table says 100; "low data -- score conservatively, pull toward 50".
    _judged(13, "only 12 queries ever", 45, 90, succ=100.0, ms=300,
            queries=12, grabs=1, auth=2, rss=6),
    # Table says 100; "RSS-heavy with few grabs: do not heavily penalize".
    _judged(14, "almost all RSS, 3 grabs", 85, 100, succ=99.6, ms=300,
            queries=2100, rss=2000, grabs=3, grab_success=100.0),
]

# Fixed, mixed order. Sorted best-to-worst, a model could score by position
# and look perfect without reading anything.
PRESENTATION_ORDER = [7, 1, 12, 3, 10, 5, 14, 2, 9, 11, 4, 13, 6, 8]


def quality_indexers() -> list[dict]:
    """The rows sent to the model, in presentation order."""
    by_n = {c["row"]["indexer_id"] - CASE_ID_BASE: c["row"] for c in CASES}
    return [by_n[n] for n in PRESENTATION_ORDER]


# --- grading ------------------------------------------------------------------

def _ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean rank), for Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def rank_correlation(a: list[float], b: list[float]) -> float:
    """Spearman's rho: 1 = same order, 0 = unrelated, -1 = reversed.
    0 when either side has no variation (a model giving everything 80
    has told us nothing about order)."""
    if len(a) < 2:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb) ** 0.5


def _valid_reason(text) -> bool:
    text = (text or "").strip() if isinstance(text, str) else ""
    return len(text) >= 15 and not text.startswith("{") and '"indexer_id"' not in text


def grade(result: dict) -> dict:
    """Grade a scorer result {indexer_id: {"health_score", "reasoning"}}
    against CASES. Pure: no I/O, so it is tested directly."""
    result = result or {}
    cases, accuracy, reasons = [], 0.0, 0
    got_scores, mids, ref_gaps = [], [], []
    for c in CASES:
        iid = c["row"]["indexer_id"]
        entry = result.get(iid) or result.get(str(iid))
        lo, hi = c["expect"]
        item = {"case": c["label"], "expected": [lo, hi], "got": None, "points": 0.0}
        score = None
        if isinstance(entry, dict):
            try:
                score = float(entry.get("health_score"))
            except (TypeError, ValueError):
                score = None
        if score is None:
            item["note"] = "missing from the answer"
        else:
            item["got"] = round(score)
            if lo <= score <= hi:
                item["points"] = 1.0
            elif lo - NEAR <= score <= hi + NEAR:
                item["points"] = 0.5
                item["note"] = f"said {score:.0f}, expected {lo}-{hi} (close)"
            else:
                item["note"] = f"said {score:.0f}, expected {lo}-{hi}"
            got_scores.append(score)
            mids.append((lo + hi) / 2)
            ref = c["row"].get("deterministic_health_score")
            if ref is not None:
                ref_gaps.append(abs(score - ref))
            if _valid_reason(entry.get("reasoning")):
                reasons += 1
            else:
                item["note"] = (item.get("note", "") + "; " if item.get("note") else "") + "no usable reason"
        accuracy += item["points"]
        cases.append(item)

    n = len(CASES)
    missing = sum(1 for c in cases if c["got"] is None)
    rho = rank_correlation(got_scores, mids) if len(got_scores) >= 2 else 0.0
    score = (WEIGHT_ACCURACY * accuracy / n
             + WEIGHT_ORDER * max(0.0, rho)
             + WEIGHT_REASONS * reasons / n)
    expected_ids = {c["row"]["indexer_id"] for c in CASES}
    invented = 0
    for k in result:
        try:
            invented += int(k) not in expected_ids
        except (TypeError, ValueError):
            invented += 1
    out = {
        "score": round(score, 1),
        "accuracy_pct": round(100 * accuracy / n),
        "order": round(rho, 2),
        "reasons_pct": round(100 * reasons / n),
        "missing": missing,
        "invented": invented,
        "complete": missing == 0 and invented == 0,
        "cases": cases,
        # How far, on average, from Inspectarr's own score. Small means the
        # model mostly repeats the reference: its "AI score" adds little.
        "reference_gap": round(sum(ref_gaps) / len(ref_gaps), 1) if ref_gaps else None,
    }
    out["follows_reference"] = (out["reference_gap"] is not None
                                and out["reference_gap"] <= FOLLOWS_REFERENCE_WITHIN)
    out["passed"] = out["complete"] and out["score"] >= PASS_SCORE
    return out


def run_quality_test(url: str, model: str, timeout: int, context_window: int,
                     think=None, stats: list | None = None) -> dict:
    """Score the test indexers in one call and grade the answer. Never raises.

    One call, all fourteen: quality is measured separately from batch size
    (calibration's job), and fourteen short rows fit any context from 4K up.
    """
    rows = quality_indexers()
    t0 = time.time()
    try:
        result = ollama_score_indexers(rows, url, model, timeout,
                                       custom_prompt=SYSTEM_PROMPT,
                                       context_window=context_window,
                                       max_indexers_per_call=len(rows),
                                       think=think, stats=stats)
    except Exception as exc:                 # ollama_score_indexers should not raise
        log.warning(f"Quality test crashed for {model}: {exc}")
        result = {}
    elapsed = time.time() - t0
    out = {"name": "Quality", **grade(result),
           "elapsed_s": round(elapsed, 1),
           "sec_per_indexer": round(elapsed / len(rows), 2),
           "expected": f"{PASS_SCORE}+ out of 100, every indexer scored"}
    if not result:
        out["detail"] = "The model returned nothing that could be read."
    elif not out["complete"]:
        out["detail"] = (f"{out['score']:.0f}/100, but {out['missing']} indexer(s) missing"
                         + (f" and {out['invented']} invented" if out["invented"] else "")
                         + " -- not safe to use.")
    else:
        worst = [c for c in out["cases"] if c["points"] == 0][:3]
        out["detail"] = (f"{out['score']:.0f}/100 (accuracy {out['accuracy_pct']}%, "
                         f"order {out['order']:+.2f}, reasons {out['reasons_pct']}%)"
                         + ("; " + "; ".join(f"{c['case']}: {c['note']}" for c in worst)
                            if worst else ""))
        if out["follows_reference"]:
            out["detail"] += ("; mostly repeats Inspectarr's own score "
                              f"(within {out['reference_gap']:.0f} points on average)")
    return out
