"""
tests/conftest.py -- shared fixtures.

Every test gets:
  * its own temp directory, chdir'd into, so the config can keep the
    relative ./data/... paths that the Settings save path insists on
    (ui/routes/config.py _validate_paths);
  * its own config.yaml and SQLite database;
  * a fake torrent client in place of the real one;
  * no network: any socket connect raises, so a forgotten outbound call
    fails loudly instead of hanging the suite.
"""
import base64
import copy
import os
import socket
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


BASE_CONFIG = {
    "torrent_client": "qbittorrent",
    "qbittorrent": {"url": "http://qbit.test:8080", "username": "u", "password": "p"},
    "arrs": {
        "sonarr": {"enabled": True, "url": "http://sonarr.test:8989", "api_key": "sonarrkey"},
        "radarr": {"enabled": False, "url": "", "api_key": ""},
        "lidarr": {"enabled": False, "url": "", "api_key": ""},
    },
    "rules": [
        {"name": "TV", "category": "tv-sonarr", "app": "sonarr",
         "conditions": {"match_mode": "any", "bad_extensions": [".exe", ".scr"]}},
    ],
    "on_arr_failure": "delete",
    "retry": {"enabled": True, "max_attempts": 10, "interval_seconds": 600},
    "logging": {"log_file": "./data/inspectarr.log.json", "retention_days": 30, "level": "INFO"},
    "state": {"db_file": "./data/inspectarr.db"},
    "notifications": {
        "apprise": {"enabled": False, "urls": [],
                    "notify_on": ["action", "error", "quarantine"]},
        "digest": {"enabled": False, "use_ollama": False},
        "summary": {"enabled": False, "schedule": "daily", "use_ollama": False},
    },
    "remediation": {
        "operating_mode": "monitor",
        "min_severity": "LOW",
        "remediate_at": "HIGH",
        "severity_overrides": {".rar": "LOW"},          # no form field: must survive
        "track_replacements": True,
    },
    "dry_run": False,
    "scanning": {
        "polling": {"enabled": True, "interval_seconds": 900},
        "webhooks": {"enabled": False, "secret": "", "scan_delay_seconds": 60},
    },
    "prowlarr": {
        "enabled": False, "url": "", "api_key": "",
        "base_priority": 10, "reorder_interval_hours": 24,
        "min_grabs_before_scoring": 10,
        "scoring": {"response_time_weight": 0.4},       # no form field: must survive
        "ollama": {"enabled": False, "url": "", "model": "phi4:latest",
                   "system_prompt": "custom prompt"},  # set by their own endpoints
    },
    "web": {
        "port": 8585,
        "scheduler_autostart": False,
        "auth": {"enabled": False, "username": "admin", "password": "changeme"},
    },
}


def write_config(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    from core.config import invalidate_config_cache
    invalidate_config_cache()


def read_config(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real outbound connection fails the test immediately."""
    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    # Name resolution happens BEFORE connect(), so without this a test that
    # touches a hostname would still make a real DNS query.
    monkeypatch.setattr(socket, "getaddrinfo", guard)


@pytest.fixture
def base_config():
    return copy.deepcopy(BASE_CONFIG)


@pytest.fixture
def config_path(tmp_path, monkeypatch, base_config):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    path = tmp_path / "config.yaml"
    write_config(path, base_config)
    return str(path)


@pytest.fixture
def fake_client():
    from tests.fakes import FakeTorrentClient
    return FakeTorrentClient()


@pytest.fixture
def app(config_path, monkeypatch, fake_client):
    # build_torrent_client is imported BY NAME in some route modules, so the
    # module attribute has to be patched at each import site, not only in
    # core.torrent_client.
    import core.torrent_client
    import ui.routes.torrents
    factory = lambda cfg: fake_client  # noqa: E731
    monkeypatch.setattr(core.torrent_client, "build_torrent_client", factory)
    monkeypatch.setattr(ui.routes.torrents, "build_torrent_client", factory)

    from web import create_app
    application = create_app(config_path)
    application.config["TESTING"] = True
    yield application
    state = application.config.get("STATE")
    if state:
        state.close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_header(config_path):
    """Turn auth on (hashed password) and return a valid Basic header."""
    from werkzeug.security import generate_password_hash
    data = read_config(config_path)
    data["web"]["auth"] = {"enabled": True, "username": "admin",
                           "password": generate_password_hash("s3cret-pass")}
    write_config(config_path, data)
    token = base64.b64encode(b"admin:s3cret-pass").decode()
    return {"Authorization": f"Basic {token}"}
