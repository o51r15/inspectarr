"""
What each Settings form field is, and where it lives in config.yaml.

One row per field replaces the old _form_to_config(), which rebuilt the whole
file from the form and so dropped anything it did not know about (S2) or
forgot to carry over (BUG-10/11, B-14). form_to_updates() returns ONLY the
keys the submitted form actually contains; core.config_store merges those
into the document and leaves everything else -- unknown keys, comments,
fields with no form control -- exactly as it was.

The one subtlety is checkboxes. A browser sends nothing for an unticked
box, which looks the same as "this pane was not on the page". So every pane
also sends a hidden `_rendered` field naming each checkbox (and each
multi-value list) it contains:

    {{ rendered("sonarr_enabled") }}  ->  <input type="hidden" name="_rendered" value="sonarr_enabled">

    named in _rendered, absent from the form  ->  unticked: write False
    not named in _rendered                    ->  not on the page: leave alone
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from werkzeug.security import generate_password_hash

RENDERED = "_rendered"

# Form names that are not settings: the mode switch, the raw YAML text, the
# _rendered markers, and the dynamic rule_* fields (handled by _rules_from_form).
NOT_FIELDS = {"edit_mode", "raw_yaml", RENDERED}
RULE_PREFIX = "rule_"


class FieldError(ValueError):
    """A submitted value that cannot be saved. The message is user-facing."""


class _Keep:
    """Sentinel: leave the stored value as it is."""
    def __repr__(self):
        return "KEEP"


KEEP = _Keep()


@dataclass(frozen=True)
class Field:
    name: str                         # form field name
    path: tuple                       # where it lives in config.yaml
    kind: str                         # str | int | float | bool | choice | lines | list | password
    label: str = ""                   # for error messages
    choices: tuple = ()
    lo: Optional[float] = None
    hi: Optional[float] = None
    clamp: bool = False               # out of range: clamp silently instead of refusing
    clean: Optional[Callable] = None  # extra normalisation applied after coercion
    keep_if_blank: bool = False       # "" means "unchanged" (e.g. operating_mode)

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ")


def _upper(v):
    return v.upper()


def _lower(v):
    return v.lower()


def _url_tidy(v):
    # A trailing slash turns f"{url}/api/tags" into a 404.
    return v.strip().rstrip("/")


F = Field
FIELDS: list[Field] = [
    # --- Connections --------------------------------------------------------
    F("torrent_client", ("torrent_client",), "str"),
    F("qbit_url", ("qbittorrent", "url"), "str"),
    F("qbit_username", ("qbittorrent", "username"), "str"),
    F("qbit_password", ("qbittorrent", "password"), "str"),
    F("transmission_url", ("transmission", "url"), "str"),
    F("transmission_username", ("transmission", "username"), "str"),
    F("transmission_password", ("transmission", "password"), "str"),
    F("deluge_url", ("deluge", "url"), "str"),
    F("deluge_password", ("deluge", "password"), "str"),
    *[row for app in ("sonarr", "radarr", "lidarr") for row in (
        F(f"{app}_enabled", ("arrs", app, "enabled"), "bool"),
        F(f"{app}_url", ("arrs", app, "url"), "str"),
        F(f"{app}_api_key", ("arrs", app, "api_key"), "str"),
    )],
    # --- Protection ---------------------------------------------------------
    F("on_arr_failure", ("on_arr_failure",), "str"),
    F("dry_run", ("dry_run",), "bool"),
    F("remediation_min_severity", ("remediation", "min_severity"), "str", clean=_upper),
    F("remediation_remediate_at", ("remediation", "remediate_at"), "str", clean=_upper),
    F("remediation_quarantine_timeout", ("remediation", "quarantine_timeout_minutes"), "int",
      label="Quarantine timeout (minutes)", lo=0),
    F("remediation_timeout_action", ("remediation", "quarantine_timeout_action"), "str", clean=_lower),
    # Blank keeps the stored mode, never "automatic": a POST that omits it
    # must not silently promote a monitor-mode install to full deletion.
    F("remediation_operating_mode", ("remediation", "operating_mode"), "str",
      clean=lambda v: v.strip().lower(), keep_if_blank=True),
    # --- Scanning -----------------------------------------------------------
    F("polling_enabled", ("scanning", "polling", "enabled"), "bool"),
    F("polling_interval_seconds", ("scanning", "polling", "interval_seconds"), "int",
      label="Polling interval (seconds)", lo=1),
    F("webhooks_enabled", ("scanning", "webhooks", "enabled"), "bool"),
    F("webhooks_secret", ("scanning", "webhooks", "secret"), "str"),
    F("webhooks_scan_delay", ("scanning", "webhooks", "scan_delay_seconds"), "int",
      label="Webhook scan delay (seconds)", lo=0),
    F("retry_enabled", ("retry", "enabled"), "bool"),
    F("retry_max_attempts", ("retry", "max_attempts"), "int", label="Retry attempts", lo=0),
    F("retry_interval_seconds", ("retry", "interval_seconds"), "int", label="Retry interval (seconds)", lo=1),
    # --- Notifications ------------------------------------------------------
    F("apprise_enabled", ("notifications", "apprise", "enabled"), "bool"),
    F("apprise_urls", ("notifications", "apprise", "urls"), "lines"),
    F("notify_on", ("notifications", "apprise", "notify_on"), "list"),
    F("digest_enabled", ("notifications", "digest", "enabled"), "bool"),
    F("digest_use_ollama", ("notifications", "digest", "use_ollama"), "bool"),
    F("summary_enabled", ("notifications", "summary", "enabled"), "bool"),
    F("summary_schedule", ("notifications", "summary", "schedule"), "str"),
    F("summary_use_ollama", ("notifications", "summary", "use_ollama"), "bool"),
    # --- Indexers (Prowlarr) ------------------------------------------------
    F("prowlarr_enabled", ("prowlarr", "enabled"), "bool"),
    F("prowlarr_url", ("prowlarr", "url"), "str"),
    F("prowlarr_api_key", ("prowlarr", "api_key"), "str"),
    F("prowlarr_base_priority", ("prowlarr", "base_priority"), "int", label="Base priority"),
    F("prowlarr_reorder_interval", ("prowlarr", "reorder_interval_hours"), "int",
      label="Reorder interval (hours)", lo=1),
    F("prowlarr_history_window", ("prowlarr", "history_window_days"), "int",
      label="History window (days)", lo=1),
    F("prowlarr_min_grabs", ("prowlarr", "min_grabs_before_scoring"), "int",
      label="Minimum grabs before scoring", lo=0),
    *[F(f"scoring_{key}", ("prowlarr", "scoring", key), "float", label=key.replace("_", " "))
      for key in ("response_time_weight", "failure_rate_weight", "malicious_weight",
                  "grab_success_weight", "backoff_penalty", "malicious_penalty_per_hit",
                  "auth_failure_mult", "grab_failure_mult", "query_failure_mult",
                  "rss_failure_mult")],
    F("prowlarr_auto_manage_enabled", ("prowlarr", "auto_manage", "enabled"), "bool"),
    F("prowlarr_auto_manage_threshold", ("prowlarr", "auto_manage", "disable_threshold"), "float",
      label="Auto-disable threshold"),
    F("prowlarr_auto_manage_runs", ("prowlarr", "auto_manage", "consecutive_runs"), "int",
      label="Consecutive runs", lo=1),
    F("prowlarr_auto_manage_cooldown", ("prowlarr", "auto_manage", "cooldown_hours"), "int",
      label="Cooldown (hours)", lo=0),
    # --- AI -----------------------------------------------------------------
    F("ollama_enabled", ("prowlarr", "ollama", "enabled"), "bool"),
    F("ollama_url", ("prowlarr", "ollama", "url"), "str", clean=_url_tidy),
    F("ollama_timeout", ("prowlarr", "ollama", "timeout"), "int", label="AI timeout (seconds)", lo=1),
    F("ollama_cache_ttl_hours", ("prowlarr", "ollama", "cache_ttl_hours"), "int",
      label="AI cache (hours)", lo=0),
    F("ollama_update_check_hours", ("prowlarr", "ollama", "update_check_hours"), "int",
      label="Model update check (hours)", lo=1),
    # Clamped, not refused: these come from range sliders, and a hand-posted
    # 0 would make batching fall back to "all of them" -- the exact failure
    # calibration exists to prevent.
    F("ollama_max_indexers_per_call", ("prowlarr", "ollama", "max_indexers_per_call"), "int",
      lo=1, hi=100, clamp=True),
    F("ollama_context_window", ("prowlarr", "ollama", "context_window"), "int",
      lo=512, hi=131072, clamp=True),
    # --- Interface, security, data -----------------------------------------
    F("web_port", ("web", "port"), "int", label="Web port", lo=1, hi=65535),
    F("scheduler_autostart", ("web", "scheduler_autostart"), "bool"),
    F("auth_enabled", ("web", "auth", "enabled"), "bool"),
    F("auth_username", ("web", "auth", "username"), "str"),
    F("auth_password", ("web", "auth", "password"), "password"),
    F("log_file", ("logging", "log_file"), "str"),
    F("retention_days", ("logging", "retention_days"), "int", label="Log retention (days)", lo=1),
    F("log_level", ("logging", "level"), "str"),
    F("db_file", ("state", "db_file"), "str"),
]
del F

FIELDS_BY_NAME = {f.name: f for f in FIELDS}
assert len(FIELDS_BY_NAME) == len(FIELDS), "duplicate form field name in FIELDS"


def _number(f: Field, raw: str, cast):
    text = raw.strip()
    try:
        value = cast(text)
    except (TypeError, ValueError):
        kind = "a whole number" if cast is int else "a number"
        raise FieldError(f"{f.title}: '{raw}' is not {kind}.") from None
    if f.lo is not None and value < f.lo:
        if not f.clamp:
            raise FieldError(f"{f.title}: must be at least {f.lo:g}.")
        value = cast(f.lo)
    if f.hi is not None and value > f.hi:
        if not f.clamp:
            raise FieldError(f"{f.title}: must be at most {f.hi:g}.")
        value = cast(f.hi)
    return value


def _hash_password(raw: str):
    """H-01: store a hash, never the password.

    Blank means "unchanged" -- the old code hashed the empty string, so an
    accidental clear set the password to nothing. A value that is already a
    werkzeug hash is the stored one echoed back and is kept as-is.
    """
    if raw == "":
        return KEEP
    if raw.startswith(("pbkdf2:", "scrypt:")):
        return raw
    return generate_password_hash(raw)


def coerce(f: Field, values: list):
    """The value to store for `f`, given every value the form sent for it.

    Returns KEEP to leave the stored value untouched.
    """
    if f.kind == "list":
        return [v for v in values if v]
    raw = values[-1] if values else ""
    if f.kind == "bool":
        return bool(values) and raw not in ("", "0", "false", "off")
    if f.kind == "lines":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if f.kind == "password":
        return _hash_password(raw)
    if f.keep_if_blank and not raw.strip():
        return KEEP
    if f.kind == "int":
        value = _number(f, raw, int)
    elif f.kind == "float":
        value = _number(f, raw, float)
    else:
        value = raw
    return f.clean(value) if f.clean else value


def _set(tree: dict, path: tuple, value) -> None:
    node = tree
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def form_to_updates(form) -> dict:
    """The config changes a submitted Settings form asks for -- and nothing else.

    Raises FieldError (user-facing message) for a value that cannot be saved;
    the caller shows it and writes nothing.
    """
    rendered = set(form.getlist(RENDERED))
    updates: dict = {}
    for f in FIELDS:
        multi = f.kind in ("bool", "list")
        if multi and f.name not in rendered and f.name not in form:
            continue                              # not on the page
        if not multi and f.name not in form:
            continue                              # not on the page
        value = coerce(f, form.getlist(f.name))
        if value is not KEEP:
            _set(updates, f.path, value)
    if "rules" in rendered:
        updates["rules"] = rules_from_form(form)
    return updates


def rules_from_form(form) -> list:
    """Rules from the dynamic rule_<field>_<index> inputs.

    Indices may be non-contiguous after the user deletes a rule in the UI
    (0, 1, 3), so every present index is collected rather than stopping at
    the first gap -- otherwise rules after a gap would be silently dropped.
    """
    indices = sorted({
        int(k.rsplit("_", 1)[1])
        for k in form.keys()
        if k.startswith("rule_name_") and k.rsplit("_", 1)[1].isdigit()
    })
    rules = []
    for i in indices:
        exts = [e.strip() for e in form.get(f"rule_extensions_{i}", "").split(",") if e.strip()]
        patterns = [p.strip() for p in form.get(f"rule_patterns_{i}", "").split(",") if p.strip()]
        conditions = {"match_mode": form.get(f"rule_match_mode_{i}", "any"), "bad_extensions": exts}
        if patterns:
            conditions["bad_filename_patterns"] = patterns
        size = form.get(f"rule_min_size_mb_{i}", "").strip()
        if size:
            try:
                conditions["min_file_size_mb"] = int(size)
            except ValueError:
                name = form.get(f"rule_name_{i}", "") or f"#{i + 1}"
                raise FieldError(f"Rule {name}: minimum size '{size}' is not a whole number.") from None
        rules.append({
            "name": form.get(f"rule_name_{i}", ""),
            "category": form.get(f"rule_category_{i}", ""),
            "app": form.get(f"rule_app_{i}", "sonarr"),
            "conditions": conditions,
        })
    return rules
