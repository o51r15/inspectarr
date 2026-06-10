import logging
from datetime import datetime, timezone
from .config import AppConfig, Rule
from .qbit import QBittorrentClient
from .arrs.base import AbstractArrClient
from .arrs.sonarr import SonarrClient
from .arrs.radarr import RadarrClient
from .notifier import Notifier
from .rules import evaluate_rule
from .state import StateManager


def _get_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("inspectarr")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(h)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def _build_arr_client(app_name: str, config: AppConfig) -> AbstractArrClient:
    if app_name == "sonarr":
        c = config.arrs.sonarr
        return SonarrClient(c.url, c.api_key)
    if app_name == "radarr":
        c = config.arrs.radarr
        return RadarrClient(c.url, c.api_key)
    raise ValueError(f"Unknown arr app: {app_name!r}")


class Scanner:
    def __init__(self, config: AppConfig):
        self.config  = config
        self.log     = _get_logger(config.logging.level)
        self.state   = StateManager(
            db_path=config.state.db_file,
            log_path=config.logging.log_file,
            retention_days=config.logging.retention_days,
        )
        self.qbit     = QBittorrentClient(
            config.qbittorrent.url,
            config.qbittorrent.username,
            config.qbittorrent.password,
        )
        self.notifier = Notifier(config)


    # ------------------------------------------------------------------
    # Public entry points (called by watchdog.py in order)
    # ------------------------------------------------------------------

    def startup(self):
        """Full startup: prune, log, and fire the startup notification.
        Used by the CLI and once by the daemon when it launches."""
        self.log.info("inspectarr starting up")
        self.prepare()
        self.state.write_log({
            "level": "INFO",
            "event": "startup",
            "dry_run": self.config.dry_run,
            "rules_loaded": len(self.config.rules),
        })
        self.notifier.notify_startup(len(self.config.rules), self.config.dry_run)

    def prepare(self):
        """Lightweight pre-scan: prune old records only. No notification.
        Safe to call before every scan cycle in the daemon."""
        self.state.prune_old_records()

    def process_retries(self, force: bool = False):
        """
        Process the retry queue. If force=True, bypasses next_attempt timing
        and the exhaustion cap — retries everything unresolved. Used by --retry-now.
        """
        if force:
            due = self.state.get_all_unresolved_retries()
        else:
            due = self.state.get_due_retries(self.config.retry.max_attempts)
        if not due:
            return
        self.log.info(f"Processing {len(due)} retry queue entry/entries")
        for entry in due:
            self._process_one_retry(entry)

    def run_scan(self) -> dict:
        stats = {
            "torrents_checked": 0,
            "flagged": 0,
            "actioned": 0,
            "last_flagged": None,
        }
        for rule in self.config.rules:
            self.log.debug(f"Scanning rule '{rule.name}' (category: {rule.category})")
            try:
                torrents = self.qbit.get_torrents_by_category(rule.category)
            except Exception as exc:
                reason  = str(exc)
                context = f"qbit_fetch:{rule.category}"
                self.log.error(f"Failed to fetch category '{rule.category}': {exc}")
                self.state.write_log({
                    "level": "ERROR", "event": "qbit_fetch_failed",
                    "category": rule.category, "reason": reason,
                })
                if self.state.get_error_state(context) != reason:
                    self.notifier.notify_error(
                        f"Category fetch failed: {rule.category}", reason
                    )
                    self.state.set_error_state(context, reason)
                continue

            # Fetch succeeded — clear stored error so a future failure notifies again
            self.state.set_error_state(f"qbit_fetch:{rule.category}", None)

            self.log.debug(f"  {len(torrents)} torrent(s) found")
            for torrent in torrents:
                flagged, actioned = self._evaluate_torrent(torrent, rule)
                stats["torrents_checked"] += 1
                if flagged:
                    stats["flagged"] += 1
                    if actioned:
                        stats["actioned"] += 1
                        stats["last_flagged"] = {
                            "torrent_name": torrent.get("name", torrent["hash"]),
                            "rule": rule.name,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }

        self.state.write_log({
            "level": "INFO", "event": "scan_complete", **stats
        })
        return stats

    # ------------------------------------------------------------------
    # Per-torrent evaluation
    # ------------------------------------------------------------------

    def _evaluate_torrent(self, torrent: dict, rule: Rule) -> tuple[bool, bool]:
        """Returns (flagged, actioned)."""
        h    = torrent["hash"]
        name = torrent.get("name", h)

        if self.state.is_processed(h):
            self.log.debug(f"  Skip (already actioned): {name}")
            return False, False

        # If retry is enabled and this hash has an unresolved retry entry,
        # let the retry queue own its timing — don't reprocess every poll cycle.
        if self.config.retry.enabled and self.state.has_active_retry(h):
            self.log.debug(f"  Skip (retry queue owns it): {name}")
            return False, False

        try:
            files = self.qbit.get_torrent_files(h)
        except Exception as exc:
            self.log.warning(f"  Could not get files for {name}: {exc}")
            return False, False

        flagged, bad_files = evaluate_rule(rule, files)
        if not flagged:
            return False, False

        self.log.info(f"FLAGGED [{rule.name}] {name} | bad: {bad_files}")

        if self.config.dry_run:
            self._handle_dry_run(h, name, rule, bad_files)
            return True, False

        actioned = self._attempt_action(h, name, rule, bad_files)
        return True, actioned


    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _handle_dry_run(self, hash: str, name: str, rule: Rule, bad_files: list[str]):
        self.log.info(f"  [DRY RUN] Would delete: {name}")
        self.state.write_log({
            "level": "DRY_RUN", "event": "dry_run_flagged",
            "torrent_name": name, "hash": hash,
            "category": rule.category, "rule": rule.name, "bad_files": bad_files,
        })
        self.state.record_action(hash, name, rule.category, rule.name,
                                  "dry_run", False, False)
        self.notifier.notify_dry_run(name, bad_files)

    def _attempt_action(
        self, hash: str, name: str, rule: Rule, bad_files: list[str]
    ) -> bool:
        """
        Blocklist in arr, then delete from qBittorrent.
        Returns True on full success, False if anything failed (retry queued).
        """
        arr_client  = _build_arr_client(rule.app, self.config)
        arr_success = False

        # Step 1 — blocklist in arr
        try:
            arr_success = arr_client.blocklist(hash)
            if arr_success:
                self.log.info(f"  Blocklisted in {rule.app}: {name}")
        except Exception as exc:
            reason = str(exc)
            self.log.error(f"  Arr blocklist failed ({rule.app}): {reason}")
            self.state.write_log({
                "level": "ERROR", "event": "arr_failure",
                "torrent_name": name, "hash": hash,
                "arr": rule.app, "reason": reason,
                "on_arr_failure": self.config.on_arr_failure,
            })
            self.notifier.notify_error(f"Arr failure ({rule.app}): {name}", reason)
            if self.config.on_arr_failure == "abort":
                self.state.record_action(hash, name, rule.category, rule.name,
                                          "failed", False, False)
                if self.config.retry.enabled:
                    count = self.state.queue_retry(
                        hash, name, rule.category, rule.name,
                        reason, self.config.retry.interval_seconds,
                    )
                    self._check_retry_limit(hash, name, count)
                return False
            # on_arr_failure == "delete": fall through

        # Step 2 — delete from qBittorrent
        try:
            qbit_ok = self.qbit.delete_torrent(hash, delete_files=True)
        except Exception as exc:
            qbit_ok = False
            reason  = str(exc)
            self.log.error(f"  qBit delete failed: {reason}")
            self.notifier.notify_error(f"qBit delete failed: {name}", reason)
            self.state.record_action(hash, name, rule.category, rule.name,
                                      "failed", arr_success, False)
            if self.config.retry.enabled:
                count = self.state.queue_retry(
                    hash, name, rule.category, rule.name,
                    reason, self.config.retry.interval_seconds,
                )
                self._check_retry_limit(hash, name, count)
            return False

        # Step 3 — record success
        self.state.record_action(hash, name, rule.category, rule.name,
                                  "deleted", arr_success, qbit_ok)
        self.state.resolve_retry(hash)
        self.state.write_log({
            "level": "ACTION", "event": "torrent_deleted",
            "torrent_name": name, "hash": hash,
            "category": rule.category, "rule": rule.name,
            "arr": rule.app, "bad_files": bad_files,
            "arr_blocklisted": arr_success, "qbit_deleted": qbit_ok,
        })
        self.notifier.notify_action(name, bad_files, arr_success, qbit_ok)
        self.log.info(f"  DONE — deleted: {name}")
        return True


    # ------------------------------------------------------------------
    # Retry helpers
    # ------------------------------------------------------------------

    def _process_one_retry(self, entry: dict):
        h        = entry["hash"]
        name     = entry["torrent_name"]
        max_att  = self.config.retry.max_attempts
        attempt  = entry["attempt_count"] + 1

        self.log.info(f"Retry {attempt}/{max_att}: {name}")
        self.state.write_log({
            "level": "INFO", "event": "retry_attempt",
            "hash": h, "torrent_name": name,
            "attempt": attempt, "max": max_att,
        })

        # Confirm the torrent is still in qBit
        try:
            files = self.qbit.get_torrent_files(h)
        except Exception as exc:
            self.log.warning(f"  Could not fetch files for retry {h}: {exc}")
            count = self.state.queue_retry(
                h, name, entry["category"], entry["rule_name"],
                str(exc), self.config.retry.interval_seconds,
            )
            self._check_retry_limit(h, name, count)
            return

        # Confirm the rule still exists
        rule = next((r for r in self.config.rules if r.name == entry["rule_name"]), None)
        if rule is None:
            self.log.warning(f"  Rule '{entry['rule_name']}' removed from config — dropping retry")
            self.state.resolve_retry(h)
            return

        # Re-evaluate (torrent may have been manually cleaned)
        flagged, bad_files = evaluate_rule(rule, files)
        if not flagged:
            self.log.info(f"  No longer flagged — resolving retry for {name}")
            self.state.resolve_retry(h)
            return

        success = self._attempt_action(h, name, rule, bad_files)
        if success:
            self.state.resolve_retry(h)

    def _check_retry_limit(self, hash: str, name: str, attempt_count: int):
        max_att = self.config.retry.max_attempts
        if attempt_count >= max_att:
            self.log.error(f"Retry limit reached ({max_att}) for {name}")
            self.state.write_log({
                "level": "ERROR", "event": "retry_exhausted",
                "hash": hash, "torrent_name": name, "attempts": attempt_count,
            })
            self.notifier.notify_retry_exhausted(name, hash, attempt_count)
