"""
core/lifecycle.py -- "Inspectarr stopped" and "stopped unexpectedly" messages.

A clean stop (docker stop, a restart, Ctrl+C) says so as it happens. A crash,
a power cut or the machine killing the process for memory can't send anything
at that moment, so each run leaves a mark ("running") and the next start
checks for it: a mark nobody cleared means the last run never said goodbye.

Both go to the places that ticked "Inspectarr started, stopped, or stopped
unexpectedly". Neither ever raises: telling you about a stop must never be
what stops a start.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("inspectarr")

RUNNING_KEY = "lifecycle_running"        # "1" while a run is up, "0" after a clean stop
SINCE_KEY = "lifecycle_running_since"
SEND_WAIT_S = 7                          # docker stop allows 10 s before it kills


def _notifier(config_path, state):
    from .config import load_config
    from .notifier import Notifier
    return Notifier(load_config(config_path), state=state)


def on_start(state, config_path: str) -> bool:
    """Call once when the app starts. Returns True if the last run had stopped
    unexpectedly (and a message was attempted)."""
    if state is None:
        return False
    unexpected = False
    try:
        if state.get_app_state(RUNNING_KEY) == "1":
            unexpected = True
            since = state.get_app_state(SINCE_KEY)
            log.warning("The last run of Inspectarr stopped without shutting down properly"
                        + (f" (it had been running since {since})" if since else ""))
            _notifier(config_path, state).notify_unexpected_stop(_readable(since))
    except Exception as exc:
        log.warning(f"Could not check how the last run stopped: {exc}")
    try:
        state.set_app_state(RUNNING_KEY, "1")
        state.set_app_state(SINCE_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as exc:
        log.warning(f"Could not record that Inspectarr is running: {exc}")
    return unexpected


def on_stop(state, config_path: str, reason: str = "") -> None:
    """Call on a clean stop. Sends "stopped" (waiting at most SEND_WAIT_S),
    then clears the running mark so the next start doesn't call it a crash."""
    if state is None:
        return
    sender = threading.Thread(target=_send_stop, args=(state, config_path, reason),
                              name="inspectarr-stop-notice", daemon=True)
    sender.start()
    sender.join(timeout=SEND_WAIT_S)
    try:
        state.set_app_state(RUNNING_KEY, "0")
    except Exception as exc:
        log.warning(f"Could not record the clean stop: {exc}")


def _send_stop(state, config_path, reason):
    try:
        _notifier(config_path, state).notify_shutdown(reason)
    except Exception as exc:
        log.warning(f"Could not send the stopped message: {exc}")


def _readable(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
