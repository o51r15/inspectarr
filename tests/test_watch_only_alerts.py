"""
Watch only (operating_mode: monitor) tells you what it would have done.

Watch only and dry_run are the same promise. dry_run always sent the
"would have been removed" alert; monitor mode used to record the catch
silently. Both now alert -- but only for catches the MODE stopped, never for
ones below the severity floor, which stay recorded-only as before.
"""
import copy

import pytest

from core.config import _parse_config
from core.scanner import Scanner
from core.state import StateManager
from tests.conftest import BASE_CONFIG


class _Client:
    def __init__(self, files):
        self._files = files

    def get_torrent_files(self, h):
        return self._files


class _Notifier:
    def __init__(self):
        self.calls = []

    def notify_dry_run(self, name, bad_files):
        self.calls.append(("dry_run", name, list(bad_files)))

    def __getattr__(self, attr):                 # any other notify_* is recorded too
        return lambda *a, **k: self.calls.append((attr,) + a)


def _scanner(tmp_path, mode, min_severity="LOW", files=None):
    raw = copy.deepcopy(BASE_CONFIG)
    raw["remediation"].update({"operating_mode": mode, "min_severity": min_severity,
                               "remediate_at": "LOW"})
    raw["rules"] = [{"name": "TV", "category": "tv-sonarr", "app": "sonarr",
                     "conditions": {"match_mode": "any", "bad_extensions": [".exe", ".rar"]}}]
    cfg = _parse_config(raw)
    s = Scanner.__new__(Scanner)                  # no real download client / notifier
    s.config = cfg
    s.log = __import__("logging").getLogger("test")
    s.state = StateManager(db_path=str(tmp_path / "s.db"), log_path=str(tmp_path / "l.json"),
                           retention_days=30)
    s.qbit = _Client(files or [{"name": "Show.S01E01/setup.exe", "size": 1000}])
    s.notifier = _Notifier()
    s._scan_id = None
    return s, cfg.rules[0]


def test_watch_only_sends_the_would_have_been_removed_alert(tmp_path):
    s, rule = _scanner(tmp_path, "monitor")
    flagged, actioned = s._evaluate_torrent({"hash": "a" * 40, "name": "Show.S01E01"}, rule)
    assert (flagged, actioned) == (True, False)
    assert s.notifier.calls == [("dry_run", "Show.S01E01", ["setup.exe"])]
    s.state.close()


def test_below_the_floor_stays_silent_in_watch_only(tmp_path):
    # .rar grades HIGH; with the floor at CRITICAL it is recorded only.
    s, rule = _scanner(tmp_path, "monitor", min_severity="CRITICAL",
                       files=[{"name": "Show.S01E02/part.rar", "size": 1000}])
    flagged, actioned = s._evaluate_torrent({"hash": "b" * 40, "name": "Show.S01E02"}, rule)
    assert (flagged, actioned) == (True, False)
    assert s.notifier.calls == []
    s.state.close()
