"""
core/defaults.py must agree with what core/config.py actually produces.

Parse a config holding only what the parser requires, then walk every leaf of
DEFAULTS and compare it to the same attribute path on the parsed AppConfig.
If someone changes a default in one place and not the other, this fails.
"""
import pytest

from core.config import _parse_config
from core.defaults import DEFAULTS, get_default, iter_leaves

MINIMAL = {
    "qbittorrent": {"url": "http://qbit:8080", "username": "u", "password": "p"},
}


def _attr(obj, path):
    for key in path:
        obj = getattr(obj, key)
    return obj


@pytest.mark.parametrize("path,expected", list(iter_leaves()),
                         ids=lambda v: ".".join(v) if isinstance(v, tuple) else None)
def test_default_matches_parser(path, expected):
    cfg = _parse_config(dict(MINIMAL))
    assert _attr(cfg, path) == expected


def test_get_default_returns_a_copy():
    get_default(("notifications", "apprise", "targets")).append("junk")
    assert DEFAULTS["notifications"]["apprise"]["targets"] == []


def test_unknown_path_raises():
    with pytest.raises(KeyError):
        get_default(("retry", "max_attemps"))
