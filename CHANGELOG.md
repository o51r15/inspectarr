# Changelog

All notable changes to Inspectarr are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

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
