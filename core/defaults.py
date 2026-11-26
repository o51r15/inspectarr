"""
The shipping defaults, in one place.

The settings form, the setup wizard (Phase 4) and the save path read these
instead of repeating literals like 25, 4096 or 600 next to each field. A test
(tests/test_defaults.py) parses a minimal config and proves every value here
equals what core/config.py actually produces, so the two cannot drift apart.

These are the values for an EXISTING install (automatic mode, scheduler
autostart off). Safer first-run values belong in a separate overlay so that an
upgrade never changes how a running install behaves.
"""
from __future__ import annotations

import copy

DEFAULTS: dict = {
    "torrent_client": "qbittorrent",
    "on_arr_failure": "delete",
    "dry_run": False,
    "retry": {"enabled": True, "max_attempts": 10, "interval_seconds": 600},
    "scanning": {
        "polling": {"enabled": True, "interval_seconds": 300},
        "webhooks": {"enabled": False, "secret": "", "scan_delay_seconds": 60},
    },
    "logging": {"log_file": "./data/inspectarr.log.json", "retention_days": 30, "level": "INFO"},
    "state": {"db_file": "./data/inspectarr.db"},
    "remediation": {
        "operating_mode": "automatic",
        "min_severity": "LOW",
        "remediate_at": "LOW",
        "quarantine_timeout_minutes": 0,
        "quarantine_timeout_action": "release",
        "track_replacements": True,
        "replacement_window_hours": 72,
    },
    "web": {
        "port": 8585,
        "scheduler_autostart": False,
        "auth": {"enabled": False, "username": "admin"},
    },
    "notifications": {
        "apprise": {"enabled": False, "notify_on": ["action", "error"]},
        "digest": {"enabled": False, "use_ollama": False},
        "summary": {"enabled": False, "schedule": "daily", "use_ollama": True},
    },
    "prowlarr": {
        "enabled": False,
        "base_priority": 10,
        "reorder_interval_hours": 24,
        "min_grabs_before_scoring": 10,
        "history_window_days": 90,
        "scoring": {
            "response_time_weight": 0.25,
            "failure_rate_weight": 0.30,
            "malicious_weight": 0.20,
            "grab_success_weight": 0.25,
            "backoff_penalty": 20.0,
            "malicious_penalty_per_hit": 10.0,
            "auth_failure_mult": 3.0,
            "grab_failure_mult": 2.0,
            "query_failure_mult": 1.0,
            "rss_failure_mult": 0.5,
        },
        "ollama": {
            "enabled": False,
            "timeout": 120,
            "cache_ttl_hours": 24,
            "update_check_hours": 24,
            "auto_update_check": True,
            "context_window": 4096,
            "max_indexers_per_call": 25,
            "max_adjustment": 30.0,
            "max_mean_deviation": 25.0,
        },
        "auto_manage": {
            "enabled": False,
            "disable_threshold": 30.0,
            "consecutive_runs": 3,
            "cooldown_hours": 24,
        },
    },
}


def get_default(path: tuple):
    """DEFAULTS at `path`, e.g. ("retry", "max_attempts") -> 10.

    Returns a copy for lists and dicts so a caller cannot edit the shared
    table by accident. Raises KeyError for a path that has no default, which
    is deliberate: a typo in a path should fail loudly in tests, not quietly
    fall back to None.
    """
    node = DEFAULTS
    for key in path:
        node = node[key]
    return copy.deepcopy(node) if isinstance(node, (dict, list)) else node


def iter_leaves(node: dict = None, prefix: tuple = ()):
    """Yield (path, value) for every non-dict value in DEFAULTS."""
    node = DEFAULTS if node is None else node
    for key, value in node.items():
        if isinstance(value, dict):
            yield from iter_leaves(value, prefix + (key,))
        else:
            yield prefix + (key,), value
