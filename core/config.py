from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re
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
    url: str = ""
    model: str = ""
    timeout: int = 120
    cache_ttl_hours: int = 24
    system_prompt: str = ""         # empty = use built-in default
    update_check_hours: int = 24    # 0 = disable update checks


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


def load_config(path: str = "config.yaml") -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
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
        ollama=OllamaConfig(
            url=o_raw.get("url", "").rstrip("/"),
            model=o_raw.get("model", ""),
            timeout=o_raw.get("timeout", 120),
            cache_ttl_hours=o_raw.get("cache_ttl_hours", 24),
            system_prompt=o_raw.get("system_prompt", ""),
            update_check_hours=int(o_raw.get("update_check_hours", 24)),
        ),
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
