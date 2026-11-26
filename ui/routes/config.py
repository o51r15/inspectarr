from flask import Blueprint, render_template, current_app, request, redirect, url_for, jsonify
import json
import yaml
import os
import logging
import threading
from datetime import datetime

from ui.routes._utils import safe_error
from core import config_store
from ui import settings_schema

# Model identity and update checking live in core/, not here -- the scorer
# needs them too, and one implementation cannot drift from itself.
from core.ollama_registry import (
    local_digest as _ollama_local_digest,
    check_for_update as _ollama_check_update,
)
# Validations are stored per Ollama server (model, server); every lookup
# here passes the server the saved config points at.

log = logging.getLogger("inspectarr")

config_bp = Blueprint("config", __name__)


# BUG-06: safe cast helpers — bare int()/float() calls raise ValueError on
# non-numeric form/JSON input and return HTTP 500 with no user feedback.
def _int(val, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _float(val, default: float) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _load_raw(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_raw(config_path: str, data: dict):
    """Write `data` into config.yaml through the comment-preserving store.

    Keys in `data` are merged into the existing document; keys it does not
    mention are left exactly as they are, comments included. Atomic write,
    bind-mount fallback, chmod 600 and cache invalidation live in
    core.config_store.write_text_atomic (L-17, ROADMAP item 14).
    """
    doc = config_store.load_document(config_path)
    config_store.merge_into(doc.data, data)
    config_store.save_document(config_path, doc)


def _file_text(config_path: str) -> str:
    """The config for the raw YAML editor: the user's own layout and comments,
    with every secret replaced by the mask (3.4). Saving restores them."""
    doc = config_store.load_document(config_path)
    settings_schema.mask_secrets(doc.data)
    return config_store.dump_text(doc)


def _render_settings(config_path: str, **extra):
    """The only way Settings is rendered, so no path can show a raw secret."""
    extra.setdefault("raw_yaml", _file_text(config_path))
    return render_template("config.html",
                           cfg=settings_schema.masked_copy(_load_raw(config_path)), **extra)


def _saved_ollama_url(config_path: str) -> str:
    """The Ollama server the saved config points at ('' if none or unreadable).

    "The server you are attached to" for every validation lookup. Typing a new
    URL in the form does not switch servers until it is saved.
    """
    try:
        raw = _load_raw(config_path)
        return (((raw.get("prowlarr") or {}).get("ollama") or {}).get("url") or "").strip()
    except Exception as exc:
        log.debug(f"Could not read the Ollama URL: {exc}")
        return ""


def _get_state(cfg):
    """
    IMP-2 (retires BUG-09): reuse the app-wide StateManager (shared with the
    scheduler) instead of opening a new SQLite connection per request.
    Falls back to a fresh instance only if the shared one is unavailable.
    """
    state = current_app.config.get("STATE")
    if state is not None:
        return state
    from core.state import StateManager
    return StateManager(
        db_path=cfg.state.db_file,
        log_path=cfg.logging.log_file,
        retention_days=cfg.logging.retention_days,
    )


def _validate_paths(data: dict) -> str | None:
    """
    SEC-5 + M-11: reject log_file / db_file paths that escape the project
    directory.  Blocks both '..' traversal and absolute paths.
    """
    import os
    for section, key in [("logging", "log_file"), ("state", "db_file")]:
        path = data.get(section, {}).get(key, "")
        if not path:
            continue
        normalized = os.path.normpath(path)
        if ".." in normalized.split(os.sep) or ".." in normalized.split("/"):
            return f"{section}.{key} must not contain '..' path components"
        if os.path.isabs(normalized):
            return f"{section}.{key} must be a relative path"
    return None


def _validation_error(data: dict) -> str | None:
    """
    IMP-4: run a candidate config dict through the core parser/validator
    before writing it to disk. Returns an error string or None if valid.
    A config that saves but fails load_config() silently stops all scans.
    """
    # SEC-5: path traversal check before core validation
    path_err = _validate_paths(data)
    if path_err:
        return path_err
    try:
        from core.config import _parse_config
        _parse_config(data)
        return None
    except Exception as exc:
        return safe_error(exc)


@config_bp.route("/config", methods=["GET"])
def config_view():
    return _render_settings(current_app.config["CONFIG_PATH"])


@config_bp.route("/config/save", methods=["POST"])
def config_save():
    config_path = current_app.config["CONFIG_PATH"]
    mode        = request.form.get("edit_mode", "form")

    if mode == "yaml":
        raw_yaml = request.form.get("raw_yaml", "")

        def _reject(message):
            # Echo back what the user typed so nothing is lost; any stored
            # secret in it is still only the mask.
            return _render_settings(config_path, raw_yaml=raw_yaml, error=message,
                                    edit_mode="yaml")

        try:
            doc = config_store.parse_text(raw_yaml)
        except Exception as e:  # ruamel raises several YAMLError subclasses
            return _reject(f"YAML error: {e}")
        # BUG-05: an empty or scalar document must never replace the config.
        # (parse_text maps an empty document to {}; a bare scalar is not a dict.)
        if not isinstance(doc.data, dict) or not doc.data:
            return _reject("YAML must be a mapping — cannot save empty or scalar content.")
        # Values still showing the mask are put back to what is stored.
        try:
            settings_schema.restore_secrets(doc.data, _load_raw(config_path))
        except settings_schema.FieldError as e:
            return _reject(f"Not saved: {e}")
        # IMP-4: refuse to write a config the core cannot load
        err = _validation_error(config_store.to_plain(doc.data))
        if err:
            return _reject(f"Config invalid — not saved: {err}")
        # The submitted text replaces the file as a whole: this editor IS
        # the whole file, so a key the user deleted here is meant to go.
        config_store.save_document(config_path, doc)
        return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")

    # Form mode: the schema turns the form into ONLY the keys it contains;
    # the store merges them in and leaves everything else as it was. The
    # merged document -- exactly what will be written -- is validated first.
    def _reject(message):
        return _render_settings(config_path, error=f"Not saved: {message}")

    doc = config_store.load_document(config_path)
    try:
        updates = settings_schema.form_to_updates(request.form, config_store.to_plain(doc.data))
    except settings_schema.FieldError as e:
        return _reject(str(e))
    config_store.merge_into(doc.data, updates)
    err = _validation_error(config_store.to_plain(doc.data))
    if err:
        return _reject(f"config invalid — {err}")
    config_store.save_document(config_path, doc)
    return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")


@config_bp.route("/config/test/qbit", methods=["POST"])
def test_qbit():
    from core.qbit import QBittorrentClient
    body     = request.get_json(silent=True) or {}   # SEC-5: no 415 on bad body
    url      = body.get("url", "")
    username = body.get("username", "")
    password = settings_schema.resolve_secret(body.get("password", ""), ('qbittorrent', 'password'), current_app.config["CONFIG_PATH"])
    try:
        client = QBittorrentClient(url, username, password)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/transmission", methods=["POST"])
def test_transmission():
    from core.transmission import TransmissionClient
    body     = request.get_json(silent=True) or {}
    url      = body.get("url", "")
    username = body.get("username", "")
    password = settings_schema.resolve_secret(body.get("password", ""), ('transmission', 'password'), current_app.config["CONFIG_PATH"])
    try:
        client = TransmissionClient(url, username, password)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/deluge", methods=["POST"])
def test_deluge():
    from core.deluge import DelugeClient
    body     = request.get_json(silent=True) or {}
    url      = body.get("url", "")
    password = settings_schema.resolve_secret(body.get("password", ""), ('deluge', 'password'), current_app.config["CONFIG_PATH"])
    try:
        client = DelugeClient(url, password)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/lidarr", methods=["POST"])
def test_lidarr():
    from core.arrs.lidarr import LidarrClient
    body    = request.get_json(silent=True) or {}   # SEC-5
    url     = body.get("url", "")
    api_key = settings_schema.resolve_secret(body.get("api_key", ""), ('arrs', 'lidarr', 'api_key'), current_app.config["CONFIG_PATH"])
    try:
        client = LidarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/radarr", methods=["POST"])
def test_radarr():
    from core.arrs.radarr import RadarrClient
    body    = request.get_json(silent=True) or {}   # SEC-5
    url     = body.get("url", "")
    api_key = settings_schema.resolve_secret(body.get("api_key", ""), ('arrs', 'radarr', 'api_key'), current_app.config["CONFIG_PATH"])
    try:
        client = RadarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/sonarr", methods=["POST"])
def test_sonarr():
    from core.arrs.sonarr import SonarrClient
    body    = request.get_json(silent=True) or {}   # SEC-5
    url     = body.get("url", "")
    api_key = settings_schema.resolve_secret(body.get("api_key", ""), ('arrs', 'sonarr', 'api_key'), current_app.config["CONFIG_PATH"])
    try:
        client = SonarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/qbit/categories", methods=["GET"])
def qbit_categories():
    """
    Return qBittorrent's category list for the rule builder dropdown.
    On any failure returns ok=false with an empty list so the frontend can
    render the rules pane in its disabled/disconnected state.
    """
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.torrent_client import build_torrent_client
        cfg    = load_config(config_path)
        client = build_torrent_client(cfg)
        cat_map = client.get_categories()
        categories = sorted(name for name in cat_map.keys() if name)
        return jsonify({"ok": True, "categories": categories})
    except Exception as exc:
        return jsonify({"ok": False, "categories": [], "message": safe_error(exc)})


# ---------------------------------------------------------------------------
# Prowlarr endpoints
# ---------------------------------------------------------------------------

@config_bp.route("/config/prowlarr/indexers", methods=["GET"])
def prowlarr_indexers():
    """Return scored indexer list as JSON. Called by the Prowlarr config tab."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = _get_state(cfg)   # IMP-2: shared app-wide StateManager
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        results = scorer.score_all(skip_ai=True)
        for r in results:
            r.pop("_raw", None)
        ai_available = cfg.prowlarr.ollama.is_active()
        return jsonify({"ok": True, "indexers": results, "ai_available": ai_available})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/prowlarr/indexers/ai", methods=["GET"])
def prowlarr_indexers_ai():
    """Return AI-scored indexer list. Separate endpoint so UI can call it async."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        if not cfg.prowlarr.ollama.enabled:
            return jsonify({"ok": False,
                            "message": "AI features are disabled"})
        if not cfg.prowlarr.ollama.is_active():
            return jsonify({"ok": False, "message": "Ollama is not configured"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = _get_state(cfg)   # IMP-2: shared app-wide StateManager
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        results = scorer.score_all(skip_ai=False)
        for r in results:
            r.pop("_raw", None)
        return jsonify({"ok": True, "indexers": results})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/prowlarr/save", methods=["POST"])
def prowlarr_save():
    """Merge the Prowlarr config block into config.yaml."""
    config_path = current_app.config["CONFIG_PATH"]
    data        = request.get_json(silent=True) or {}   # SEC-5
    try:
        raw = _load_raw(config_path)
        # BUG-10: merge onto the existing block instead of replacing it.
        # Replacing dropped every key without a UI field — most importantly
        # the `ollama` sub-block, silently disabling AI scoring on every save
        # (same clobber class as the old `scoring` bug).
        block = dict(raw.get("prowlarr", {}) or {})
        block.update({
            "enabled":                data.get("enabled", False),
            "url":                    data.get("url", ""),
            "api_key":                data.get("api_key", ""),
            "base_priority":          _int(data.get("base_priority"), 10),
            "reorder_interval_hours": _int(data.get("reorder_interval_hours"), 24),
            "history_window_days":    _int(data.get("history_window_days"), 90),
            "min_grabs_before_scoring": _int(data.get("min_grabs_before_scoring"), 10),
            "scoring": {
                "response_time_weight":      _float(data.get("response_time_weight"), 0.25),
                "failure_rate_weight":       _float(data.get("failure_rate_weight"), 0.30),
                "malicious_weight":          _float(data.get("malicious_weight"), 0.20),
                "grab_success_weight":       _float(data.get("grab_success_weight"), 0.25),
                "backoff_penalty":           _float(data.get("backoff_penalty"), 20.0),
                "malicious_penalty_per_hit": _float(data.get("malicious_penalty_per_hit"), 10.0),
                "auth_failure_mult":         _float(data.get("auth_failure_mult"), 3.0),
                "grab_failure_mult":         _float(data.get("grab_failure_mult"), 2.0),
                "query_failure_mult":        _float(data.get("query_failure_mult"), 1.0),
                "rss_failure_mult":          _float(data.get("rss_failure_mult"), 0.5),
            },
        })
        raw["prowlarr"] = block
        _save_raw(config_path, raw)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/prowlarr/set-ignored", methods=["POST"])
def prowlarr_set_ignored():
    """Toggle the ignore/pin state for a single indexer."""
    config_path = current_app.config["CONFIG_PATH"]
    data        = request.get_json(silent=True) or {}   # SEC-5
    try:
        from core.config import load_config
        cfg   = load_config(config_path)
        state = _get_state(cfg)   # IMP-2: shared app-wide StateManager
        state.set_indexer_ignored(
            indexer_id=int(data["indexer_id"]),
            indexer_name=data["indexer_name"],
            ignored=bool(data["ignored"]),
            pinned_position=data.get("pinned_position"),
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/prowlarr/rescore", methods=["POST"])
def prowlarr_rescore():
    """Manually trigger a full score + reorder cycle."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = _get_state(cfg)   # IMP-2: shared app-wide StateManager
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        changed = scorer.reorder()
        return jsonify({"ok": True, "changed": changed})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


# ---------------------------------------------------------------------------
# Calibration
#
# Calibrating takes minutes, so it never runs inside a request: the
# CalibrationRunner (core/calibration_runs.py) works through stored runs on
# its own thread, one model at a time, and the page polls. A run survives a
# restart; the model in progress starts again.
# ---------------------------------------------------------------------------
from core.ollama_models import DEFAULT_CONTEXT


def _runner():
    """The app's CalibrationRunner, started on first use outside tests."""
    runner = current_app.config.get("CAL_RUNNER")
    if runner and not runner.started and not current_app.config.get("TESTING"):
        runner.start()
    return runner


def _ai_server_or_error():
    """(url, ollama dict, None) or (None, None, error response)."""
    raw = _load_raw(current_app.config["CONFIG_PATH"])
    ollama = (raw.get("prowlarr", {}) or {}).get("ollama", {}) or {}
    url = (ollama.get("url") or "").strip()
    # Calibration makes real model calls, so it honours the master switch.
    if not ollama.get("enabled", bool(url)):
        return None, None, (jsonify({"ok": False, "message": "AI features are disabled"}), 400)
    if not url:
        return None, None, (jsonify({"ok": False, "message": "Set an Ollama URL first"}), 400)
    return url, ollama, None


# ROADMAP item 13's lesson, applied again: identity and update checking are
# not web concerns. They live in core/ollama_registry.py so the scorer can
# use them for its cache key too, and so there is one implementation of
# "which build is this" rather than two that can drift.
_ollama_digest = _ollama_local_digest



def _calibrated_settings(record: dict | None) -> dict | None:
    """The settings a calibration chose, from its stored result."""
    if not record or not record.get("calibrated"):
        return None
    try:
        res = json.loads(record.get("results_json") or "{}") or {}
    except (TypeError, ValueError):
        res = {}
    settings = res.get("settings")
    if isinstance(settings, dict) and settings.get("context_window"):
        return settings
    # Calibrated before Calibrate chose settings (the old batch-only search):
    # it measured a batch size at a context, nothing else. Marked legacy so
    # applying it does not pretend to know a timeout or thinking choice.
    if record.get("max_safe_batch"):
        return {"context_window": res.get("context_window"),
                "max_indexers_per_call": record.get("max_safe_batch"),
                "legacy": True}
    return None


def _apply_model_settings(ollama: dict, record, url: str, model: str) -> dict:
    """Write a model's settings into the ollama config block (in place).

    Calibrated: its measured context, batch, timeout and thinking.
    Otherwise: the context resets to 8K, or the model's maximum if smaller,
    and the stored thinking choice is cleared (it belonged to another model).
    Returns what was applied, for the page to show.
    """
    from core.ollama_models import context_for_model, model_profile, valid_think
    settings = _calibrated_settings(record)
    if settings and settings.get("legacy"):
        ctx = context_for_model((model_profile(url, model) or {}).get("max_context"))
        ollama["context_window"] = ctx
        _clear_think(ollama)
        applied = {"context_window": ctx, "calibrated": False}
        if settings.get("context_window") in (None, ctx):
            ollama["max_indexers_per_call"] = int(settings["max_indexers_per_call"])
            applied["max_indexers_per_call"] = ollama["max_indexers_per_call"]
        return applied
    if settings:
        applied = {"context_window": int(settings["context_window"]),
                   "max_indexers_per_call": int(settings["max_indexers_per_call"]),
                   "timeout": int(settings["timeout"])}
        think = settings.get("think")
        ollama.update(applied)
        if think is None or not valid_think(think):
            _clear_think(ollama)
        else:
            ollama["think"] = think
            applied["think"] = think
        applied["calibrated"] = True
        return applied
    ctx = context_for_model((model_profile(url, model) or {}).get("max_context"))
    ollama["context_window"] = ctx
    _clear_think(ollama)
    return {"context_window": ctx, "calibrated": False}


def _clear_think(ollama: dict) -> None:
    """Back to the recommended thinking setting.

    Saving MERGES into the file (lossless round-trip), so a key dropped from
    the dict would survive on disk; null is how "recommended" is written.
    Only written when the key is there, so files that never had it stay
    unchanged.
    """
    if "think" in ollama:
        ollama["think"] = None


@config_bp.route("/config/ai/model", methods=["POST"])
def ai_set_model():
    """
    Set the active scoring model.

    Gated: a model that has not passed validation is refused unless the
    caller passes force=true, which the UI only sends after the user
    confirms an explicit "Apply anyway". Forced selections are recorded as
    such so the UI can badge them -- distinct from a model that failed,
    because the user chose this knowingly.
    """
    config_path = current_app.config["CONFIG_PATH"]
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    force = bool(data.get("force"))
    if not model:
        return jsonify({"ok": False, "message": "No model specified"}), 400

    state = current_app.config.get("STATE")
    # A verdict describes a model ON A SERVER, so only this server's counts.
    # A pass earned on other hardware says nothing about this one.
    current_url = _saved_ollama_url(config_path)
    record = state.get_validation(model, current_url) if (state and current_url) else None
    validated = bool(record and record.get("status") == "supported")

    if not validated and not force:
        return jsonify({
            "ok": False,
            "needs_validation": True,
            "status": (record or {}).get("status", "untested"),
            "current_host": current_url,
            "message": f"{model} has not passed a check on this server.",
        }), 409

    try:
        raw = _load_raw(config_path)
        prowlarr = dict(raw.get("prowlarr", {}) or {})
        ollama = dict(prowlarr.get("ollama", {}) or {})
        ollama["model"] = model
        applied = _apply_model_settings(ollama, record, current_url, model)
        prowlarr["ollama"] = ollama
        raw["prowlarr"] = prowlarr
        _save_raw(config_path, raw)

        if not validated and state:
            state.mark_model_forced(
                model, digest=_ollama_digest(ollama.get("url", ""), model),
                ollama_url=ollama.get("url", ""))

        return jsonify({
            "ok": True,
            "forced": not validated,
            "applied": applied,
            "message": (f"{model} applied without validation"
                        if not validated else f"{model} applied"),
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500


@config_bp.route("/config/ai/calibration", methods=["GET"])
def ai_calibration():
    """
    What the batch slider needs to colour itself: the calibrated ceiling for
    the CURRENTLY configured (host, model), or an honest statement that there
    is not one.

    Deliberately keyed on the live config rather than on a model passed in by
    the caller. The slider writes a value that the scorer will use with
    whatever host and model are actually configured, so that is the pair the
    ceiling has to describe.
    """
    state = current_app.config.get("STATE")
    try:
        raw = _load_raw(current_app.config["CONFIG_PATH"])
        ollama = ((raw.get("prowlarr") or {}).get("ollama") or {})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500

    model = ollama.get("model") or ""
    url = (ollama.get("url") or "").strip()
    record = state.get_validation(model, url) if (state and model and url) else None

    settings = _calibrated_settings(record)
    # The measured batch size holds for the context it was measured at. If
    # the context has been changed since, it is not a measurement any more.
    current_ctx = _int(ollama.get("context_window"), DEFAULT_CONTEXT)
    measured_at = (settings or {}).get("context_window")
    calibrated = bool(settings and (measured_at is None or int(measured_at) == current_ctx))
    stale = bool(settings) and not calibrated
    return jsonify({
        "settings": settings,
        "stale": stale,
        "ok": True,
        "model": model,
        "host": url,
        "calibrated": calibrated,
        # Only meaningful when calibrated -- a quick validation records the
        # size it happened to test, which is not a measured ceiling.
        "max_safe_batch": (record or {}).get("max_safe_batch") if calibrated else None,
        "tok_per_s": (record or {}).get("tok_per_s"),
        "gpu_offload_pct": (record or {}).get("gpu_offload_pct"),
        "current": _int(ollama.get("max_indexers_per_call"), 25),
    })


@config_bp.route("/config/ai/validate", methods=["POST"])
def ai_validate():
    """Calibrate one model: a run with one model in it. Returns immediately."""
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"ok": False, "message": "No model specified"}), 400
    try:
        url, _ollama, err = _ai_server_or_error()
        if err:
            return err
        runner, state = _runner(), current_app.config.get("STATE")
        busy = [r for r in state.list_calibration_runs(("running",))]
        if busy or (runner and runner.progress):
            who = (runner.progress or {}).get("model") if runner else None
            return jsonify({"ok": False, "message": f"Already calibrating {who or 'another model'}"}), 409
        run_id = state.create_calibration_run(url, [model], kind="one")
        if runner:
            runner.wake()
        return jsonify({"ok": True, "message": "Calibration started", "run_id": run_id})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)}), 500


@config_bp.route("/config/ai/validate/status", methods=["GET"])
def ai_validate_status():
    """What is being calibrated right now, for the model list."""
    runner = current_app.config.get("CAL_RUNNER")
    state = current_app.config.get("STATE")
    p = dict((runner.progress if runner else None) or {})
    running_runs = state.list_calibration_runs(("running",)) if state else []
    waiting_now = []
    if state and not p:
        # Same format as stored start_after values (+00:00): mixed Z / +00:00
        # strings do not compare correctly as text.
        from datetime import timezone as _tz
        now = datetime.now(_tz.utc).isoformat()
        waiting_now = [r for r in state.list_calibration_runs(("waiting",)) if r["start_after"] <= now]
    running = bool(p) or bool(running_runs) or bool(waiting_now)
    if not p and (running_runs or waiting_now):
        r = (running_runs or waiting_now)[0]
        nxt = next((i for i in r["items"] if i["status"] in ("running", "queued")), None)
        p = {"run_id": r["id"], "model": nxt["model"] if nxt else None, "stage": "Starting"}
    return jsonify({"ok": True, "running": running, "model": p.get("model"),
                    "stage": p.get("stage"), "done": p.get("done", 0), "total": p.get("total", 0),
                    "position": p.get("position"), "of": p.get("of"), "run_id": p.get("run_id")})


@config_bp.route("/config/ai/runs", methods=["GET", "POST"])
def ai_runs():
    """GET: runs waiting, running or paused, the last finished one, and the
    better-model banner. POST {models, when: now|tonight}: Calibrate all."""
    state = current_app.config.get("STATE")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        models = [m for m in (data.get("models") or []) if isinstance(m, str) and m.strip()]
        if not models:
            return jsonify({"ok": False, "message": "Tick at least one model"}), 400
        url, ollama, err = _ai_server_or_error()
        if err:
            return err
        from core.calibration_runs import DEFAULT_OVERNIGHT_HOUR, next_hour
        start_after = None
        if data.get("when") == "tonight":
            start_after = next_hour(_int(ollama.get("overnight_hour"), DEFAULT_OVERNIGHT_HOUR))
        run_id = state.create_calibration_run(url, models, start_after=start_after, kind="all")
        runner = _runner()
        if runner:
            runner.wake()
        return jsonify({"ok": True, "run_id": run_id, "start_after": start_after})
    from core.calibration_runs import BANNER_KEY
    banner = None
    try:
        raw_banner = state.get_app_state(BANNER_KEY) if state else None
        banner = json.loads(raw_banner) if raw_banner else None
    except (TypeError, ValueError):
        banner = None
    last = state.last_calibration_run() if state else None
    return jsonify({"ok": True,
                    "runs": state.list_calibration_runs() if state else [],
                    "last": last if last and last["status"] in ("done", "cancelled") else None,
                    "banner": banner})


@config_bp.route("/config/ai/runs/<int:run_id>/<action>", methods=["POST"])
def ai_run_action(run_id, action):
    """Pause, resume or cancel a run."""
    if action not in ("pause", "resume", "cancel"):
        return jsonify({"ok": False, "message": "Unknown action"}), 400
    runner = _runner()
    if not runner:
        return jsonify({"ok": False, "message": "Calibration is not available"}), 503
    runner.request(run_id, action)
    return jsonify({"ok": True})


@config_bp.route("/config/ai/banner/dismiss", methods=["POST"])
def ai_banner_dismiss():
    """Keep the current model: hide the better-model banner."""
    from core.calibration_runs import BANNER_KEY
    state = current_app.config.get("STATE")
    if state:
        state.set_app_state(BANNER_KEY, "")
    return jsonify({"ok": True})


@config_bp.route("/config/ai/validation/delete", methods=["POST"])
def ai_validation_delete():
    """
    Clear a stored validation record (B-02).

    Without this a 'forced' or 'failed' badge could only be replaced by a
    successful re-validation, never retracted.
    """
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"ok": False, "message": "No model specified"}), 400
    state = current_app.config.get("STATE")
    if not state:
        return jsonify({"ok": False, "message": "State unavailable"}), 503
    url = _saved_ollama_url(current_app.config["CONFIG_PATH"])
    if not url:
        return jsonify({"ok": False, "message": "No Ollama server configured"}), 400
    ok = state.delete_validation(model, url)
    return jsonify({"ok": ok,
                    "message": (f"Cleared the result for {model} on this server"
                                if ok else "Could not clear record")})


@config_bp.route("/config/ai/validations", methods=["GET"])
def ai_validations():
    """Check results for the server the saved config points at.

    Results from other servers stay in the database and reappear when that
    server is configured again; only how many there are is reported, so the
    page can say they exist without listing them.
    """
    state = current_app.config.get("STATE")
    current_url = _saved_ollama_url(current_app.config["CONFIG_PATH"])
    if not state or not current_url:
        return jsonify({"ok": True, "server": current_url, "validations": [],
                        "other_servers": 0})

    out = []
    mine = state.get_validations(current_url)
    other_servers = len({v.get("ollama_url") for v in state.get_validations()}
                        - {v.get("ollama_url") for v in mine} - {None, ""})
    for v in mine:
        import json as _json
        try:
            res = _json.loads(v.get("results_json") or "{}")
        except Exception:
            res = {}
        out.append({
            "model": v.get("model"),
            "status": v.get("status"),
            "validated_at": v.get("validated_at"),
            "indexer_count": v.get("indexer_count"),
            "avg_response_ms": v.get("avg_response_ms"),
            "calibrated": bool(v.get("calibrated")),
            "max_safe_batch": v.get("max_safe_batch"),
            "gpu_offload_pct": v.get("gpu_offload_pct"),
            "tok_per_s": v.get("tok_per_s"),
            # Calibrate's results: what the ranking and the model list use.
            "quality_score": res.get("quality_score"),
            "quality_runs": res.get("quality_runs"),
            "quality_spread": res.get("quality_spread"),
            "follows_reference": bool(res.get("follows_reference")),
            "sec_per_indexer": res.get("sec_per_indexer"),
            "total_s": res.get("total_s"),
            "settings": res.get("settings"),
            "explanation": res.get("explanation"),
            "thinking_tried": res.get("thinking_tried"),
            "tests": [{"name": t.get("name"), "passed": t.get("passed"),
                       "detail": t.get("detail", "")}
                      for t in (res.get("tests") or [])],
        })
    return jsonify({"ok": True, "server": current_url, "validations": out,
                    "other_servers": other_servers})


@config_bp.route("/config/ai/test-connection", methods=["POST"])
def ai_test_connection():
    """
    Probe an Ollama server and report what it has.

    Takes the URL from the request body so the field can be tested before it
    is saved -- matching the other eight test-connection endpoints.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if not url:
        return jsonify({"ok": False, "message": "No URL provided"})
    try:
        import requests as req
        resp = req.get(f"{url}/api/tags", timeout=10)
        if resp.status_code != 200:
            return jsonify({"ok": False,
                            "message": f"HTTP {resp.status_code} from {url}"})
        models = resp.json().get("models", []) or []
        return jsonify({
            "ok": True,
            "message": f"Connected — {len(models)} model(s) available",
            "model_count": len(models),
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/ollama/models", methods=["GET"])
def ollama_models():
    """Fetch available models from the configured Ollama instance."""
    import requests as req
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    ollama_url = raw.get("prowlarr", {}).get("ollama", {}).get("url", "")
    current_model = raw.get("prowlarr", {}).get("ollama", {}).get("model", "")
    if not ollama_url:
        return jsonify({"ok": False, "message": "Ollama URL not configured", "models": [], "current": ""})
    try:
        resp = req.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tags = {m.get("name"): m for m in data.get("models", []) if m.get("name")}
        models = sorted(tags)
        # Each model's limits and abilities, for the model list. Fetched
        # concurrently and cached (core/ollama_models.py); a model whose
        # lookup failed is still listed, with nothing known about it.
        from core.ollama_models import list_models
        profiles = {p["name"]: p for p in (list_models(ollama_url) or [])}
        details = []
        for name in models:
            tag, prof = tags[name], profiles.get(name, {})
            info = tag.get("details") or {}
            details.append({
                "name": name,
                "size_gb": round((tag.get("size") or 0) / 2**30, 1),
                "parameter_size": info.get("parameter_size") or "",
                "family": info.get("family") or "",
                "max_context": prof.get("max_context"),
                "thinking": prof.get("thinking", "none"),
                "think_levels": prof.get("think_levels", []),
                "can_score": prof.get("can_score", True),
            })
        return jsonify({"ok": True, "models": models, "details": details,
                        "current": current_model})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc), "models": [], "current": current_model})


# What the machine running Ollama has. Ollama cannot say how much graphics
# memory there is, so the user is asked once per server (DockLlama's
# question and figures). Only a starting guess: calibration measures where a
# model actually sits, and that is what the ranking uses.
HARDWARE = {
    "none":    {"label": "No graphics card", "gib": 8, "cpu": True},
    "gpu4":    {"label": "4-6 GB graphics card", "gib": 4.5, "cpu": False},
    "gpu8":    {"label": "8 GB graphics card", "gib": 8, "cpu": False},
    "gpu12":   {"label": "12 GB graphics card", "gib": 12, "cpu": False},
    "gpu16":   {"label": "16 GB graphics card", "gib": 16, "cpu": False},
    "gpu24":   {"label": "24 GB or more", "gib": 24, "cpu": False},
    "apple":   {"label": "Apple Silicon (shared memory)", "gib": 12, "cpu": True},
    "unknown": {"label": "Not sure", "gib": 8, "cpu": True},
}


def _hardware_key(url: str) -> str:
    from core.state import normalize_ollama_url
    return "ollama_hardware::" + (normalize_ollama_url(url) or "")


@config_bp.route("/config/ai/hardware", methods=["GET", "POST"])
def ai_hardware():
    """The graphics-card answer for the connected Ollama server."""
    state = current_app.config.get("STATE")
    url = _saved_ollama_url(current_app.config["CONFIG_PATH"])
    if request.method == "POST":
        choice = ((request.get_json(silent=True) or {}).get("hardware") or "").strip()
        if choice not in HARDWARE:
            return jsonify({"ok": False, "message": "Unknown choice"}), 400
        if not (state and url):
            return jsonify({"ok": False, "message": "Connect an Ollama server first"}), 400
        state.set_app_state(_hardware_key(url), choice)
    choice = (state.get_app_state(_hardware_key(url)) if (state and url) else None) or ""
    return jsonify({"ok": True, "server": url, "hardware": choice,
                    "options": [{"value": k, **v} for k, v in HARDWARE.items()]})


@config_bp.route("/config/ai/history", methods=["GET"])
def ai_history():
    """Earlier calibrations of one model on the connected server."""
    state = current_app.config.get("STATE")
    model = (request.args.get("model") or "").strip()
    url = _saved_ollama_url(current_app.config["CONFIG_PATH"])
    if not (state and url and model):
        return jsonify({"ok": True, "history": []})
    return jsonify({"ok": True, "history": state.get_calibration_history(model, url)})


@config_bp.route("/config/ollama/model", methods=["POST"])
def ollama_set_model():
    """Save the selected Ollama model to config.yaml."""
    config_path = current_app.config["CONFIG_PATH"]
    data = request.get_json(silent=True) or {}
    model = data.get("model", "").strip()
    if not model:
        return jsonify({"ok": False, "message": "No model specified"})
    try:
        raw = _load_raw(config_path)
        prowlarr = raw.setdefault("prowlarr", {})
        ollama = prowlarr.setdefault("ollama", {})
        ollama["model"] = model
        _save_raw(config_path, raw)
        return jsonify({"ok": True, "message": f"Model set to {model}"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/ollama/system-prompt", methods=["GET"])
def ollama_get_system_prompt():
    """Return the current (or default) Ollama system prompt."""
    from core.llm_client import SYSTEM_PROMPT
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    custom = raw.get("prowlarr", {}).get("ollama", {}).get("system_prompt", "")
    return jsonify({
        "ok": True,
        "prompt": custom if custom else SYSTEM_PROMPT,
        "is_default": not bool(custom),
        "default_prompt": SYSTEM_PROMPT,
    })


@config_bp.route("/config/ollama/system-prompt", methods=["POST"])
def ollama_set_system_prompt():
    """Save or reset the Ollama system prompt."""
    config_path = current_app.config["CONFIG_PATH"]
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    try:
        raw = _load_raw(config_path)
        prowlarr = raw.setdefault("prowlarr", {})
        ollama = prowlarr.setdefault("ollama", {})
        # Empty string = reset to default (built-in prompt used at runtime)
        ollama["system_prompt"] = prompt
        _save_raw(config_path, raw)
        msg = "System prompt reset to default" if not prompt else "System prompt saved"
        return jsonify({"ok": True, "message": msg})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/ollama/update-check", methods=["GET"])
def ollama_update_check():
    """
    Report whether the active model has changed since it was validated.

    This deliberately does NOT ask the registry whether a newer version
    exists upstream. The previous implementation called /api/pull with
    stream=false to find out -- but Ollama has no dry-run, so that call
    genuinely pulls: every Settings page load re-pulled the configured model,
    and when an update DID exist it began a multi-gigabyte download that was
    then abandoned at the 30s timeout. Its verdict was inverted too, because
    a successful pull of a NEW version also reports "success", which the code
    read as "up to date".

    What actually matters here is local: has the model under this name been
    replaced since we proved it could score correctly? Comparing the current
    /api/tags digest against the one stored at validation answers that
    exactly, costs one cheap GET, and changes nothing on the host.

    Honours prowlarr.ollama.update_check_hours (0 disables). The last check
    is stamped in app_state so a page refresh does not re-query.
    """
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    ollama = (raw.get("prowlarr", {}) or {}).get("ollama", {}) or {}
    url = (ollama.get("url") or "").strip()
    model = (ollama.get("model") or "").strip()
    if not url or not model:
        return jsonify({"ok": False, "message": "Ollama not configured"})

    interval = _int(ollama.get("update_check_hours"), 24)
    state = current_app.config.get("STATE")

    if interval <= 0:
        return jsonify({"ok": True, "update_available": False, "model": model,
                        "message": "Update checking disabled"})

    # Respect the configured interval. Cached verdicts are returned verbatim
    # so the badge stays stable between checks rather than flickering off.
    CACHE_KEY = "ollama_update_check"
    if state:
        try:
            cached = state.get_app_state(CACHE_KEY)
            if cached:
                import json as _json
                c = _json.loads(cached)
                last = datetime.fromisoformat(c["checked_at"])
                age_h = (datetime.now() - last).total_seconds() / 3600
                # Per server too: the same model name on another server is
                # another build, with its own update status.
                if (c.get("model") == model and c.get("server") == url
                        and age_h < interval):
                    c["cached"] = True
                    return jsonify(c)
        except Exception as exc:
            log.debug(f"Ignoring unreadable update-check cache: {exc}")

    current = _ollama_digest(url, model)
    if not current:
        return jsonify({"ok": True, "update_available": False, "model": model,
                        "message": "Could not read model digest"})

    record = state.get_validation(model, url) if state else None
    known = (record or {}).get("model_digest")

    if not known:
        result = {
            "ok": True, "update_available": False, "model": model,
            "digest": current[:12],
            "reason": None,
            "message": ("Not validated yet" if not record
                        else "Validated before digests were recorded"),
        }
    elif known == current:
        result = {"ok": True, "update_available": False, "model": model,
                  "digest": current[:12], "reason": None,
                  "message": "Model unchanged since validation"}

    else:
        result = {
            "ok": True, "update_available": True, "model": model,
            "reason": "local",
            "digest": current[:12], "validated_digest": known[:12],
            "message": (f"{model} has changed since it was validated "
                        f"({known[:12]} -> {current[:12]}). Re-validate to "
                        f"confirm it still scores correctly."),
        }

    # The SEPARATE question: is a newer build published upstream?
    #
    # Asked regardless of what the local check concluded, because the two are
    # independent. A model that was never validated, or validated before
    # digests were recorded, can still be out of date -- and nesting this
    # inside the "unchanged since validation" branch meant it never ran for
    # either, which is most models on an existing install.
    #
    # Opt-out: this leaves the network the machine is on, and a self-hosted
    # tool should not phone anywhere the user did not agree to.
    if ollama.get("auto_update_check", True):
        try:
            up = _ollama_check_update(url, model)
            if up.get("known") and up.get("update_available"):
                result.update({
                    "update_available": True,
                    "reason": "upstream",
                    "remote_digest": (up.get("remote") or "")[:12],
                    "message": "A newer build of this model is published upstream",
                })
            elif not up.get("known"):
                # "Could not reach the registry" must never read as a verdict.
                result["registry"] = "unavailable"
            else:
                result["registry"] = "current"
        except Exception as exc:
            log.debug(f"Registry update check failed: {exc}")
            result["registry"] = "unavailable"

    if state:
        try:
            import json as _json
            stamped = dict(result)
            stamped["checked_at"] = datetime.now().isoformat()
            stamped["server"] = url
            state.set_app_state(CACHE_KEY, _json.dumps(stamped))
        except Exception as exc:
            log.debug(f"Could not stamp update check: {exc}")
    return jsonify(result)


# ---------------------------------------------------------------------
# Model pull (ROADMAP item 17)
# ---------------------------------------------------------------------
#
# In a background thread with polled progress, exactly like validation, and
# for a stronger reason: a model pull is gigabytes. Doing it in-request would
# hold a waitress worker for minutes and then time out anyway -- which is
# precisely the shape of B-06, where a pull was fired from a page load and
# abandoned half-finished at the 30s mark.
#
# Ollama's /api/pull streams NDJSON progress. Streaming it is what makes this
# honest: a multi-gigabyte download with no progress is indistinguishable
# from a hang, and the user's only recourse would be to refresh, which starts
# a second one.

_pull_lock = threading.Lock()
_pull_state = {"running": False, "model": None, "status": None,
               "completed": 0, "total": 0, "error": None, "done": False}


def _pull_worker(url, model):
    """Stream a pull, recording progress. Never raises out of the thread."""
    import requests as req
    try:
        with req.post(f"{url.rstrip('/')}/api/pull",
                      json={"model": model, "stream": True},
                      stream=True, timeout=(10, 600)) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    import json as _j
                    ev = _j.loads(line)
                except Exception:
                    continue
                if ev.get("error"):
                    with _pull_lock:
                        _pull_state.update(error=ev["error"], running=False,
                                           done=True)
                    return
                with _pull_lock:
                    _pull_state.update(
                        status=ev.get("status") or _pull_state["status"],
                        completed=int(ev.get("completed") or 0),
                        total=int(ev.get("total") or 0))
        with _pull_lock:
            _pull_state.update(running=False, done=True, status="Complete")
    except Exception as exc:
        with _pull_lock:
            _pull_state.update(running=False, done=True, error=str(exc))


@config_bp.route("/config/ollama/pull", methods=["POST"])
def ollama_pull():
    """Start pulling the configured model. One at a time."""
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    ollama = (raw.get("prowlarr", {}) or {}).get("ollama", {}) or {}
    url = (ollama.get("url") or "").strip()
    model = ((request.get_json(silent=True) or {}).get("model")
             or ollama.get("model") or "").strip()

    if not ollama.get("enabled", bool(url)):
        return jsonify({"ok": False, "message": "AI features are disabled"}), 400
    if not url or not model:
        return jsonify({"ok": False, "message": "Ollama is not configured"}), 400

    with _pull_lock:
        if _pull_state["running"]:
            # Concurrent pulls of the same model race on the same blobs and
            # make both slower; of different models they saturate the link.
            return jsonify({"ok": False,
                            "message": f"Already pulling {_pull_state['model']}"}), 409
        _pull_state.update(running=True, model=model, status="Starting",
                           completed=0, total=0, error=None, done=False)

    threading.Thread(target=_pull_worker, args=(url, model), daemon=True,
                     name=f"inspectarr-pull-{model}").start()
    return jsonify({"ok": True, "message": f"Pulling {model}"})


@config_bp.route("/config/ollama/pull/status", methods=["GET"])
def ollama_pull_status():
    """Progress for the in-flight pull, if any."""
    with _pull_lock:
        s = dict(_pull_state)
    pct = 0
    if s["total"]:
        pct = min(100, int(s["completed"] * 100 / s["total"]))
    s["percent"] = pct
    s["ok"] = True

    # A finished pull replaces the local build, so the stored update verdict
    # and the validation record both describe a model that no longer exists.
    # Clearing the cached verdict here means the badge re-evaluates rather
    # than insisting an update is still available after it was installed.
    if s["done"] and not s["error"]:
        state = current_app.config.get("STATE")
        if state:
            try:
                state.set_app_state("ollama_update_check", "")
            except Exception:
                pass
    return jsonify(s)


@config_bp.route("/config/prowlarr/toggle-indexer", methods=["POST"])
def prowlarr_toggle_indexer():
    """Manually enable or disable an indexer in Prowlarr."""
    config_path = current_app.config["CONFIG_PATH"]
    data = request.get_json(silent=True) or {}
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        indexers = prowlarr.get_torrent_indexers(include_disabled=True)
        target = next((i for i in indexers if i["id"] == int(data["indexer_id"])), None)
        if not target:
            return jsonify({"ok": False, "message": "Indexer not found"})
        enable = bool(data.get("enabled", True))
        ok = prowlarr.set_indexer_enabled(target, enable)
        if ok:
            # Clear auto-disable state if manually re-enabled
            state = _get_state(cfg)
            if enable:
                state.clear_auto_disabled(int(data["indexer_id"]))
            action = "enabled" if enable else "disabled"
            return jsonify({"ok": True, "message": f"Indexer {action}"})
        return jsonify({"ok": False, "message": "Failed to update indexer"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/test/prowlarr", methods=["POST"])
def test_prowlarr():
    from core.prowlarr import ProwlarrClient
    body    = request.get_json(silent=True) or {}   # SEC-5
    url     = body.get("url", "")
    api_key = settings_schema.resolve_secret(body.get("api_key", ""), ("prowlarr", "api_key"),
                                             current_app.config["CONFIG_PATH"])
    try:
        client = ProwlarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/test/apprise", methods=["POST"])
def test_apprise():
    """Send a test notification via Apprise to verify URLs."""
    import apprise
    body = request.get_json(silent=True) or {}   # SEC-5
    urls = body.get("urls", [])
    if isinstance(urls, str):
        urls = urls.split("\n")
    if not isinstance(urls, list):
        urls = []
    # Masked lines ("discord://•••••••• (saved 1)") stand for stored URLs.
    try:
        urls = settings_schema.resolve_urls([u for u in urls if isinstance(u, str)],
                                            current_app.config["CONFIG_PATH"])
    except settings_schema.FieldError as exc:
        return jsonify({"ok": False, "message": str(exc)})
    if not urls:
        return jsonify({"ok": False, "message": "No notification URLs provided"})
    try:
        ap = apprise.Apprise()
        for url in urls:
            ap.add(url)
        ok = ap.notify(
            title="inspectarr",
            body="Test notification — Apprise is configured correctly.",
        )
        if ok:
            return jsonify({"ok": True, "message": "Test notification sent"})
        return jsonify({"ok": False, "message": "Apprise failed to send — check your URLs"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


# ---------------------------------------------------------------------------
# Backup endpoints
# ---------------------------------------------------------------------------

def _backup_dir() -> str:
    """Return (and create) the backups directory next to the config file."""
    config_path = current_app.config["CONFIG_PATH"]
    base = os.path.dirname(os.path.abspath(config_path))
    backup_path = os.path.join(base, "data", "backups")
    os.makedirs(backup_path, exist_ok=True)
    return backup_path


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


@config_bp.route("/config/backups", methods=["GET"])
def backups_list():
    """List existing backup zip files."""
    import re as _re
    backup_path = _backup_dir()
    backups = []
    for name in sorted(os.listdir(backup_path), reverse=True):
        if not name.startswith("inspectarr-backup-") or not name.endswith(".zip"):
            continue
        full = os.path.join(backup_path, name)
        stat = os.stat(full)
        from datetime import datetime
        created = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        backups.append({"name": name, "created": created, "size": _human_size(stat.st_size)})
    return jsonify({"ok": True, "backups": backups})


@config_bp.route("/config/backups/create", methods=["POST"])
def backups_create():
    """Create a new backup zip containing config.yaml and the SQLite DB."""
    import zipfile
    from datetime import datetime
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    db_file = raw.get("state", {}).get("db_file", "./data/inspectarr.db")
    # Resolve db_file relative to the config directory
    base = os.path.dirname(os.path.abspath(config_path))
    db_path = os.path.join(base, db_file) if not os.path.isabs(db_file) else db_file

    # M-11: realpath guard — ensure db_path doesn't escape the project directory
    allowed = os.path.realpath(base)
    if not os.path.realpath(db_path).startswith(allowed + os.sep):
        return jsonify({"ok": False, "message": "Database path escapes project directory"}), 400

    backup_path = _backup_dir()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"inspectarr-backup-{ts}.zip"
    zip_path = os.path.join(backup_path, zip_name)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(config_path, "config.yaml")
            if os.path.isfile(db_path):
                zf.write(db_path, "inspectarr.db")
        size = _human_size(os.path.getsize(zip_path))
        return jsonify({"ok": True, "message": f"Backup created: {zip_name} ({size})"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


@config_bp.route("/config/backups/download/<filename>", methods=["GET"])
def backups_download(filename):
    """Download a backup zip file."""
    import re as _re
    from flask import send_from_directory, abort
    # SEC: validate filename to prevent path traversal
    if not _re.match(r"^inspectarr-backup-[\d\-]+\.zip$", filename):
        abort(400, description="Invalid backup filename")
    backup_path = _backup_dir()
    full = os.path.join(backup_path, filename)
    if not os.path.isfile(full):
        abort(404, description="Backup not found")
    return send_from_directory(backup_path, filename, as_attachment=True)


@config_bp.route("/config/backups/delete", methods=["POST"])
def backups_delete():
    """Delete a backup zip file."""
    import re as _re
    data = request.get_json(silent=True) or {}
    filename = data.get("name", "")
    if not _re.match(r"^inspectarr-backup-[\d\-]+\.zip$", filename):
        return jsonify({"ok": False, "message": "Invalid backup filename"})
    backup_path = _backup_dir()
    full = os.path.join(backup_path, filename)
    if not os.path.isfile(full):
        return jsonify({"ok": False, "message": "Backup not found"})
    try:
        os.remove(full)
        return jsonify({"ok": True, "message": f"Deleted {filename}"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})
