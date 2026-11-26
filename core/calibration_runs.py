"""
core/calibration_runs.py -- calibrate models one after another, in the
background, safely (DockLlama's test runner, for Inspectarr).

A run is a list of models (Calibrate on one model, Calibrate all, or the new
models found on the server), stored in the database so it carries on after
a restart. One run at a time, one model at a time:

  * each model takes its turn on the AI (core/ai_turn.py) and releases it
    between models, so live scoring gets its turn in a long run;
  * Pause and Cancel take effect at the next step of the model in progress,
    not at the end of it (calibrate() is interrupted from its progress
    callback); a paused model starts again from the beginning on Resume;
  * a run can wait until a set hour ("tonight");
  * after a restart, the model that was in progress starts again;
  * each model is unloaded afterwards unless it is the one in use;
  * when a run finishes, a banner can offer a clearly better model
    (DockLlama's rule), but nothing switches by itself.

New models: every few hours the runner lists the server's models; usable
ones it has not seen before are queued for the next overnight window, if
"calibrate new models" is on.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("inspectarr")

TICK_S = 15
NEW_MODEL_CHECK_S = 6 * 3600
BANNER_KEY = "ai_better_model"
KNOWN_KEY = "ai_known_models::"
DEFAULT_OVERNIGHT_HOUR = 1

# DockLlama's thresholds for "clearly better"
BETTER_POINTS = 3.0          # this many points higher at similar speed...
SIMILAR_SPEED = 1.3          # ...where similar means at most 30% slower
SAME_POINTS = 2.0            # or about the same score...
FASTER = 0.7                 # ...and at least 30% faster


class Interrupted(Exception):
    """Pause or Cancel was pressed while a model was being calibrated.

    Raised from calibrate()'s progress callback; calibrate() lets anything
    marked interrupts_calibration through instead of reporting it as a
    failed calibration."""
    interrupts_calibration = True


def next_hour(hour: int, now: datetime | None = None) -> str:
    """The next time the local clock shows hour:00, as a UTC ISO string."""
    now = now or datetime.now().astimezone()
    at = now.replace(hour=int(hour) % 24, minute=0, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc).isoformat()


def meaningfully_better(cand: dict, cur: dict | None) -> bool:
    """Plan 8A (DockLlama): at least 3 points better at similar speed, or
    about the same score and at least 30% faster; never by moving off the
    graphics card."""
    if not cand or cand.get("quality_score") is None:
        return False
    if not cur or cur.get("quality_score") is None:
        return False
    on = lambda r: (r.get("gpu_offload_pct") is None) or r["gpu_offload_pct"] >= 99   # noqa: E731
    if on(cur) and not on(cand):
        return False
    cs, ks = cand["quality_score"], cur["quality_score"]
    ct, kt = cand.get("total_s"), cur.get("total_s")
    similar = ct is None or kt is None or ct <= kt * SIMILAR_SPEED
    if cs >= ks + BETTER_POINTS and similar:
        return True
    if abs(cs - ks) <= SAME_POINTS and ct and kt and ct <= kt * FASTER:
        return True
    return False


def best_on_server(validations: list[dict]) -> dict | None:
    """The ranking the page uses: highest score; within 2 points, on the
    graphics card, then fastest."""
    rows = []
    for v in validations:
        try:
            res = json.loads(v.get("results_json") or "{}")
        except (TypeError, ValueError):
            res = {}
        if v.get("status") == "supported" and v.get("calibrated") and res.get("quality_score") is not None:
            rows.append({"model": v["model"], "quality_score": res["quality_score"],
                         "total_s": res.get("total_s"), "gpu_offload_pct": v.get("gpu_offload_pct")})
    if not rows:
        return None
    top = max(r["quality_score"] for r in rows)
    close = [r for r in rows if r["quality_score"] >= top - SAME_POINTS]
    on = lambda r: (r.get("gpu_offload_pct") is None) or r["gpu_offload_pct"] >= 99   # noqa: E731
    close.sort(key=lambda r: (not on(r), r.get("total_s") or 1e9))
    return close[0]


class CalibrationRunner:
    """Works through calibration runs on its own thread.

    start() is called by web.main() only, so tests and imports never get a
    background thread; step() does one unit of work and is what tests call.
    """

    def __init__(self, state, config_path: str):
        self.state, self.config_path = state, config_path
        self._thread = None
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._request = {}          # run_id -> "pause" | "cancel"
        self.progress = {}          # what the page shows while a model runs
        self.last_result = None
        self._last_new_check = 0.0

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        n = self.state.recover_calibration_runs()
        if n:
            log.info(f"Calibration: {n} run(s) interrupted by a restart will carry on")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="inspectarr-calibration")
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    @property
    def started(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:                       # never let the thread die
                log.warning(f"Calibration runner: {exc}", exc_info=True)
            self._wake.wait(TICK_S)
            self._wake.clear()

    # --- config -----------------------------------------------------------------
    def _ollama(self) -> dict:
        import yaml
        try:
            with open(self.config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}
        return ((raw.get("prowlarr") or {}).get("ollama") or {})

    def _indexer_count(self) -> int:
        try:
            rows = self.state.get_all_indexer_stats()
            return len(rows) if rows else 20
        except Exception:
            return 20

    # --- requests from the page ------------------------------------------------
    def request(self, run_id: int, what: str) -> None:
        """Pause, cancel or resume a run."""
        run = self.state.get_calibration_run(run_id)
        if not run:
            return
        if what == "resume":
            if run["status"] == "paused":
                self.state.set_calibration_run(run_id, "waiting", note="")
                self.wake()
            return
        if run["status"] == "running":
            with self._lock:
                self._request[run_id] = what          # picked up at the next step
        elif what == "cancel" and run["status"] in ("waiting", "paused"):
            self.state.set_calibration_run(run_id, "cancelled", note="Cancelled")
        elif what == "pause" and run["status"] == "waiting":
            self.state.set_calibration_run(run_id, "paused", note="Paused")

    def _check(self, run_id: int) -> None:
        with self._lock:
            what = self._request.get(run_id)
        if what:
            raise Interrupted(what)

    # --- the work ------------------------------------------------------------------
    def step(self) -> bool:
        """Run the next due run to completion (or interruption). True if it did work."""
        now = datetime.now(timezone.utc).isoformat()
        for run in self.state.list_calibration_runs(("waiting",)):
            if run["start_after"] <= now:
                self._run(run)
                return True
        self._maybe_queue_new_models()
        return False

    def _run(self, run: dict) -> None:
        if run.get("kind") == "prompt":
            return self._run_prompt_test(run)
        from .ai_turn import ai_turn
        from .calibration import calibrate
        from .ollama_models import unload
        run_id, url = run["id"], run["ollama_url"]
        self.state.set_calibration_run(run_id, "running", note="")
        items = [i for i in run["items"] if i["status"] == "queued"]
        total = len(run["items"])
        n = self._indexer_count()
        try:
            for item in items:
                self._check(run_id)
                model = item["model"]
                self.state.set_calibration_item(run_id, item["position"], "running")

                def progress(stage, done, steps, _m=model, _p=item["position"]):
                    self._check(run_id)
                    self.progress = {"run_id": run_id, "model": _m, "stage": stage, "done": done,
                                     "total": steps, "position": _p + 1, "of": total}

                self.progress = {"run_id": run_id, "model": model, "stage": "Waiting for the AI to be free",
                                 "done": 0, "total": 0, "position": item["position"] + 1, "of": total}
                try:
                    with ai_turn("calibration", model, wait=None):
                        result = calibrate(url, model, n, progress=progress)
                        active = self._ollama().get("model")
                        if model != active:
                            unload(url, model)
                except Interrupted:
                    self.state.set_calibration_item(run_id, item["position"], "queued")
                    raise
                digest = None
                try:
                    from .ollama_registry import local_digest
                    digest = local_digest(url, model)
                except Exception:
                    pass
                self.state.save_validation(model, result, digest=digest, ollama_url=url)
                self.last_result = result
                self.state.set_calibration_item(
                    run_id, item["position"], "done" if result.get("passed") else "failed",
                    score=result.get("quality_score"),
                    note=(result.get("explanation") or result.get("error") or "")[:300])
            self.state.set_calibration_run(run_id, "done", note="Finished")
            self._update_banner(url)
        except Interrupted as why:
            what = str(why) or "cancel"
            with self._lock:
                self._request.pop(run_id, None)
            if what == "pause":
                self.state.set_calibration_run(run_id, "paused", note="Paused")
            else:
                self.state.set_calibration_run(run_id, "cancelled", note="Cancelled")
        finally:
            self.progress = {}

    # --- testing a scoring prompt ----------------------------------------------------
    def _run_prompt_test(self, run: dict) -> None:
        """Grade a custom prompt on each chosen model: the quality test, once,
        with the model's calibrated thinking and context if it has them.
        Results go to prompt_tests only -- never to the model ranking."""
        from .ai_turn import ai_turn
        from .calibration import context_for_thinking
        from .model_quality import run_quality_test
        from .ollama_models import model_profile, recommended_think, unload
        from .scoring_prompts import build
        run_id, url = run["id"], run["ollama_url"]
        prompt = self.state.get_scoring_prompt(run.get("prompt_id") or 0)
        if not prompt:
            self.state.set_calibration_run(run_id, "cancelled", note="The prompt was deleted")
            return
        system = build(prompt["text"])
        self.state.set_calibration_run(run_id, "running", note="")
        total = len(run["items"])
        try:
            for item in [i for i in run["items"] if i["status"] == "queued"]:
                self._check(run_id)
                model = item["model"]
                self.state.set_calibration_item(run_id, item["position"], "running")
                rec = self.state.get_validation(model, url) or {}
                try:
                    settings = (json.loads(rec.get("results_json") or "{}") or {}).get("settings") or {}
                except (TypeError, ValueError):
                    settings = {}
                think = settings.get("think", recommended_think(model_profile(url, model)))
                ctx = settings.get("context_window") or context_for_thinking(
                    think, (model_profile(url, model) or {}).get("max_context"))
                self.progress = {"run_id": run_id, "model": model, "stage": "Testing the prompt",
                                 "done": 0, "total": 1, "position": item["position"] + 1, "of": total}
                with ai_turn("prompt test", model, wait=None):
                    self._check(run_id)
                    r = run_quality_test(url, model, 900, ctx, think=think, prompt=system)
                    if model != self._ollama().get("model"):
                        unload(url, model)
                self.state.save_prompt_test(prompt["id"], prompt["revision"], model, url,
                                            r.get("score"), r.get("complete", False), r.get("detail", ""))
                self.state.set_calibration_item(run_id, item["position"],
                                                "done" if r.get("complete") else "failed",
                                                score=r.get("score"))
            self.state.set_calibration_run(run_id, "done", note="Finished")
        except Interrupted as why:
            with self._lock:
                self._request.pop(run_id, None)
            self.state.set_calibration_run(run_id, "paused" if str(why) == "pause" else "cancelled",
                                           note="Paused" if str(why) == "pause" else "Cancelled")
        finally:
            self.progress = {}

    # --- after a run: is something clearly better than the model in use? -------------
    def _update_banner(self, url: str) -> None:
        from .state import normalize_ollama_url
        ollama = self._ollama()
        if normalize_ollama_url(ollama.get("url")) != normalize_ollama_url(url):
            return
        rows = self.state.get_validations(url)
        best = best_on_server(rows)
        current_model = ollama.get("model")
        if not best or best["model"] == current_model:
            self.state.set_app_state(BANNER_KEY, "")
            return
        cur = next((b for b in [best_on_server([r]) for r in rows if r["model"] == current_model] if b), None)
        if meaningfully_better(best, cur):
            self.state.set_app_state(BANNER_KEY, json.dumps({
                "server": url, "model": best["model"], "score": best["quality_score"],
                "total_s": best.get("total_s"), "current": current_model,
                "current_score": cur["quality_score"] if cur else None}))

    # --- new models -----------------------------------------------------------------------
    def _maybe_queue_new_models(self) -> None:
        if time.time() - self._last_new_check < NEW_MODEL_CHECK_S:
            return
        self._last_new_check = time.time()
        ollama = self._ollama()
        url = (ollama.get("url") or "").strip()
        enabled = ollama.get("enabled", bool(url))
        if not (url and enabled and ollama.get("calibrate_new_models", True)):
            return
        from .ollama_models import list_models
        from .state import normalize_ollama_url
        models = list_models(url)
        if models is None:
            return
        key = KNOWN_KEY + (normalize_ollama_url(url) or "")
        names = sorted(m["name"] for m in models)
        known_raw = self.state.get_app_state(key)
        self.state.set_app_state(key, json.dumps(names))
        if known_raw is None:
            return            # first look at this server: everything is "known", nothing queued
        try:
            known = set(json.loads(known_raw))
        except (TypeError, ValueError):
            known = set()
        new = [m["name"] for m in models if m["name"] not in known and m.get("can_score")]
        if new:
            hour = ollama.get("overnight_hour", DEFAULT_OVERNIGHT_HOUR)
            self.state.create_calibration_run(url, new, start_after=next_hour(hour), kind="new")
            log.info(f"Calibration: {len(new)} new model(s) on {url} queued for tonight: {', '.join(new)}")
