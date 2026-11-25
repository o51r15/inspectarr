"""
ui/overview.py — the decisions behind the Overview page, as pure functions.

The sentence at the top of Overview is what a newcomer believes about their
system, so it lives here, free of Flask, with a test for every branch
(tests/test_overview.py). Templates only render what these return.
"""
import re
from dataclasses import dataclass


@dataclass
class Health:
    tone: str                       # ok | info | warn | danger
    title: str
    detail: str
    icon: str = "shield-check"
    action: str | None = None       # data-action name, handled in overview.js
    action_label: str | None = None
    href: str | None = None         # a link instead of an action


def compute_health(status: dict, mode: str, held: int, down: list[str]) -> Health:
    """
    Pick the single most urgent thing to say. Order is the whole point:

      1. a configured service is unreachable  (scans will fail)
      2. the last scan errored
      3. paused                                (nothing is being checked)
      4. releases are waiting for a decision
      5. Monitor mode                          (watching, not acting)
      6. Quarantine mode                       (protected, holding catches)
      7. protected

    "Protected" is a claim, so it is only reachable when the scheduler is
    running, the last scan succeeded and nothing is down.
    """
    status = status or {}
    last = status.get("last_result") or {}

    if down:
        names = ", ".join(down)
        return Health("danger", f"Can't reach {names}",
                      "Scans will fail until the connection is fixed.",
                      icon="plug", href="/system/status", action_label="Check connections")
    if last.get("error"):
        return Health("danger", "The last scan failed", str(last["error"])[:200],
                      icon="alert", href="/system/events", action_label="See events")
    if not status.get("running"):
        return Health("warn", "Paused — nothing is being checked",
                      "New downloads are not inspected until you resume.",
                      icon="pause", action="toggle-scheduler", action_label="Resume")
    if held:
        noun = "release is" if held == 1 else "releases are"
        return Health("warn", f"{held} {noun} waiting for your decision",
                      "Held releases stay paused until you choose what to do.",
                      icon="hand", href="/activity/review", action_label="Review")
    if mode == "monitor":
        return Health("info", "Watching only",
                      "Findings are recorded, but nothing is paused, blocklisted or deleted.",
                      icon="eye", href="/activity/history", action_label="See what it found")
    if mode == "quarantine":
        return Health("ok", "Protected — review mode",
                      "Catches are paused and held for you instead of deleted.")
    return Health("ok", "Protected",
                  "Bad releases are blocklisted and removed automatically.")


_WOULD_HAVE = re.compile(r"would have been (remediate|quarantine)", re.I)


def outcome_label(row: dict) -> tuple[str, str]:
    """
    Plain-English outcome for an inspection row, with a badge tone.

    Mapped from what core/scanner.py actually writes (checked against the
    live database and the scanner source, 2026-11-25):

      action       decision    action_detail
      deleted      remediate   -                       removed
      quarantined  quarantine  -                       held
      dry_run      ...         -                       dry run
      failed       remediate   ...                     action failed, retrying
      recorded     record      "held by operating_mode=monitor
                               (would have been remediate)"   mode stopped it
      recorded     record      -                       below the severity floor

    Note the mode case: `decision` holds the CAPPED value ("record"), so the
    original intent survives only in action_detail. Anything unrecognised
    falls through to "Recorded" rather than guessing.
    """
    action = (row.get("action") or "").lower()
    decision = (row.get("decision") or "").lower()
    detail = row.get("action_detail") or ""

    if action == "deleted":
        return "Removed and blocklisted", "danger"
    if action == "quarantined":
        return "Held for review", "warn"
    if action == "dry_run":
        return "Dry run — no action taken", "violet"
    if action == "failed":
        return "Action failed — will retry", "danger"
    if action == "recorded":
        m = _WOULD_HAVE.search(detail)
        if m:
            verb = "removed" if m.group(1).lower() == "remediate" else "held"
            return f"Would have been {verb}", "info"
        if decision == "record":
            return "Recorded — below your threshold", "neutral"
    return "Recorded", "neutral"


RISK_TONES = {"CRITICAL": "danger", "HIGH": "danger", "MEDIUM": "warn", "LOW": "info"}


def risk_tone(level: str | None) -> str:
    return RISK_TONES.get((level or "").upper(), "neutral")
