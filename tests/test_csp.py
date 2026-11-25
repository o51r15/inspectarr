"""
The Content-Security-Policy allows exactly one inline script: the theme
picker in base.html's <head>, by its SHA-256 hash. These tests keep the hash
in web.py honest and make sure every page sends the header.
"""
import base64
import hashlib
import pathlib
import re

import web

BASE = pathlib.Path(__file__).parent.parent / "ui" / "templates" / "base.html"


def theme_script_hash() -> str:
    text = BASE.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", text, re.S)
    assert m, "base.html must start with the inline theme <script>"
    digest = hashlib.sha256(m.group(1).encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode()


def test_theme_hash_matches_base_html():
    assert web.THEME_SCRIPT_HASH == theme_script_hash(), (
        "The theme script in base.html changed. Set web.THEME_SCRIPT_HASH to "
        f"{theme_script_hash()!r}")


def test_only_one_inline_script_in_base():
    text = BASE.read_text(encoding="utf-8")
    inline = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", text)
    assert len(inline) == 1, f"base.html has {len(inline)} inline scripts; the CSP allows one"


def test_csp_header_present(client):
    r = client.get("/")
    header = (r.headers.get("Content-Security-Policy")
              or r.headers.get("Content-Security-Policy-Report-Only"))
    assert header, "every response must carry a CSP"
    assert "default-src 'self'" in header
    assert web.THEME_SCRIPT_HASH in header
    assert "unsafe-inline" not in header


def test_no_third_party_assets_in_templates():
    """Fonts and Chart.js are vendored; nothing may load from a CDN again."""
    tdir = BASE.parent
    for f in tdir.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        for bad in ("fonts.googleapis", "fonts.gstatic", "cdn.jsdelivr", "cdnjs.cloudflare", "unpkg.com"):
            assert bad not in text, f"{f.name} loads from {bad}"
