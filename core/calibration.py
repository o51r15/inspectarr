"""
core/calibration.py -- find the best way to run one model on one server.

Calibrate answers four questions about a (server, model) pair, by measuring
rather than guessing:

  1. Should it think, and how much?
       Thinking off and on (or each level, for gpt-oss), graded twice each
       with the quality test. Measured on .125: gemma4:latest scored 65 with
       thinking off and 91 with it on; gpt-oss 68 at "low" and 88-97 at
       "medium". The best average wins; among results within 2 points of
       it, the faster one (DockLlama's rule).
  2. How many indexers per call?
       The user's real indexer count split evenly (37 -> 37, 19, 13, 10...):
       19 per call and 25 per call both take two calls, so 19 is as fast and
       asks less of the model. Largest size that scores every indexer
       cleanly twice in a row wins.
  3. What context size?
       Start at 8K (16K when thinking, which needs room). Go up a step only
       if room was what stopped a bigger batch, and only if the model stays
       on the graphics card. Go down a step only if it saves memory for the
       same batch. Batch sizes are sized from the tokens Ollama actually
       reported, not from an estimate.
  4. What timeout?
       2.5 times the slowest call measured, kept between 120 and 900 seconds.

The answer is a settings dict that "Use this model" applies, plus enough of
the evidence to show why. Never raises.
"""
from __future__ import annotations

import logging
import math
import time

from .llm_client import (SYSTEM_PROMPT, _batch_size, _encode_payload,
                         _estimate_tokens, _key_legend)
from .model_quality import PASS_SCORE, run_quality_test
from .ollama_models import DEFAULT_CONTEXT, model_profile

log = logging.getLogger("inspectarr")

QUALITY_RUNS = 2            # one run is not evidence: gpt-oss scored 97 then 88
WITHIN_POINTS = 2.0         # results this close count as a tie; faster wins
CAL_TIMEOUT = 900           # per call while measuring; the result sets the real one
THINK_CONTEXT = 16384       # thinking needs room: at 8K gemma4 filled it and stopped
CONTEXT_STEPS = (4096, 8192, 16384, 32768)
MAX_BATCH, MIN_BATCH = 50, 5
BATCH_PASSES = 2            # silent omission is intermittent: a size must pass twice
ROOM_MARGIN = 0.9           # a batch must need at most 90% of the context
# Thinking makes call times vary run to run (gemma4:latest: 34 s and 45 s for
# the same batch), so the margin is generous; 120 s is DockLlama's default.
TIMEOUT_FACTOR, TIMEOUT_FLOOR, TIMEOUT_CEILING = 2.5, 120, 900
SAME_SPEED = 0.10           # within 10% counts as the same speed: smaller context wins


# --- what to try ---------------------------------------------------------------

def thinking_options(profile: dict | None) -> list:
    """Think values to try, cheapest first."""
    mode = (profile or {}).get("thinking", "none")
    levels = (profile or {}).get("think_levels") or []
    if mode == "levels":
        return [False if lv == "off" else lv for lv in levels]
    if mode == "toggle":
        return [False, True]
    return [None]


def thinks(value) -> bool:
    return value not in (None, False)


def even_splits(n: int, cap: int = MAX_BATCH, floor: int = MIN_BATCH) -> list[int]:
    """Batch sizes that split n indexers evenly into 1, 2, 3... calls,
    largest first, each the smallest size giving that number of calls."""
    if n <= floor:
        return [max(1, n)]
    out = []
    for calls in range(1, n + 1):
        size = math.ceil(n / calls)
        if size < floor:
            break
        if size <= cap and size not in out:
            out.append(size)
    return out or [floor]


def suggested_timeout(worst_call_s: float | None) -> int:
    if not worst_call_s:
        return 300
    return int(max(TIMEOUT_FLOOR, min(TIMEOUT_CEILING, math.ceil(TIMEOUT_FACTOR * worst_call_s))))


def context_for_thinking(think, max_context: int | None) -> int:
    want = THINK_CONTEXT if thinks(think) else DEFAULT_CONTEXT
    return min(want, max_context) if max_context else want


# --- measuring the need -------------------------------------------------------

class TokenModel:
    """How many tokens a batch of b indexers needs, from what Ollama reported.

    prompt: our estimate scaled by how far off it was on a real call
    answer: measured tokens written per indexer (thinking included)
    """

    def __init__(self, stats: list, prompt_rows: list[dict]):
        real = [s for s in stats if s.get("prompt_tokens") and s.get("answer_tokens") and s.get("n")]
        est = _estimate_tokens(SYSTEM_PROMPT + "\n" + _key_legend() + "Indexer data:\n"
                               + _encode_payload(prompt_rows))
        if real:
            self.scale = max(0.3, min(3.0, real[0]["prompt_tokens"] / max(1, est)))
            self.answer_per_indexer = max(r["answer_tokens"] / r["n"] for r in real)
        else:
            self.scale, self.answer_per_indexer = 1.0, 150.0

    def need(self, rows: list[dict]) -> int:
        prompt = _estimate_tokens(SYSTEM_PROMPT + "\n" + _key_legend() + "Indexer data:\n"
                                  + _encode_payload(rows))
        return int(self.scale * prompt + self.answer_per_indexer * len(rows) + 256)


def think_label(value) -> str:
    if value is None:
        return "n/a (the model does not think)"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def ctx_label(ctx: int) -> str:
    return f"{ctx // 1024}K" if ctx % 1024 == 0 else f"{ctx:,}"


# --- the search -----------------------------------------------------------------

class Calibration:
    """One calibration run. Holds the measurements so each step can use the
    last one's; the public entry point is calibrate()."""

    def __init__(self, url, model, indexer_count, progress=None):
        from .model_validator import _synthetic_indexers   # avoids an import cycle
        self.url, self.model = url, model
        self.n = max(1, min(int(indexer_count or 20), 200))
        self.rows = _synthetic_indexers(self.n)
        self.wanted = {r["indexer_id"] for r in self.rows}
        self.progress = progress or (lambda *a: None)
        self.profile = model_profile(url, model)
        self.max_context = (self.profile or {}).get("max_context")
        self.step, self.steps = 0, 1
        self.overhead = _estimate_tokens(SYSTEM_PROMPT + _key_legend() + "\nIndexer data:\n")

    def _tick(self, stage):
        self.progress(stage, min(self.step, self.steps - 1), self.steps)
        self.step += 1

    # 1. thinking ----------------------------------------------------------------
    def quality(self, think) -> dict:
        from .model_validator import _warmup
        ctx = context_for_thinking(think, self.max_context)
        _warmup(self.url, self.model, ctx)
        runs, stats = [], []
        for i in range(QUALITY_RUNS):
            self._tick(f"Quality test, thinking {think_label(think)} ({i + 1}/{QUALITY_RUNS})")
            r = run_quality_test(self.url, self.model, CAL_TIMEOUT, ctx, think=think, stats=stats)
            runs.append(r)
            if not r["complete"]:
                break
        scores = [r["score"] for r in runs]
        complete = len(runs) == QUALITY_RUNS and all(r["complete"] for r in runs)
        cut_off = any(s.get("done_reason") == "length" for s in stats)
        return {
            "think": think, "context_window": ctx,
            "runs": scores, "complete": complete, "cut_off": cut_off,
            "avg": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "spread": round(max(scores) - min(scores), 1) if scores else 0.0,
            "sec_per_indexer": round(sum(r["sec_per_indexer"] for r in runs) / len(runs), 2) if runs else None,
            "follows_reference": any(r.get("follows_reference") for r in runs),
            "detail": runs[-1].get("detail", "") if runs else "",
            "stats": stats,
        }

    @staticmethod
    def choose_thinking(tried: list[dict]) -> dict | None:
        """Highest average; among those within WITHIN_POINTS of it, the
        fastest. Only settings that answered completely every time count."""
        ok = [t for t in tried if t["complete"]]
        if not ok:
            return None
        best = max(t["avg"] for t in ok)
        close = [t for t in ok if t["avg"] >= best - WITHIN_POINTS]
        return min(close, key=lambda t: t["sec_per_indexer"] or 1e9)

    # 2-3. batch and context ---------------------------------------------------------
    def fitting(self, ctx: int, tokens: TokenModel) -> list[int]:
        """Even splits that fit in ctx: by measured tokens, and by the same
        budget rule live scoring applies (so the size is the one that runs)."""
        out = []
        for b in even_splits(self.n):
            rows = self.rows[:b]
            if tokens.need(rows) > ctx * ROOM_MARGIN:
                continue
            if _batch_size(rows, self.overhead, ctx, b) < b:
                continue
            out.append(b)
        return out

    def probe(self, ctx: int, think, batch: int) -> dict:
        from .llm_client import ollama_score_indexers
        from .model_validator import _score_of
        stats = []
        t0 = time.time()
        result = ollama_score_indexers(self.rows, self.url, self.model, CAL_TIMEOUT,
                                       custom_prompt=SYSTEM_PROMPT, context_window=ctx,
                                       max_indexers_per_call=batch, think=think, stats=stats)
        elapsed = time.time() - t0
        got = set()
        for k in (result or {}):
            try:
                got.add(int(k))
            except (TypeError, ValueError):
                got.add(None)
        scored = sum(1 for i in self.wanted if _score_of(result or {}, i) is not None)
        worst = max((s["seconds"] for s in stats), default=elapsed)
        out = {"context_window": ctx, "batch": batch, "elapsed_s": round(elapsed, 1),
               "worst_call_s": round(worst, 1), "calls": len(stats) or 1,
               "scored": scored, "missing": len(self.wanted - got),
               "invented": len(got - self.wanted),
               "cut_off": any(s.get("done_reason") == "length" for s in stats)}
        out["passed"] = (scored == self.n and not out["invented"] and not out["cut_off"]
                         and worst <= CAL_TIMEOUT * 0.8)
        return out

    def search_at(self, ctx: int, think, sizes: list[int], attempts: list) -> dict | None:
        from .model_validator import _warmup
        if not sizes:
            return None
        warm = _warmup(self.url, self.model, ctx)
        for b in sizes:
            self._tick(f"{b} indexers per call at {ctx_label(ctx)}")
            runs = []
            for _ in range(BATCH_PASSES):
                r = self.probe(ctx, think, b)
                attempts.append(r)
                runs.append(r)
                if not r["passed"]:
                    break
            if len(runs) == BATCH_PASSES and all(r["passed"] for r in runs):
                return {"context_window": ctx, "batch": b,
                        "calls": runs[0]["calls"],
                        "total_s": max(r["elapsed_s"] for r in runs),
                        "worst_call_s": max(r["worst_call_s"] for r in runs),
                        "gpu_offload_pct": warm.get("gpu_offload_pct"),
                        "tok_per_s": warm.get("tok_per_s")}
        return None

    @staticmethod
    def choose_setting(found: list[dict]) -> dict | None:
        """Stay on the graphics card first, then fastest; a smaller context
        wins when it is within SAME_SPEED of the fastest."""
        if not found:
            return None
        top_gpu = max(round(f.get("gpu_offload_pct") or 100) for f in found)
        on_card = [f for f in found if round(f.get("gpu_offload_pct") or 100) >= top_gpu - 1]
        fastest = min(f["total_s"] for f in on_card)
        close = [f for f in on_card if f["total_s"] <= fastest * (1 + SAME_SPEED)]
        return min(close, key=lambda f: (f["context_window"], f["total_s"]))

    def neighbours(self, ctx: int) -> tuple[int | None, int | None]:
        steps = [s for s in CONTEXT_STEPS if not self.max_context or s <= self.max_context]
        up = next((s for s in steps if s > ctx), None)
        down = next((s for s in reversed(steps) if s < ctx), None)
        return up, down

    def batch_and_context(self, chosen: dict, notes: list, attempts: list) -> dict | None:
        from .model_quality import quality_indexers
        think, base = chosen["think"], chosen["context_window"]
        tokens = TokenModel(chosen["stats"], quality_indexers())
        sizes = even_splits(self.n)
        fit_base = self.fitting(base, tokens)
        self.steps = self.step + len(fit_base) + 3
        found = []
        at_base = self.search_at(base, think, fit_base, attempts)
        if at_base:
            found.append(at_base)
        up, down = self.neighbours(base)
        gpu_base = (at_base or {}).get("gpu_offload_pct")

        # Up a step: only when room, not the model, stopped a bigger batch,
        # and only while the model is fully on the graphics card.
        room_limited = bool(at_base and fit_base and at_base["batch"] == fit_base[0]
                            and fit_base[0] < sizes[0])
        if room_limited and up and (gpu_base is None or gpu_base >= 99):
            bigger = [b for b in self.fitting(up, tokens) if b > at_base["batch"]]
            at_up = self.search_at(up, think, bigger, attempts)
            if at_up and gpu_base is not None and (at_up.get("gpu_offload_pct") or 0) < gpu_base - 1:
                notes.append(f"{ctx_label(up)} fit {at_up['batch']} per call but moved part of the "
                             f"model off the graphics card, so it was not used.")
            elif at_up:
                found.append(at_up)
        elif at_base and fit_base and at_base["batch"] < fit_base[0]:
            notes.append(f"More indexers fit in {ctx_label(base)}, but the model dropped or "
                         f"invented indexers above {at_base['batch']} per call.")

        # Down a step: when it saves memory for the same batch, or when the
        # base size did not fit on the graphics card or found nothing.
        spilled = gpu_base is not None and gpu_base < 99
        model_limited = bool(at_base and fit_base and at_base["batch"] < fit_base[0])
        if down and (spilled or model_limited or not at_base):
            fit_down = self.fitting(down, tokens)
            want = [at_base["batch"]] if at_base and at_base["batch"] in fit_down else (
                fit_down if not at_base else [])
            at_down = self.search_at(down, think, want, attempts)
            if at_down:
                found.append(at_down)
        return self.choose_setting(found)


def _explain(chosen: dict, setting: dict, n: int, timeout: int, notes: list) -> str:
    calls = math.ceil(n / setting["batch"])
    parts = [f"Thinking {think_label(chosen['think'])}, {ctx_label(setting['context_window'])} context, "
             f"{setting['batch']} indexers per call ({calls} call{'s' if calls != 1 else ''}, "
             f"about {setting['total_s']:.0f} s for all {n}), timeout {timeout} s."]
    if setting.get("gpu_offload_pct") is not None and setting["gpu_offload_pct"] < 99:
        parts.append(f"Only {setting['gpu_offload_pct']:.0f}% of the model fits on the graphics card here.")
    parts.extend(notes)
    return " ".join(parts)


def calibrate(url: str, model: str, indexer_count: int = 20, progress=None) -> dict:
    """Measure the best way to run `model` on `url`. Never raises.

    progress(stage, done, total) is called as it goes, for the UI.
    """
    started = time.time()
    out = {"model": model, "ollama_url": url, "indexer_count": indexer_count,
           "calibration": "deep", "calibrated": False, "passed": False,
           "tests": [], "avg_response_ms": 0}
    try:
        cal = Calibration(url, model, indexer_count, progress)
        out["indexer_count"] = cal.n
        if cal.profile is not None and not cal.profile.get("can_score"):
            out["error"] = ("This model cannot write answers (it is an embedding or decision "
                            "model), so it cannot score indexers.")
            out["tests"].append({"name": "Quality", "passed": False, "detail": out["error"]})
            return out

        options = thinking_options(cal.profile)
        cal.steps = len(options) * QUALITY_RUNS + 4
        tried = []
        for think in options:
            t = cal.quality(think)
            tried.append(t)
            if (cal.profile or {}).get("thinking") == "levels" and not t["complete"]:
                break           # a higher level thinks longer still
        out["thinking_tried"] = [{k: v for k, v in t.items() if k != "stats"} for t in tried]
        chosen = cal.choose_thinking(tried)

        if chosen is None:
            why = ("ran out of room before finishing its answer" if any(t["cut_off"] for t in tried)
                   else "left indexers out of its answer")
            q = {"name": "Quality", "passed": False,
                 "detail": f"No setting scored every indexer: the model {why}."}
            out["tests"].append(q)
            return out

        out["quality_score"] = chosen["avg"]
        out["quality_runs"] = chosen["runs"]
        out["quality_spread"] = chosen["spread"]
        out["follows_reference"] = chosen["follows_reference"]
        out["sec_per_indexer"] = chosen["sec_per_indexer"]
        quality_ok = chosen["avg"] >= PASS_SCORE
        others = [t for t in tried if t is not chosen]
        compared = "; ".join(f"thinking {think_label(t['think'])}: "
                             + (f"{t['avg']:.0f}" if t["complete"] else "did not finish")
                             for t in others)
        out["tests"].append({
            "name": "Quality", "passed": quality_ok,
            "expected": f"{PASS_SCORE}+ out of 100, every indexer scored, twice",
            "detail": (f"{chosen['avg']:.0f}/100 with thinking {think_label(chosen['think'])} "
                       f"(runs {' and '.join(f'{s:.0f}' for s in chosen['runs'])})"
                       + (f"; {compared}" if compared else "")
                       + ("; mostly repeats Inspectarr's own score" if chosen["follows_reference"] else "")),
        })
        if not quality_ok:
            return out

        notes, attempts = [], []
        setting = cal.batch_and_context(chosen, notes, attempts)
        out["batch_attempts"] = attempts
        if not setting:
            last = attempts[-1] if attempts else {}
            why = ("ran out of room" if last.get("cut_off") else
                   f"dropped {last.get('missing')} indexer(s)" if last.get("missing") else
                   "was too slow" if last else "had no batch size that fit")
            out["tests"].append({"name": "Best settings", "passed": False,
                                 "detail": f"No batch size was reliable: the model {why}."})
            return out

        timeout = suggested_timeout(setting["worst_call_s"])
        settings = {"context_window": setting["context_window"],
                    "max_indexers_per_call": setting["batch"],
                    "think": chosen["think"], "timeout": timeout}
        out.update(settings=settings, calibrated=True, passed=True,
                   max_safe_batch=setting["batch"],
                   context_window=setting["context_window"],
                   total_s=setting["total_s"],
                   gpu_offload_pct=setting.get("gpu_offload_pct"),
                   tok_per_s=setting.get("tok_per_s"),
                   avg_response_ms=round(1000 * setting["total_s"] / max(1, setting["calls"])))
        out["explanation"] = _explain(chosen, setting, cal.n, timeout, notes)
        out["tests"].append({"name": "Best settings", "passed": True, "detail": out["explanation"]})
        return out
    except Exception as exc:
        log.warning(f"Calibration crashed for {model}: {exc}", exc_info=True)
        out["error"] = str(exc)
        out["passed"] = False
        return out
    finally:
        out["duration_ms"] = round((time.time() - started) * 1000)
        if progress:
            progress("Done", 1, 1)
