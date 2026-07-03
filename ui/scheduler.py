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
            # Recover last_detection directly — dedicated query finds the most
            # recent flagged run regardless of how many clean scans followed it.
            self.last_detection = self._state.get_last_detection()

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def start(self):
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="inspectarr-scheduler")
            self._thread.start()
            self.running = True

    def stop(self):
        self._stop_event.set()
        with self._lock:
            self.running  = False
            self.next_run = None

    def trigger(self):
        """Fire an immediate one-shot scan (non-blocking)."""
        t = threading.Thread(target=lambda: self._execute_scan(is_first=False),
                             daemon=True, name="inspectarr-manual")
        t.start()

    def is_scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running":        self.running,
                "scanning":       self._scanning,
                "last_run":       self.last_run,
                "next_run":       self.next_run,
                "last_result":    self.last_result,
                "last_detection": self.last_detection,
                "run_history":    list(self.run_history),
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
            self._execute_scan(is_first=first)
            first    = False
            interval = self._get_interval()
            next_dt  = datetime.now(timezone.utc) + timedelta(seconds=interval)
            with self._lock:
                self.next_run = next_dt.isoformat()
            # Sleep interruptibly
            self._stop_event.wait(timeout=interval)
        with self._lock:
            self.running  = False
            self.next_run = None

    def _execute_scan(self, is_first: bool = False):
        from core.config import load_config
        from core.scanner import Scanner

        with self._lock:
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
            scanner = Scanner(config)
            if is_first:
                scanner.startup()      # full startup + one notification
            else:
                scanner.prepare()      # prune only, no notification
            if config.retry.enabled:
                # BUG-04 (documented): retries that succeed here do NOT update
                # the originating scan's run_history actioned count — that row
                # permanently shows actioned=0. Expected; see _process_one_retry.
                scanner.process_retries()
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
            except Exception:
                pass  # persistence failure must never affect scan operation

        # After each scan cycle, check whether a Prowlarr reorder is due.
        self._maybe_reorder()

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
            scorer.score_all(skip_ai=False)   # rescore (with AI if configured) before auto-reorder
            changed  = scorer.reorder()
            self.last_reorder = now
            if self._state:
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
                except Exception:
                    pass

    def _get_interval(self) -> int:
        try:
            from core.config import load_config
            config = load_config(self.config_path)
            return getattr(config, "poll_interval_seconds", 300)
        except Exception:
            return 300
