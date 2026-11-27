"""
core/indexer_jobs.py -- "Rescore & reorder" (and "Run now") as a background job.

These used to run inside the web request: minutes of AI scoring, then the
reorder. The button greyed out only on the page that clicked it; leave and come
back and nothing said whether it was still running. Meanwhile other passes (the
page asking for AI scores, the scheduler) could score at the same time and
overwrite the stored scores after the reorder had used them, so the page showed
an indexer at 92 below ones at 75.

Now:
  * SCORING_LOCK: one scoring-and-reorder pass at a time, for every path
    (this job, the scheduled reorder, the page's AI refresh).
  * The reorder uses the scores from its own pass (IndexerScorer.reorder(scored)).
  * Progress is kept in app_state, so any page -- or a page opened later --
    can show it. A run cut short by a restart is reported as such.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("inspectarr")

SCORING_LOCK = threading.Lock()
STATE_KEY = "indexer_rescore_job"
LAST_REORDER_KEY = "last_prowlarr_reorder"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status(state) -> dict:
    """The last or current job, as the page shows it. {} if there never was one."""
    try:
        raw = state.get_app_state(STATE_KEY) if state else None
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def recover(state) -> None:
    """At start: a job stored as running was cut short by the restart."""
    job = status(state)
    if job.get("state") == "running":
        job.update(state="failed", stage="Stopped by a restart before it finished",
                   finished_at=_now())
        state.set_app_state(STATE_KEY, json.dumps(job))


class RescoreJob:
    """Starts and runs one job at a time. The web routes hold one instance."""

    def __init__(self, state, config_path: str):
        self.state = state
        self.config_path = config_path
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, sync: bool = False) -> tuple[bool, str]:
        """(started, message). Refuses while another scoring pass is running."""
        if self.running() or SCORING_LOCK.locked():
            return False, "Scoring is already running. This page shows its progress."
        job = {"state": "running", "stage": "Starting", "started_at": _now(),
               "finished_at": None, "sync": bool(sync), "moved": None, "synced": None,
               "indexers": None, "ai_model": None, "ai_estimate_s": None, "message": ""}
        self._save(job)
        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True,
                                        name="inspectarr-rescore")
        self._thread.start()
        return True, "Started"

    # ------------------------------------------------------------------------------
    def _save(self, job: dict) -> None:
        try:
            self.state.set_app_state(STATE_KEY, json.dumps(job))
        except Exception as exc:
            log.warning(f"Could not save the rescore progress: {exc}")

    def _stage(self, job: dict, text: str, **more) -> None:
        job.update(stage=text, **more)
        self._save(job)

    def _run(self, job: dict) -> None:
        if not SCORING_LOCK.acquire(timeout=5):
            job.update(state="failed", stage="Another scoring pass was running", finished_at=_now())
            self._save(job)
            return
        try:
            from .config import load_config
            from .indexer_scorer import IndexerScorer
            from .prowlarr import ProwlarrClient
            cfg = load_config(self.config_path)
            if not cfg.prowlarr.enabled:
                raise RuntimeError("Prowlarr is not enabled")
            prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
            scorer = IndexerScorer(prowlarr, self.state, cfg.prowlarr)
            ocfg = cfg.prowlarr.ollama
            if ocfg.is_active():
                self._stage(job, f"Scoring indexers with AI ({ocfg.model})",
                            ai_model=ocfg.model, ai_estimate_s=self._estimate(ocfg))
            else:
                self._stage(job, "Scoring indexers")
            scored = scorer.score_all(skip_ai=False)
            job["indexers"] = len(scored)
            verdict = getattr(scorer, "last_ai_verdict", None)
            if verdict and not verdict.get("accepted"):
                job["message"] = ("The AI's scores were rejected (" + str(verdict.get("reason")) +
                                  "); Inspectarr's own scores were used.")
            self._stage(job, "Reordering in Prowlarr")
            moved = scorer.reorder(scored)
            self.state.set_app_state(LAST_REORDER_KEY, _now())
            job["moved"] = moved
            if job["sync"]:
                self._stage(job, "Sending the new order to Sonarr, Radarr and the other apps")
                job["synced"] = bool(prowlarr.sync_to_apps())
            job.update(state="done", finished_at=_now(),
                       stage=f"Done: {moved} indexer(s) moved" +
                             ("" if not job["sync"] else
                              ", new order sent to the apps" if job["synced"] else ", but sending to the apps failed"))
            log.info(f"Rescore & reorder finished: {moved} moved")
        except Exception as exc:
            log.warning(f"Rescore & reorder failed: {exc}")
            job.update(state="failed", finished_at=_now(), stage=f"Failed: {exc}")
        finally:
            SCORING_LOCK.release()
            self._save(job)

    def _estimate(self, ocfg) -> float | None:
        """How long the AI pass took when this model was calibrated here, if known."""
        try:
            rec = self.state.get_validation(ocfg.model, ocfg.url) or {}
            res = json.loads(rec.get("results_json") or "{}") or {}
            return res.get("total_s")
        except Exception:
            return None
