Welcome to the **Inspectarr** wiki — a torrent watchdog for *arr ecosystems.

Inspectarr watches your qBittorrent categories for bad downloads (wrong extensions, suspicious filenames, undersized files), blocklists them in Sonarr / Radarr / Lidarr, deletes the torrent, and notifies you via Pushover. Scans can be triggered by polling, incoming webhooks from your *arr apps, or both.

It also scores your Prowlarr torrent indexers by health — response time, weighted failure rate, malicious content, grab success, and historical trend — then automatically reorders them so your best indexers are searched first. Indexers that consistently score below a threshold are automatically disabled and re-enabled after a cooldown. Optionally uses a local Ollama LLM for AI-powered scoring with content-hash caching.

## Key features

- Bad torrent detection with configurable rules per category
- Automatic blocklist + delete + retry
- Webhook and polling scan triggers
- Prowlarr indexer health scoring with auto-reorder and auto-manage
- Optional AI scoring via Ollama with LLM result caching
- Ollama-narrated notification digests and periodic log summaries
- LLM Logs page with per-indexer AI reasoning, score trend charts, and run history
- Full web UI with dashboard, scheduler, indexer health, stats, settings, backups, tasks, and update checker
- Dark/light theme toggle
- Mobile responsive design
- Docker-native with non-root container support
- CLI daemon mode for headless operation

## Wiki pages

- [Configuration](Configuration) — every `config.yaml` option explained
- [Rules](Rules) — how detection rules work
- [Prowlarr Indexer Scoring](Prowlarr-Indexer-Scoring) — health score formula, AI scoring, auto-manage
- [Web UI](Web-UI) — page-by-page guide to the web interface
- [LLM Logs](LLM-Logs) — AI scoring output visibility and trend analysis
- [Notifications](Notifications) — Pushover setup, digests, and summaries
- [Docker](Docker) — image tags, compose examples, non-root setup
- [CLI](CLI) — command-line flags and daemon mode

## Quick start

```bash
cp config.example.yaml config.yaml   # edit with your URLs and credentials
docker compose up -d
```

Open `http://your-server:8585`, configure your settings, then start the scheduler from the dashboard.

## Links

- [GitHub repo](https://github.com/o51r15/inspectarr)
- [Releases](https://github.com/o51r15/inspectarr/releases)
- [Issues](https://github.com/o51r15/inspectarr/issues)
