<p align="center">
  <img src="assets/inspectarr-banner.svg" alt="Inspectarr" width="900">
</p>

# inspectarr

Torrent watchdog for \*arr ecosystems. Polls qBittorrent categories, detects
downloads that match configurable bad-file rules (e.g. `.exe` files in a TV
category), blocklists them in Sonarr, Radarr, or Lidarr, deletes the torrent and
files, logs all events to JSON Lines, and notifies via Pushover.

Also monitors Prowlarr torrent indexer health — scoring each indexer by response
time (logarithmic curve), weighted failure rate (auth > grab > query > RSS),
malicious content rate, and grab success — automatically reorders them so your
best indexers are always searched first, and tracks per-indexer grab and
malicious-hit statistics over time. Optionally integrates with a local
[Ollama](https://ollama.com) instance to apply AI-powered health scoring that
considers the full picture of each indexer's behaviour.

Runs two ways: a **web UI with a built-in scheduler daemon** (`web.py`), or a
**one-shot CLI** (`inspectarr.py`) for manual runs and testing.

---

## Quick Start (Web UI)

```bash
cp config.example.yaml config.yaml
# edit config.yaml with your URLs, credentials, and rules
pip install -r requirements.txt
python3 web.py
```

Open `http://<host>:8585`. The scheduler starts **off** — enable it from the
dashboard once you've confirmed your config and categories are correct.

## Quick Start (CLI)

```bash
cp config.example.yaml config.yaml
python3 inspectarr.py --dry-run    # confirm matches without deleting
python3 inspectarr.py              # live run
```

## Requirements

- Python 3.12+
- qBittorrent with Web UI enabled (v4.x or v5.x)
- One or more of: Sonarr v4, Radarr v3, Lidarr v2
- Prowlarr (optional — required only for indexer health scoring and attribution)
- Ollama (optional — enables AI-powered indexer health scoring via a local LLM)

```bash
pip install -r requirements.txt   # requests, pyyaml, flask
```

---

## Web UI

Served on port `8585` by default (configurable via `web.port`). Responsive layout works on desktop and mobile.

| Page | What it does |
|---|---|
| **Dashboard** | Scheduler status, last-scan stats (checked / flagged / actioned), last flagged torrent (persists across clean scans), recent run history. Live-updates every 5s. |
| **Scheduler** | Start/stop the daemon, run-now, poll interval, last/next run, run history. |
| **Torrents** | Quick-look dashboard of all qBittorrent torrents. Filter by status/category, change categories, pause/resume, and delete. Per-torrent detail view with tracker status and file list. Compatible with qBittorrent 4.x and 5.x. |
| **Indexers** | Prowlarr torrent indexer health table. Rescore computes deterministic scores and, if Ollama is configured, runs AI scoring in the background. Reorder & Sync applies priority changes using cached scores and pushes the updated order to all connected apps. Per-indexer Ignore toggle and stats reset. |
| **Stats** | Per-indexer grab and malicious-hit statistics. Total grabs attributed on first scan of each torrent; malicious hits increment when Inspectarr flags and deletes a torrent from that indexer. |
| **Logs** | Paginated JSON Lines viewer (100/page), level filter, color-coded badges, auto-refresh, clear-log. |
| **Settings** | Full form editor organized into sub-panes: Connections, Rules, Indexers (with Ollama model selector), Notifications, General, Advanced, Backups. Test-connection buttons for all services. Raw-YAML mode for advanced edits. |
| **System → Status** | Application info, scheduler state, storage usage, live connection checks for all services. |
| **System → Tasks** | Scheduled jobs view with state indicators (à la \*arr apps). |
| **System → Updates** | GitHub releases check with version comparison and release notes. |

The scheduler reloads `config.yaml` from disk before every scan, so changes
saved in the Config page take effect on the next cycle — no restart needed.
Changing `web.port` is the one exception; that requires a restart.

### Authentication

Optional HTTP Basic Auth, configurable from the Config page (Form tab → Web UI
Authentication). Takes effect immediately — no restart needed. Fails open on
config errors so a broken `config.yaml` never locks you out.

---

## Docker

Pull the published image from GHCR:

```bash
docker pull ghcr.io/o51r15/inspectarr:latest
```

Run it:

```bash
docker run -d \
  --name inspectarr \
  --user "$(id -u):$(id -g)" \
  -p 8585:8585 \
  -v ./data:/app/data \
  -v ./config.yaml:/app/config.yaml \
  ghcr.io/o51r15/inspectarr:latest
```

Or with Docker Compose (using the included `docker-compose.yml`):

```bash
docker compose up -d
```

The compose file uses `PUID`/`PGID` environment variables (defaulting to `1000:1000`)
to run the container as your host user, avoiding permission issues with mounted volumes.

A `ghcr.io/o51r15/inspectarr:dev` image is built automatically on every push to `main`.

To run a one-off CLI scan against a running container:

```bash
docker exec inspectarr python inspectarr.py --dry-run
```

---

## Systemd (auto-start on boot)

A `inspectarr.service` unit file is included. Copy it and enable it:

```bash
sudo cp inspectarr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable inspectarr
sudo systemctl start inspectarr
```

Check status and logs:

```bash
sudo systemctl status inspectarr
sudo journalctl -u inspectarr -f
```

---

## Configuration

See `config.example.yaml` for all options with inline documentation.

Key settings:

| Setting | Purpose |
|---|---|
| `rules[].conditions.match_mode` | `any` = flag on any bad file; `primary` = only if largest file is bad |
| `rules[].conditions.bad_extensions` | List of file extensions to flag (e.g. `.exe`, `.zip`) |
| `rules[].conditions.min_file_size_mb` | Flag if the primary (largest) file is below this size in MB |
| `rules[].conditions.bad_filename_patterns` | List of regex patterns matched against filenames |
| `on_arr_failure` | `delete` = remove from qBit anyway; `abort` = skip and retry |
| `poll_interval_seconds` | How often the scheduler daemon scans (default: 300) |
| `retry.max_attempts` | How many times to retry before giving up (default: 10) |
| `retry.interval_seconds` | Seconds between retry attempts (default: 600) |
| `web.port` | Web UI port (default: 8585) |
| `web.scheduler_autostart` | `true` = start the scheduler automatically on launch (default: false) |
| `web.auth.enabled` | `true` = require Basic Auth login to access the web UI |
| `web.auth.username` / `web.auth.password` | Credentials for Basic Auth |
| `dry_run` | `true` = log matches only, no deletions |
| `prowlarr.enabled` | Enable Prowlarr indexer health scoring, auto-reorder, and grab attribution |
| `prowlarr.url` | Prowlarr URL including base path if set (e.g. `http://host:9696/prowlarr`) |
| `prowlarr.base_priority` | Priority number assigned to the best-scoring torrent indexer; others count up from here |
| `prowlarr.reorder_interval_hours` | How often the auto-reorder runs (driven by the scheduler) |
| `prowlarr.history_window_days` | Rolling window for response time and failure rate scoring |
| `prowlarr.min_grabs_before_scoring` | Minimum history records required before scoring an indexer |
| `prowlarr.scoring.response_time_weight` | Weight for logarithmic response time score (default 0.25) |
| `prowlarr.scoring.failure_rate_weight` | Weight for weighted failure rate score (default 0.30) |
| `prowlarr.scoring.malicious_weight` | Weight for malicious content rate score (default 0.20) |
| `prowlarr.scoring.grab_success_weight` | Weight for grab success sub-score (default 0.25) |
| `prowlarr.scoring.auth_failure_mult` | Auth failure severity multiplier (default 3.0) |
| `prowlarr.scoring.grab_failure_mult` | Grab failure severity multiplier (default 2.0) |
| `prowlarr.scoring.query_failure_mult` | Query failure severity multiplier (default 1.0) |
| `prowlarr.scoring.rss_failure_mult` | RSS failure severity multiplier (default 0.5) |
| `prowlarr.ollama.url` | Ollama API base URL (e.g. `http://192.168.1.125:11434`) |
| `prowlarr.ollama.model` | Model name to use for AI scoring (e.g. `gemma4:latest`) |
| `prowlarr.ollama.timeout` | Request timeout in seconds (default: 120) |

---

## CLI Flags

```
python3 inspectarr.py                    # single scan run
python3 inspectarr.py --config /path     # alternate config location
python3 inspectarr.py --dry-run          # override config dry_run=true
python3 inspectarr.py --retry-now        # force flush retry queue, then scan
python3 inspectarr.py --daemon           # continuous loop (poll_interval_seconds)
```

The web UI scheduler lives in `web.py`. The CLI is single-shot by default;
`--daemon` runs a continuous scan loop with graceful SIGINT/SIGTERM handling.

---

## Persistent Data

Everything in `data/` — mount as a Docker volume:

| File | Contents |
|---|---|
| `inspectarr.db` | SQLite: processed hashes, retry queue, run history, indexer stats, grab attribution |
| `inspectarr.log.json` | JSON Lines: one event object per line |

---

## Project Layout

```
inspectarr/
├── inspectarr.py            # CLI entry point (single-shot)
├── web.py                   # Web UI + scheduler daemon entry point
├── core/                    # Core logic — no awareness of the UI
│   ├── config.py            # Config loader + dataclasses
│   ├── scanner.py           # Main orchestrator (scan, flag, action, attribute)
│   ├── rules.py             # Rule evaluation engine
│   ├── qbit.py              # qBittorrent Web API v2 client (4.x + 5.x compatible)
│   ├── prowlarr.py          # Prowlarr API client (indexers, priority, sync)
│   ├── indexer_scorer.py    # Indexer health score computation + reorder logic
│   ├── llm_client.py        # Ollama LLM client for AI-powered indexer scoring
│   ├── arrs/
│   │   ├── base.py          # AbstractArrClient (history lookup, grab attribution)
│   │   ├── sonarr.py        # Sonarr v4 client
│   │   ├── radarr.py        # Radarr v3 client
│   │   └── lidarr.py        # Lidarr v2 client
│   ├── notifier.py          # Pushover client
│   └── state.py             # SQLite + JSON Lines log + grab attribution tables
├── ui/                      # Web UI layer
│   ├── auth.py              # HTTP Basic Auth enforcement
│   ├── scheduler.py         # Background scheduler daemon thread
│   ├── routes/
│   │   ├── dashboard.py     # Dashboard page + /status JSON endpoint
│   │   ├── scheduler.py     # Scheduler controls
│   │   ├── logs.py          # Log viewer
│   │   ├── torrents.py      # Torrents page + AJAX actions
│   │   ├── indexers.py      # Indexer health page + reorder & sync
│   │   ├── stats.py         # Indexer grab/malicious stats page
│   │   ├── system.py        # System status, tasks, updates pages
│   │   └── config.py        # Config form + test/backup/Ollama model endpoints
│   ├── templates/
│   │   ├── base.html        # Sidebar, nav, fonts, toast system
│   │   ├── dashboard.html
│   │   ├── scheduler.html
│   │   ├── logs.html
│   │   ├── torrents.html
│   │   ├── torrent_detail.html
│   │   ├── indexers.html
│   │   ├── stats.html
│   │   ├── config.html
│   │   ├── system_tasks.html
│   │   └── system_updates.html
│   └── static/
│       ├── style.css        # Dark theme — IBM Plex + Syne typography, mobile responsive
│       ├── app.js
│       └── logo.svg         # Vector logo mark
├── assets/
│   └── inspectarr-banner.jpg
├── .devcontainer/
│   └── devcontainer.json    # Dev container configuration
├── .github/workflows/
│   ├── release.yml          # Build + push Docker image to GHCR on tag
│   ├── devcontainer.yml     # Build dev container on push to main
│   └── mirror.yml           # Mirror to Gitea
├── config.example.yaml
├── docker-compose.yml
├── inspectarr.service
├── data/                    # Runtime state (gitignored, Docker volume)
├── Dockerfile
└── requirements.txt
```
