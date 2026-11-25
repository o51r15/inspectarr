"""
Templates may not carry inline style="" or on*="" handlers.

The CSP (web.py) will forbid both once enforced; this test catches them at
build time instead of as a blank button in someone's browser. Templates not
yet migrated to the design system are listed below. The list only shrinks:
each migration commit removes a name, and Phase 1 is done when it is empty.
"""
import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).parent.parent / "ui" / "templates"
INLINE_STYLE = re.compile(r'\sstyle="')
INLINE_HANDLER = re.compile(r'\son[a-z]+="')
INLINE_STYLE_BLOCK = re.compile(r"<style[\s>]")

NOT_YET_MIGRATED = {
    "config.html", "indexers.html", "logs.html", "scheduler.html",
    "system_notifications.html", "system_status.html", "system_tasks.html",
    "system_updates.html", "torrents.html", "torrent_detail.html",
}


def _templates():
    return sorted(TEMPLATES.glob("*.html"))


@pytest.mark.parametrize("f", _templates(), ids=lambda p: p.name)
def test_no_inline_style_or_handlers(f):
    if f.name in NOT_YET_MIGRATED:
        pytest.skip("not yet migrated")
    text = f.read_text(encoding="utf-8")
    for pattern, what in ((INLINE_STYLE, "style attribute"),
                          (INLINE_HANDLER, "inline event handler"),
                          (INLINE_STYLE_BLOCK, "<style> block")):
        m = pattern.search(text)
        assert not m, f"{what} in {f.name} at line {text[:m.start()].count(chr(10)) + 1}"


def test_allowlist_only_names_real_templates():
    names = {p.name for p in _templates()}
    stale = NOT_YET_MIGRATED - names
    assert not stale, f"remove deleted templates from NOT_YET_MIGRATED: {stale}"


def test_new_scripts_have_no_inline_handlers_built_in_strings():
    """JS under static/js builds DOM with createElement, not onclick strings."""
    for f in (TEMPLATES.parent / "static" / "js").rglob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"""on(click|change|input)\s*=\s*["']""", text), f.name
        assert ".style.cssText" not in text, f"{f.name}: use a class or a CSS custom property"
