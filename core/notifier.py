import logging
import re

import apprise

from .llm_client import narrate
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
            config.prowlarr.ollama, "context_window", 4096)
        self._buffer: list[dict] = []
        # Build the Apprise instance once
        self._apprise = apprise.Apprise()
        for url in self.cfg.urls:
            self._apprise.add(url)

    @property
    def _digest_mode(self) -> bool:
        return self.digest_cfg.enabled and self.cfg.enabled

    def _should(self, event_type: str) -> bool:
        return self.cfg.enabled and event_type in self.cfg.notify_on

    def _send(self, title: str, message: str, event: str = "notification"):
        if not self.cfg.enabled or not self.cfg.urls:
            # Suppressed, not sent. Recorded because "I never got told" and
            # "notifications are switched off" look identical otherwise.
            self._record(event, title, message, "suppressed",
                         "notifications disabled" if not self.cfg.enabled
                         else "no Apprise URLs configured")
            return
        try:
            ok = self._apprise.notify(title=title, body=message)
            # Apprise returns False when every target rejected the message.
            self._record(event, title, message,
                         "sent" if ok is not False else "failed",
                         None if ok is not False else "Apprise reported no "
                         "target accepted the message")
        except Exception as exc:
            log.warning("Apprise notification failed: %s", exc)
            self._record(event, title, message, "failed", str(exc))

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

        if not self.cfg.enabled:
            return

        # Try AI narration first
        if self.digest_cfg.use_ollama and self._ollama_url and self._ollama_model:
            narrated = self._ollama_narrate(events)
            if narrated:
                self._send("inspectarr: Scan Digest", narrated,
                           event="digest")
                return

        # Fallback: plain summary
        self._send("inspectarr: Scan Digest", self._plain_summary(events),
                   event="digest")

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
