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

import copy
import re
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
    kind: str                         # str | int | float | bool | lines | list | secret | secret_lines | password
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


def _think(v):
    """Form value -> stored think value. "" = recommended (null)."""
    v = (v or "").strip().lower()
    if v in ("", "recommended", "auto"):
        return None
    if v in ("off", "false"):
        return False
    if v in ("on", "true"):
        return True
    if v in ("low", "medium", "high"):
        return v
    raise FieldError("How the model thinks: choose recommended, off, on, low, medium or high")


def _url_tidy(v):
    # A trailing slash turns f"{url}/api/tags" into a 404.
    return v.strip().rstrip("/")


F = Field
FIELDS: list[Field] = [
    # --- Connections --------------------------------------------------------
    F("torrent_client", ("torrent_client",), "str"),
    F("qbit_url", ("qbittorrent", "url"), "str"),
    F("qbit_username", ("qbittorrent", "username"), "str"),
    F("qbit_password", ("qbittorrent", "password"), "secret"),
    F("transmission_url", ("transmission", "url"), "str"),
    F("transmission_username", ("transmission", "username"), "str"),
    F("transmission_password", ("transmission", "password"), "secret"),
    F("deluge_url", ("deluge", "url"), "str"),
    F("deluge_password", ("deluge", "password"), "secret"),
    *[row for app in ("sonarr", "radarr", "lidarr") for row in (
        F(f"{app}_enabled", ("arrs", app, "enabled"), "bool"),
        F(f"{app}_url", ("arrs", app, "url"), "str"),
        F(f"{app}_api_key", ("arrs", app, "api_key"), "secret"),
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
    F("webhooks_secret", ("scanning", "webhooks", "secret"), "secret"),
    F("webhooks_scan_delay", ("scanning", "webhooks", "scan_delay_seconds"), "int",
      label="Webhook scan delay (seconds)", lo=0),
    F("retry_enabled", ("retry", "enabled"), "bool"),
    F("retry_max_attempts", ("retry", "max_attempts"), "int", label="Retry attempts", lo=0),
    F("retry_interval_seconds", ("retry", "interval_seconds"), "int", label="Retry interval (seconds)", lo=1),
    # --- Notifications ------------------------------------------------------
    F("apprise_enabled", ("notifications", "apprise", "enabled"), "bool"),
    F("apprise_urls", ("notifications", "apprise", "urls"), "secret_lines"),
    F("notify_on", ("notifications", "apprise", "notify_on"), "list"),
    F("digest_enabled", ("notifications", "digest", "enabled"), "bool"),
    F("digest_use_ollama", ("notifications", "digest", "use_ollama"), "bool"),
    F("summary_enabled", ("notifications", "summary", "enabled"), "bool"),
    F("summary_schedule", ("notifications", "summary", "schedule"), "str"),
    F("summary_use_ollama", ("notifications", "summary", "use_ollama"), "bool"),
    # --- Indexers (Prowlarr) ------------------------------------------------
    F("prowlarr_enabled", ("prowlarr", "enabled"), "bool"),
    F("prowlarr_url", ("prowlarr", "url"), "str"),
    F("prowlarr_api_key", ("prowlarr", "api_key"), "secret"),
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
    F("ollama_think", ("prowlarr", "ollama", "think"), "str", label="How the model thinks",
      clean=_think),
    F("ollama_calibrate_new_models", ("prowlarr", "ollama", "calibrate_new_models"), "bool"),
    F("ollama_overnight_hour", ("prowlarr", "ollama", "overnight_hour"), "int",
      label="Overnight hour", lo=0, hi=23, clamp=True),
    F("ollama_context_window", ("prowlarr", "ollama", "context_window"), "int",
      lo=512, hi=262144, clamp=True),
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

    Blank or the mask means "unchanged" -- the old code hashed the empty
    string, so an accidental clear set the password to nothing. The hash
    itself is never sent to the browser any more (it is masked like every
    other secret); a value that looks like a hash is still kept as-is, for
    anyone posting the form by hand.
    """
    if raw in ("", SECRET_MASK):
        return KEEP
    if raw.startswith(("pbkdf2:", "scrypt:")):
        return raw
    return generate_password_hash(raw)


def coerce(f: Field, values: list, current: dict = None):
    """The value to store for `f`, given every value the form sent for it.

    `current` is the stored config (plain dict), needed to restore masked
    values. Returns KEEP to leave the stored value untouched.
    """
    if f.kind == "list":
        chosen = [v for v in values if v]
        stored = _get(current or {}, f.path)
        # A set of choices (notify_on): the checkboxes render in a fixed order,
        # so the same set ticked in a different order is not a change.
        if isinstance(stored, list) and set(map(str, stored)) == set(chosen):
            return KEEP
        return chosen
    raw = values[-1] if values else ""
    if f.kind == "secret":
        return KEEP if raw == SECRET_MASK else raw
    if f.kind == "secret_lines":
        stored = _get(current or {}, f.path) or []
        return [unmask_url(line.strip(), stored) for line in raw.splitlines() if line.strip()]
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


def form_to_updates(form, current: dict = None) -> dict:
    """The config changes a submitted Settings form asks for -- and nothing else.

    `current` is the stored config as a plain dict, used to restore masked
    secrets. Raises FieldError (user-facing message) for a value that cannot
    be saved; the caller shows it and writes nothing.
    """
    rendered = set(form.getlist(RENDERED))
    effective = _effective_config(current)
    updates: dict = {}
    for f in FIELDS:
        multi = f.kind in ("bool", "list")
        if multi and f.name not in rendered and f.name not in form:
            continue                              # not on the page
        if not multi and f.name not in form:
            continue                              # not on the page
        value = coerce(f, form.getlist(f.name), current)
        if value is KEEP:
            continue
        if effective is not None and _absent(current, f.path) \
                and _same_value(value, _effective_value(effective, f)):
            continue                              # would only spell out what is already in effect
        _set(updates, f.path, value)
    if "rules" in rendered:
        rules = rules_from_form(form)
        if not (rules == [] and current is not None and _absent(current, ("rules",))):
            updates["rules"] = rules
    return updates


def _absent(tree: dict, path: tuple) -> bool:
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return True
        node = node[key]
    return False


def _effective_config(current):
    """What the app makes of the stored file, or None if it cannot say."""
    if current is None:
        return None
    try:
        from core.config import _parse_config
        return _parse_config(copy.deepcopy(current))
    except Exception:
        return None          # unparseable file: write everything the form shows


_UNSET = object()


def _effective_value(cfg, f: Field):
    """The value the app is using right now for `f`'s path.

    A section the parser did not build (no `deluge:` block -> cfg.deluge is
    None) means the field is effectively empty.
    """
    node = cfg
    for key in f.path:
        if node is None:
            return {"str": "", "secret": "", "password": "", "bool": False,
                    "lines": [], "secret_lines": [], "list": []}.get(f.kind, _UNSET)
        if not hasattr(node, key):
            return _UNSET
        node = getattr(node, key)
    return node


def _same_value(value, effective) -> bool:
    if effective is _UNSET:
        return False
    if isinstance(value, bool) or isinstance(effective, bool):
        return type(value) is type(effective) and value == effective
    return value == effective


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


# --- Secrets ------------------------------------------------------------------
#
# The browser only ever holds a mask. A stored secret is rendered as
# SECRET_MASK; posting the mask back means "unchanged", clearing the field
# clears the secret, typing replaces it. The same mask is used in the form,
# in the raw YAML editor and by the Test buttons, so there is one rule to
# get right. (A real password of exactly eight bullet characters cannot be
# saved through the UI; accepted.)
#
# Apprise URLs carry tokens in the URL itself, so each is shown as
# "discord://•••••••• (saved 1)" -- the scheme stays readable, the number says
# which stored URL it stands for. Reordering or deleting lines still works.

SECRET_MASK = "•" * 8
APPRISE_PATH = ("notifications", "apprise", "urls")
SECRET_PATHS = tuple(f.path for f in FIELDS if f.kind in ("secret", "password"))
_SCHEME = re.compile(r"[A-Za-z][\w+.-]*")
_MASKED_URL = re.compile(
    r"^(?:[A-Za-z][\w+.-]*://)?" + re.escape(SECRET_MASK) + r" \(saved (\d+)\)$")


def _get(tree, path: tuple):
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _assign(tree, path: tuple, value) -> None:
    """Set a value that already exists. Never creates keys."""
    parent = _get(tree, path[:-1]) if len(path) > 1 else tree
    if isinstance(parent, dict) and path[-1] in parent:
        parent[path[-1]] = value


def _url_list(value) -> list:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, str) and value else []


def mask_url(url: str, number: int) -> str:
    scheme = url.split("://", 1)[0] if "://" in url else ""
    prefix = f"{scheme}://" if scheme and _SCHEME.fullmatch(scheme) else ""
    return f"{prefix}{SECRET_MASK} (saved {number})"


def unmask_url(line: str, stored) -> str:
    """A masked line back to the stored URL it stands for; anything else as typed."""
    match = _MASKED_URL.match(line)
    if not match:
        return line
    stored = _url_list(stored)
    number = int(match.group(1))
    if 1 <= number <= len(stored) and isinstance(stored[number - 1], str):
        return stored[number - 1]
    raise FieldError(f"Notification URL '{line}' stands for a saved URL that no longer "
                     "exists. Paste the URL in again.")


def mask_secrets(tree) -> None:
    """Replace every stored secret in `tree` with the mask, in place.

    Works on a plain dict or a ruamel document; list items are replaced one
    by one so comments inside the list survive in the YAML view.
    """
    for path in SECRET_PATHS:
        if _get(tree, path):
            _assign(tree, path, SECRET_MASK)
    urls = _get(tree, APPRISE_PATH)
    if isinstance(urls, list):
        for i, url in enumerate(urls):
            if isinstance(url, str) and url:
                urls[i] = mask_url(url, i + 1)
    elif isinstance(urls, str) and urls:
        _assign(tree, APPRISE_PATH, mask_url(urls, 1))


def masked_copy(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    mask_secrets(out)
    return out


def restore_secrets(tree, current: dict) -> None:
    """Undo mask_secrets on a submitted document, using the stored values.

    Used by the raw YAML editor: whatever still carries the mask is put back
    to what is stored, so editing the file never leaks or loses a secret.
    """
    for path in SECRET_PATHS:
        if _get(tree, path) == SECRET_MASK:
            _assign(tree, path, _get(current, path) or "")
    urls = _get(tree, APPRISE_PATH)
    stored = _get(current, APPRISE_PATH)
    if isinstance(urls, list):
        for i, url in enumerate(urls):
            if isinstance(url, str):
                urls[i] = unmask_url(url, stored)
    elif isinstance(urls, str):
        _assign(tree, APPRISE_PATH, unmask_url(urls, stored))


def resolve_secret(submitted, path: tuple, config_path: str) -> str:
    """For the Test buttons: the mask means 'use the stored value'."""
    if submitted != SECRET_MASK:
        return submitted or ""
    from core.config import load_raw_config
    return _get(load_raw_config(config_path), path) or ""


def resolve_urls(lines, config_path: str) -> list:
    """For the Apprise Test button: masked lines become the stored URLs."""
    from core.config import load_raw_config
    stored = _get(load_raw_config(config_path), APPRISE_PATH)
    return [unmask_url(line.strip(), stored) for line in lines if line and line.strip()]
