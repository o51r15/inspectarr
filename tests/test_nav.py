"""The navigation is data (ui/nav.py); these tests pin how paths map to it."""
import pytest

from ui.nav import NAV, active_key, active_subnav


@pytest.mark.parametrize("path,key", [
    ("/", "overview"),
    ("/activity", "activity"),
    ("/activity/history", "activity"),
    ("/torrents", "activity"),
    ("/torrents/ABCDEF", "activity"),
    ("/indexers", "indexers"),
    ("/indexers/ai-scoring", "indexers"),
    ("/config", "settings"),
    ("/system/status", "system"),
    ("/system/events", "system"),
    ("/logs", "system"),
    ("/scheduler", "system"),
    ("/nope", ""),
])
def test_active_key(path, key):
    assert active_key(path) == key


def test_root_does_not_swallow_everything():
    # "/" matching by prefix would make every page "Overview".
    assert active_key("/system/status") != "overview"


def test_prefix_needs_a_boundary():
    # /configure must not light up Settings just because it starts with /config.
    assert active_key("/configure") == ""


@pytest.mark.parametrize("path,tab", [
    ("/activity/review", "review"),
    ("/activity/history", "history"),
    ("/torrents", "torrents"),
    ("/system/tasks", "tasks"),
    ("/logs", "events"),
])
def test_active_subnav(path, tab):
    tabs, active = active_subnav(path)
    assert tabs and active == tab


def test_five_top_level_items():
    assert [i["key"] for i in NAV] == ["overview", "activity", "indexers", "settings", "system"]
