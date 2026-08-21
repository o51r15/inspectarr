import logging
import re
from datetime import datetime, timezone
from .config import AppConfig, Rule
from .torrent_client import build_torrent_client
from .arrs.base import AbstractArrClient
from .arrs.sonarr import SonarrClient
from .arrs.radarr import RadarrClient
from .notifier import Notifier
from .rules import evaluate_rule, findings_to_filenames
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
    if app_name == "lidarr":
        from .arrs.lidarr import LidarrClient
        c = config.arrs.lidarr
        return LidarrClient(c.url, c.api_key)
    raise ValueError(f"Unknown arr app: {app_name!r}")


def _normalize_indexer_name(name: str) -> str:
    """
    Normalize an indexer name for cross-system matching.

    When Prowlarr syncs an indexer to Sonarr/Radarr/Lidarr, the arr appends
    " (Prowlarr)" to the name. So the arr's grab history shows
    "TorrentProject2 (Prowlarr)" while Prowlarr's own /api/v1/indexer list
    returns "TorrentProject2". Stripping that trailing suffix (and lowercasing)
    lets the two sides match. Documented behaviour — see Servarr wiki.
    """
    return re.sub(r"\s*\(prowlarr\)\s*$", "", name or "", flags=re.IGNORECASE).strip().lower()


class Scanner:
    def __init__(self, config: AppConfig, state: StateManager = None):
        self.config  = config
        self.log     = _get_logger(config.logging.level)
        # IMP-3: accept a shared StateManager to avoid opening a new SQLite
        # connection per scan cycle. Falls back to creating its own if none provided.
        self.state   = state or StateManager(
            db_path=config.state.db_file,
            log_path=config.logging.log_file,
            retention_days=config.logging.retention_days,
        )
        self.qbit     = build_torrent_client(config)
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
        # Track unique torrent hashes seen this scan so a torrent matched by
        # more than one rule on the same category isn't counted multiple times.
        checked_hashes: set[str] = set()
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
                # BUG-01: per-torrent try/except so one failure can't zero the
                # entire scan's stats by propagating to _execute_scan.
                try:
                    flagged, actioned = self._evaluate_torrent(torrent, rule)
                except Exception as exc:
                    self.log.error(
                        f"  Unexpected error evaluating "
                        f"'{torrent.get('name', torrent.get('hash', '?'))}': {exc}"
                    )
                    self.state.write_log({
                        "level":        "ERROR",
                        "event":        "evaluation_error",
                        "torrent_name": torrent.get("name", torrent.get("hash", "?")),
                        "hash":         torrent.get("hash", "?"),
                        "reason":       str(exc),
                    })
                    continue
                checked_hashes.add(torrent["hash"])
                if flagged:
                    stats["flagged"] += 1
                    # BUG-02: set last_flagged on any detection, not just actioned.
                    # Dry-run and retry-queued detections were previously invisible
                    # to get_last_detection() because last_flagged stayed None.
                    stats["last_flagged"] = {
                        "torrent_name": torrent.get("name", torrent["hash"]),
                        "rule": rule.name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    if actioned:
                        stats["actioned"] += 1

        stats["torrents_checked"] = len(checked_hashes)
        self.state.write_log({
            "level": "INFO", "event": "scan_complete", **stats
        })
        # Flush digest buffer — sends a single summary notification if digest
        # mode is enabled and events were buffered during this scan.
        self.notifier.flush_digest()
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

        if self.config.retry.enabled and self.state.has_active_retry(h):
            self.log.debug(f"  Skip (retry queue owns it): {name}")
            return False, False

        try:
            files = self.qbit.get_torrent_files(h)
        except Exception as exc:
            self.log.warning(f"  Could not get files for {name}: {exc}")
            return False, False

        flagged, findings = evaluate_rule(rule, files)
        bad_files = findings_to_filenames(findings)

        if not flagged:
            # Attribute grab for clean torrents (first sight only, deduped)
            if self.config.prowlarr.enabled:
                self._record_grab_attribution(h, name, rule.app)
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

        # Step 0 — grab attribution BEFORE blocklisting.
        # Must happen before the arr blocklist call because blocklisting adds a
        # new history event (e.g. downloadFailed) which would become records[0],
        # hiding the original grabbed event that carries data.indexer.
        indexer_id, indexer_name = None, None
        if self.config.prowlarr.enabled:
            indexer_id, indexer_name = self._record_grab_attribution(
                hash, name, rule.app
            )

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
        self.notifier.notify_action(name, bad_files, arr_success, qbit_ok, rule.app)
        self.log.info(f"  DONE — deleted: {name}")

        # Step 4 — record malicious hit against the indexer
        # BUG-03: when attribution failed (indexer_id is None) emit a WARNING
        # rather than silently dropping the hit with no trace in the logs.
        if self.config.prowlarr.enabled:
            if indexer_id:
                self._record_malicious_hit(hash, name, indexer_id, indexer_name)
            else:
                self.log.warning(
                    f"  Malicious hit NOT recorded for '{name}' — indexer "
                    f"attribution failed (no indexer_id). Check for "
                    f"grab_attribution_no_match / grab_attribution_error events."
                )
                self.state.write_log({
                    "level":        "WARNING",
                    "event":        "malicious_hit_skipped",
                    "torrent_name": name,
                    "hash":         hash,
                    "reason":       "indexer attribution failed — indexer_id is None",
                })

        return True


    # ------------------------------------------------------------------
    # Retry helpers
    # ------------------------------------------------------------------

    def _process_one_retry(self, entry: dict):
        # BUG-04 (by design, documented): when a retry here succeeds,
        # _attempt_action() records the deletion in processed_hashes and
        # calls write_log, but there is no path back to update the originating
        # scan's run_history row. That row permanently shows actioned=0 even
        # after the torrent is resolved. The flagged count was correct at the
        # time; actioned will always lag flagged by the number of retry-resolved
        # torrents. This is expected — do not treat the gap as a bug.
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
        flagged, retry_findings = evaluate_rule(rule, files)
        bad_files = findings_to_filenames(retry_findings)
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

    def _record_grab_attribution(
        self, torrent_hash: str, name: str, app: str
    ) -> tuple[int | None, str | None]:
        """
        Look up which Prowlarr indexer served this torrent via *arr grab history
        and record it. Increments total_grabs on first sight only (deduped via
        seen_torrents table). Returns (indexer_id, indexer_name) or (None, None).

        Must be called BEFORE blocklisting so the original grabbed event is still
        records[0] in arr history (blocklisting adds a new event that would hide it).
        Non-fatal — all failures are logged at DEBUG only.
        """
        # Return cached result if we've already attributed this torrent
        seen = self.state.get_torrent_seen(torrent_hash)
        if seen:
            return seen.get("indexer_id"), seen.get("indexer_name")

        try:
            arr_client   = _build_arr_client(app, self.config)
            indexer_name = arr_client.get_grab_indexer(torrent_hash)
            if not indexer_name:
                self.log.debug(
                    f"No indexer found in {app} history for {torrent_hash} — "
                    f"skipping grab attribution"
                )
                return None, None

            from .prowlarr import ProwlarrClient
            prowlarr = ProwlarrClient(
                self.config.prowlarr.url,
                self.config.prowlarr.api_key,
            )
            indexers = prowlarr.get_torrent_indexers()
            target   = _normalize_indexer_name(indexer_name)
            match = next(
                (i for i in indexers if _normalize_indexer_name(i["name"]) == target),
                None,
            )
            if not match:
                available = ", ".join(i["name"] for i in indexers) or "(none)"
                self.log.warning(
                    f"Indexer '{indexer_name}' from {app} history did not match "
                    f"any Prowlarr torrent indexer. Available: {available}"
                )
                self.state.write_log({
                    "level":            "WARNING",
                    "event":            "grab_attribution_no_match",
                    "torrent_name":     name,
                    "hash":             torrent_hash,
                    "arr_indexer_name": indexer_name,
                    "available":        [i["name"] for i in indexers],
                })
                return None, None

            iid   = match["id"]
            iname = match["name"]  # canonical name from Prowlarr

            is_new = self.state.record_torrent_seen(torrent_hash, iid, iname)
            if is_new:
                count = self.state.increment_total_grabs(iid, iname)
                self.log.info(
                    f"Grab attributed — indexer: '{iname}' "
                    f"(total grabs: {count})"
                )
                self.state.write_log({
                    "level":        "INFO",
                    "event":        "grab_attributed",
                    "torrent_name": name,
                    "hash":         torrent_hash,
                    "indexer":      iname,
                    "indexer_id":   iid,
                    "total_grabs":  count,
                })

            return iid, iname

        except Exception as exc:
            self.log.warning(f"Could not attribute grab for '{name}': {exc}")
            self.state.write_log({
                "level":        "WARNING",
                "event":        "grab_attribution_error",
                "torrent_name": name,
                "hash":         torrent_hash,
                "reason":       str(exc),
            })
            return None, None

    def _record_malicious_hit(
        self, torrent_hash: str, name: str, indexer_id: int, indexer_name: str
    ):
        """
        Record a malicious hit against the given indexer.
        indexer_id and indexer_name come from _record_grab_attribution so no
        additional API calls are needed here.
        Non-fatal — all failures are logged at DEBUG only.
        """
        try:
            count = self.state.increment_malicious_hit(indexer_id, indexer_name)
            self.log.info(
                f"Malicious hit recorded — indexer: '{indexer_name}' "
                f"(total: {count})"
            )
            self.state.write_log({
                "level":        "INFO",
                "event":        "malicious_hit_recorded",
                "torrent_name": name,
                "hash":         torrent_hash,
                "indexer":      indexer_name,
                "indexer_id":   indexer_id,
                "total_hits":   count,
            })
        except Exception as exc:
            self.log.debug(f"Could not record malicious hit for '{name}': {exc}")
