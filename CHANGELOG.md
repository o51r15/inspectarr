# Changelog

All notable changes to Inspectarr are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Unreleased]

Work on `main` since v1.6.0, not yet tagged. Two themes: the **safety and
inspection foundation** (ROADMAP Cluster 7) and **AI settings + model
validation** (ROADMAP Cluster 8).

### Added — Safety & Inspection Foundation

- **Structured inspection records** — every flagged release now writes a durable `inspections` row plus one `inspection_reasons` row per finding (signal, detail, file path, file size, severity). Deliberately one row per *flagged* release rather than per torrent per scan; the literal reading would write thousands of "still clean" rows a day at a 15-minute poll.
- **Structured findings** — `evaluate_rule()` returns `list[Finding]` (signal, detail, file_path, file_size) instead of a flat list of filenames, so the reason a file was flagged travels with the file.
- **Correlation IDs** — a `scan_id` per run and an `inspection_id` per flagged release thread through the arr call, the torrent-client call, and every structured log event, so one release can be followed end to end.
- **Severity engine** (`core/severity.py`) — assigns a severity per *finding* (executables CRITICAL, archives HIGH, undersized primary file HIGH, filename pattern MEDIUM) and aggregates with **MAX**, never an average, so a pile of minor findings cannot dilute one dangerous one. Per-extension overrides via `remediation.severity_overrides`.
- **Quarantine mode** — a third outcome between "record it" and "delete it". Three bands: below `min_severity` is recorded only; between `min_severity` and `remediate_at` is **quarantined** (paused and held for review); at or above `remediate_at` is blocklisted and deleted. Both thresholds default to `LOW`, which collapses the quarantine band to nothing and reproduces pre-quarantine behaviour exactly.
- **Quarantine review page** (`/quarantine`) with three distinct outcomes — Release (resume, false positive), Keep paused (stop asking), Delete (blocklist + remove). Client presence is verified before acting, because qBittorrent answers `200 OK` to a resume for a hash it has never heard of.
- **Quarantine timeout sweep** — expired holds are resolved before each scan, in both the web scheduler and the `--daemon` CLI path. Only entries with an `expires_at` are considered; the default is hold indefinitely. The timeout action defaults to `release`, because an expiring clock means nobody looked, not that the release was malicious.
- **Remediation & Severity settings** under **Settings → Rules** — both thresholds, the quarantine timeout, and the timeout action, with a live explainer that spells out the resulting bands ("Recorded only: LOW | Quarantined: MEDIUM, HIGH | Deleted: CRITICAL"), including the clamped reading when the delete threshold is set below the act threshold.
- **`quarantine` notification event** — falls back to the `action` event when `quarantine` is absent from `notify_on`, so existing configs are not silent about held torrents.
- **`GET /api/health`** liveness endpoint. The fast path makes no outbound calls; `?deps=1` opts in to dependency checks and requires auth.

### Added — AI Settings & Model Validation

- **Dedicated Settings → AI pane** — AI is optional, so it got its own pane. Exposes `ollama.url`, `timeout`, `cache_ttl_hours`, and `update_check_hours`; four of six keys previously had no UI at all, `url` among them, so AI scoring could not be enabled without hand-editing `config.yaml`.
- **Master AI enable/disable switch**, shipping **off** by default. While off, indexer AI scoring, AI notification digests, and periodic summaries are all skipped and no Ollama request is made. Existing installs are not broken: `enabled` defaults to whether a URL is configured, so an install that worked before this key existed keeps working. An explicit `enabled: false` always wins.
- **Model validation engine** (`core/model_validator.py`) — three tests against the real scoring path: **discrimination** (good vs bad indexer, requires a ≥25-point spread, catches constant output and inverted scales), **schema compliance** (catches the echo bug and malformed scores), and **context capacity** at the real indexer count.
- **Validation UI with a hard gate** — `/config/ai/model` returns 409 unless the model passed; `force=true` requires an explicit "Apply anyway" confirmation and is recorded as `forced`, distinct from `failed`. Runs in a background thread with polling, because a multi-minute validation in-request would hold a waitress worker.
- **Model comparison table** listing every validated model with its test results and average response time.

### Changed

- **LLM score cache key now includes model and system prompt.** It hashed indexer stats only, so swapping models served the previous model's scores under the new model's name for a full TTL. `llm_cache` gained a `model` column.
- **`OllamaConfig.is_active()`** is now the single question every consumer asks ("enabled AND url AND model"). The URL on disk is never blanked to represent "off"; disabling and re-enabling round-trips the configured value untouched.
- **System → Status** distinguishes "Ollama (disabled)" from "not configured".
- **New config keys:** `prowlarr.ollama.enabled`, `remediation.min_severity`, `remediation.remediate_at`, `remediation.severity_overrides`, `remediation.quarantine_timeout_minutes`, `remediation.quarantine_timeout_action`.
- **New tables:** `inspections`, `inspection_reasons`, `quarantine`, `validated_models`.

### Fixed

- **Ollama update check was pulling models.** `/config/ollama/update-check` called `/api/pull` with `stream=false`; Ollama has no dry-run, so it genuinely pulled on **every Settings page load**, starting a multi-GB download that was abandoned at the 30s timeout. The verdict was inverted too. Replaced with a side-effect-free digest comparison.
- **`config.example.yaml` could not be loaded.** `dict.get(k, default)` returns `None` when the key exists but is empty, and the example ships `urls:` empty — so copying the reference config crashed on startup with a `TypeError`. Same latent bug on `rules`, `bad_extensions`, `bad_filename_patterns`, and `notify_on`. Added `_as_list()`; a rule left with no conditions now fails validation with a message naming the rule.
- **Model digests were never real.** `/api/show` has no `digest` key at all, so every stored digest was a fallback guess. Digests are now read from `/api/tags`, and a migration NULLs any stored value that is not 64 hex characters — without it the corrected digest would report "changed since validation" for every model forever.
- **Remediation settings had no UI.** Items 24 and 25 added five config keys and exposed none of them, and the quarantine empty state linked to Settings → AI, a page that never contained them. Quarantine is not an AI feature: severity is entirely deterministic and Ollama is never consulted on that path.
- **Validation UI could not rejoin a run already in progress.** Nothing checked for an in-flight run on page load, and a 409 was treated as a plain error — so the one message telling you a run existed was also the one path that refused to show it.
- **AI pane load hooks** were still bound to the old pane after the controls moved, so the system prompt showed "Loading…" forever and the model list never populated.
- **Torrent-client delete failures wrote nothing** to the structured log, though the arr path did. Now emits `client_delete_failed`.
- **Docker HEALTHCHECK pointed at `/`**, which is behind basic auth — any auth-enabled deploy would have been restart-looped.
- **`:dev` tag race** between `release.yml` and `devcontainer.yml`. The dev container now publishes `:devcontainer`.
- **No way to clear a stored validation record** — added `StateManager.delete_validation()`, `POST /config/ai/validation/delete`, and a per-row Clear action.
- **Validation state was process-local**, so a restart orphaned the UI poll. An in-flight marker now lives in `app_state` and the status endpoint reports an interrupted run once, then self-clears.
- **`update_check_hours` was exposed but unused.** The interval is now honoured and the last verdict stamped in `app_state`.
- **AI scoring parser** accepts alternate key names (`score` for `health_score`, `id` for `indexer_id`) for model compatibility, with parsed-item and raw-response logging on parse failure.

---

## [v1.6.0] — 2026-08-16

### Added — UI Navigation Reorganization
- **Tabbed Indexers hub** — the Indexers page now has three tabs: **Health** (the existing indexer health table), **Stats** (moved from standalone sidebar entry), and **AI Scoring** (moved from System → LLM Logs). All indexer-related views are consolidated in one place.
- **Stats tab** — the former standalone Stats page is now embedded as the second tab under Indexers. The `/stats` route returns a 301 redirect to `/indexers/stats` for backward compatibility.
- **AI Scoring tab** — the former LLM Logs page (System → LLM Logs) is now the third tab under Indexers. The `/system/llm-logs` route returns a 301 redirect to `/indexers/ai-scoring`. The `/api/llm-logs` API endpoint is preserved unchanged.
- **Tab bar CSS** — new `.tab-bar` and `.tab-btn` styles with active/hover states matching the existing dark theme.

### Changed — Sidebar Navigation
- **Sidebar "Stats" entry removed** — consolidated into the Indexers hub tabs.
- **Sidebar "LLM Logs" entry removed** from the System group — consolidated into the Indexers hub tabs.
- **"Backups" moved to System group** — was previously under Settings, now lives under System alongside Tasks, Events, and Updates where it logically belongs.
- **"Events" renamed to "Logs"** — the sidebar label now reads "Logs" (route unchanged at `/events`), aligning with common terminology.
- **"Indexers" renamed to "Prowlarr"** under Settings — the Settings sub-nav item that links to Prowlarr connection config is now labeled "Prowlarr" to distinguish it from the top-level Indexers hub.

### Added — Infrastructure & Hardening
- **Transmission & Deluge Settings UI** — torrent client selector dropdown in Connections pane, with URL/username/password fields for Transmission and URL/password for Deluge, matching the existing qBittorrent layout. Includes test connection buttons wired to existing backend endpoints. Only the active client's fields are shown.
- **Flagged Torrents History** — dashboard "Last Flagged Torrent" card now has Last/Historical tabs; Historical tab shows a scrollable list of all flagged torrents with rule name and date.
- **Dashboard retention labels** — Flagged and Actioned stat cards now show "last N days" based on `retention_days` config value, since counts are pruned at that interval.
- **Table captions** — all data tables across dashboard, torrents, indexers, logs, LLM logs, and stats pages now have screen-reader-only `<caption>` elements for accessibility.
- **`.sr-only` CSS utility** — global screen-reader-only class in style.css for accessibility.
- **Flask SECRET_KEY** from env var `FLASK_SECRET_KEY` or random per-restart.
- **StateManager.close()** with atexit registration for clean SQLite shutdown.
- **Notification failure logging** — Pushover and summary failures now logged at WARNING instead of silently swallowed.
- **Torrent client failure logging** — all action methods (pause, resume, delete, set_category) across qBit, Transmission, Deluge now log warnings on failure.
- **Scheduler DB fallback** — stderr output when DB logging fails in auto_manage, reorder, and summary tasks.
- **HTTP session cleanup** — `close()` method on AbstractTorrentClient to release connection pools.

### Fixed
- **Config save PermissionError in Docker** — `tempfile.mkstemp()` fails in `/app/` because the non-root container user can't write to the read-only app directory. The `_save_raw()` function now catches `PermissionError` and falls back to a direct overwrite of the bind-mounted `config.yaml`, which the container user *can* write to. Atomic temp-file-then-replace is still attempted first on systems where it works.
- **False "unsaved changes" warning on config pages** — JavaScript-driven population of form fields (qBit category dropdowns, Ollama model selects, scoring weight defaults) fired `change` events during page initialization, immediately marking the form dirty. The dirty-state tracker now delays arming by 500ms so all dynamic population completes before user interactions are monitored.
- **Bind-mount breakage** — `shutil.move` atomic writes replaced the config file inode, breaking Docker bind mounts. Replaced with direct write (open → write → fsync) which preserves the inode.
- **Base image digest pin removed** — no *arr pins base image digests; makes updates harder for no meaningful security benefit. Healthcheck retained.
- **Chart.js CDN 404** — Indexers page health score history chart failed to load because Chart.js 4.4.4 does not exist on cdnjs. Changed to 4.4.1.
- **Config dirty warning on torrent pane toggle** — initial visibility setup called `markDirty()` during page load. Fixed by only marking dirty on user-initiated changes.

### Changed
- **Security audit pruned to community standards** — evaluated all 35 findings against Sonarr, Radarr, Prowlarr, and LinuxServer.io practices. Dropped 13 items that exceeded community expectations with no significant security justification. See SECURITY_AUDIT.md for full disposition.

---

## [v1.5.0] — 2026-08-09

### Added
- **LLM Logs page** under System — report card table (deterministic vs AI score + delta + reasoning per indexer), per-indexer trend chart (Chart.js, up to 30 historical points), and run history audit trail
- **Multi-torrent-client support** — abstract TorrentClient base class with factory pattern; QBitClient, TransmissionClient (JSON-RPC 2.0 + CSRF), DelugeClient (Web UI JSON-RPC + Label plugin); config-driven selection via `torrent_client` field
- **Dark/light theme toggle** — moon/sun icon in header, CSS custom properties, preference persisted in localStorage
- **Dashboard lifetime stats** — Flagged and Actioned cards now show total historical counts from `processed_hashes` table instead of last-scan-only values
- **Last Flagged Torrent persistence** — falls back to `processed_hashes` when `run_history` is pruned, so the card always shows the most recent catch
- **ARIA landmarks** on every page — `<nav>`, `role="main"`, proper heading hierarchy
- **Log export button** on Events page — downloads current filtered view as `.json`
- **Retry queue count** displayed on Scheduler page
- **Torrent pagination** — 50 per page with navigation controls
- **Wiki screenshots** for dashboard, scheduler, indexers, settings, LLM Logs
- **README screenshots** inline for dashboard and indexer health table

### Fixed
- **Ollama echo bug** — AI was returning input fields instead of reasoning; fixed with explicit output schema constraints in system prompt
- **Banner color mismatch** — SVG banner updated from old purple to teal (#2dd4bf) accent
- **--text-dim contrast** — muted text color adjusted for WCAG compliance in both themes
- Debug logging downgraded for raw Ollama responses (was `log.info`, now `log.debug`)

---

## [v1.4.0] — 2026-07-30

### Added
- **Scoring engine rewrite** — six weighted signals replacing the original three-factor formula:
  - Response time: logarithmic curve (`log(1 + avg_ms) / log(1 + 5000)`) instead of linear
  - Failure rate: severity multipliers per type (auth 3×, grab 2×, query 1×, RSS 0.5×)
  - Malicious rate: ratio-based (hits ÷ total grabs) instead of raw count
  - Grab success: dedicated 0.25 weight signal (previously folded into failure rate)
  - Backoff penalty: flat −20 when indexer is in Prowlarr cooldown
  - Trend: linear regression over last 30 snapshots, ±10 points
- **LLM result caching** — SHA256 hash of scoring payload, configurable TTL (`cache_ttl_hours`, default 24), 60–80% reduction in Ollama calls on stable homelabs
- **Webhook receiver** — event-driven scanning from Sonarr (`/webhook/sonarr`), Radarr (`/webhook/radarr`), Lidarr (`/webhook/lidarr`) with shared-secret auth and configurable scan delay
- **Auto-managed indexers** — automatically disable indexers below a health threshold after N consecutive low runs, re-enable after configurable cooldown; manual override from Indexers page
- **Ollama model selector** in Settings UI — dropdown populated from Ollama `/api/tags`
- **System sub-pages**: Tasks (scheduler job queue), Backups (snapshot/restore config + DB), Updates (GitHub releases check)
- **Mobile responsive** UI across all pages
- **Notification digest** — batched Pushover alerts with Ollama-narrated summaries; daily/weekly periodic log summaries
- **`--daemon` CLI flag** for headless operation with graceful SIGINT/SIGTERM shutdown
- **Per-indexer stats reset** from Indexers page
- **Waitress WSGI** server replacing Flask dev server in production
- **Non-root Docker** container with configurable PUID/PGID
- **Dev container CI** — syntax check and import test on every push

### Changed
- All scoring weights and multipliers now configurable in `config.yaml`
- README redesigned with feature sections and architecture overview
- Reorder & Sync reads cached scores from SQLite (no longer triggers a full rescore)

---

## [v1.2.5] — 2026-06-25 (estimated)

### Added
- **Lidarr v2 support** — third arr client implementation on the AbstractArrClient base

### Fixed
- Minor scoring and UI fixes between v1.2.0 and v1.4.0

---

## [v1.2.0] — 2026-06-20

### Added
- **Grab attribution fix** — indexer name normalization strips "(Prowlarr)" suffix for correct matching; diagnostic script `debug_attribution.py` traces attribution chain
- **UI overhaul** — IBM Plex Sans/Mono + Syne typography, darker palette (~#090b0e), rgba borders, SVG logo (helm wheel + magnifying glass)
- **Settings sub-navigation** — sidebar expands to Connections, Rules, Indexers, Notifications, General, Advanced; unified save with "Unsaved changes" marker
- **System section** — Status page (live concurrent connection checks, storage usage, disk space bar), Events (renamed from Logs to match arr convention)
- **Rules category dropdown** — populated live from qBittorrent categories, replacing free-text input; read-only mode with connection banner when qBit is unreachable
- **GitHub wiki** — 12 pages: installation, configuration reference, rules, detection sequence, Prowlarr scoring, web UI tour, notifications, CLI usage, authentication, FAQ, troubleshooting

### Fixed
- Silent grab attribution failures bumped from DEBUG to WARNING with explicit log events
- Attribution matching now works across all arr apps regardless of Prowlarr name suffix

---

## [v1.1.1] — 2026-06-15 (estimated)

### Fixed
- **Indexer scoring: identical values bug** — all indexers reported the same response time and success rate because Prowlarr's `/api/v1/history` silently ignores `indexerId` filter; replaced with `/api/v1/indexerstats` which returns pre-computed per-indexer stats
- Scoring runs now make 3 API calls total instead of N+2 (one per indexer)

---

## [v1.1.0] — 2026-06-12 (estimated)

### Added
- **Prowlarr indexer health scoring** — 0–100% score per indexer weighing response time, success/failure rate, and malicious content; dedicated Indexers page with Rescore and Reorder actions; per-indexer Ignore toggle; automatic reordering on configurable interval
- **Malicious-content attribution** — traces bad torrents to serving indexer via arr grab history, penalizes indexer score
- **Web UI authentication** — optional HTTP Basic Auth, configurable from Settings, fails open on config error
- **Automated Docker publishing** — GitHub Actions CI builds and publishes to GHCR on tagged releases

### Changed
- NZB indexers excluded from scoring (torrent indexers only)
- Priority writes use Prowlarr `forceSave` for unreachable indexers

---

## [v1.0.0] — 2026-06-07

### Added
- **Rule engine** — configurable rules per qBittorrent category: bad extensions, filename patterns, match mode (any file vs primary file only)
- **Sonarr v4 integration** — blocklist via queue delete or history fail, triggering automatic re-search
- **qBittorrent Web API v2** client — category-based monitoring, torrent deletion with file cleanup
- **Retry queue** — SQLite-backed, 10 attempts with configurable interval, fail-open and abort modes
- **Pushover notifications** — per-event alerts with deduplication
- **State management** — SQLite database for processed hashes, retry queue; JSON Lines event log with configurable retention
- **CLI** — `inspectarr.py` with `--dry-run` flag
- **Web UI** — Flask on port 8585: Dashboard (live polling), Scheduler (start/stop/run now), Logs (paginated, level-filtered, auto-refresh), Config (form + raw YAML editor, test-connection buttons, dynamic rule builder)
- **Scheduler daemon** — background thread in Flask process, config reloaded before every scan, starts disabled by default
- **`--retry-now` CLI flag** — force-flush retry queue bypassing timing and exhaustion cap
- **systemd unit file** and **docker-compose.yml**
- **Radarr v3 support** — second arr client on AbstractArrClient base

### Fixed
- Notification spam loop in retry subsystem — exhausted entries no longer re-selected
- Abort-mode timing bypass — `has_active_retry()` check prevents reprocessing torrents already in retry queue
- Orphan retry entries when retry disabled — queue writes gated behind `retry.enabled` flag
