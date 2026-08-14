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

H-01: Passwords are now stored as werkzeug hashes (pbkdf2/scrypt).
Legacy plaintext passwords are auto-migrated on first successful login.
"""
import hmac
import logging
import os
import tempfile
from urllib.parse import urlparse

import yaml
from flask import request, Response
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("inspectarr")

_HASH_PREFIXES = ("pbkdf2:", "scrypt:")


def _is_hashed(value: str) -> bool:
    """Return True if the value looks like a werkzeug password hash."""
    return isinstance(value, str) and value.startswith(_HASH_PREFIXES)


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


def _migrate_password(config_path: str, plaintext: str):
    """
    H-01: Auto-migrate a plaintext password to a werkzeug hash.
    Uses atomic write (temp → fsync → replace) to avoid corruption.
    Best-effort — failures are logged but never block authentication.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        web = raw.get("web", {})
        auth = web.get("auth", {})
        auth["password"] = generate_password_hash(plaintext)
        web["auth"] = auth
        raw["web"] = web

        # Atomic write: temp in /app/data (writable + same device as config)
        data_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), "data")
        if not os.path.isdir(data_dir) or not os.access(data_dir, os.W_OK):
            data_dir = os.path.dirname(os.path.abspath(config_path))
        fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
            log.info("Password auto-migrated to hash")
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning(f"Password migration failed (non-fatal): {exc}")


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

    # Webhook endpoints use their own shared-secret auth
    if request.path.startswith("/webhook/"):
        return None

    if _cross_origin_write():
        return Response("Cross-origin request rejected.", 403)

    auth = read_auth_block(config_path)

    if not auth.get("enabled", False):
        return None

    creds = request.authorization
    if not creds:
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Inspectarr"'},
        )

    # SEC-3: constant-time comparison for username
    username_ok = hmac.compare_digest(
        creds.username or "", str(auth.get("username", ""))
    )

    stored_pw = str(auth.get("password", ""))
    provided_pw = creds.password or ""

    if _is_hashed(stored_pw):
        # H-01: verify against werkzeug hash
        password_ok = check_password_hash(stored_pw, provided_pw)
    else:
        # Legacy plaintext — constant-time compare, then auto-migrate
        password_ok = hmac.compare_digest(provided_pw, stored_pw)
        if username_ok and password_ok:
            _migrate_password(config_path, provided_pw)

    if not username_ok or not password_ok:
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Inspectarr"'},
        )
    return None
