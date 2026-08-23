import sys
import threading
import json
from datetime import datetime, timezone, timedelta
from typing import Optional


class Scheduler:
    """
    Background daemon thread that runs inspectarr scans on a configurable interval.
    Config is reloaded from disk before every scan so UI changes take effect
    immediately without restarting the server.
    """

    def __init__(self, config_path: str):
        self.config_path  = config_path
        self._thread: Optional[threading.Thread] = None
        self._stop_event  = threading.Event()
        self._lock        = threading.Lock()
        self._scanning    = False

        self.running      = False
        self.last_run:    Optional[str] = None
        self.next_run:    Optional[str] = None
        self.last_result: Optional[dict] = None
        self.run_history: list[dict] = []
        self.last_reorder: Optional[datetime] = None
        # Persists the most recent flagged torrent across clean scans and restarts.
        # Never reset to None when a clean scan runs — only updated when flagged > 0.
        self.last_detection: Optional[dict] = None

        # Persistent state — best effort; None if config/DB unavailable at startup
        self._state = self._init_state()
        if self._state:
            self.run_history = self._state.get_recent_runs(10)
            if self.run_history:
                self.last_result = self.run_history[0]
            # Recover last_detection — try run_history first, fall back to
            # processed_hashes (survives container recreations).
            self.last_detection = self._state.get_last_detection()
            if not self.last_detection:
                self.last_detection = self._state.get_last_flagged_torrent()

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        Start the scheduler loop. Returns True if the loop is running.

        BUG-11: after stop(), the old thread may still be mid-scan. start()
        used to clear _stop_event immediately, letting the old loop miss the
        stop signal and survive alongside the new one (two loops). Now we wait
        briefly for the old thread to exit and refuse to double-start.
        """
        old = None
        with self._lock:
            if self.running:
                return True
            if self._thread is not None and self._thread.is_alive():
                old = self._thread
        if old is not None:
            old.join(timeout=10)   # never join while holding self._lock
            if old.is_alive():
                return False       # previous loop still finishing a scan
        with self._lock:
            if self.running:
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="inspectarr-scheduler")
            self._thread.start()
            self.running = True
        return True

    def stop(self):
        self._stop_event.set()
        with self._lock:
            self.running  = False
            self.next_run = None

    def trigger(self) -> bool:
        """
        Fire an immediate one-shot scan (non-blocking).
        Returns False if a scan is already in flight (BUG-12).
        L-06: set _scanning under lock BEFORE starting thread to close
        the TOCTOU gap that allowed double-trigger.
        """
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
        t = threading.Thread(target=lambda: self._execute_scan(is_first=False, already_claimed=True),
                             daemon=True, name="inspectarr-manual")
        t.start()
        return True

    def is_scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def get_status(self) -> dict:
        totals = self._state.get_total_stats() if self._state else {}
        with self._lock:
            return {
                "running":        self.running,
                "scanning":       self._scanning,
                "last_run":       self.last_run,
                "next_run":       self.next_run,
                "last_result":    self.last_result,
                "last_detection": self.last_detection,
                "run_history":    list(self.run_history),
                "total_flagged":  totals.get("total_flagged", 0),
                "total_actioned": totals.get("total_actioned", 0),
            }


    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _init_state(self):
        """
        Instantiate a StateManager from the current config for run_history
        persistence. Returns None on any failure — persistence is best-effort
        and must never prevent the scheduler from starting.
        """
        try:
            from core.config import load_config
            from core.state import StateManager
            config = load_config(self.config_path)
            return StateManager(
                db_path=config.state.db_file,
                log_path=config.logging.log_file,
                retention_days=config.logging.retention_days,
            )
        except Exception:
            return None

    def _loop(self):
        first = True
        while not self._stop_event.is_set():
            # Check if polling is enabled (webhooks-only mode skips polling scans)
            polling_enabled = self._is_polling_enabled()
            if first or polling_enabled:
                self._execute_scan(is_first=first)
            first    = False
            interval = self._get_interval()
            next_dt  = datetime.now(timezone.utc) + timedelta(seconds=interval)
            with self._lock:
                self.next_run = next_dt.isoformat() if polling_enabled else None
            # Sleep interruptibly
            self._stop_event.wait(timeout=interval)
        with self._lock:
            self.running  = False
            self.next_run = None

    def _is_polling_enabled(self) -> bool:
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            return config.scanning.polling.enabled
        except Exception:
            return True  # fail-open: poll by default

    def _execute_scan(self, is_first: bool = False, already_claimed: bool = False):
        from core.config import load_config
        from core.scanner import Scanner

        with self._lock:
            if self._scanning and not already_claimed:
                # BUG-12: another scan is already in flight — skip.
                return
            self._scanning = True

        start  = datetime.now(timezone.utc)
        result = {
            "scan_start":       start.isoformat(),
            "scan_end":         None,
            "duration_seconds": 0,
            "torrents_checked": 0,
            "flagged":          0,
            "actioned":         0,
            "last_flagged":     None,
            "error":            None,
        }

        # BUG-01: keep stats separate so result.update() always runs even if
        # an earlier step raises. run_scan() itself now catches per-torrent
        # exceptions internally, so stats should always be returned on a
        # normal scan; this is belt-and-suspenders for config/init failures.
        stats = None
        try:
            config  = load_config(self.config_path)
            # IMP-3: pass shared StateManager to avoid new SQLite connection per cycle
            scanner = Scanner(config, state=self._state)
            if is_first:
                scanner.startup()      # full startup + one notification
            else:
                scanner.prepare()      # prune only, no notification
            if config.retry.enabled:
                # BUG-04 (documented): retries that succeed here do NOT update
                # the originating scan's run_history actioned count — that row
                # permanently shows actioned=0. Expected; see _process_one_retry.
                scanner.process_retries()
            # Runs before the scan so an expired hold is resolved before the
            # same torrent is re-evaluated, and so a release takes effect
            # promptly rather than waiting a full extra cycle.
            try:
                scanner.process_quarantine_timeouts()
                scanner.process_replacement_watches()
            except Exception as exc:
                # Never let timeout bookkeeping abort the scan itself.
                print(f"Quarantine timeout sweep failed: {exc}",
                      file=sys.stderr)
            stats = scanner.run_scan()
        except Exception as exc:
            result["error"] = str(exc)

        if stats is not None:
            result.update(stats)

        end = datetime.now(timezone.utc)
        result["scan_end"]         = end.isoformat()
        result["duration_seconds"] = round((end - start).total_seconds(), 2)

        with self._lock:
            self._scanning   = False
            self.last_run    = end.isoformat()
            self.last_result = result
            # Only update last_detection when this scan actually flagged something.
            # This preserves the most recent detection across subsequent clean scans.
            if result.get("last_flagged"):
                self.last_detection = result["last_flagged"]
            self.run_history.insert(0, result)
            if len(self.run_history) > 10:
                self.run_history = self.run_history[:10]

        if self._state:
            try:
                self._state.save_run(result)
            except Exception as db_exc:
                print(f"DB logging failed (save_run): {db_exc}", file=sys.stderr)

        # After each scan cycle, run auto-manage (every scan) and check reorder (interval-gated).
        self._maybe_auto_manage()
        self._maybe_reorder()
        # Check if a periodic log summary is due.
        self._maybe_summary()

    def _maybe_auto_manage(self):
        """
        Run auto-manage after every scan cycle (not gated by reorder interval).
        Uses deterministic scoring only (skip_ai=True) for speed.
        Best-effort: any failure is logged and never affects the scan loop.
        """
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            if not config.prowlarr.enabled or not config.prowlarr.auto_manage.enabled:
                return

            if self._state is None:
                self._state = self._init_state()
            if self._state is None:
                return

            from core.prowlarr import ProwlarrClient
            from core.indexer_scorer import IndexerScorer
            prowlarr = ProwlarrClient(config.prowlarr.url, config.prowlarr.api_key)
            scorer   = IndexerScorer(prowlarr, self._state, config.prowlarr)
            scored   = scorer.score_all(skip_ai=True)   # fast deterministic pass
            am_result = scorer.auto_manage(scored)
            if am_result.get("disabled") or am_result.get("re_enabled"):
                self._state.write_log({
                    "level": "INFO", "event": "prowlarr_auto_manage",
                    "disabled": am_result["disabled"],
                    "re_enabled": am_result["re_enabled"],
                })
        except Exception as exc:
            if self._state:
                try:
                    self._state.write_log({
                        "level": "ERROR", "event": "prowlarr_auto_manage_failed",
                        "reason": str(exc),
                    })
                except Exception as db_exc:
                    print(f"DB logging failed (auto_manage): {db_exc}", file=sys.stderr)

    def _maybe_reorder(self):
        """
        If Prowlarr scoring is enabled and reorder_interval_hours has elapsed
        since the last reorder, run one. Best-effort: any failure is logged and
        never affects the scan loop.
        """
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            if not config.prowlarr.enabled:
                return

            # BUG-17: _state can be None (config/DB unavailable at startup).
            # Passing None into IndexerScorer raised AttributeError which was
            # swallowed below with zero trace — reorder just never happened.
            if self._state is None:
                self._state = self._init_state()
            if self._state is None:
                import logging
                logging.getLogger("inspectarr").warning(
                    "Prowlarr auto-reorder skipped — state DB unavailable"
                )
                return

            # BUG-20: recover last_reorder from the DB after a restart so the
            # reorder interval survives process restarts.
            if self.last_reorder is None:
                stored = self._state.get_app_state("last_prowlarr_reorder")
                if stored:
                    try:
                        self.last_reorder = datetime.fromisoformat(stored)
                    except ValueError:
                        pass

            interval_h = config.prowlarr.reorder_interval_hours
            now = datetime.now(timezone.utc)
            if self.last_reorder is not None:
                elapsed_h = (now - self.last_reorder).total_seconds() / 3600.0
                if elapsed_h < interval_h:
                    return

            from core.prowlarr import ProwlarrClient
            from core.indexer_scorer import IndexerScorer
            prowlarr = ProwlarrClient(config.prowlarr.url, config.prowlarr.api_key)
            scorer   = IndexerScorer(prowlarr, self._state, config.prowlarr)
            scored   = scorer.score_all(skip_ai=False)   # rescore (with AI if configured) before auto-reorder
            changed  = scorer.reorder()
            self.last_reorder = now
            self._state.set_app_state("last_prowlarr_reorder", now.isoformat())
            self._state.write_log({
                "level": "INFO", "event": "prowlarr_auto_reorder",
                "indexers_moved": changed,
            })
        except Exception as exc:
            if self._state:
                try:
                    self._state.write_log({
                        "level": "ERROR", "event": "prowlarr_auto_reorder_failed",
                        "reason": str(exc),
                    })
                except Exception as db_exc:
                    print(f"DB logging failed (auto_reorder): {db_exc}", file=sys.stderr)

    def _maybe_summary(self):
        """
        If notifications.summary is enabled and enough time has elapsed since
        the last summary, generate and send one. Best-effort.
        """
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            if not config.notifications.summary.enabled:
                return
            if self._state is None:
                return

            # Check timing — daily = 24h, weekly = 168h
            schedule = config.notifications.summary.schedule
            interval_h = 168 if schedule == "weekly" else 24
            now = datetime.now(timezone.utc)

            stored = self._state.get_app_state("last_log_summary")
            if stored:
                try:
                    last = datetime.fromisoformat(stored)
                    elapsed_h = (now - last).total_seconds() / 3600.0
                    if elapsed_h < interval_h:
                        return
                except ValueError:
                    pass

            from core.summarizer import LogSummarizer
            summarizer = LogSummarizer(config)
            sent = summarizer.generate_and_send(self._state)
            if sent:
                self._state.set_app_state("last_log_summary", now.isoformat())

        except Exception as exc:
            if self._state:
                try:
                    self._state.write_log({
                        "level": "ERROR",
                        "event": "log_summary_failed",
                        "reason": str(exc),
                    })
                except Exception as db_exc:
                    print(f"DB logging failed (log_summary): {db_exc}", file=sys.stderr)

    def _get_interval(self) -> int:
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            return config.scanning.polling.interval_seconds
        except Exception:
            return 300
