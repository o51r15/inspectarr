from flask import Blueprint, render_template, current_app, request, redirect, url_for, jsonify
import yaml
import os

from werkzeug.security import generate_password_hash
from ui.routes._utils import safe_error

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
    """
    L-17: Atomic config write — try temp-file-then-replace first (safest),
    fall back to direct overwrite if the directory isn't writable (e.g.
    Docker bind-mounted config.yaml where /app is read-only).
    """
    import tempfile
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    dir_name = os.path.dirname(os.path.abspath(config_path))

    # --- Attempt 1: atomic write via temp file in same dir ---
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except PermissionError:
        # --- Attempt 2: direct overwrite (bind-mount safe) ---
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass  # Windows / container without chown support


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
    config_path = current_app.config["CONFIG_PATH"]
    raw         = _load_raw(config_path)
    raw_yaml    = yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return render_template("config.html", cfg=raw, raw_yaml=raw_yaml)


@config_bp.route("/config/save", methods=["POST"])
def config_save():
    config_path = current_app.config["CONFIG_PATH"]
    mode        = request.form.get("edit_mode", "form")

    if mode == "yaml":
        raw_yaml = request.form.get("raw_yaml", "")
        try:
            data = yaml.safe_load(raw_yaml)
        except yaml.YAMLError as e:
            raw = _load_raw(config_path)
            return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                                   error=f"YAML error: {e}", edit_mode="yaml")
        # BUG-05: yaml.safe_load("") returns None; writing None destroys the
        # config file (yaml.dump(None) → "null\n") and bricks the app.
        if not isinstance(data, dict):
            raw = _load_raw(config_path)
            return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                                   error="YAML must be a mapping — cannot save empty or scalar content.",
                                   edit_mode="yaml")
        # IMP-4: refuse to write a config the core cannot load
        err = _validation_error(data)
        if err:
            raw = _load_raw(config_path)
            return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                                   error=f"Config invalid — not saved: {err}",
                                   edit_mode="yaml")
        _save_raw(config_path, data)
        return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")

    # Form mode — rebuild config dict from form fields
    data = _form_to_config(request.form, _load_raw(config_path))
    # IMP-4: refuse to write a config the core cannot load
    err = _validation_error(data)
    if err:
        raw      = _load_raw(config_path)
        raw_yaml = yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                               error=f"Config invalid — not saved: {err}")
    _save_raw(config_path, data)
    return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")


@config_bp.route("/config/test/qbit", methods=["POST"])
def test_qbit():
    from core.qbit import QBittorrentClient
    body     = request.get_json(silent=True) or {}   # SEC-5: no 415 on bad body
    url      = body.get("url", "")
    username = body.get("username", "")
    password = body.get("password", "")
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
    password = body.get("password", "")
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
    password = body.get("password", "")
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
    api_key = body.get("api_key", "")
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
    api_key = body.get("api_key", "")
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
    api_key = body.get("api_key", "")
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


def _hash_if_new(raw_password: str, existing: dict) -> str:
    """
    H-01: Hash the password before saving to config.yaml.
    If the submitted value is already a werkzeug hash (user didn't change the
    password field), return it as-is to avoid double-hashing.
    """
    if raw_password.startswith(("pbkdf2:", "scrypt:")):
        return raw_password  # already hashed — user didn't change it
    return generate_password_hash(raw_password)


def _form_to_config(form, existing: dict) -> dict:
    """Rebuild the config dict from submitted form data."""
    # Parse rules (dynamic fields: rule_name_0, rule_category_0, etc.)
    # Indices may be non-contiguous after the user deletes a rule in the UI
    # (e.g. 0, 1, 3), so collect all present indices rather than stopping at
    # the first gap — otherwise rules after a gap would be silently dropped.
    rule_indices = sorted({
        int(k.rsplit("_", 1)[1])
        for k in form.keys()
        if k.startswith("rule_name_") and k.rsplit("_", 1)[1].isdigit()
    })
    rules = []
    for i in rule_indices:
        exts     = [e.strip() for e in form.get(f"rule_extensions_{i}", "").split(",") if e.strip()]
        patterns = [p.strip() for p in form.get(f"rule_patterns_{i}", "").split(",") if p.strip()]
        min_size_raw = form.get(f"rule_min_size_mb_{i}", "").strip()
        conditions = {
            "match_mode":     form.get(f"rule_match_mode_{i}", "any"),
            "bad_extensions": exts,
        }
        if patterns:
            conditions["bad_filename_patterns"] = patterns
        if min_size_raw:
            try:
                conditions["min_file_size_mb"] = int(min_size_raw)
            except ValueError:
                pass
        rules.append({
            "name":       form.get(f"rule_name_{i}", ""),
            "category":   form.get(f"rule_category_{i}", ""),
            "app":        form.get(f"rule_app_{i}", "sonarr"),
            "conditions": conditions,
        })

    notify_on = form.getlist("notify_on")

    # Fold Prowlarr's visible fields into the unified save WITHOUT clobbering
    # the nested `scoring` block (which has no form fields). Start from whatever
    # is already in the file and overwrite only the keys the form controls.
    prowlarr_block = dict(existing.get("prowlarr", {}) or {})
    if "prowlarr_url" in form:  # the Prowlarr pane was rendered/submitted
        prowlarr_block.update({
            "enabled":                  "prowlarr_enabled" in form,
            "url":                      form.get("prowlarr_url", ""),
            "api_key":                  form.get("prowlarr_api_key", ""),
            "base_priority":            _int(form.get("prowlarr_base_priority"), 10),
            "reorder_interval_hours":   _int(form.get("prowlarr_reorder_interval"), 24),
            "history_window_days":      _int(form.get("prowlarr_history_window"), 90),
            "min_grabs_before_scoring": _int(form.get("prowlarr_min_grabs"), 10),
        })
        # Save scoring weights from form fields if present, otherwise preserve existing
        if "scoring_response_time_weight" in form:
            prowlarr_block["scoring"] = {
                "response_time_weight":      _float(form.get("scoring_response_time_weight"), 0.25),
                "failure_rate_weight":       _float(form.get("scoring_failure_rate_weight"), 0.30),
                "malicious_weight":          _float(form.get("scoring_malicious_weight"), 0.20),
                "grab_success_weight":       _float(form.get("scoring_grab_success_weight"), 0.25),
                "backoff_penalty":           _float(form.get("scoring_backoff_penalty"), 20.0),
                "malicious_penalty_per_hit": _float(form.get("scoring_malicious_penalty_per_hit"), 10.0),
                "auth_failure_mult":         _float(form.get("scoring_auth_failure_mult"), 3.0),
                "grab_failure_mult":         _float(form.get("scoring_grab_failure_mult"), 2.0),
                "query_failure_mult":        _float(form.get("scoring_query_failure_mult"), 1.0),
                "rss_failure_mult":          _float(form.get("scoring_rss_failure_mult"), 0.5),
            }
        elif "scoring" in (existing.get("prowlarr") or {}):
            prowlarr_block["scoring"] = existing["prowlarr"]["scoring"]
        # Auto-manage from form fields
        prowlarr_block["auto_manage"] = {
            "enabled": "prowlarr_auto_manage_enabled" in form,
            "disable_threshold": _float(form.get("prowlarr_auto_manage_threshold"), 30.0),
            "consecutive_runs": _int(form.get("prowlarr_auto_manage_runs"), 3),
            "cooldown_hours": _int(form.get("prowlarr_auto_manage_cooldown"), 24),
        }

    return {
        "torrent_client": form.get("torrent_client", "qbittorrent"),
        "qbittorrent": {
            "url":      form.get("qbit_url", ""),
            "username": form.get("qbit_username", ""),
            "password": form.get("qbit_password", ""),
        },
        "transmission": {
            "url":      form.get("transmission_url", ""),
            "username": form.get("transmission_username", ""),
            "password": form.get("transmission_password", ""),
        },
        "deluge": {
            "url":      form.get("deluge_url", ""),
            "password": form.get("deluge_password", ""),
        },
        "arrs": {
            "sonarr": {
                "enabled": "sonarr_enabled" in form,
                "url":     form.get("sonarr_url", ""),
                "api_key": form.get("sonarr_api_key", ""),
            },
            "radarr": {
                "enabled": "radarr_enabled" in form,
                "url":     form.get("radarr_url", ""),
                "api_key": form.get("radarr_api_key", ""),
            },
            "lidarr": {
                "enabled": "lidarr_enabled" in form,
                "url":     form.get("lidarr_url", ""),
                "api_key": form.get("lidarr_api_key", ""),
            },
        },
        "rules": rules,
        "on_arr_failure":        form.get("on_arr_failure", "delete"),
        "poll_interval_seconds": _int(form.get("polling_interval_seconds"), 300),
        "dry_run":               "dry_run" in form,
        "scanning": {
            "polling": {
                "enabled": "polling_enabled" in form,
                "interval_seconds": _int(form.get("polling_interval_seconds"), 300),
            },
            "webhooks": {
                "enabled": "webhooks_enabled" in form,
                "secret": form.get("webhooks_secret", ""),
                "scan_delay_seconds": _int(form.get("webhooks_scan_delay"), 60),
            },
        },
        "retry": {
            "enabled":          "retry_enabled" in form,
            "max_attempts":     _int(form.get("retry_max_attempts"), 10),
            "interval_seconds": _int(form.get("retry_interval_seconds"), 600),
        },
        "logging": {
            "log_file":       form.get("log_file", "./data/inspectarr.log.json"),
            "retention_days": _int(form.get("retention_days"), 30),
            "level":          form.get("log_level", "INFO"),
        },
        "state":  {"db_file": form.get("db_file", "./data/inspectarr.db")},
        "web":    {
            "port": _int(form.get("web_port"), 8585),
            "scheduler_autostart": "scheduler_autostart" in form,
            "auth": {
                "enabled":  "auth_enabled" in form,
                "username": form.get("auth_username", "admin"),
                "password": _hash_if_new(form.get("auth_password", "changeme"), existing),
            },
        },
        "prowlarr": prowlarr_block,
        "notifications": {
            "apprise": {
                "enabled":   "apprise_enabled" in form,
                "urls":      [u.strip() for u in form.get("apprise_urls", "").split("\n") if u.strip()],
                "notify_on": notify_on,
            },
            "digest": {
                "enabled": "digest_enabled" in form,
                "use_ollama": "digest_use_ollama" in form,
            },
            "summary": {
                "enabled": "summary_enabled" in form,
                "schedule": form.get("summary_schedule", "daily"),
                "use_ollama": "summary_use_ollama" in form,
            },
        },
    }


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
        ai_available = bool(cfg.prowlarr.ollama.url and cfg.prowlarr.ollama.model)
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
        if not cfg.prowlarr.ollama.url or not cfg.prowlarr.ollama.model:
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
        models = sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))
        return jsonify({"ok": True, "models": models, "current": current_model})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc), "models": [], "current": current_model})


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
    """Check if the current Ollama model has an update available."""
    import requests as req
    config_path = current_app.config["CONFIG_PATH"]
    raw = _load_raw(config_path)
    ollama_url = raw.get("prowlarr", {}).get("ollama", {}).get("url", "")
    model = raw.get("prowlarr", {}).get("ollama", {}).get("model", "")
    if not ollama_url or not model:
        return jsonify({"ok": False, "message": "Ollama not configured"})
    try:
        # Get local model digest
        local_resp = req.post(
            f"{ollama_url.rstrip('/')}/api/show",
            json={"name": model},
            timeout=15,
        )
        if local_resp.status_code != 200:
            return jsonify({"ok": False, "message": f"Could not query local model: HTTP {local_resp.status_code}"})
        local_data = local_resp.json()
        local_digest = local_data.get("digest", "")
        # Try to check the registry for the latest digest
        # Ollama's /api/pull with stream=false in dry-run is not available,
        # so we use the manifest approach: pull with stream and read first status
        try:
            pull_resp = req.post(
                f"{ollama_url.rstrip('/')}/api/pull",
                json={"name": model, "stream": False},
                timeout=30,
            )
            if pull_resp.status_code == 200:
                pull_data = pull_resp.json()
                # If status is "success" immediately, model is up to date
                status = pull_data.get("status", "")
                if "up to date" in status.lower() or status == "success":
                    return jsonify({"ok": True, "update_available": False, "model": model})
                else:
                    return jsonify({"ok": True, "update_available": True, "model": model,
                                    "message": f"Update available for {model}"})
        except Exception:
            pass
        # If pull check fails, just return the local info
        return jsonify({"ok": True, "update_available": False, "model": model,
                        "message": "Could not check registry — local model info returned",
                        "digest": local_digest[:12] if local_digest else ""})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc)})


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
    api_key = body.get("api_key", "")
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
        urls = [u.strip() for u in urls.split("\n") if u.strip()]
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
