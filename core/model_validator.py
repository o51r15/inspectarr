"""
core/model_validator.py -- prove an Ollama model can actually do the scoring
job before it is allowed to become the active model.

Two failure modes motivated this, both previously discovered only in
production, days after selecting a model:

  1. Small-context models receive a prompt larger than their window, lose the
     instructions, and return fabricated data that looks like a valid score.
     (ROADMAP item 19.)
  2. Models return the right shape with the wrong key names, or echo the input
     back as "reasoning". (Commit 64ae65f, and the v1.5.0 echo bug.)

Neither raises. Both silently produce garbage scores. So validation asserts on
behaviour, not on "did the call succeed".

Tests run against the REAL scoring path (ollama_score_indexers), so the
parser, prompt and request shape under test are the ones production uses.
"""
import json
import logging
import time

from .llm_client import (ollama_score_indexers,
                         DEFAULT_CONTEXT_WINDOW,
                         MAX_INDEXERS_PER_CALL,
                         NEAR_TIMEOUT_WARN_FRACTION)

log = logging.getLogger("inspectarr")

# Thresholds. A model must separate a clearly-good indexer from a clearly-bad
# one by a wide margin -- ordering them correctly by two points would satisfy
# a naive check while being useless in practice.
GOOD_MIN   = 70
BAD_MAX    = 40
MIN_SPREAD = 25

# Batch sizes tried during in-depth calibration, largest first.
#
# Descending rather than ascending because the answer is usually near the top
# and we stop at the first size that works -- climbing would pay for every
# rung below it every time.
BATCH_LADDER = (25, 20, 15, 10, 5)

# How many times a candidate size must score cleanly before it is accepted.
#
# Two, not one, and this is the whole reason in-depth mode exists. Silent
# omission is stochastic: gemma:latest passed a single-pass validation at 39
# indexers and dropped 15 of 39 the next day, same host, same window. One
# clean run is not evidence a size is safe -- it is one sample of something
# that fails intermittently, and accepting it is how a confident, wrong
# calibration gets written to the database.
BATCH_PASSES_REQUIRED = 2


def _fixture_good(indexer_id: int = 9001) -> dict:
    """An indexer that is unambiguously healthy by every scoring signal."""
    return {
        "indexer_id": indexer_id,
        "name": "validation-fixture-good",
        "deterministic_health_score": 94.0,
        "avg_response_ms": 210,
        "weighted_success_rate_pct": 99.8,
        "grab_success_pct": 99.5,
        "total_queries": 4820, "failed_queries": 9,
        "total_grabs": 612, "failed_grabs": 3,
        "total_rss_queries": 1400, "failed_rss_queries": 2,
        "total_auth_queries": 40, "failed_auth_queries": 0,
        "malicious_hits": 0, "malicious_rate_pct": 0.0,
        "in_backoff": False, "total_records": 4820,
        "priority": 10, "trend": 2.0,
    }


def _fixture_bad(indexer_id: int = 9002) -> dict:
    """An indexer that is unambiguously unhealthy by every scoring signal."""
    return {
        "indexer_id": indexer_id,
        "name": "validation-fixture-bad",
        "deterministic_health_score": 14.0,
        "avg_response_ms": 6200,
        "weighted_success_rate_pct": 61.0,
        "grab_success_pct": 44.0,
        "total_queries": 2100, "failed_queries": 819,
        "total_grabs": 180, "failed_grabs": 101,
        "total_rss_queries": 600, "failed_rss_queries": 240,
        "total_auth_queries": 55, "failed_auth_queries": 22,
        "malicious_hits": 9, "malicious_rate_pct": 5.0,
        "in_backoff": True, "total_records": 2100,
        "priority": 25, "trend": -8.0,
    }


def _synthetic_indexers(count: int) -> list[dict]:
    """
    `count` plausible indexers spanning the quality range.

    Sized to the deployment's real indexer count so a pass means "works on
    this setup", not "works in the abstract".
    """
    out = []
    for i in range(count):
        f = i / max(count - 1, 1)          # 0.0 (good) .. 1.0 (bad)
        out.append({
            "indexer_id": 9100 + i,
            "name": f"validation-fixture-{i:02d}",
            "deterministic_health_score": round(92 - 70 * f, 1),
            "avg_response_ms": int(220 + 5200 * f),
            "weighted_success_rate_pct": round(99.5 - 36 * f, 1),
            "grab_success_pct": round(98.0 - 50 * f, 1),
            "total_queries": 3000 - int(900 * f), "failed_queries": int(700 * f),
            "total_grabs": 400 - int(220 * f), "failed_grabs": int(95 * f),
            "total_rss_queries": 900, "failed_rss_queries": int(210 * f),
            "total_auth_queries": 40, "failed_auth_queries": int(18 * f),
            "malicious_hits": int(8 * f), "malicious_rate_pct": round(5.0 * f, 1),
            "in_backoff": f > 0.85, "total_records": 3000 - int(900 * f),
            "priority": 10 + int(15 * f), "trend": round(3.0 - 10.0 * f, 1),
        })
    return out


def _warmup(ollama_url: str, model: str) -> dict:
    """
    Load the model before anything is timed, and measure the host while we
    are there.

    Without the load, the first test absorbs model-load time -- often tens of
    seconds -- and the reported response times say more about disk speed than
    about the model. Failure is non-fatal: if warmup cannot run, the tests
    still can, they are just slower.

    It also returns two host measurements, because this call has to happen
    anyway and they are free here:

      tok_per_s        generation throughput
      gpu_offload_pct  how much of the model Ollama actually put on the GPU

    The second is the one that explains the first. Ollama reports size_vram
    of 0 for a model it is running entirely on CPU, and there is no other
    honest way to tell a slow GPU from no GPU at all. A partial offload --
    common on AMD/ROCm where VRAM is tight -- lands in between, which is
    precisely the case a fixed default handles worst.

    num_predict is 24 rather than 1: a single token gives a token rate
    dominated by first-token latency, which is not the number we want.

    Returns {} when it cannot measure. Callers must treat every key as
    optional -- an unmeasurable host is not a failed validation.
    """
    out: dict = {}
    try:
        import requests
        base = ollama_url.rstrip("/")
        resp = requests.post(
            f"{base}/api/generate",
            json={"model": model, "prompt": "Ready.", "stream": False,
                  "options": {"num_predict": 24}},
            timeout=300,
        )
        data = resp.json() if resp.status_code == 200 else {}
        if data.get("eval_duration") and data.get("eval_count"):
            out["tok_per_s"] = round(
                data["eval_count"] / (data["eval_duration"] / 1e9), 1)
    except Exception as exc:
        log.debug(f"Model warmup failed (non-fatal): {exc}")

    try:
        import requests
        ps = requests.get(f"{ollama_url.rstrip('/')}/api/ps", timeout=30)
        for m in (ps.json().get("models", []) if ps.status_code == 200 else []):
            if m.get("name") != model:
                continue
            size = m.get("size") or 0
            if size > 0:
                out["gpu_offload_pct"] = round(
                    100.0 * (m.get("size_vram") or 0) / size)
            break
    except Exception as exc:
        log.debug(f"Could not read Ollama /api/ps (non-fatal): {exc}")

    return out


def _score_of(result: dict, indexer_id: int):
    entry = result.get(indexer_id) or result.get(str(indexer_id))
    if not isinstance(entry, dict):
        return None
    val = entry.get("health_score")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _test_discrimination_and_schema(url, model, timeout, prompt):
    """
    Tests 1 and 2 share one call -- they are two questions about one response,
    and a second round trip would only add latency.

    Discrimination: can the model tell a good indexer from a bad one, by a
    margin wide enough to be useful? Catches constant output and inverted
    scales, which a single-fixture check would pass.

    Schema: is every entry shaped the way the scorer expects? Catches echoed
    input and malformed scores.
    """
    good, bad = _fixture_good(), _fixture_bad()
    t0 = time.time()
    result = ollama_score_indexers([good, bad], url, model, timeout,
                                   custom_prompt=prompt)
    ms = round((time.time() - t0) * 1000)

    disc = {"name": "Discrimination", "passed": False, "response_ms": ms,
            "expected": f"good >= {GOOD_MIN}, bad <= {BAD_MAX}, "
                        f"spread >= {MIN_SPREAD}"}
    schema = {"name": "Schema compliance", "passed": False, "response_ms": ms,
              "expected": "every entry has a numeric health_score 0-100 and "
                          "non-empty reasoning"}

    if not result:
        disc["detail"] = "Model returned nothing the scorer could parse."
        schema["detail"] = "No parseable response."
        return disc, schema, ms

    g = _score_of(result, good["indexer_id"])
    b = _score_of(result, bad["indexer_id"])
    if g is None or b is None:
        disc["detail"] = f"Missing scores (good={g}, bad={b})."
    else:
        spread = g - b
        disc["good_score"], disc["bad_score"] = g, b
        disc["spread"] = round(spread, 1)
        disc["passed"] = (g >= GOOD_MIN and b <= BAD_MAX
                          and spread >= MIN_SPREAD)
        if disc["passed"]:
            disc["detail"] = f"good={g:.0f}, bad={b:.0f}, spread={spread:.0f}"
        elif spread < 0:
            disc["detail"] = (f"Scale inverted -- rated the bad indexer "
                              f"higher ({b:.0f}) than the good one ({g:.0f}).")
        elif spread < MIN_SPREAD:
            disc["detail"] = (f"Separated by only {spread:.0f} points "
                              f"(good={g:.0f}, bad={b:.0f}); needs "
                              f"{MIN_SPREAD}.")
        else:
            disc["detail"] = (f"good={g:.0f} (needs >= {GOOD_MIN}), "
                              f"bad={b:.0f} (needs <= {BAD_MAX}).")

    problems = []
    for iid, entry in result.items():
        if not isinstance(entry, dict):
            problems.append(f"{iid}: not an object")
            continue
        s = entry.get("health_score")
        try:
            s = float(s)
            if not 0 <= s <= 100:
                problems.append(f"{iid}: health_score {s} out of range")
        except (TypeError, ValueError):
            problems.append(f"{iid}: health_score not numeric ({s!r})")
        r = (entry.get("reasoning") or "").strip()
        if not r:
            problems.append(f"{iid}: empty reasoning")
        elif r.startswith("{") or '"indexer_id"' in r:
            problems.append(f"{iid}: reasoning echoes the input")
    schema["passed"] = not problems
    schema["detail"] = ("All entries well-formed." if not problems
                        else "; ".join(problems[:4]))
    return disc, schema, ms


def _test_context(url, model, timeout, prompt, indexer_count,
                  context_window=DEFAULT_CONTEXT_WINDOW):
    """
    Test 3: can the model handle a prompt the size of a real scoring run?

    This is the ROADMAP item 19 detector. A model whose context window is too
    small does not error -- it drops indexers, invents ids, or returns a
    fabricated example. Sized to the deployment's real indexer count, so
    passing means passing here.
    """
    data = _synthetic_indexers(indexer_count)
    approx_tokens = len(json.dumps(data)) // 4

    t0 = time.time()
    # Budget against the window the deployment actually configures, so a
    # pass here means "works on this setup" rather than "works at Ollama's
    # default". That is the entire premise of this test.
    result = ollama_score_indexers(data, url, model, timeout,
                                   custom_prompt=prompt,
                                   context_window=context_window)
    ms = round((time.time() - t0) * 1000)

    out = {"name": f"Context capacity ({indexer_count} indexers)",
           "passed": False, "response_ms": ms,
           "requested": indexer_count,
           "approx_prompt_tokens": approx_tokens,
           "expected": f"all {indexer_count} indexers scored, no invented ids"}

    if not result:
        out["returned"] = 0
        out["detail"] = ("Returned nothing parseable -- typical of a context "
                         "window too small for this many indexers.")
        return out, ms

    wanted = {d["indexer_id"] for d in data}
    got = set()
    for k in result:
        try:
            got.add(int(k))
        except (TypeError, ValueError):
            pass
    missing, invented = wanted - got, got - wanted
    scored_ok = sum(1 for i in wanted if _score_of(result, i) is not None)

    out["returned"] = len(got)
    out["scored_ok"] = scored_ok
    out["passed"] = not missing and not invented and scored_ok == indexer_count
    if out["passed"]:
        out["detail"] = (f"All {indexer_count} scored "
                         f"(~{approx_tokens} prompt tokens).")
    else:
        bits = []
        if missing:
            bits.append(f"{len(missing)} indexer(s) missing from the response")
        if invented:
            bits.append(f"{len(invented)} id(s) not in the request "
                        f"(model invented data)")
        if scored_ok < indexer_count - len(missing):
            bits.append("some entries had no usable score")
        bits.append(f"~{approx_tokens} prompt tokens -- raise the model's "
                    f"context window (16k+ recommended)")
        out["detail"] = "; ".join(bits)
    return out, ms


def _probe_batch(url, model, timeout, prompt, indexer_count,
                 context_window, batch) -> dict:
    """
    Score `indexer_count` indexers with calls of at most `batch` each, and
    report whether the result was complete AND comfortably inside the timeout.

    Both conditions matter and they fail differently. A size can be correct
    and still be wrong to ship: 25 indexers per call scored 39/39 on the CPU
    host here and took 269-295s against a 300s timeout, which is a batch that
    works right up until the host is slightly busier.

    per_call_s is the measured total divided by the number of calls. That is
    an average rather than the true worst call -- ollama_score_indexers does
    not report per-call timings, and threading that through for a diagnostic
    would put test-only plumbing in the scoring path. The average is slightly
    optimistic (the last batch is usually the small remainder), which is worth
    knowing when reading a borderline result.
    """
    data = _synthetic_indexers(indexer_count)
    wanted = {d["indexer_id"] for d in data}
    calls = max(1, -(-indexer_count // max(1, batch)))

    t0 = time.time()
    result = ollama_score_indexers(data, url, model, timeout,
                                   custom_prompt=prompt,
                                   context_window=context_window,
                                   max_indexers_per_call=batch)
    elapsed = time.time() - t0

    got = set()
    for k in (result or {}):
        try:
            got.add(int(k))
        except (TypeError, ValueError):
            pass
    scored_ok = sum(1 for i in wanted if _score_of(result or {}, i) is not None)
    per_call = elapsed / calls

    out = {
        "batch": batch,
        "scored_ok": scored_ok,
        "missing": len(wanted - got),
        "invented": len(got - wanted),
        "elapsed_s": round(elapsed, 1),
        "per_call_s": round(per_call, 1),
        "calls": calls,
    }
    out["complete"] = (scored_ok == indexer_count and not out["invented"])
    out["within_timeout"] = (
        not timeout or per_call <= timeout * NEAR_TIMEOUT_WARN_FRACTION)
    out["passed"] = out["complete"] and out["within_timeout"]
    return out


def _calibrate_batch(url, model, timeout, prompt, indexer_count,
                     context_window, ceiling, progress_cb=None) -> dict:
    """
    Find the largest batch size this (host, model) pair handles reliably.

    Descends BATCH_LADDER and returns the first size that passes
    BATCH_PASSES_REQUIRED times in a row. Never raises; on total failure it
    reports the smallest rung tried and lets the caller decide, because
    "we could not measure" and "this model is broken" are different answers
    and only validate_model's other tests can tell them apart.
    """
    rungs = [b for b in BATCH_LADDER if b <= min(ceiling, indexer_count)]
    if not rungs:
        rungs = [max(1, min(ceiling, indexer_count))]

    out = {"name": "Batch calibration", "passed": False,
           "ladder": rungs, "attempts": [], "max_safe_batch": None,
           "expected": (f"largest batch scoring {indexer_count}/{indexer_count} "
                        f"cleanly {BATCH_PASSES_REQUIRED}x in a row")}

    for step, batch in enumerate(rungs):
        if progress_cb:
            progress_cb(f"Calibrating batch size ({batch})", step, len(rungs))
        runs = []
        for _ in range(BATCH_PASSES_REQUIRED):
            attempt = _probe_batch(url, model, timeout, prompt,
                                   indexer_count, context_window, batch)
            out["attempts"].append(attempt)
            runs.append(attempt)
            if not attempt["passed"]:
                break            # a failed rung needs no second opinion
        if all(r["passed"] for r in runs) and len(runs) == BATCH_PASSES_REQUIRED:
            out["max_safe_batch"] = batch
            out["passed"] = True
            worst = max(r["per_call_s"] for r in runs)
            out["detail"] = (
                f"{batch} per call: {indexer_count}/{indexer_count} scored "
                f"{BATCH_PASSES_REQUIRED}x, worst call ~{worst:.0f}s of a "
                f"{timeout}s timeout.")
            return out

    last = out["attempts"][-1] if out["attempts"] else {}
    why = []
    if last.get("missing"):
        why.append(f"{last['missing']} indexer(s) dropped")
    if last.get("invented"):
        why.append(f"{last['invented']} id(s) invented")
    if last and not last.get("within_timeout", True):
        why.append(f"~{last.get('per_call_s')}s per call against a "
                   f"{timeout}s timeout")
    out["detail"] = (
        f"No batch size down to {rungs[-1]} was reliable"
        + (" (" + "; ".join(why) + ")" if why else "")
        + ". Try a smaller model, or raise prowlarr.ollama.timeout.")
    return out


def validate_model(ollama_url: str, model: str, timeout: int = 300,
                   indexer_count: int = 20, system_prompt: str = "",
                   progress_cb=None,
                   context_window: int = DEFAULT_CONTEXT_WINDOW,
                   calibration: str = "quick",
                   max_indexers_per_call: int = MAX_INDEXERS_PER_CALL) -> dict:
    """
    Run the full validation suite. Never raises -- a validation run must not
    be able to take down the caller, and every failure mode is a result the
    user needs to see rather than an exception.

    progress_cb(stage: str, done: int, total: int) is called before each test
    so a UI can show which stage is running.

    Returns a dict suitable for storage and for rendering directly.
    """
    started = time.time()
    # ollama_url is recorded in the result because a verdict belongs to a
    # (host, model) pair. Storing it here rather than only at the call site
    # means anything that persists a result carries the host with it by
    # default, instead of each caller having to remember.
    out = {"model": model, "indexer_count": indexer_count,
           "context_window": context_window, "ollama_url": ollama_url,
           "tests": [], "passed": False, "avg_response_ms": 0}

    if not ollama_url or not model:
        out["error"] = "Ollama URL and model are both required."
        return out

    indexer_count = max(1, min(int(indexer_count or 20), 200))
    out["indexer_count"] = indexer_count

    try:
        if progress_cb:
            progress_cb("Loading model", 0, 3)
        out.update(_warmup(ollama_url, model))

        if progress_cb:
            progress_cb("Discrimination + schema", 1, 3)
        disc, schema, ms1 = _test_discrimination_and_schema(
            ollama_url, model, timeout, system_prompt)

        if progress_cb:
            progress_cb("Context capacity", 2, 3)
        ctx, ms2 = _test_context(ollama_url, model, timeout,
                                 system_prompt, indexer_count,
                                 context_window=context_window)

        out["tests"] = [disc, schema, ctx]

        # In-depth calibration only runs once the model has shown it can
        # score at all. Searching for the best batch size of a model that
        # cannot produce a usable score is minutes spent proving nothing --
        # every rung would fail for the same reason, and the report would
        # blame batch size for a discrimination problem.
        if calibration == "deep" and disc["passed"] and schema["passed"]:
            cal = _calibrate_batch(ollama_url, model, timeout, system_prompt,
                                   indexer_count, context_window,
                                   ceiling=max_indexers_per_call,
                                   progress_cb=progress_cb)
            out["tests"].append(cal)
            out["max_safe_batch"] = cal.get("max_safe_batch")
        elif ctx["passed"]:
            # Quick mode proved the CONFIGURED size works, which is a weaker
            # claim than "this is the largest safe size" and is recorded as
            # such. calibrated stays False so the UI can offer the real thing.
            out["max_safe_batch"] = min(max_indexers_per_call, indexer_count)

        out["calibration"] = calibration
        out["calibrated"] = bool(calibration == "deep"
                                 and out.get("max_safe_batch"))

        if progress_cb:
            progress_cb("Done", 3, 3)

        out["passed"] = all(t["passed"] for t in out["tests"])
        out["avg_response_ms"] = round((ms1 + ms2) / 2)
    except Exception as exc:
        # Defensive: the individual tests already swallow their own failures,
        # so reaching here means something unexpected. Report it as a failed
        # validation rather than propagating.
        log.warning(f"Model validation crashed for {model}: {exc}")
        out["error"] = str(exc)
        out["passed"] = False

    out["duration_ms"] = round((time.time() - started) * 1000)
    return out
