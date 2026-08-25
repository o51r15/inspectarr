from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re
import copy
import os
import threading
import yaml


# ---------------------------------------------------------------------------
# Dataclasses — one per config section
# ---------------------------------------------------------------------------

@dataclass
class QBittorrentConfig:
    url: str
    username: str
    password: str


@dataclass
class TransmissionConfig:
    url: str                   # e.g. http://localhost:9091
    username: str = ""
    password: str = ""


@dataclass
class DelugeConfig:
    url: str                   # e.g. http://localhost:8112
    password: str = ""


@dataclass
class ArrConfig:
    enabled: bool
    url: str
    api_key: str


@dataclass
class ArrsConfig:
    sonarr: ArrConfig
    radarr: ArrConfig
    lidarr: ArrConfig


@dataclass
class RuleConditions:
    bad_extensions: list[str]
    match_mode: str = "any"   # "any" | "primary"
    min_file_size_mb: Optional[int] = None
    bad_filename_patterns: list[str] = field(default_factory=list)


@dataclass
class Rule:
    name: str
    category: str
    app: str                  # "sonarr" | "radarr" (future)
    conditions: RuleConditions


@dataclass
class RetryConfig:
    enabled: bool = True
    max_attempts: int = 10
    interval_seconds: int = 600


@dataclass
class LoggingConfig:
    log_file: str = "./data/inspectarr.log.json"
    retention_days: int = 30
    level: str = "INFO"


@dataclass
class StateConfig:
    db_file: str = "./data/inspectarr.db"


@dataclass
class AppriseConfig:
    enabled: bool = False
    urls: list[str] = field(default_factory=list)
    notify_on: list[str] = field(default_factory=lambda: ["action", "error"])


@dataclass
class DigestConfig:
    enabled: bool = False
    use_ollama: bool = False


@dataclass
class SummaryConfig:
    enabled: bool = False
    schedule: str = "daily"        # "daily" | "weekly"
    use_ollama: bool = True


@dataclass
class NotificationsConfig:
    apprise: AppriseConfig = field(default_factory=AppriseConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)


@dataclass
class AuthConfig:
    enabled: bool = False
    username: str = "admin"
    password: str = "changeme"


@dataclass
class OllamaConfig:
    # Master switch for every AI feature. Ships off: AI is optional, and a
    # fresh install should not reach out to a model nobody configured.
    enabled: bool = False   # master switch
    url: str = ""
    model: str = ""
    timeout: int = 120
    cache_ttl_hours: int = 24
    system_prompt: str = ""         # empty = use built-in default
    update_check_hours: int = 24

    # Ask the Ollama REGISTRY whether a newer build of the configured model
    # is published (ROADMAP item 15).
    #
    # Separate from update_check_hours, which governs the LOCAL check --
    # "has the model under this name been replaced since we validated it".
    # That one is a LAN request and always runs. This one leaves the network
    # the machine is on, so it is opt-out rather than assumed: some installs
    # are deliberately air-gapped, and a self-hosted tool should not phone
    # anywhere the user did not agree to.
    #
    # It never pulls. One unauthenticated GET of a manifest, hashed locally.
    auto_update_check: bool = True

    # The context window to budget the scoring prompt against, and the value
    # sent to Ollama as num_ctx (ROADMAP item 19).
    #
    # 4096 by default because that is Ollama's own default for most models.
    # Budgeting for more than Ollama actually applies is worse than useless:
    # it silently truncates at its default and the model loses the scoring
    # instructions, then answers with confident, invented numbers rather
    # than an error.
    #
    # Raise it if the model genuinely supports more -- 8192 or 16384 are
    # common -- and the prompt will be sent in one call instead of several.
    context_window: int = 4096

    # Hard cap on indexers per scoring call, independent of the token budget.
    #
    # Measured: qwen2.5-coder:7b scores 25 correctly, echoes the input at 30,
    # and at 37 (in a window twice as large) returns well-formed JSON that
    # silently omits five. The last is the dangerous one -- it looks like
    # success. So how many items the model will reason about is a separate
    # limit from how many fit, and much lower.
    #
    # A property of the model. Raise it if yours copes; the AI settings
    # page's validation run is how to find out.
    max_indexers_per_call: int = 25    # 0 = disable update checks

    def is_active(self) -> bool:
        """
        True when AI can actually run: switched on AND configured.

        Every consumer asks this rather than testing `url` directly, so the
        master switch is honoured explicitly instead of being implied by a
        blanked-out value.
        """
        return bool(self.enabled and self.url and self.model)


@dataclass
class PollingConfig:
    enabled: bool = True
    interval_seconds: int = 300


@dataclass
class WebhookConfig:
    enabled: bool = False
    secret: str = ""
    scan_delay_seconds: int = 60


@dataclass
class ScanningConfig:
    polling: PollingConfig = field(default_factory=PollingConfig)
    webhooks: WebhookConfig = field(default_factory=WebhookConfig)


@dataclass
class AutoManageConfig:
    enabled: bool = False
    disable_threshold: float = 30.0
    consecutive_runs: int = 3
    cooldown_hours: int = 24


@dataclass
class ProwlarrScoringConfig:
    response_time_weight: float = 0.25
    failure_rate_weight: float  = 0.30
    malicious_weight: float     = 0.20
    grab_success_weight: float  = 0.25
    backoff_penalty: float      = 20.0
    malicious_penalty_per_hit: float = 10.0
    # Failure type multipliers (weighted failure rate)
    auth_failure_mult: float    = 3.0
    grab_failure_mult: float    = 2.0
    query_failure_mult: float   = 1.0
    rss_failure_mult: float     = 0.5


@dataclass
class ProwlarrConfig:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    base_priority: int = 10
    reorder_interval_hours: int = 24
    min_grabs_before_scoring: int = 10
    history_window_days: int = 90
    scoring: ProwlarrScoringConfig = field(default_factory=ProwlarrScoringConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    auto_manage: AutoManageConfig = field(default_factory=AutoManageConfig)


@dataclass
class WebConfig:
    port: int = 8585
    scheduler_autostart: bool = False
    auth: AuthConfig = field(default_factory=AuthConfig)


@dataclass
class RemediationConfig:
    """
    Severity gating for automatic remediation (ROADMAP item 24).

    min_severity defaults to LOW, i.e. act on everything -- byte-identical to
    behaviour before severity existed. Raising it to HIGH stops auto-deleting
    matches whose worst finding is only an archive or a filename heuristic,
    while still acting on anything that finds an executable.

    severity_overrides maps an extension (".rar") or a signal name
    ("bad_filename_pattern") to one of LOW/MEDIUM/HIGH/CRITICAL. Extension
    keys win over signal keys.
    """
    min_severity: str = "LOW"
    severity_overrides: dict = field(default_factory=dict)

    # Two thresholds, forming three bands:
    #   below min_severity          -> record only, no action
    #   >= min_severity, < remediate_at -> QUARANTINE (pause and hold)
    #   >= remediate_at             -> remediate (blocklist + delete)
    #
    # remediate_at defaults to LOW, which collapses the quarantine band to
    # nothing and reproduces the behaviour from before quarantine existed.
    # Raising it to HIGH holds low- and medium-severity catches for review
    # while still deleting anything that finds an executable.
    remediate_at: str = "LOW"

    # How a hold ends on its own. None/0 = hold indefinitely (the default):
    # a torrent waits for a human rather than being deleted by a timer.
    quarantine_timeout_minutes: int = 0
    # What happens when a timeout does elapse: "release" or "remediate".
    # Defaults to release -- a timer expiring is not evidence of guilt.
    quarantine_timeout_action: str = "release"

    # Operating mode (ROADMAP item 26): a CEILING on how far the system may
    # act, applied AFTER the thresholds above have chosen a band.
    #
    #   monitor     record findings; never pause, never delete
    #   quarantine  hold instead of deleting; nothing is removed automatically
    #   automatic   apply the thresholds as configured  (default)
    #
    # It lives here, next to the thresholds it caps, rather than at the top
    # level as originally sketched -- one place to look, and the UI shows it
    # alongside the settings it constrains.
    #
    # It does NOT rewrite min_severity or remediate_at. A preset that edited
    # them would leave the file describing something the user never chose;
    # a ceiling leaves both readings true and lets the UI explain the
    # difference ("thresholds say DELETE, mode caps at record").
    #
    # Defaults to automatic so behaviour is unchanged for every existing
    # install: the mode can only ever reduce what happens, so any other
    # default would silently stop remediating on upgrade.
    operating_mode: str = "automatic"

    # Replacement outcome tracking (ROADMAP item 27).
    #
    # After a rejection, watch the arr to see whether a replacement arrives,
    # from which indexer, and whether it survives inspection. Purely
    # observational -- it never causes or prevents an action.
    #
    # Defaults ON because the resulting statistic is the point: "this
    # indexer's bad releases are replaced cleanly 80% of the time" is a very
    # different judgement from "this indexer's bad releases are never
    # replaced at all", and neither is visible without it.
    #
    # Cost is bounded rather than free: watches are polled with an
    # exponential backoff, capped per sweep, and abandoned after the window
    # below. Set false to make no extra arr calls at all.
    track_replacements: bool = True

    # How long to keep asking before giving up and recording the rejection
    # as never replaced. 72h covers a weekend, which is roughly how long an
    # arr may keep searching before anything else becomes available.
    replacement_window_hours: int = 72


@dataclass
class AppConfig:
    qbittorrent: QBittorrentConfig
    on_arr_failure: str          # "delete" | "abort"
    retry: RetryConfig
    logging: LoggingConfig
    state: StateConfig
    notifications: NotificationsConfig
    torrent_client: str = "qbittorrent"   # "qbittorrent" | "transmission" | "deluge"
    transmission: TransmissionConfig | None = None
    deluge: DelugeConfig | None = None
    arrs: ArrsConfig = field(default_factory=lambda: ArrsConfig(
        sonarr=ArrConfig(False, "", ""),
        radarr=ArrConfig(False, "", ""),
        lidarr=ArrConfig(False, "", ""),
    ))
    rules: list[Rule] = field(default_factory=list)
    web: WebConfig = field(default_factory=WebConfig)
    prowlarr: ProwlarrConfig = field(default_factory=ProwlarrConfig)
    scanning: ScanningConfig = field(default_factory=ScanningConfig)
    remediation: RemediationConfig = field(default_factory=RemediationConfig)
    poll_interval_seconds: int = 300  # backward compat — overridden by scanning.polling
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _as_int(value, default: int) -> int:
    """
    int() that never raises.

    _parse_config runs BEFORE _validate, so a bare int() on a user-supplied
    value surfaces as an unhandled ValueError from deep in the parser rather
    than as the friendly, field-named message _validate is there to produce.
    Parsing stays permissive; _validate stays the place that says no.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value, default=None):
    """
    Coerce a YAML value to a list.

    dict.get(key, default) only returns `default` when the key is ABSENT. A
    key present but empty ("urls:" with nothing under it) yields None, and
    every list comprehension downstream then raises TypeError. That is not a
    hypothetical: config.example.yaml ships with `urls:` empty, so copying
    the reference config to config.yaml crashed on startup.

    Accepts a bare string as a single-element list, which is the other shape
    users naturally write.
    """
    if value is None:
        return list(default) if default else []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_ollama(o_raw: dict) -> "OllamaConfig":
    """
    Build the Ollama config, honouring the master switch.

    The URL is kept exactly as configured whether or not AI is enabled --
    "disabled" and "not configured" are different states and conflating them
    makes the config lie about itself. Callers ask is_active() instead.

    `enabled` defaults to whether a URL is configured, NOT to False. A fresh
    install has no URL and is therefore off, which is the intended shipping
    default -- but an existing install that was working before this key
    existed keeps working instead of silently losing AI on upgrade. Writing
    `enabled: false` explicitly always wins.
    """
    url = (o_raw.get("url") or "").rstrip("/")
    enabled = bool(o_raw.get("enabled", bool(url)))
    return OllamaConfig(
        enabled=enabled,
        url=url,
        model=o_raw.get("model", ""),
        timeout=o_raw.get("timeout", 120),
        cache_ttl_hours=o_raw.get("cache_ttl_hours", 24),
        system_prompt=o_raw.get("system_prompt", ""),
        update_check_hours=int(o_raw.get("update_check_hours", 24)),
        auto_update_check=bool(
            True if o_raw.get("auto_update_check") is None
            else o_raw.get("auto_update_check")),
        context_window=_as_int(o_raw.get("context_window"), 4096),
        max_indexers_per_call=_as_int(
            o_raw.get("max_indexers_per_call"), 25),
    )


# ---------------------------------------------------------------------------
# Config parse cache (ROADMAP item 14)
# ---------------------------------------------------------------------------
#
# Measured on the real config before writing any of this:
#
#   load_config()                     9.61 ms
#     yaml.safe_load                  9.56 ms   99.5%
#     building the dataclasses        0.12 ms    1.2%
#   os.stat()                         0.002 ms  (4570x cheaper than a load)
#
#   Per page render: 4 parses, ~38 ms -- 52% of the total time for the
#   dashboard and 70% for the quarantine page. All four parse the same
#   unchanged file.
#
# So the thing worth caching is the PARSE, and only the parse.
#
# Why not cache the AppConfig itself
#   Callers mutate what they get back -- inspectarr.py sets cfg.dry_run,
#   tests set cfg.prowlarr.enabled. Handing every caller the same instance
#   would let one request's mutation leak into the next, which is a
#   correctness bug traded for 1.2% more speed. Rebuilding the dataclasses
#   each call costs 0.12 ms and keeps every caller's object private.
#
# Why stat validation rather than explicit invalidation alone
#   config.yaml is a file a homelab user edits by hand. A cache that only
#   noticed the app's own saves would silently ignore those edits until
#   restart -- turning a documented behaviour ("settings take effect on the
#   next scan, no restart needed") into a confusing bug. st_mtime_ns plus
#   size plus inode catches every writer, and costs 0.002 ms to check.
#
#   Explicit invalidation on save is kept as well, so an in-app save is
#   correct even on a filesystem with coarse timestamp resolution.

_CONFIG_CACHE: dict = {}
_CONFIG_CACHE_LOCK = threading.Lock()


def _config_signature(path: str):
    """Cheap fingerprint of the file. None if it cannot be stat'd."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def invalidate_config_cache(path: str = None) -> None:
    """
    Drop cached parses. Called after the app writes config.yaml.

    Belt and braces alongside stat validation: an in-app save must be visible
    immediately even if two writes land inside one filesystem timestamp tick.
    """
    with _CONFIG_CACHE_LOCK:
        if path is None:
            _CONFIG_CACHE.clear()
        else:
            _CONFIG_CACHE.pop(os.path.abspath(path), None)


def config_cache_stats() -> dict:
    """
    Hit/miss/reload counters for the parse cache.

    Deliberately not called by application code -- it exists so a test can
    assert the cache is actually caching (a cache that silently stopped
    working would otherwise look identical to one that works), and so the
    numbers are reachable from a REPL when diagnosing. Left in place rather
    than deleted for that reason; it is not abandoned code.
    """
    with _CONFIG_CACHE_LOCK:
        return dict(_CACHE_COUNTERS)


_CACHE_COUNTERS = {"hits": 0, "misses": 0, "reloads": 0}


def load_raw_config(path: str = "config.yaml") -> dict:
    """
    The parsed YAML for `path`, from cache when the file has not changed.

    Returns a deep copy: the cached dict is shared, and a caller that mutated
    it -- as the Settings save path legitimately does -- would poison every
    later reader. Copying costs 0.064 ms against 9.56 ms to reparse, so
    safety here is 149x cheaper than the alternative.
    """
    abspath = os.path.abspath(path)
    sig = _config_signature(abspath)

    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(abspath)
        if cached is not None and sig is not None and cached[0] == sig:
            _CACHE_COUNTERS["hits"] += 1
            return copy.deepcopy(cached[1])
        if cached is not None:
            _CACHE_COUNTERS["reloads"] += 1
        else:
            _CACHE_COUNTERS["misses"] += 1

        # Parsed inside the lock on purpose. A cold cache with several
        # threads arriving at once would otherwise have all of them parse
        # the same file simultaneously -- the stampede this exists to avoid.
        with open(abspath, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if raw is None:
            raw = {}
        # Re-stat after reading: if the file changed while we were parsing,
        # store the signature of what we actually read, not what we saw
        # before. Storing the earlier one would cache a parse under the
        # wrong fingerprint and serve it until the next write.
        _CONFIG_CACHE[abspath] = (_config_signature(abspath) or sig, raw)
        return copy.deepcopy(raw)


def load_config(path: str = "config.yaml") -> AppConfig:
    """
    Load and validate the config.

    The YAML parse is cached and revalidated by file signature, but the
    AppConfig itself is rebuilt every call (0.12 ms). Callers mutate what
    they get back -- inspectarr.py sets dry_run, tests flip feature switches
    -- so handing out a shared instance would let one caller's change leak
    into everyone else's.
    """
    raw = load_raw_config(path)
    return _parse_config(raw)


def _parse_config(raw: dict) -> AppConfig:
    # qBittorrent
    qb = raw["qbittorrent"]
    qbit_cfg = QBittorrentConfig(
        url=qb["url"].rstrip("/"),
        username=qb["username"],
        password=qb["password"],
    )

    # *arr clients
    arrs_raw = raw.get("arrs", {})

    def _arr(key: str) -> ArrConfig:
        a = arrs_raw.get(key, {})
        return ArrConfig(
            enabled=a.get("enabled", False),
            url=a.get("url", "").rstrip("/"),
            api_key=a.get("api_key", ""),
        )

    arrs_cfg = ArrsConfig(sonarr=_arr("sonarr"), radarr=_arr("radarr"), lidarr=_arr("lidarr"))

    # Rules
    rules: list[Rule] = []
    for r in _as_list(raw.get("rules")):
        c = r.get("conditions", {})
        exts = [str(e).lower() for e in _as_list(c.get("bad_extensions"))]
        rules.append(Rule(
            name=r["name"],
            category=r["category"],
            app=r["app"].lower(),
            conditions=RuleConditions(
                bad_extensions=exts,
                match_mode=c.get("match_mode", "any"),
                min_file_size_mb=c.get("min_file_size_mb", None),
                bad_filename_patterns=_as_list(c.get("bad_filename_patterns")),
            ),
        ))


    # Retry
    ret_raw = raw.get("retry", {})
    retry_cfg = RetryConfig(
        enabled=ret_raw.get("enabled", True),
        max_attempts=ret_raw.get("max_attempts", 10),
        interval_seconds=ret_raw.get("interval_seconds", 600),
    )

    # Logging
    log_raw = raw.get("logging", {})
    logging_cfg = LoggingConfig(
        log_file=log_raw.get("log_file", "./data/inspectarr.log.json"),
        retention_days=log_raw.get("retention_days", 30),
        level=log_raw.get("level", "INFO"),
    )

    # State
    state_raw = raw.get("state", {})
    state_cfg = StateConfig(db_file=state_raw.get("db_file", "./data/inspectarr.db"))

    # Notifications
    notif_raw = raw.get("notifications", {})
    app_raw = notif_raw.get("apprise", {})
    dig_raw = notif_raw.get("digest", {})
    sum_raw = notif_raw.get("summary", {})
    # Normalize urls: accept string (single URL) or list
    apprise_urls = _as_list(app_raw.get("urls"))
    notif_cfg = NotificationsConfig(
        apprise=AppriseConfig(
            enabled=app_raw.get("enabled", False),
            urls=[u for u in apprise_urls if u and u.strip()],
            notify_on=_as_list(app_raw.get("notify_on"),
                               ["action", "error"]),
        ),
        digest=DigestConfig(
            enabled=dig_raw.get("enabled", False),
            use_ollama=dig_raw.get("use_ollama", False),
        ),
        summary=SummaryConfig(
            enabled=sum_raw.get("enabled", False),
            schedule=sum_raw.get("schedule", "daily"),
            use_ollama=sum_raw.get("use_ollama", True),
        ),
    )

    # Torrent client type
    torrent_client = raw.get("torrent_client", "qbittorrent").lower()

    # Transmission config
    tr_raw = raw.get("transmission", {})
    transmission_cfg = None
    if tr_raw:
        transmission_cfg = TransmissionConfig(
            url=tr_raw.get("url", "").rstrip("/"),
            username=tr_raw.get("username", ""),
            password=tr_raw.get("password", ""),
        )

    # Deluge config
    dl_raw = raw.get("deluge", {})
    deluge_cfg = None
    if dl_raw:
        deluge_cfg = DelugeConfig(
            url=dl_raw.get("url", "").rstrip("/"),
            password=dl_raw.get("password", ""),
        )

    _validate(raw, rules, arrs_cfg)

    p_raw = raw.get("prowlarr", {})
    s_raw = p_raw.get("scoring", {})
    o_raw = p_raw.get("ollama", {})
    am_raw = p_raw.get("auto_manage", {})
    prowlarr_cfg = ProwlarrConfig(
        enabled=p_raw.get("enabled", False),
        url=p_raw.get("url", "").rstrip("/"),
        api_key=p_raw.get("api_key", ""),
        base_priority=p_raw.get("base_priority", 10),
        reorder_interval_hours=p_raw.get("reorder_interval_hours", 24),
        min_grabs_before_scoring=p_raw.get("min_grabs_before_scoring", 10),
        history_window_days=p_raw.get("history_window_days", 90),
        scoring=ProwlarrScoringConfig(
            response_time_weight=s_raw.get("response_time_weight", 0.25),
            failure_rate_weight=s_raw.get("failure_rate_weight", 0.30),
            malicious_weight=s_raw.get("malicious_weight", 0.20),
            grab_success_weight=s_raw.get("grab_success_weight", 0.25),
            backoff_penalty=s_raw.get("backoff_penalty", 20.0),
            malicious_penalty_per_hit=s_raw.get("malicious_penalty_per_hit", 10.0),
            auth_failure_mult=float(s_raw.get("auth_failure_mult", 3.0)),
            grab_failure_mult=float(s_raw.get("grab_failure_mult", 2.0)),
            query_failure_mult=float(s_raw.get("query_failure_mult", 1.0)),
            rss_failure_mult=float(s_raw.get("rss_failure_mult", 0.5)),
        ),
        ollama=_parse_ollama(o_raw),
        auto_manage=AutoManageConfig(
            enabled=am_raw.get("enabled", False),
            disable_threshold=float(am_raw.get("disable_threshold", 30.0)),
            consecutive_runs=int(am_raw.get("consecutive_runs", 3)),
            cooldown_hours=int(am_raw.get("cooldown_hours", 24)),
        ),
    )

    # Scanning config — backward compat: if no scanning block, use top-level poll_interval_seconds
    sc_raw = raw.get("scanning", {})
    pol_raw = sc_raw.get("polling", {})
    wh_raw = sc_raw.get("webhooks", {})
    legacy_interval = raw.get("poll_interval_seconds", 300)
    scanning_cfg = ScanningConfig(
        polling=PollingConfig(
            enabled=pol_raw.get("enabled", True),
            interval_seconds=pol_raw.get("interval_seconds", legacy_interval),
        ),
        webhooks=WebhookConfig(
            enabled=wh_raw.get("enabled", False),
            secret=wh_raw.get("secret", ""),
            scan_delay_seconds=int(wh_raw.get("scan_delay_seconds", 60)),
        ),
    )

    rem_raw = raw.get("remediation", {}) or {}
    remediation_cfg = RemediationConfig(
        min_severity=str(rem_raw.get("min_severity", "LOW")).upper(),
        # Normalise keys once here so lookups never have to care about case
        # or a missing leading dot on an extension.
        severity_overrides={
            (k if not k.startswith(".") else k.lower()): str(v).upper()
            for k, v in (rem_raw.get("severity_overrides", {}) or {}).items()
        },
        remediate_at=str(rem_raw.get("remediate_at", "LOW")).upper(),
        quarantine_timeout_minutes=_as_int(
            rem_raw.get("quarantine_timeout_minutes"), 0),
        quarantine_timeout_action=str(
            rem_raw.get("quarantine_timeout_action", "release")).lower(),
        # Not defaulted through .get() alone: an explicitly empty
        # `operating_mode:` in YAML parses as None, and `or` catches that
        # where a bare default would not. Same class of bug as B-07.
        operating_mode=str(
            rem_raw.get("operating_mode") or "automatic").lower().strip(),
        track_replacements=bool(
            True if rem_raw.get("track_replacements") is None
            else rem_raw.get("track_replacements")),
        replacement_window_hours=_as_int(
            rem_raw.get("replacement_window_hours"), 72),
    )

    return AppConfig(
        remediation=remediation_cfg,
        qbittorrent=qbit_cfg,
        torrent_client=torrent_client,
        transmission=transmission_cfg,
        deluge=deluge_cfg,
        arrs=arrs_cfg,
        rules=rules,
        on_arr_failure=raw.get("on_arr_failure", "delete"),
        retry=retry_cfg,
        logging=logging_cfg,
        state=state_cfg,
        notifications=notif_cfg,
        scanning=scanning_cfg,
        web=WebConfig(
            port=raw.get("web", {}).get("port", 8585),
            scheduler_autostart=raw.get("web", {}).get("scheduler_autostart", False),
            auth=AuthConfig(
                enabled=raw.get("web", {}).get("auth", {}).get("enabled", False),
                username=raw.get("web", {}).get("auth", {}).get("username", "admin"),
                password=raw.get("web", {}).get("auth", {}).get("password", "changeme"),
            ),
        ),
        poll_interval_seconds=raw.get("poll_interval_seconds", 300),
        dry_run=raw.get("dry_run", False),
        prowlarr=prowlarr_cfg,
    )


def _validate(raw: dict, rules: list[Rule], arrs: ArrsConfig) -> None:
    errors: list[str] = []

    tc = raw.get("torrent_client", "qbittorrent").lower()
    if tc not in ("qbittorrent", "transmission", "deluge"):
        errors.append(f"torrent_client must be 'qbittorrent', 'transmission', or 'deluge' (got '{tc}')")

    if tc == "qbittorrent" and not raw.get("qbittorrent", {}).get("url"):
        errors.append("qbittorrent.url is required when torrent_client is 'qbittorrent'")
    if tc == "transmission" and not raw.get("transmission", {}).get("url"):
        errors.append("transmission.url is required when torrent_client is 'transmission'")
    if tc == "deluge" and not raw.get("deluge", {}).get("url"):
        errors.append("deluge.url is required when torrent_client is 'deluge'")

    on_fail = raw.get("on_arr_failure", "delete")
    if on_fail not in ("delete", "abort"):
        errors.append("on_arr_failure must be 'delete' or 'abort'")

    # A mistyped operating mode must be reported, never absorbed. Falling
    # back silently would mean a typo in the one setting whose whole job is
    # to STOP the system acting resolves to full automatic deletion -- the
    # most destructive possible reading of a typo.
    from core import severity as _sev
    _rem = raw.get("remediation", {}) or {}

    # An omitted or explicitly-empty key means "use the default" and is fine.
    # Only a value the user actually wrote, and got wrong, is an error.
    _mode = _rem.get("operating_mode")
    if _mode is not None and str(_mode).strip():
        if not _sev.is_valid_mode(str(_mode)):
            errors.append(
                f"remediation.operating_mode must be one of "
                f"{', '.join(_sev.MODES)} (got '{_mode}')")

    for _field in ("min_severity", "remediate_at"):
        _level = _rem.get(_field)
        if _level is not None and str(_level).strip():
            if not _sev.is_valid(str(_level)):
                errors.append(
                    f"remediation.{_field} must be one of "
                    f"{', '.join(_sev.ORDER)} (got '{_level}')")

    _tta = _rem.get("quarantine_timeout_action")
    if _tta is not None and str(_tta).strip():
        if str(_tta).lower() not in ("release", "remediate"):
            errors.append(
                f"remediation.quarantine_timeout_action must be "
                f"'release' or 'remediate' (got '{_tta}')")

    _rwh = _rem.get("replacement_window_hours")
    if _rwh is not None and str(_rwh).strip():
        try:
            if int(_rwh) <= 0:
                errors.append(
                    f"remediation.replacement_window_hours must be positive "
                    f"(got {_rwh}); set track_replacements: false to disable "
                    f"tracking instead")
        except (TypeError, ValueError):
            errors.append(
                f"remediation.replacement_window_hours must be a whole number "
                f"of hours (got '{_rwh}')")

    _qtm = _rem.get("quarantine_timeout_minutes")
    if _qtm is not None and str(_qtm).strip():
        try:
            if int(_qtm) < 0:
                errors.append(
                    f"remediation.quarantine_timeout_minutes cannot be "
                    f"negative (got {_qtm})")
        except (TypeError, ValueError):
            errors.append(
                f"remediation.quarantine_timeout_minutes must be a whole "
                f"number of minutes (got '{_qtm}')")

    for rule in rules:
        if rule.app == "sonarr" and not arrs.sonarr.enabled:
            errors.append(f"Rule '{rule.name}': app=sonarr but sonarr is not enabled")
        if rule.app == "radarr" and not arrs.radarr.enabled:
            errors.append(f"Rule '{rule.name}': app=radarr but radarr is not enabled")
        if rule.app == "lidarr" and not arrs.lidarr.enabled:
            errors.append(f"Rule '{rule.name}': app=lidarr but lidarr is not enabled")
        if rule.app not in ("sonarr", "radarr", "lidarr"):
            errors.append(f"Rule '{rule.name}': unknown app '{rule.app}' (must be sonarr, radarr, or lidarr)")
        if rule.conditions.match_mode not in ("any", "primary"):
            errors.append(f"Rule '{rule.name}': match_mode must be 'any' or 'primary'")
        has_conditions = (
            bool(rule.conditions.bad_extensions)
            or bool(rule.conditions.bad_filename_patterns)
            or rule.conditions.min_file_size_mb is not None
        )
        if not has_conditions:
            errors.append(f"Rule '{rule.name}': at least one condition must be set "
                          "(bad_extensions, bad_filename_patterns, or min_file_size_mb)")
        for pattern in rule.conditions.bad_filename_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"Rule '{rule.name}': invalid regex pattern '{pattern}': {exc}")

    if errors:
        raise ValueError(
            "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
