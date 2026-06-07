# inspectarr

Torrent watchdog for *arr ecosystems. Polls qBittorrent categories, detects
downloads that match configurable bad-file rules (e.g. `.exe` files in a TV
category), blocklists them in Sonarr (Radarr planned), deletes the torrent and
files, logs all events to JSON Lines, and notifies via Pushover.

---

## Quick Start

```bash
cp config.example.yaml config.yaml
# edit config.yaml with your URLs, credentials, and rules
python watchdog.py --dry-run    # confirm matches without deleting
python watchdog.py              # live run
```

## Requirements

- Python 3.12+
- qBittorrent with Web UI enabled
- Sonarr v4

```bash
pip install -r requirements.txt
```

## Docker

```bash
docker build -t inspectarr .
docker run \
  -v ./data:/app/data \
  -v ./config.yaml:/app/config.yaml \
  inspectarr
```

For scheduled runs, pair with a cron container or a systemd timer that
executes the container on your desired interval.

## Configuration

See `config.example.yaml` for all options with inline documentation.

Key settings:

| Setting | Purpose |
|---|---|
| `rules[].conditions.match_mode` | `any` = flag on any bad file; `primary` = only if largest file is bad |
| `on_arr_failure` | `delete` = remove from qBit anyway; `abort` = skip and retry |
| `retry.max_attempts` | How many times to retry before giving up (default: 10) |
| `retry.interval_seconds` | Seconds between retry attempts (default: 600) |
| `dry_run` | `true` = log matches only, no deletions |

## CLI Flags

```
python watchdog.py                    # single scan run
python watchdog.py --config /path     # alternate config location
python watchdog.py --dry-run          # override config dry_run=true
python watchdog.py --daemon           # (v2) continuous loop
python watchdog.py --retry-now        # (v2) force flush retry queue
```

## Persistent Data

Everything in `data/` — mount as a Docker volume:

| File | Contents |
|---|---|
| `inspectarr.db` | SQLite: processed hashes + retry queue |
| `inspectarr.log.json` | JSON Lines: one event object per line |

## Extending to Radarr / Other *arrs

1. Implement `core/arrs/radarr.py` (mirrors `sonarr.py` against Radarr's API)
2. Set `arrs.radarr.enabled: true` in config
3. Add rules with `app: radarr`
4. That's it — no other changes needed

## Project Layout

```
inspectarr/
├── watchdog.py              # CLI entry point
├── core/
│   ├── config.py            # Config loader + dataclasses
│   ├── scanner.py           # Main orchestrator
│   ├── rules.py             # Rule evaluation engine
│   ├── qbit.py              # qBittorrent Web API v2 client
│   ├── arrs/
│   │   ├── base.py          # AbstractArrClient
│   │   ├── sonarr.py        # Sonarr v4 client
│   │   └── radarr.py        # Radarr stub (v2)
│   ├── notifier.py          # Pushover client
│   └── state.py             # SQLite + JSON Lines log
├── config.example.yaml
├── data/                    # Runtime state (gitignored, Docker volume)
├── Dockerfile
└── requirements.txt
```
