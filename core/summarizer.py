"""
core/summarizer.py — Daily/weekly log summary via Apprise + optional Ollama

Queries recent run_history and action log entries, builds a summary, and
sends it via Apprise. If Ollama is configured and reachable, the summary
is narrated by the LLM for a plain-English digest. Otherwise falls back
to a structured plain-text summary.

Called by the scheduler on a daily or weekly cadence (configurable).
Never raises — all failures are logged and swallowed.
"""
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import apprise
import requests

from .llm_client import narrate

log = logging.getLogger("inspectarr")


class LogSummarizer:
    """Generates and sends periodic log summaries."""

    def __init__(self, config, state=None):
        self._notice_state = state
        self.cfg = config
        self.apprise_cfg = config.notifications.apprise
        self.summary = config.notifications.summary
        _ocfg = config.prowlarr.ollama
        self._ollama_url = _ocfg.url if _ocfg.is_active() else ""
        self._ollama_model = _ocfg.model if _ocfg.is_active() else ""
        self._ollama_timeout = config.prowlarr.ollama.timeout
        self._ollama_context_window = getattr(
            config.prowlarr.ollama, "context_window", 4096)
        # Build Apprise instance
        self._apprise = apprise.Apprise()
        for url in self.apprise_cfg.urls:
            self._apprise.add(url)

    def generate_and_send(self, state) -> bool:
        """
        Build a summary from recent data and send via Apprise.
        Returns True if sent, False if skipped or failed.
        """
        if not self.summary.enabled or not self.apprise_cfg.enabled:
            return False

        window_hours = 168 if self.summary.schedule == "weekly" else 24
        label = "Weekly" if self.summary.schedule == "weekly" else "Daily"

        try:
            # Gather data
            runs = state.get_recent_runs(limit=50)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

            # Filter to runs within the window
            recent = [r for r in runs if (r.get("scan_start") or "") >= cutoff]
            if not recent:
                log.info("Summary: no scans in the last %dh — skipping", window_hours)
                return False

            # Aggregate stats
            total_scans = len(recent)
            total_checked = sum(r.get("torrents_checked", 0) for r in recent)
            total_flagged = sum(r.get("flagged", 0) for r in recent)
            total_actioned = sum(r.get("actioned", 0) for r in recent)
            errors = sum(1 for r in recent if r.get("error"))

            # Read recent log entries for richer context
            log_entries = self._read_recent_log(cutoff)

            data = {
                "window": f"Last {window_hours}h",
                "total_scans": total_scans,
                "torrents_checked": total_checked,
                "flagged": total_flagged,
                "actioned": total_actioned,
                "errors": errors,
                "actions": [e for e in log_entries if e.get("event") == "torrent_deleted"][:10],
                "error_events": [e for e in log_entries if e.get("level") == "ERROR"][:5],
            }

            # Try AI narration
            if self.summary.use_ollama and self._ollama_url and self._ollama_model:
                narrated = self._ollama_summarize(data, label)
                if narrated:
                    self._send(f"inspectarr: {label} Summary", narrated)
                    return True
                # Narration was unusable (see _as_prose). Falling through to
                # the plain summary is the point, not a silent degradation:
                # the deterministic version is always correct.
                log.info("Summary narration unusable - sending plain summary")

            # Fallback: plain summary
            self._send(f"inspectarr: {label} Summary", self._plain_summary(data))
            return True

        except Exception as exc:
            log.warning("Summary generation failed: %s", exc)
            return False

    def _read_recent_log(self, cutoff_iso: str) -> list[dict]:
        """Read log entries newer than cutoff from the JSON Lines log."""
        import os
        log_path = self.cfg.logging.log_file
        if not os.path.exists(log_path):
            return []
        entries = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("timestamp", "") >= cutoff_iso:
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            log.warning("Failed to read log entries for summary: %s", exc)
        return entries

    def _plain_summary(self, data: dict) -> str:
        lines = [
            f"{data['window']}:",
            f"  Scans: {data['total_scans']}",
            f"  Torrents checked: {data['torrents_checked']}",
            f"  Flagged: {data['flagged']}",
            f"  Actioned: {data['actioned']}",
            f"  Errors: {data['errors']}",
        ]
        if data["actions"]:
            lines.append("Removed:")
            for a in data["actions"][:5]:
                lines.append(f"  - {a.get('torrent_name', '?')}")
        if data["error_events"]:
            lines.append("Errors:")
            for e in data["error_events"][:3]:
                lines.append(f"  - {e.get('event', '?')}: {e.get('reason', '?')[:60]}")
        return "\n".join(lines)

    def _ollama_summarize(self, data: dict, label: str) -> str | None:
        prompt = (
            f"You are writing a {label.lower()} summary notification for a torrent "
            f"watchdog called inspectarr. Summarize the following activity data into "
            f"a brief, readable notification (max 500 chars). Use plain "
            f"language, no markdown. Highlight anything unusual.\n\n"
            # Compact, not indent=2. This payload carries up to ten action
            # entries and five errors; pretty-printing it spends context on
            # whitespace, and a prompt that overruns the window is how the
            # instructions get lost in the first place.
            f"Data:\n{json.dumps(data, separators=(',', ':'), default=str)}"
        )
        return narrate(self._ollama_url, self._ollama_model, prompt,
                       self._ollama_timeout,
                       context_window=self._ollama_context_window,
                       what="Summary narration")

    def _send(self, title: str, message: str):
        if not self.apprise_cfg.enabled or not self.apprise_cfg.urls:
            self._record(title, message, "suppressed",
                         "notifications disabled"
                         if not self.apprise_cfg.enabled
                         else "no Apprise URLs configured")
            return
        try:
            ok = self._apprise.notify(title=title, body=message)
            self._record(title, message,
                         "sent" if ok is not False else "failed",
                         None if ok is not False else "Apprise reported no "
                         "target accepted the message")
        except Exception as exc:
            log.warning("Apprise summary notification failed: %s", exc)
            self._record(title, message, "failed", str(exc))

    def _record(self, title, body, status, detail=None):
        if not self._notice_state:
            return
        try:
            self._notice_state.record_notice(
                "push", "log_summary", title, body, status, detail)
        except Exception as exc:
            log.debug("Could not record notice: %s", exc)
