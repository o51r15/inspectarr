"""
core/ai_turn.py -- one thing at a time on the AI server.

Calibration loads models, changes context sizes and runs for minutes. Live
scoring at the same moment would slow both, make Ollama reload models back
and forth, and leave calibration's timings meaning nothing. So the AI is
used by one job at a time, the way DockLlama's tests and checks take turns.

    with ai_turn("calibration", "gemma4:latest", wait=None):   # waits
        ...
    with ai_turn("scoring", "", wait=30) as got:               # gives up
        if not got: ...

Reentrant: calibration holds the turn for its whole run, and the scoring
calls it makes itself go straight through. Another thread finds it busy.

Live scoring gives up rather than waiting out a calibration: its fallback
(Inspectarr's own scores, or the cached AI scores) is fine for a round, and
a scan must never sit for twenty minutes behind a benchmark.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_lock = threading.RLock()
_holder: dict = {}


@contextmanager
def ai_turn(kind: str, label: str = "", wait: float | None = None):
    """Yield True once this thread has the AI, False if it gave up.

    wait=None waits as long as it takes; wait=0 does not wait at all.
    """
    if wait is None:
        got = _lock.acquire()
    elif wait <= 0:
        got = _lock.acquire(blocking=False)
    else:
        got = _lock.acquire(timeout=wait)
    outer = got and not _holder.get("depth")
    if got:
        if outer:
            _holder.update(kind=kind, label=label, since=time.time(), depth=0)
        _holder["depth"] = _holder.get("depth", 0) + 1
    try:
        yield got
    finally:
        if got:
            _holder["depth"] -= 1
            if _holder["depth"] == 0:
                _holder.clear()
            _lock.release()


def busy_with() -> dict | None:
    """Who has the AI right now: {"kind", "label", "since"}, or None.
    A snapshot for messages; it can change the moment after."""
    h = dict(_holder)
    return {k: h[k] for k in ("kind", "label", "since")} if h.get("kind") else None
