from flask import Blueprint, render_template, current_app, request, redirect, url_for, jsonify
import yaml
import os

config_bp = Blueprint("config", __name__)


def _load_raw(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_raw(config_path: str, data: dict):
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


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
            _save_raw(config_path, data)
            return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")
        except yaml.YAMLError as e:
            raw = _load_raw(config_path)
            return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                                   error=f"YAML error: {e}", edit_mode="yaml")

    # Form mode — rebuild config dict from form fields
    data = _form_to_config(request.form, _load_raw(config_path))
    _save_raw(config_path, data)
    return redirect(url_for("config.config_view") + "?toast=Configuration+saved&level=success")


@config_bp.route("/config/test/qbit", methods=["POST"])
def test_qbit():
    from core.qbit import QBittorrentClient
    url      = request.json.get("url", "")
    username = request.json.get("username", "")
    password = request.json.get("password", "")
    try:
        client = QBittorrentClient(url, username, password)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/lidarr", methods=["POST"])
def test_lidarr():
    from core.arrs.lidarr import LidarrClient
    url     = request.json.get("url", "")
    api_key = request.json.get("api_key", "")
    try:
        client = LidarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/radarr", methods=["POST"])
def test_radarr():
    from core.arrs.radarr import RadarrClient
    url     = request.json.get("url", "")
    api_key = request.json.get("api_key", "")
    try:
        client = RadarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@config_bp.route("/config/test/sonarr", methods=["POST"])
def test_sonarr():
    from core.arrs.sonarr import SonarrClient
    url     = request.json.get("url", "")
    api_key = request.json.get("api_key", "")
    try:
        client = SonarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


def _form_to_config(form, existing: dict) -> dict:
    """Rebuild the config dict from submitted form data."""
    # Parse rules (dynamic fields: rule_name_0, rule_category_0, etc.)
    rules = []
    i = 0
    while f"rule_name_{i}" in form:
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
        i += 1

    notify_on = form.getlist("notify_on")

    return {
        "qbittorrent": {
            "url":      form.get("qbit_url", ""),
            "username": form.get("qbit_username", ""),
            "password": form.get("qbit_password", ""),
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
        "poll_interval_seconds": int(form.get("poll_interval_seconds", 300)),
        "dry_run":               "dry_run" in form,
        "retry": {
            "enabled":          "retry_enabled" in form,
            "max_attempts":     int(form.get("retry_max_attempts", 10)),
            "interval_seconds": int(form.get("retry_interval_seconds", 600)),
        },
        "logging": {
            "log_file":       form.get("log_file", "./data/inspectarr.log.json"),
            "retention_days": int(form.get("retention_days", 30)),
            "level":          form.get("log_level", "INFO"),
        },
        "state":  {"db_file": form.get("db_file", "./data/inspectarr.db")},
        "web":    {
            "port": int(form.get("web_port", 8585)),
            "scheduler_autostart": "scheduler_autostart" in form,
            "auth": {
                "enabled":  "auth_enabled" in form,
                "username": form.get("auth_username", "admin"),
                "password": form.get("auth_password", "changeme"),
            },
        },
        "prowlarr": existing.get("prowlarr", {}),
        "notifications": {
            "pushover": {
                "enabled":   "pushover_enabled" in form,
                "app_token": form.get("pushover_app_token", ""),
                "user_key":  form.get("pushover_user_key", ""),
                "notify_on": notify_on,
                "priority":  int(form.get("pushover_priority", 0)),
            }
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
        from core.state import StateManager
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = StateManager(
            db_path=cfg.state.db_file,
            log_path=cfg.logging.log_file,
            retention_days=cfg.logging.retention_days,
        )
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        results = scorer.score_all()
        for r in results:
            r.pop("_raw", None)
        return jsonify({"ok": True, "indexers": results})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@config_bp.route("/config/prowlarr/save", methods=["POST"])
def prowlarr_save():
    """Merge the Prowlarr config block into config.yaml."""
    config_path = current_app.config["CONFIG_PATH"]
    data        = request.json or {}
    try:
        raw = _load_raw(config_path)
        raw["prowlarr"] = {
            "enabled":                data.get("enabled", False),
            "url":                    data.get("url", ""),
            "api_key":                data.get("api_key", ""),
            "base_priority":          int(data.get("base_priority", 10)),
            "reorder_interval_hours": int(data.get("reorder_interval_hours", 24)),
            "history_window_days":    int(data.get("history_window_days", 90)),
            "min_grabs_before_scoring": int(data.get("min_grabs_before_scoring", 10)),
            "scoring": {
                "response_time_weight":      float(data.get("response_time_weight", 0.35)),
                "failure_rate_weight":       float(data.get("failure_rate_weight", 0.40)),
                "malicious_weight":          float(data.get("malicious_weight", 0.25)),
                "backoff_penalty":           float(data.get("backoff_penalty", 20.0)),
                "malicious_penalty_per_hit": float(data.get("malicious_penalty_per_hit", 10.0)),
            },
        }
        _save_raw(config_path, raw)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@config_bp.route("/config/prowlarr/set-ignored", methods=["POST"])
def prowlarr_set_ignored():
    """Toggle the ignore/pin state for a single indexer."""
    config_path = current_app.config["CONFIG_PATH"]
    data        = request.json or {}
    try:
        from core.config import load_config
        from core.state import StateManager
        cfg   = load_config(config_path)
        state = StateManager(
            db_path=cfg.state.db_file,
            log_path=cfg.logging.log_file,
            retention_days=cfg.logging.retention_days,
        )
        state.set_indexer_ignored(
            indexer_id=int(data["indexer_id"]),
            indexer_name=data["indexer_name"],
            ignored=bool(data["ignored"]),
            pinned_position=data.get("pinned_position"),
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@config_bp.route("/config/prowlarr/rescore", methods=["POST"])
def prowlarr_rescore():
    """Manually trigger a full score + reorder cycle."""
    config_path = current_app.config["CONFIG_PATH"]
    try:
        from core.config import load_config
        from core.prowlarr import ProwlarrClient
        from core.indexer_scorer import IndexerScorer
        from core.state import StateManager
        cfg = load_config(config_path)
        if not cfg.prowlarr.enabled:
            return jsonify({"ok": False, "message": "Prowlarr is not enabled"})
        prowlarr = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
        state    = StateManager(
            db_path=cfg.state.db_file,
            log_path=cfg.logging.log_file,
            retention_days=cfg.logging.retention_days,
        )
        scorer  = IndexerScorer(prowlarr, state, cfg.prowlarr)
        changed = scorer.reorder()
        return jsonify({"ok": True, "changed": changed})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@config_bp.route("/config/test/prowlarr", methods=["POST"])
def test_prowlarr():
    from core.prowlarr import ProwlarrClient
    url     = request.json.get("url", "")
    api_key = request.json.get("api_key", "")
    try:
        client = ProwlarrClient(url, api_key)
        ok     = client.test_connection()
        return jsonify({"ok": ok, "message": "Connected" if ok else "Failed"})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@config_bp.route("/config/test/pushover", methods=["POST"])
def test_pushover():
    """Send a test Pushover notification to verify credentials."""
    import requests as req
    app_token = request.json.get("app_token", "")
    user_key  = request.json.get("user_key", "")
    if not app_token or not user_key:
        return jsonify({"ok": False, "message": "App token and user key are required"})
    try:
        resp = req.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   app_token,
                "user":    user_key,
                "title":   "inspectarr",
                "message": "Test notification — Pushover is configured correctly.",
                "priority": 0,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == 1:
            return jsonify({"ok": True, "message": "Test notification sent"})
        errors = ", ".join(data.get("errors", ["Unknown error"]))
        return jsonify({"ok": False, "message": errors})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})
