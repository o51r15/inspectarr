"""Regression tests for the Phase 0 quick fixes and the example config."""
import os

import yaml

from tests.conftest import ROOT, read_config, write_config

EXAMPLE = os.path.join(ROOT, "config.example.yaml")


class _NoDuplicateKeys(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with the same key twice.

    Plain YAML keeps the LAST value silently, so a duplicated block means
    editing the first copy does nothing -- which is how the example config
    carried two `context_window` and two `max_indexers_per_call` keys.
    """


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise AssertionError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_NoDuplicateKeys.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def test_example_config_has_no_duplicate_keys():
    with open(EXAMPLE, encoding="utf-8") as f:
        yaml.load(f, Loader=_NoDuplicateKeys)


def test_example_config_loads_as_a_real_config():     # B-07
    from core.config import load_config
    cfg = load_config(EXAMPLE)
    assert cfg.prowlarr.ollama.context_window == 4096
    assert cfg.prowlarr.ollama.max_indexers_per_call == 25


def test_tasks_page_shows_the_interval_the_scheduler_uses(client, config_path):
    # BASE_CONFIG polls every 900s and has no legacy poll_interval_seconds,
    # which defaults to 300 -- the value the page used to show.
    html = client.get("/system/tasks").get_data(as_text=True)
    assert "Every 900s" in html
    assert "Every 300s" not in html


def test_tasks_page_says_when_polling_is_off(client, config_path):
    data = read_config(config_path)
    data["scanning"]["polling"]["enabled"] = False
    write_config(config_path, data)
    assert "Polling disabled" in client.get("/system/tasks").get_data(as_text=True)


def test_quarantine_nav_link_is_marked_active(client):
    html = client.get("/quarantine").get_data(as_text=True)
    assert 'href="/quarantine" class="active"' in html


def test_version_is_the_running_one_and_never_fetched_from_github(client, app):
    from core import __version__
    for page in ("/", "/system/status"):
        html = client.get(page).get_data(as_text=True)
        assert "api.github.com" not in html
        assert f"v{__version__}" in html
