import logging
import re

from .llm_client import narrate
from .notify_targets import describe, send_now, wants
import requests

log = logging.getLogger("inspectarr")


class Notifier:
    """
    Dispatches notifications via Apprise (supports 100+ services).

    Digest mode (notifications.digest.enabled):
      Instead of sending per-event notifications, events are buffered in
      memory. Call flush_digest() after a scan completes to send a single
      summary notification. If use_ollama is true and Ollama is reachable,
      the summary is narrated by the LLM; otherwise a plain bullet list.
      Startup and retry_exhausted notifications are always sent immediately.
    """

    def __init__(self, config, state=None):
        # Optional so nothing breaks if a caller has no StateManager; when it
        # is supplied every send is recorded, including the ones that fail or
        # never leave.
        self._notice_state = state
        self.cfg = config.notifications.apprise
        self.digest_cfg = config.notifications.digest
        # Only expose Ollama to the digest path when the master switch is on;
        # a configured-but-disabled server must not be contacted.
        _ocfg = config.prowlarr.ollama
        self._ollama_url = _ocfg.url if _ocfg.is_active() else ""
        self._ollama_model = _ocfg.model if _ocfg.is_active() else ""
        self._ollama_timeout = config.prowlarr.ollama.timeout
        self._ollama_context_window = getattr(
            config.prowlarr.ollama, "context_window", 8192)
        self._buffer: list[dict] = []
        # The places to send to; each has its own events (notify_targets).
        self._targets = list(getattr(self.cfg, "targets", None) or [])

    @property
    def _digest_mode(self) -> bool:
        return self.digest_cfg.enabled and self.cfg.enabled

    def _should(self, event_type: str) -> bool:
        return any(wants(t, event_type) for t in self._targets)

    def _send(self, title: str, message: str, event: str = "notification",
              targets: list[dict] | None = None):
        """Send to every place that wants `event` (or to `targets`, when the
        caller already chose them). Never raises."""
        chosen = targets if targets is not None else [t for t in self._targets if wants(t, event)]
        if not chosen:
            # Suppressed, not sent. Recorded because "I never got told" and
            # "nobody asked for this" look identical otherwise.
            self._record(event, title, message, "suppressed",
                         "no place to send this is switched on")
            return
        problems = []
        for t in chosen:
            ok, problem = send_now(t["url"], title, message)
            self._target_status(t, ok, problem)
            if not ok:
                log.warning(f"Notification to {describe(t['service'], t['fields'])} failed: {problem}")
                problems.append(f"{describe(t['service'], t['fields'])}: {problem}")
        if len(problems) == len(chosen):
            self._record(event, title, message, "failed", "; ".join(problems))
        else:
            self._record(event, title, message, "sent",
                         ("some places failed: " + "; ".join(problems)) if problems else None)

    def _target_status(self, target: dict, ok: bool, problem: str) -> None:
        if not self._notice_state:
            return
        try:
            self._notice_state.set_notify_status(target["id"], ok, problem)
        except Exception as exc:
            log.debug("Could not record notification status: %s", exc)

    def _record(self, event, title, body, status, detail=None):
        if not self._notice_state:
            return
        try:
            self._notice_state.record_notice(
                "push", event, title, body, status, detail)
        except Exception as exc:
            log.debug("Could not record notice: %s", exc)

    # ------------------------------------------------------------------
    # Event methods — buffer in digest mode, send immediately otherwise
    # ------------------------------------------------------------------

    def notify_startup(self, rules_count: int, dry_run: bool):
        """Always immediate — never buffered."""
        if not self._should("startup"):
            return
        tag = " [DRY RUN]" if dry_run else ""
        self._send(
            f"inspectarr started{tag}",
            f"{rules_count} rule(s) loaded and active.",
            event="startup",
        )

    def notify_shutdown(self, reason: str = ""):
        """Inspectarr is stopping on purpose (docker stop, a restart). Immediate."""
        if not self._should("shutdown"):
            return
        self._send("inspectarr stopped",
                   "Inspectarr was stopped" + (f" ({reason})" if reason else "") +
                   ". Nothing is being checked until it starts again.",
                   event="shutdown")

    def notify_unexpected_stop(self, last_seen: str | None):
        """Sent at start when the previous run never said goodbye: a crash,
        a power cut, or the machine killing it for memory. Nothing can send a
        message at the moment that happens, so this is the earliest it can be
        known."""
        if not self._should("unexpected_stop"):
            return
        when = f" It was last running at {last_seen}." if last_seen else ""
        self._send("inspectarr restarted after an unexpected stop",
                   "Inspectarr stopped without shutting down properly (a crash, a power cut, "
                   f"or it ran out of memory) and has now started again.{when} Releases that "
                   "arrived while it was down are checked on this scan.",
                   event="unexpected_stop")

    def notify_dry_run(self, torrent_name: str, bad_files: list[str]):
        if not self._should("dry_run"):
            return
        files = ", ".join(bad_files[:3])
        if len(bad_files) > 3:
            files += f" (+{len(bad_files) - 3} more)"
        if self._digest_mode:
            self._buffer.append({
                "type": "dry_run",
                "torrent": torrent_name,
                "files": files,
            })
            return
        self._send("[DRY RUN] Would remove",
                   f"{torrent_name}\nBad files: {files}", event="dry_run")

    def notify_quarantine(self, torrent_name: str, bad_files: list[str],
                          risk_level: str = None, paused: bool = True):
        """
        A torrent is being held for review.

        Unlike an action notification this one asks for something: the hold
        waits for a decision. A failed pause is called out explicitly, because
        the torrent is still downloading and that changes how urgent this is.
        """
        # Fall back to "action" when "quarantine" is not listed. Existing
        # configs predate this event, and a hold is the one notification that
        # actually needs a reply -- defaulting it to silent would let torrents
        # pile up unseen. Adding "quarantine" explicitly still works.
        if not (self._should("quarantine") or self._should("action")):
            return
        files = ", ".join(bad_files[:3])
        if len(bad_files) > 3:
            files += f" (+{len(bad_files) - 3} more)"
        level = f"[{risk_level}] " if risk_level else ""
        warn = "" if paused else "\nWARNING: could not pause — still downloading."
        if self._digest_mode:
            self._buffer.append({
                "type": "quarantine",
                "torrent": torrent_name,
                "files": files,
                "risk_level": risk_level,
                "paused": paused,
            })
            return
        self._send(
            f"{level}Quarantined — awaiting review",
            f"{torrent_name}\nBad files: {files}{warn}",
            event="quarantine",
        )

    def notify_action(
        self, torrent_name: str, bad_files: list[str],
        arr_blocklisted: bool, qbit_deleted: bool, app_name: str = "arr"
    ):
        if not self._should("action"):
            return
        arr_status  = "blocklisted" if arr_blocklisted else "FAILED"
        qbit_status = "deleted"     if qbit_deleted    else "qbit FAILED"
        files = ", ".join(bad_files[:3])
        if len(bad_files) > 3:
            files += f" (+{len(bad_files) - 3} more)"
        if self._digest_mode:
            self._buffer.append({
                "type": "action",
                "torrent": torrent_name,
                "files": files,
                "app": app_name,
                "arr_status": arr_status,
                "qbit_status": qbit_status,
            })
            return
        self._send(
            "inspectarr: Torrent removed",
            f"{torrent_name}\nFiles: {files}\n"
            f"{app_name.capitalize()}: {arr_status}  |  Client: {qbit_status}",
            event="action",
        )

    def notify_error(self, context: str, reason: str):
        if not self._should("error"):
            return
        if self._digest_mode:
            self._buffer.append({
                "type": "error",
                "context": context,
                "reason": reason,
            })
            return
        self._send("inspectarr: Error", f"{context}\n{reason}", event="error")

    def notify_retry_exhausted(self, torrent_name: str, hash: str, attempts: int):
        """Always immediate — retry exhaustion is critical."""
        if not self._should("error"):
            return
        self._send(
            "inspectarr: Retry exhausted",
            f"{torrent_name}\nHash: {hash[:12]}...\nFailed after {attempts} attempts.",
            event="retry_exhausted",
        )

    # ------------------------------------------------------------------
    # Digest flush — call after scan completes
    # ------------------------------------------------------------------

    def flush_digest(self):
        """
        Send all buffered events as a single summary notification.
        If use_ollama is enabled and reachable, the summary is AI-narrated.
        Otherwise falls back to a plain bullet-point list.
        Clears the buffer regardless of success.
        """
        if not self._buffer:
            return
        events = list(self._buffer)
        self._buffer.clear()

        # Each place gets only the events it asked for, so places that asked
        # for the same events share one message (and one AI narration).
        groups: dict[tuple, list[dict]] = {}
        for t in self._targets:
            mine = tuple(i for i, e in enumerate(events) if wants(t, e["type"]))
            if mine:
                groups.setdefault(mine, []).append(t)
        for picked, targets in groups.items():
            subset = [events[i] for i in picked]
            body = None
            if self.digest_cfg.use_ollama and self._ollama_url and self._ollama_model:
                body = self._ollama_narrate(subset)
            self._send("inspectarr: Scan Digest", body or self._plain_summary(subset),
                       event="digest", targets=targets)

    def _plain_summary(self, events: list[dict]) -> str:
        """Build a plain-text bullet summary from buffered events."""
        actions  = [e for e in events if e["type"] == "action"]
        dry_runs = [e for e in events if e["type"] == "dry_run"]
        errors   = [e for e in events if e["type"] == "error"]
        holds    = [e for e in events if e["type"] == "quarantine"]

        lines = []
        # Held torrents go FIRST: they are the only event type in this
        # digest that is waiting on a reply. Everything else is a report of
        # something already finished.
        if holds:
            lines.append(f"Quarantined {len(holds)} torrent(s) awaiting review:")
            for e in holds[:5]:
                level = f"[{e['risk_level']}] " if e.get("risk_level") else ""
                # Not paused means it is still downloading, which changes how
                # urgent this is. The single-notification path says so; the
                # digest must too, or the two disagree about the same event.
                warn = "" if e.get("paused", True) else "  (NOT PAUSED)"
                lines.append(f"  - {level}{e.get('torrent')}{warn}")
            if len(holds) > 5:
                lines.append(f"  ... and {len(holds) - 5} more")
            unpaused = [e for e in holds if not e.get("paused", True)]
            if unpaused:
                lines.append(f"  WARNING: {len(unpaused)} could not be paused "
                             f"and are still downloading.")
        if actions:
            lines.append(f"Removed {len(actions)} torrent(s):")
            for e in actions[:5]:
                lines.append(f"  - {e['torrent']} ({e['files']})")
            if len(actions) > 5:
                lines.append(f"  ... and {len(actions) - 5} more")
        if dry_runs:
            lines.append(f"Flagged {len(dry_runs)} torrent(s) [DRY RUN]:")
            for e in dry_runs[:5]:
                lines.append(f"  - {e['torrent']}")
            if len(dry_runs) > 5:
                lines.append(f"  ... and {len(dry_runs) - 5} more")
        if errors:
            lines.append(f"{len(errors)} error(s):")
            for e in errors[:3]:
                lines.append(f"  - {e['context']}: {e['reason']}")
        return "\n".join(lines) if lines else "Scan complete — no events."

    def _ollama_narrate(self, events: list[dict]) -> str | None:
        """
        Send buffered events to Ollama for a narrative digest.
        Returns the narrated string, or None on any failure.
        """
        import json
        prompt = (
            "You are a concise notification writer for a torrent watchdog called inspectarr. "
            "Summarize the following scan events into a brief, readable notification "
            "(max 500 chars). Use plain language, no markdown. Group by event type.\n\n"
            f"Events:\n{json.dumps(events, separators=(',', ':'))}"
        )
        return narrate(self._ollama_url, self._ollama_model, prompt,
                       self._ollama_timeout,
                       context_window=self._ollama_context_window,
                       what="Digest narration")
