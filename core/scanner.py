import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from .config import AppConfig, Rule
from .torrent_client import build_torrent_client
from .arrs.base import AbstractArrClient
from .arrs.sonarr import SonarrClient
from .arrs.radarr import RadarrClient
from .notifier import Notifier
from .rules import evaluate_rule, findings_to_filenames
from . import severity as sev
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
        # Correlation ID for the current scan run. Every inspection and
        # log event produced by one pass shares it (ROADMAP item 22).
        self._scan_id = None


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
        # process_retries can run outside a scan (--retry-now), so it needs a
        # correlation ID of its own rather than inheriting a stale one.
        self._scan_id = str(uuid.uuid4())
        self.log.info(f"Processing {len(due)} retry queue entry/entries")
        for entry in due:
            self._process_one_retry(entry)

    def process_quarantine_timeouts(self) -> int:
        """
        Resolve holds whose timeout has elapsed. Returns how many were acted on.

        Only entries with an expires_at are considered -- the default is to
        hold indefinitely, and a torrent waiting on a person must never be
        swept up by a timer that was never configured.

        The timeout action defaults to `release` because an expiring clock is
        not evidence of guilt: nobody looked, which is not the same as
        somebody deciding it was malicious. Setting it to `remediate` is an
        explicit choice to let the timer delete.
        """
        expired = self.state.get_expired_quarantine()
        if not expired:
            return 0

        action = (getattr(self.config.remediation,
                          "quarantine_timeout_action", "release")
                  or "release").lower()
        if action not in ("release", "remediate"):
            self.log.warning(
                f"Unknown quarantine_timeout_action {action!r} — treating as "
                f"'release' (the non-destructive option)")
            action = "release"

        self.log.info(f"Quarantine: {len(expired)} hold(s) expired — {action}")
        handled = 0
        for entry in expired:
            # Per-entry isolation: one unreachable torrent must not stop the
            # rest of the queue from being processed.
            try:
                if self._expire_one(entry, action):
                    handled += 1
            except Exception as exc:
                self.log.error(
                    f"  Quarantine timeout failed for "
                    f"{entry.get('torrent_name')}: {exc}")
        return handled

    def _expire_one(self, entry: dict, action: str) -> bool:
        h = entry.get("hash")
        name = entry.get("torrent_name") or h

        if action == "release":
            ok = False
            try:
                ok = bool(self.qbit.resume_torrent(h))
            except Exception as exc:
                self.log.warning(f"  Could not resume {name}: {exc}")
            # Resolve either way: the hold has expired and leaving it "held"
            # would make the timer fire again on every pass. The resolution
            # text records whether the resume actually worked.
            self.state.resolve_quarantine(
                h, "released",
                "quarantine timeout elapsed"
                + ("" if ok else " (resume failed)"))
            self.state.write_log({
                "level": "ACTION", "event": "quarantine_timeout_released",
                "inspection_id": entry.get("inspection_id"),
                "torrent_name": name, "hash": h, "resumed": ok,
            })
            self.log.info(f"  Released {name}{'' if ok else ' (resume failed)'}")
            return True

        # action == "remediate"
        arr_ok = False
        try:
            arr_ok = bool(_build_arr_client(
                entry.get("arr_app") or "sonarr", self.config).blocklist(h))
        except Exception as exc:
            self.log.warning(f"  Blocklist failed for {name}: {exc}")
        try:
            deleted = bool(self.qbit.delete_torrent(h, delete_files=True))
        except Exception as exc:
            self.log.error(f"  Delete failed for {name}: {exc}")
            # Stay held rather than claiming a deletion that did not happen.
            return False
        if not deleted:
            self.log.error(f"  Client refused to delete {name} — still held")
            return False

        self.state.record_action(h, name, entry.get("category"),
                                 entry.get("rule_name"), "deleted", arr_ok, True)
        self.state.resolve_quarantine(
            h, "remediated",
            f"quarantine timeout elapsed (blocklist "
            f"{'ok' if arr_ok else 'failed'})")
        self.state.write_log({
            "level": "ACTION", "event": "quarantine_timeout_remediated",
            "inspection_id": entry.get("inspection_id"),
            "torrent_name": name, "hash": h,
            "category": entry.get("category"), "rule": entry.get("rule_name"),
            "arr_blocklisted": arr_ok, "qbit_deleted": True,
        })
        self.log.info(f"  Remediated {name}")
        return True

    def run_scan(self) -> dict:
        self._scan_id = str(uuid.uuid4())
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

        if self.state.has_active_quarantine(h):
            self.log.debug(f"  Skip (held in quarantine): {name}")
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

        # One inspection per flagged release. This id correlates the log
        # events, the arr/client calls and the stored evidence below.
        inspection_id = str(uuid.uuid4())

        # Severity is assessed per finding and aggregated with max, so one
        # executable is never diluted by a pile of low-severity noise.
        rem = getattr(self.config, "remediation", None)
        overrides = getattr(rem, "severity_overrides", {}) or {}
        min_sev = getattr(rem, "min_severity", sev.LOW) or sev.LOW
        assessment = sev.assess(findings, overrides)
        remediate_at = getattr(rem, "remediate_at", sev.LOW) or sev.LOW

        # The thresholds decide which band this belongs in. The operating
        # mode decides how far we are allowed to act on that -- two separate
        # questions, kept separate, so both answers stay readable in the log.
        raw_decision = sev.decide(assessment["risk_level"], min_sev, remediate_at)
        mode = getattr(rem, "operating_mode", sev.DEFAULT_MODE) or sev.DEFAULT_MODE

        self.log.info(
            f"FLAGGED [{rule.name}] {name} | bad: {bad_files} "
            f"| risk={sev.explain(assessment['risk_level'], assessment['counts'])} "
            f"| decision={raw_decision} | mode={mode} | inspection={inspection_id}"
        )

        file_count = len(files)
        total_size = sum(f.get("size", 0) or 0 for f in files)

        # Was this torrent the replacement for something we rejected earlier?
        # If so the cycle repeated, and that is worth recording regardless of
        # what the mode lets us do about it now (ROADMAP item 27).
        try:
            watched = self.state.find_replacement_by_hash(h)
            if watched:
                self.state.update_replacement(
                    watched["id"], status="rejected",
                    outcome_detail=(
                        f"replacement from {watched.get('replacement_indexer')} "
                        f"was itself flagged by rule '{rule.name}'"),
                    resolved_at=datetime.now(timezone.utc).isoformat())
                self.log.warning(
                    f"  Replacement for '{watched.get('original_name')}' is "
                    f"ALSO bad — {watched.get('original_indexer')} -> "
                    f"{watched.get('replacement_indexer')}")
                self.state.write_log({
                    "level": "WARNING", "event": "replacement_rejected",
                    "inspection_id": inspection_id,
                    "torrent_name": name, "hash": h,
                    "original_name": watched.get("original_name"),
                    "original_indexer": watched.get("original_indexer"),
                    "replacement_indexer": watched.get("replacement_indexer"),
                    "rule": rule.name,
                })
        except Exception as exc:
            # Observational only -- must never break an evaluation.
            self.log.debug(f"  Replacement lookup failed: {exc}")

        if raw_decision == sev.RECORD:
            # Flagged, but below the configured remediation floor. The
            # evidence is still recorded -- that is the whole point of having
            # a floor rather than simply not matching.
            self.log.info(
                f"  Below remediation floor ({min_sev}) — recording only: {name}")
            self.state.write_log({
                "level": "INFO", "event": "below_severity_floor",
                "inspection_id": inspection_id,
                "torrent_name": name, "hash": h,
                "category": rule.category, "rule": rule.name,
                "risk_level": assessment["risk_level"],
                "min_severity": min_sev, "bad_files": bad_files,
            })
            self._record_inspection(
                inspection_id, findings, h, name, rule, action="recorded",
                file_count=file_count, total_size=total_size,
                assessment=assessment, decision=raw_decision)
            return True, False

        if self.config.dry_run:
            # Checked before the cap so that --dry-run combined with a mode
            # still takes the dry-run path and still notifies. The flag is a
            # per-run override; the mode is the persistent setting.
            self._handle_dry_run(h, name, rule, bad_files,
                                 inspection_id=inspection_id, findings=findings,
                                 assessment=assessment, decision=raw_decision)
            return True, False

        # Apply the ceiling. This can only reduce the decision, never raise
        # it, so nothing below can become more destructive than the
        # thresholds already permitted.
        decision = sev.cap_for_mode(raw_decision, mode)

        if decision != raw_decision:
            self.log.info(
                f"  Operating mode '{mode}' caps {raw_decision} "
                f"-> {decision}: {name}")
            self.state.write_log({
                "level": "INFO", "event": "capped_by_operating_mode",
                "inspection_id": inspection_id,
                "torrent_name": name, "hash": h,
                "category": rule.category, "rule": rule.name,
                "risk_level": assessment["risk_level"],
                "operating_mode": mode,
                "decision_before_mode": raw_decision,
                "decision": decision,
                "bad_files": bad_files,
            })

        if decision == sev.RECORD:
            # The mode, not the floor, stopped this one. Recorded with the
            # full evidence so the Quarantine and Events pages can show
            # exactly what would have happened in automatic mode.
            self._record_inspection(
                inspection_id, findings, h, name, rule, action="recorded",
                file_count=file_count, total_size=total_size,
                assessment=assessment, decision=decision,
                action_detail=f"held by operating_mode={mode} "
                              f"(would have been {raw_decision})")
            return True, False

        if decision == sev.QUARANTINE:
            self._handle_quarantine(h, name, rule, bad_files,
                                    inspection_id=inspection_id,
                                    findings=findings, assessment=assessment,
                                    file_count=file_count,
                                    total_size=total_size)
            return True, False

        actioned = self._attempt_action(
            h, name, rule, bad_files,
            inspection_id=inspection_id, findings=findings,
            file_count=file_count, total_size=total_size,
            assessment=assessment, decision=decision,
        )
        return True, actioned


    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _record_inspection(self, inspection_id, findings, hash, name, rule,
                           action, arr_success=None, client_success=None,
                           action_detail=None, indexer=None, file_count=None,
                           total_size=None, assessment=None, decision=None):
        """
        Persist the evidence for one flagged release (ROADMAP item 23).

        Called at every terminal point of the action paths so each outcome --
        deleted, dry-run, aborted, failed -- states explicitly what happened
        rather than being inferred later from log ordering.

        Never raises: the state layer swallows and logs its own failures, and
        recording evidence must not be able to break a scan.
        """
        levels = (assessment or {}).get("severities") or []
        _rem = getattr(self.config, "remediation", None)
        _mode = getattr(_rem, "operating_mode", sev.DEFAULT_MODE) or sev.DEFAULT_MODE
        self.state.record_inspection(
            {
                "inspection_id":  inspection_id,
                "operating_mode": _mode,
                "scan_id":        self._scan_id,
                "torrent_hash":   hash,
                "torrent_name":   name,
                "category":       rule.category,
                "rule_name":      rule.name,
                "arr_app":        rule.app,
                "indexer":        indexer,
                "file_count":     file_count,
                "total_size":     total_size,
                "flagged":        1,
                "risk_level":     (assessment or {}).get("risk_level"),
                "risk_score":     (assessment or {}).get("risk_score"),
                "decision":       decision,
                "action":         action,
                "action_detail":  action_detail,
                "arr_success":    None if arr_success is None else int(arr_success),
                "client_success": None if client_success is None else int(client_success),
            },
            [
                {
                    "signal":    f.signal,
                    "detail":    f.detail,
                    "file_path": f.file_path,
                    "file_size": f.file_size,
                    # Positionally aligned with `findings` by assess().
                    "severity":  (levels[i] if i < len(levels) else None),
                }
                for i, f in enumerate(findings or [])
            ],
        )

    def _handle_quarantine(self, hash: str, name: str, rule: Rule,
                           bad_files: list[str], inspection_id: str = None,
                           findings: list = None, assessment: dict = None,
                           file_count: int = None, total_size: int = None):
        """
        Hold a torrent for review instead of deleting it.

        Pausing is attempted first and its outcome recorded. A failed pause
        does NOT abort the hold: the torrent still needs a human decision and
        must appear in the queue. But paused_ok=0 is stored so the UI can say
        the torrent is still downloading rather than implying it is contained
        -- silently claiming safety we did not achieve would be worse than
        reporting the failure.
        """
        paused = False
        try:
            paused = bool(self.qbit.pause_torrent(hash))
            if not paused:
                self.log.warning(f"  Could not pause {name} — holding anyway")
        except Exception as exc:
            self.log.warning(f"  Pause failed for {name}: {exc} — holding anyway")

        expires_at = None
        timeout_min = getattr(self.config.remediation,
                              "quarantine_timeout_minutes", 0) or 0
        if timeout_min > 0:
            expires_at = (datetime.now(timezone.utc)
                          + timedelta(minutes=timeout_min)).isoformat()

        risk_level = (assessment or {}).get("risk_level")
        self.state.add_quarantine({
            "hash": hash, "inspection_id": inspection_id,
            "torrent_name": name, "category": rule.category,
            "rule_name": rule.name, "arr_app": rule.app,
            "risk_level": risk_level,
            "risk_score": (assessment or {}).get("risk_score"),
            "bad_files": bad_files, "expires_at": expires_at,
            "status": "held", "paused_ok": paused,
        })

        self.state.write_log({
            "level": "ACTION", "event": "torrent_quarantined",
            "inspection_id": inspection_id,
            "torrent_name": name, "hash": hash,
            "category": rule.category, "rule": rule.name,
            "risk_level": risk_level, "bad_files": bad_files,
            "paused": paused, "expires_at": expires_at,
        })
        self._record_inspection(
            inspection_id, findings, hash, name, rule, action="quarantined",
            client_success=paused, file_count=file_count,
            total_size=total_size, assessment=assessment,
            decision=sev.QUARANTINE,
            action_detail=None if paused else "pause failed; held unpaused")

        try:
            self.notifier.notify_quarantine(name, bad_files, risk_level, paused)
        except AttributeError:
            # Older notifier without the quarantine event -- fall back rather
            # than losing the alert entirely.
            self.notifier.notify_dry_run(name, bad_files)
        except Exception as exc:
            self.log.warning(f"  Quarantine notification failed: {exc}")

        self.log.info(f"  QUARANTINED [{risk_level}] {name}"
                      f"{'' if paused else ' (pause failed)'}")

    def _handle_dry_run(self, hash: str, name: str, rule: Rule, bad_files: list[str],
                        inspection_id: str = None, findings: list = None,
                        assessment: dict = None, decision: str = None):
        self.log.info(f"  [DRY RUN] Would delete: {name}")
        self.state.write_log({
            "level": "DRY_RUN", "event": "dry_run_flagged",
            "inspection_id": inspection_id,
            "risk_level": (assessment or {}).get("risk_level"),
            "decision": decision,
            "torrent_name": name, "hash": hash,
            "category": rule.category, "rule": rule.name, "bad_files": bad_files,
        })
        self.state.record_action(hash, name, rule.category, rule.name,
                                  "dry_run", False, False)
        self._record_inspection(inspection_id, findings, hash, name, rule,
                                action="dry_run", assessment=assessment,
                                decision=decision)
        self.notifier.notify_dry_run(name, bad_files)

    def _attempt_action(
        self, hash: str, name: str, rule: Rule, bad_files: list[str],
        inspection_id: str = None, findings: list = None,
        file_count: int = None, total_size: int = None,
        assessment: dict = None, decision: str = None,
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

        # Step 0b — capture what identifies this release to the arr, so a
        # replacement can be recognised later (ROADMAP item 27).
        #
        # Done here, before blocklisting, for the same reason attribution is:
        # blocklisting appends new history events, and while episodeId/movieId
        # do survive that, reading identity and indexer from the same snapshot
        # keeps the two consistent. Independent of Prowlarr -- replacement
        # tracking is useful without indexer scoring enabled.
        watch = None
        if getattr(self.config.remediation, "track_replacements", False):
            watch = self._capture_replacement_identity(
                arr_client, hash, name, rule, indexer_name)

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
                "inspection_id": inspection_id,
                "torrent_name": name, "hash": hash,
                "arr": rule.app, "reason": reason,
                "on_arr_failure": self.config.on_arr_failure,
            })
            self.notifier.notify_error(f"Arr failure ({rule.app}): {name}", reason)
            if self.config.on_arr_failure == "abort":
                self.state.record_action(hash, name, rule.category, rule.name,
                                          "failed", False, False)
                self._record_inspection(
                    inspection_id, findings, hash, name, rule,
                    action="failed", arr_success=False, client_success=False,
                    action_detail=f"arr blocklist failed: {reason}",
                    indexer=indexer_name, file_count=file_count,
                    total_size=total_size,
                    assessment=assessment, decision=decision,
                )
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
            # The arr-failure path above writes a structured event but this
            # one never did, so a torrent-client delete failure was invisible
            # on the Events page -- visible only as a notification and a line
            # in the process log. Symmetry matters here: this is a failed
            # destructive action and must leave a trace in the action log.
            self.state.write_log({
                "level": "ERROR", "event": "client_delete_failed",
                "inspection_id": inspection_id,
                "torrent_name": name, "hash": hash,
                "category": rule.category, "rule": rule.name,
                "client": self.config.torrent_client,
                "reason": reason, "arr_blocklisted": arr_success,
            })
            self.notifier.notify_error(f"qBit delete failed: {name}", reason)
            self.state.record_action(hash, name, rule.category, rule.name,
                                      "failed", arr_success, False)
            self._record_inspection(
                inspection_id, findings, hash, name, rule,
                action="failed", arr_success=arr_success, client_success=False,
                action_detail=f"torrent client delete failed: {reason}",
                indexer=indexer_name, file_count=file_count,
                total_size=total_size,
                assessment=assessment, decision=decision,
            )
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
        self._record_inspection(
            inspection_id, findings, hash, name, rule,
            action="deleted", arr_success=arr_success, client_success=qbit_ok,
            indexer=indexer_name, file_count=file_count, total_size=total_size,
                                                         assessment=assessment, decision=decision,
        )
        self.state.write_log({
            "level": "ACTION", "event": "torrent_deleted",
            "inspection_id": inspection_id,
            "risk_level": (assessment or {}).get("risk_level"),
            "decision": decision,
            "torrent_name": name, "hash": hash,
            "category": rule.category, "rule": rule.name,
            "arr": rule.app, "bad_files": bad_files,
            "arr_blocklisted": arr_success, "qbit_deleted": qbit_ok,
        })
        self.notifier.notify_action(name, bad_files, arr_success, qbit_ok, rule.app)
        self.log.info(f"  DONE — deleted: {name}")

        # Watch for a replacement. Opened only now, after the removal really
        # happened: a failed remediation leaves the release in place, so
        # there is nothing for the arr to replace and a watch would sit open
        # until it timed out and reported a false "never replaced".
        if watch:
            self.state.open_replacement_watch(watch)
            self.log.debug(
                f"  Watching for a replacement ({watch['media_field']}="
                f"{watch['media_id']}): {name}")

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

        # A retry that reaches here is a fresh action attempt, so it opens
        # its own inspection rather than mutating the original.
        success = self._attempt_action(
            h, name, rule, bad_files,
            inspection_id=str(uuid.uuid4()), findings=retry_findings,
            file_count=len(files),
            total_size=sum(f.get("size", 0) or 0 for f in files),
        )
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

    # ------------------------------------------------------------------
    # Replacement tracking (ROADMAP item 27)
    # ------------------------------------------------------------------

    # A sweep must never become the slow part of a scan. Both caps are
    # deliberate and are logged when they bite, so a truncated sweep is
    # never mistaken for a complete one.
    MAX_REPLACEMENT_CHECKS = 25

    def _capture_replacement_identity(self, arr_client, hash: str, name: str,
                                      rule, indexer_name: str = None) -> dict | None:
        """
        Build a watch record, or None if this release cannot be watched.

        Returns None rather than raising for every "cannot": an arr that does
        not support scoped history (Lidarr), a release the arr never knew
        about (a manual client add), or an unreachable arr. None of those are
        reasons to fail a remediation that is otherwise fine.
        """
        try:
            if not getattr(arr_client, "MEDIA_HISTORY_VERIFIED", False):
                return None
            record = arr_client.find_in_history(hash)
            if not record:
                return None
            media_id = arr_client.media_id_of(record)
            if media_id in (None, ""):
                return None
            return {
                "inspection_id":    None,
                "original_hash":    (hash or "").upper(),
                "original_name":    name,
                "original_indexer": indexer_name or arr_client.get_grab_indexer(hash),
                "arr_app":          rule.app,
                "media_id":         media_id,
                "media_field":      arr_client.MEDIA_ID_FIELD,
                "rejected_at":      datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            self.log.debug(f"  Could not capture replacement identity: {exc}")
            return None

    @staticmethod
    def _replacement_backoff_minutes(check_count: int) -> int:
        """
        How long to wait before re-checking a watch.

        Replacements usually arrive quickly or not at all, so checking often
        at first and rarely later costs very little and still catches the
        slow ones: 10, 20, 40, 80, 160, 320 minutes, then capped at 6 hours.
        A 72-hour window works out at roughly ten checks per rejection rather
        than one per scan cycle, which at a 15-minute poll would be ~288.
        """
        return min(10 * (2 ** max(0, check_count)), 360)

    def process_replacement_watches(self) -> int:
        """
        Advance open replacement watches. Returns how many were resolved.

        Purely observational: this never pauses, deletes or blocklists
        anything. The worst a bug in here can do is record a wrong
        statistic, which is why every arr call is wrapped per-entry -- one
        unreachable arr must not stop the rest of the queue, matching the
        per-torrent isolation in run_scan (BUG-01).
        """
        rem = getattr(self.config, "remediation", None)
        if not getattr(rem, "track_replacements", False):
            return 0

        watches = self.state.get_open_replacements()
        if not watches:
            return 0

        window_hours = int(getattr(rem, "replacement_window_hours", 72) or 72)
        now = datetime.now(timezone.utc)
        resolved = 0
        checked = 0

        for w in watches:
            if checked >= self.MAX_REPLACEMENT_CHECKS:
                self.log.info(
                    f"Replacements: stopping at {self.MAX_REPLACEMENT_CHECKS} "
                    f"checks this pass; {len(watches) - checked} watch(es) "
                    f"deferred to the next scan")
                break
            try:
                if self._advance_replacement_watch(w, now, window_hours):
                    resolved += 1
                checked += 1
            except Exception as exc:
                self.log.warning(
                    f"Replacement watch {w.get('id')} failed: {exc}")
                self.state.update_replacement(
                    w["id"], last_checked=now.isoformat(),
                    check_count=int(w.get("check_count") or 0) + 1)

        return resolved

    def _advance_replacement_watch(self, w: dict, now, window_hours: int) -> bool:
        """One watch. Returns True if it reached a final state."""
        rejected_at = w.get("rejected_at") or ""
        count = int(w.get("check_count") or 0)

        # Backoff -- not due yet.
        last = w.get("last_checked")
        if last:
            try:
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
                if elapsed < self._replacement_backoff_minutes(count):
                    return False
            except Exception:
                pass   # unparseable timestamp: check it now and overwrite

        # Window expired.
        try:
            age_h = (now - datetime.fromisoformat(rejected_at)).total_seconds() / 3600
        except Exception:
            age_h = 0
        if age_h > window_hours:
            if w.get("status") == "grabbed":
                # Something was grabbed but never imported and we never
                # flagged it. Unknown is not the same as never replaced, and
                # recording it as the latter would understate the indexer.
                detail = "replacement grabbed but never imported within the window"
                status = "abandoned"
            else:
                detail = f"no replacement grabbed within {window_hours}h"
                status = "abandoned"
            self.state.update_replacement(
                w["id"], status=status, outcome_detail=detail,
                resolved_at=now.isoformat(), last_checked=now.isoformat(),
                check_count=count + 1)
            self.state.write_log({
                "level": "INFO", "event": "replacement_abandoned",
                "torrent_name": w.get("original_name"),
                "hash": w.get("original_hash"),
                "indexer": w.get("original_indexer"),
                "arr": w.get("arr_app"), "reason": detail,
            })
            return True

        arr_client = _build_arr_client(w["arr_app"], self.config)
        media_id = w.get("media_id")

        if w.get("status") == "pending":
            found = arr_client.find_replacement_grab(
                media_id, rejected_at, exclude_hash=w.get("original_hash"))
            if not found:
                self.state.update_replacement(
                    w["id"], last_checked=now.isoformat(), check_count=count + 1)
                return False
            self.state.update_replacement(
                w["id"], status="grabbed",
                replacement_hash=found.get("hash"),
                replacement_name=found.get("title"),
                replacement_indexer=found.get("indexer"),
                grabbed_at=found.get("grabbed_at"),
                last_checked=now.isoformat(), check_count=count + 1)
            self.log.info(
                f"Replacement grabbed for {w.get('original_name')} "
                f"from {found.get('indexer')}")
            self.state.write_log({
                "level": "INFO", "event": "replacement_grabbed",
                "torrent_name": w.get("original_name"),
                "hash": w.get("original_hash"),
                "original_indexer": w.get("original_indexer"),
                "replacement_indexer": found.get("indexer"),
                "replacement_name": found.get("title"),
                "arr": w.get("arr_app"),
            })
            w = dict(w, status="grabbed", grabbed_at=found.get("grabbed_at"),
                     replacement_hash=found.get("hash"),
                     replacement_indexer=found.get("indexer"))
            count += 1

        if w.get("status") == "grabbed":
            # Success is the arr importing it. Inspectarr only records
            # inspections for FLAGGED releases, so a clean replacement leaves
            # no evidence of its own -- the import is the observable proof it
            # downloaded and passed. The rejected case is handled elsewhere,
            # at the moment we flag it.
            if arr_client.was_imported(media_id, w.get("grabbed_at") or rejected_at,
                                       w.get("replacement_hash")):
                self.state.update_replacement(
                    w["id"], status="imported",
                    outcome_detail=f"replacement from "
                                   f"{w.get('replacement_indexer')} imported cleanly",
                    resolved_at=now.isoformat(), last_checked=now.isoformat(),
                    check_count=count + 1)
                self.state.write_log({
                    "level": "INFO", "event": "replacement_imported",
                    "torrent_name": w.get("original_name"),
                    "hash": w.get("original_hash"),
                    "original_indexer": w.get("original_indexer"),
                    "replacement_indexer": w.get("replacement_indexer"),
                    "arr": w.get("arr_app"),
                })
                return True
            self.state.update_replacement(
                w["id"], last_checked=now.isoformat(), check_count=count + 1)

        return False

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
