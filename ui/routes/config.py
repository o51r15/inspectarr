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
            return redirect(url_for("config.config_view") + "?saved=1")
        except yaml.YAMLError as e:
            raw = _load_raw(config_path)
            return render_template("config.html", cfg=raw, raw_yaml=raw_yaml,
                                   error=f"YAML error: {e}", edit_mode="yaml")

    # Form mode — rebuild config dict from form fields
    data = _form_to_config(request.form, _load_raw(config_path))
    _save_raw(config_path, data)
    return redirect(url_for("config.config_view") + "?saved=1")


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
        exts = [e.strip() for e in form.get(f"rule_extensions_{i}", "").split(",") if e.strip()]
        rules.append({
            "name":     form.get(f"rule_name_{i}", ""),
            "category": form.get(f"rule_category_{i}", ""),
            "app":      form.get(f"rule_app_{i}", "sonarr"),
            "conditions": {
                "match_mode":     form.get(f"rule_match_mode_{i}", "any"),
                "bad_extensions": exts,
            }
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
                "enabled": False,
                "url":     form.get("radarr_url", existing.get("arrs", {}).get("radarr", {}).get("url", "")),
                "api_key": existing.get("arrs", {}).get("radarr", {}).get("api_key", ""),
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
        "web":    {"port": int(form.get("web_port", 8585))},
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
