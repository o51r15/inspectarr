# inspectarr — Project Roadmap & Specification
> Generated: 2026-06-07
> Status: Pre-development / Specification locked

---

## Overview

`inspectarr` is a Python-based torrent watchdog that polls qBittorrent, evaluates
active downloads against configurable rules, blocklists bad torrents in *arr apps
(Sonarr first, others planned), deletes the torrent and files from qBittorrent, logs
all actions, and sends Pushover notifications.

**Problem it solves:** Fake or malicious torrents (e.g., .exe files masquerading as
TV episodes) slip through indexers and land in Sonarr-managed qBittorrent categories.
inspectarr detects and eliminates them automatically before they can execute or waste
bandwidth, and ensures Sonarr never retries the same bad release.

**Design goals:**
- Standalone Python script, zero heavy dependencies
- Clean importable core for future HomelabPy integration
- GUI-wrapper ready (core never references UI)
- Docker-native from the start (single volume mount)
- Extensible to Radarr, Lidarr, Readarr without core rewrites

---

## Repository Structure

```
inspectarr/
├── watchdog.py              # CLI entry point
├── core/
│   ├── config.py            # YAML loader, validator, typed dataclasses
│   ├── scanner.py           # Main scan orchestrator
│   ├── rules.py             # Rule evaluation engine
│   ├── qbit.py              # qBittorrent Web API v2 client
│   ├── arrs/
│   │   ├── base.py          # AbstractArrClient
│   │   ├── sonarr.py        # SonarrClient(AbstractArrClient)
│   │   └── radarr.py        # Stub — not wired in v1
│   ├── notifier.py          # Pushover dispatch
│   └── state.py             # SQLite state manager + JSON log writer
├── config.yaml              # Live config (gitignored)
├── config.example.yaml      # Committed example, all options documented
├── data/
│   ├── inspectarr.db        # SQLite state (Docker volume)
│   └── inspectarr.log.json  # JSON Lines action log (Docker volume)
├── Dockerfile
├── requirements.txt
└── README.md
```

`data/` is the single Docker volume mount. All persistent state lives here.

---

## Configuration Schema (Final)

```yaml
qbittorrent:
  url: http://192.168.1.192:8080
  username: admin
  password: changeme

arrs:
  sonarr:
    enabled: true
    url: http://192.168.1.192:8989
    api_key: abc123
  radarr:
    enabled: false
    url: http://192.168.1.192:7878
    api_key: ~

rules:
  - name: "TV Bad Extensions"
    category: "tv-sonarr"
    app: sonarr
    conditions:
      match_mode: any         # "any" = flag if any file is bad (default)
                              # "primary" = flag only if largest file is bad
      bad_extensions:
        - ".exe"
        - ".zip"
        - ".bat"
        - ".msi"
        - ".js"
        - ".vbs"
        - ".dmg"
        - ".rar"
      # Reserved for future use (not evaluated in v1):
      # min_file_size_mb: ~
      # bad_filename_patterns: []

on_arr_failure: delete        # "delete" = remove from qbit anyway, log + notify
                              # "abort"  = skip qbit deletion, retry via queue

retry:
  enabled: true
  max_attempts: 10
  interval_seconds: 600       # 10 minutes between retries

logging:
  log_file: ./data/inspectarr.log.json
  retention_days: 30
  level: INFO                 # DEBUG for verbose

state:
  db_file: ./data/inspectarr.db

notifications:
  pushover:
    enabled: true
    app_token: your_app_token
    user_key: your_user_key
    notify_on:
      - action                # Torrent deleted + blocklisted
      - error                 # Any API failure or unhandled exception
      - startup               # Script launched
      - dry_run               # Match found, no action taken (dry run mode)
    priority: 0               # -2=lowest, -1=low, 0=normal, 1=high, 2=emergency

dry_run: false
```

---

## SQLite State Schema

### Table: `processed_hashes`
Prevents reprocessing already-actioned torrents.

```sql
CREATE TABLE processed_hashes (
    hash         TEXT PRIMARY KEY,
    torrent_name TEXT,
    category     TEXT,
    rule_name    TEXT,
    action       TEXT,        -- 'deleted', 'dry_run', 'failed'
    actioned_at  TEXT,        -- ISO8601
    arr_success  INTEGER,     -- 1 / 0
    qbit_success INTEGER      -- 1 / 0
);
```

### Table: `retry_queue`
Tracks failed attempts pending retry.

```sql
CREATE TABLE retry_queue (
    hash           TEXT PRIMARY KEY,
    torrent_name   TEXT,
    category       TEXT,
    rule_name      TEXT,
    attempt_count  INTEGER DEFAULT 0,
    last_attempt   TEXT,      -- ISO8601
    next_attempt   TEXT,      -- ISO8601
    failure_reason TEXT,
    resolved       INTEGER DEFAULT 0
);
```

Retention cleanup on every startup. Records older than `retention_days` pruned from
both tables and JSON log.

---

## JSON Lines Log Format

One object per event, appended to `inspectarr.log.json`.

```
{"timestamp": "2026-06-07T03:14:00Z", "level": "INFO",    "event": "startup",         "config": "config.yaml", "dry_run": false, "rules_loaded": 1}
{"timestamp": "2026-06-07T03:14:22Z", "level": "ACTION",  "event": "torrent_deleted", "torrent_name": "Show.S01E01.1080p", "hash": "abc123", "category": "tv-sonarr", "rule": "TV Bad Extensions", "bad_files": ["setup.exe"], "arr_blocklisted": true, "qbit_deleted": true}
{"timestamp": "2026-06-07T03:14:23Z", "level": "ERROR",   "event": "arr_failure",     "torrent_name": "Show.S01E01.1080p", "hash": "abc123", "reason": "Sonarr 503", "action_taken": "deleted_anyway"}
{"timestamp": "2026-06-07T03:14:30Z", "level": "DRY_RUN", "event": "dry_run_flagged", "torrent_name": "Show.S01E01.1080p", "hash": "abc123", "bad_files": ["crack.exe"]}
{"timestamp": "2026-06-07T03:20:00Z", "level": "INFO",    "event": "retry_attempt",   "hash": "abc123", "attempt": 2, "max": 10}
```

Log levels: `DEBUG`, `INFO`, `ACTION`, `DRY_RUN`, `ERROR`

---

## Core Logic Flow

### Startup sequence
1. Load and validate config
2. Initialize SQLite — create tables if not exist
3. Prune records older than `retention_days`
4. Process retry queue — attempt any entries where `next_attempt <= now`
5. Fire `startup` Pushover notification if enabled

### Scan loop (single-shot in v1)
```
for each rule in config.rules:
    fetch torrents from qbit by category
    for each torrent:
        if hash in processed_hashes (action != 'failed'): skip
        fetch file list from qbit
        evaluate conditions:
            match_mode=any:     flag if ANY file has a bad extension
            match_mode=primary: flag if LARGEST file has a bad extension
        if flagged:
            if dry_run:
                log DRY_RUN, notify, insert processed_hashes(action='dry_run')
                continue
            call attempt_action(torrent, rule)
```

### attempt_action(torrent, rule)
```
Step 1: Find torrent in arr queue by infohash
Step 2a: If found in queue  → DELETE /queue/<id>?blocklist=true&removeFromClient=false
Step 2b: If not in queue    → GET /history?downloadId=<hash> → POST /blocklist
Step 3: If arr call fails:
    log error + fire error notification
    if on_arr_failure == 'abort': insert retry_queue, return
    # else fall through — delete from qbit anyway
Step 4: DELETE torrent + files from qBittorrent
Step 5: Insert/update processed_hashes
Step 6: Remove from retry_queue if present
Step 7: Log ACTION event + fire action notification
```

### Retry logic
```
On startup, query retry_queue where resolved=0 and next_attempt <= now:
    increment attempt_count
    update last_attempt + next_attempt (last + interval_seconds)
    re-run attempt_action
    if attempt_count >= max_attempts and still failing:
        leave resolved=0 as permanent failure record
        log ERROR
        fire error notification
```

---

## Arr Client Interface (base.py)

Every *arr client must implement:

```python
class AbstractArrClient:
    def find_in_queue(self, infohash: str) -> dict | None
    def blocklist_from_queue(self, queue_id: int) -> bool
    def find_in_history(self, infohash: str) -> dict | None
    def blocklist_from_history(self, history_record: dict) -> bool
    def blocklist(self, infohash: str) -> bool   # orchestrates the above
```

Adding Radarr = create radarr.py, inherit AbstractArrClient, wire app: radarr config
key. Nothing else changes.

---

## CLI Interface

```
python watchdog.py                    # Single scan run (v1 default)
python watchdog.py --config /path     # Alternate config path
python watchdog.py --dry-run          # Override config dry_run flag
python watchdog.py --daemon           # FUTURE: continuous loop
python watchdog.py --retry-now        # FUTURE: force flush retry queue
```

---

## Dockerfile (planned)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
VOLUME ["/app/data"]
CMD ["python", "watchdog.py"]
```

```
docker run -v ./data:/app/data -v ./config.yaml:/app/config.yaml inspectarr
```

---

## Dependencies

```
requests    # qBittorrent API, Sonarr API, Pushover
pyyaml      # Config parsing
```

No ORM. No async. SQLite via stdlib sqlite3. Minimal footprint.

---

## v1 Scope

### In
- [ ] Config loading + validation with typed dataclasses
- [ ] qBittorrent Web API v2 client (auth, torrent list, file list, delete)
- [ ] Sonarr API client (queue lookup, history lookup, blocklist)
- [ ] Rule engine (match_mode: any / primary, bad_extensions)
- [ ] SQLite state (processed_hashes + retry_queue)
- [ ] JSON Lines logging with retention cleanup
- [ ] Retry logic (configurable attempts + interval)
- [ ] Pushover notifications (configurable events + priority)
- [ ] Dry-run mode
- [ ] Single-shot CLI
- [ ] Dockerfile + requirements.txt + config.example.yaml + README

### Deferred (Post-v1)
| Item | Notes |
|---|---|
| --daemon loop mode | Stub the flag, raise NotImplementedError |
| Radarr support | radarr.py stub present, not wired |
| min_file_size_mb condition | Config key reserved, not evaluated |
| bad_filename_patterns | Config key reserved, not evaluated |
| GUI wrapper | Core importable, no GUI code in v1 |
| HomelabPy module integration | Drop-in after v1 is stable |

---

## Build Order

1. core/config.py       — dataclasses + YAML loader + validation
2. core/state.py        — SQLite init, helpers, retention cleanup
3. core/qbit.py         — qBittorrent client
4. core/arrs/base.py    — AbstractArrClient
5. core/arrs/sonarr.py  — SonarrClient
6. core/notifier.py     — Pushover client
7. core/rules.py        — rule evaluation
8. core/scanner.py      — orchestrator
9. watchdog.py          — CLI entry point
10. config.example.yaml
11. Dockerfile + requirements.txt
12. README.md
