"""
ui/auth.py — HTTP Basic Auth enforcement

Registered as a before_request hook in create_app() so every route —
including any added in future — is covered automatically.

read_auth_block() is also imported by web.py for the context processor
that injects auth_enabled into all templates.

Config is read fresh per-request via a cheap yaml.safe_load of just the
web.auth block.  No full config parse or validation, so a broken config
never locks the user out of the UI.

Fails open on any config read error (homelab posture: prefer access over
lockout).
"""
import hmac
from urllib.parse import urlparse

import yaml
from flask import request, Response


def _cross_origin_write() -> bool:
    """
    SEC-1 (CSRF): with HTTP Basic Auth the browser auto-attaches credentials,
    so a malicious page can fire form POSTs at the UI (config save, torrent
    delete, log clear). Browsers send Origin (and/or Referer) on such
    requests — reject writes whose origin host doesn't match our Host.
    Non-browser clients (curl, scripts) send neither header and are allowed.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return False
    for header in ("Origin", "Referer"):
        val = request.headers.get(header)
        if val:
            host = urlparse(val).netloc
            return bool(host) and host != request.host
    return False


def read_auth_block(config_path: str) -> dict:
    """Return the raw web.auth dict.  Returns {} on any error (fail-open)."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("web", {}).get("auth", {})
    except Exception:
        return {}


def check_auth(config_path: str):
    """
    Intended for use as a Flask before_request hook.

    /logout is always exempt — that route returns 401 itself to clear
    the browser's cached credentials.

    Returns a 401 Response if auth is enabled and credentials are
    wrong or missing.  Returns None to let the request proceed.
    """
    if request.path == "/logout":
        return None

    if _cross_origin_write():
        return Response("Cross-origin request rejected.", 403)

    auth = read_auth_block(config_path)

    if not auth.get("enabled", False):
        return None

    # SEC-3: compare_digest for constant-time comparison; str() because YAML
    # can parse a numeric password as int.
    creds = request.authorization
    if (
        not creds
        or not hmac.compare_digest(creds.username or "", str(auth.get("username", "")))
        or not hmac.compare_digest(creds.password or "", str(auth.get("password", "")))
    ):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Inspectarr"'},
        )
    return None
