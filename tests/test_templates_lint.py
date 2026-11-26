"""
Templates may not carry inline style="", on*="" handlers, <style> blocks or
inline <script> code.

The CSP (web.py) forbids all four once enforced; this test catches them at
build time instead of as a blank button in someone's browser. Templates not
yet migrated to the design system are listed below. The list only shrinks:
each migration commit removes a name, and Phase 1 is done when it is empty.

Allowed: <script src=...>, <script type="application/json"> data blocks,
and the single hashed theme script in base.html (tests/test_csp.py).
"""
import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).parent.parent / "ui" / "templates"
INLINE_STYLE = re.compile(r'\sstyle="')
INLINE_HANDLER = re.compile(r'\son[a-z]+="')
INLINE_STYLE_BLOCK = re.compile(r"<style[\s>]")
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>")

NOT_YET_MIGRATED = {
    "config.html", "indexers.html",
}


def _templates():
    return sorted(TEMPLATES.glob("*.html"))


@pytest.mark.parametrize("f", _templates(), ids=lambda p: p.name)
def test_no_inline_style_script_or_handlers(f):
    if f.name in NOT_YET_MIGRATED:
        pytest.skip("not yet migrated")
    text = f.read_text(encoding="utf-8")
    checks = [(INLINE_STYLE, "style attribute"), (INLINE_HANDLER, "inline event handler"),
              (INLINE_STYLE_BLOCK, "<style> block")]
    if f.name != "base.html":          # base.html's one script is hash-allowed
        checks.append((INLINE_SCRIPT, "inline <script>"))
    for pattern, what in checks:
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
        assert "style=" not in text, f"{f.name}: inline style in a built string"
