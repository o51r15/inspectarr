# inspectarr — Project Log & Handoff Document
> Last updated: 2026-06-07
> Status: v1 complete, tested, working in production

---

## What This Project Is

inspectarr is a Python-based torrent watchdog for *arr ecosystems. It polls
qBittorrent categories, detects downloads containing bad file extensions (e.g.
.exe files masquerading as TV episodes), blocklists the release in Sonarr, deletes
the torrent and files from qBittorrent, logs all events to JSON Lines, and sends
Pushover notifications.

**The problem it solves:** Fake/malicious torrents slip through indexers and land
in Sonarr-managed qBittorrent categories. inspectarr catches them automatically
before they can execute or waste bandwidth, and ensures Sonarr never retries the
same bad release.

---

## Repository / File Locations

**Source on Thor (Windows):**
  E:\docker\configs\inspectarr\

**Deployed on Optiplex (Ubuntu 26.04):**
  /home/o51r15/scripts/inspectarr/

**Gitea username:** o51r15
  (Not yet pushed to Gitea as of this log — next step)

---

## Architecture

```
inspectarr/
├── inspectarr.py            # CLI entry point (renamed from watchdog.py)
├── core/
│   ├── config.py            # YAML loader, validator, typed dataclasses
│   ├── scanner.py           # Main scan orchestrator
│   ├── rules.py             # Rule evaluation engine
│   ├── qbit.py              # qBittorrent Web API v2 client
│   ├── arrs/
│   │   ├── base.py          # AbstractArrClient
│   │   ├── sonarr.py        # SonarrClient (Sonarr v4, /api/v3 prefix)
│   │   └── radarr.py        # Stub — not implemented in v1
│   ├── notifier.py          # Pushover dispatch
│   └── state.py             # SQLite state + JSON Lines log writer
├── config.yaml              # Live config (gitignored)
├── config.example.yaml      # Committed example, all options documented
├── data/
│   ├── inspectarr.db        # SQLite: processed_hashes + retry_queue tables
│   └── inspectarr.log.json  # JSON Lines action log
├── venv/                    # Python virtualenv (on Optiplex only, gitignored)
├── Dockerfile
├── requirements.txt         # requests>=2.31.0, pyyaml>=6.0
├── ROADMAP.md               # Full original spec document
└── README.md
```

---

## Key Design Decisions (Locked)

| Decision | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Stdlib only + requests + pyyaml |
| Config format | YAML | config.example.yaml is the reference |
| match_mode default | any | Flag if ANY file has bad extension |
| match_mode option | primary | Flag only if LARGEST file is bad |
| on_arr_failure default | delete | Delete from qBit anyway, log + notify |
| on_arr_failure option | abort | Skip qBit deletion, queue for retry |
| Retry | 10 attempts, 600s interval | Configurable in config.yaml |
| State | SQLite | processed_hashes + retry_queue tables |
| Log format | JSON Lines | One event object per line |
| Log retention | 30 days default | Configurable, pruned on every startup |
| Notifications | Pushover.net | Configurable events + priority |
| Arr support | Sonarr v4 now | Abstract base ready for Radarr/Lidarr |
| Execution | Single-shot | --daemon planned for v2 |
| Deployment target | Docker | Dockerfile written, data/ is the volume |

---

## SQLite Schema

### processed_hashes
Tracks every torrent that has been evaluated and actioned.
action values: 'deleted', 'dry_run', 'failed'
Note: 'dry_run' and 'failed' are re-eligible on next scan.
Only 'deleted' is treated as terminal/skip.

```sql
CREATE TABLE processed_hashes (
    hash         TEXT PRIMARY KEY,
    torrent_name TEXT,
    category     TEXT,
    rule_name    TEXT,
    action       TEXT,
    actioned_at  TEXT,
    arr_success  INTEGER,
    qbit_success INTEGER
);
```

### retry_queue
Tracks failed attempts awaiting retry.
Retries are processed on every startup if retry.enabled = true.

```sql
CREATE TABLE retry_queue (
    hash           TEXT PRIMARY KEY,
    torrent_name   TEXT,
    category       TEXT,
    rule_name      TEXT,
    attempt_count  INTEGER DEFAULT 0,
    last_attempt   TEXT,
    next_attempt   TEXT,
    failure_reason TEXT,
    resolved       INTEGER DEFAULT 0
);
```

---

## Sonarr API Behavior (v4 specific)

**Queue path (primary):**
  DELETE /api/v3/queue/{id}?removeFromClient=false&blocklist=true&skipRedownload=false
  Used when the torrent is still in Sonarr's active download queue.

**History path (fallback):**
  POST /api/v3/history/failed/{id}
  Used when the torrent is no longer in the queue.
  NOTE: This marks as failed AND triggers a re-search for the episode.
  This is acceptable behavior — the release is confirmed bad, re-search is desired.

**Not-found case:**
  If the hash isn't in queue or history (manual qBit add, not Sonarr-managed),
  blocklist() returns True and logs a warning. qBit deletion still proceeds.

---

## qBittorrent Client Notes

- Uses Web API v2 session cookie auth (SID cookie)
- Automatically re-auths on 403 (session expiry) before failing
- Login response must be "Ok." or "ok" (case-insensitive check)
- All category names are case-sensitive and must match qBit exactly

---

## CLI Interface

```
python3 inspectarr.py                    # single scan run
python3 inspectarr.py --config /path     # alternate config file
python3 inspectarr.py --dry-run          # flag matches, no action taken
python3 inspectarr.py --daemon           # NOT IMPLEMENTED (v2)
python3 inspectarr.py --retry-now        # NOT IMPLEMENTED (v2)
```

---

## Deployment on Optiplex

**OS:** Ubuntu 26.04 (Desktop)
**Python:** 3.14 (system), venv used for isolation
**Path:** /home/o51r15/scripts/inspectarr/
**Run method:** Manual / will be scheduled (cron or systemd timer — not yet set up)

**Setup commands used:**
```bash
sudo apt install python3.14-venv -y
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
nano config.yaml
```

**Run commands:**
```bash
source venv/bin/activate
python3 inspectarr.py --dry-run    # test
python3 inspectarr.py              # live
```

**Entry point filename:** inspectarr.py
  (Originally written as watchdog.py, renamed with mv watchdog.py inspectarr.py)
  Nothing inside the codebase references the filename — rename is safe.

---

## Test Results (2026-06-07)

### Test 1 — Category scoping
Torrent placed outside of the configured category.
Result: Not flagged. Correct behavior.

### Test 2 — Detection + dry run
Test torrent: "Adobe Lightroom v9.3.2 (x64) + Fix {CracksHash}"
Moved into tv-sonarr category, ran with --dry-run.

Log output:
```json
{
  "level": "DRY_RUN",
  "event": "dry_run_flagged",
  "torrent_name": "Adobe Lightroom v9.3.2 (x64) + Fix {CracksHash}",
  "hash": "a6184b00a84c13f367a554b70e615065cf7d9bcc",
  "category": "tv-sonarr",
  "rule": "TV Bad Extensions",
  "bad_files": ["GenP-v3.8.0.exe", "Set-up.exe", "AdobeLightroomCC-mul.zip"]
}
```
Result: Correctly flagged 2x .exe and 1x .zip. Pushover notification received.

### Test 3 — Live run
Ran without --dry-run.
Result: Torrent blocklisted in Sonarr, deleted from qBittorrent, ACTION event
logged with arr_blocklisted: true and qbit_deleted: true.

NOTE: The Optiplex hard-crashed during or shortly after this test run. Cause
unknown — likely unrelated to inspectarr (Python HTTP calls cannot crash a kernel).
After hard reboot, system came back up cleanly. Log confirmed the action
completed successfully before the crash.

---

## What Works (v1 Complete)

- [x] Config loading + validation
- [x] qBittorrent Web API v2 client
- [x] Sonarr v4 client (queue + history + blocklist)
- [x] Rule engine (match_mode: any / primary)
- [x] SQLite state (processed_hashes + retry_queue)
- [x] JSON Lines logging with retention
- [x] Retry logic (configurable attempts + interval)
- [x] Pushover notifications (configurable events + priority)
- [x] Dry-run mode
- [x] Single-shot CLI
- [x] Dockerfile + requirements.txt
- [x] Deployed and tested on Optiplex

---

## What's Next (v2 / Backlog)

### High priority
- [ ] Push to Gitea (o51r15 account) as a public repo
- [ ] Set up cron job or systemd timer on Optiplex for scheduled runs
- [ ] Docker Compose file for the Optiplex stack deployment

### Planned features
- [ ] --daemon mode (continuous loop with poll_interval_seconds from config)
- [ ] --retry-now flag (force flush retry queue without waiting for next_attempt)
- [ ] Radarr support (radarr.py stub exists, needs implementation)
- [ ] GUI wrapper (core is importable, no GUI code in v1)
- [ ] HomelabPy module integration (drop-in after v2 is stable)

### Future rule conditions (config keys reserved, not evaluated)
- [ ] min_file_size_mb — flag if primary file is suspiciously small
- [ ] bad_filename_patterns — regex matching on filenames

### Potential enhancements
- [ ] Multi-rule support tested (only one rule tested so far)
- [ ] Lidarr support (music category protection)
- [ ] Prowlarr integration for indexer-level blocking
- [ ] Web UI / status page (longer term)

---

## Config Reference (current live config structure)

```yaml
qbittorrent:
  url: http://192.168.1.192:8080
  username: <your_username>
  password: <your_password>

arrs:
  sonarr:
    enabled: true
    url: http://192.168.1.192:8989
    api_key: <your_sonarr_api_key>
  radarr:
    enabled: false
    url: http://192.168.1.192:7878
    api_key: ~

rules:
  - name: "TV Bad Extensions"
    category: "tv-sonarr"
    app: sonarr
    conditions:
      match_mode: any
      bad_extensions:
        - ".exe"
        - ".zip"
        - ".bat"
        - ".msi"
        - ".js"
        - ".vbs"
        - ".dmg"
        - ".rar"

on_arr_failure: delete

retry:
  enabled: true
  max_attempts: 10
  interval_seconds: 600

logging:
  log_file: ./data/inspectarr.log.json
  retention_days: 30
  level: INFO

state:
  db_file: ./data/inspectarr.db

notifications:
  pushover:
    enabled: true
    app_token: <your_app_token>
    user_key: <your_user_key>
    notify_on:
      - action
      - error
      - startup
      - dry_run
    priority: 0

dry_run: false
```

---

## Notes for Future Development

- The venv on Optiplex lives at /home/o51r15/scripts/inspectarr/venv/
  Always activate before running: source venv/bin/activate

- When adding Radarr: implement core/arrs/radarr.py mirroring sonarr.py,
  set arrs.radarr.enabled: true in config, add rules with app: radarr.
  No other changes needed — the abstract base and scanner already handle it.

- When adding to HomelabPy: import Scanner from core.scanner, instantiate
  with a loaded AppConfig, call startup() -> process_retries() -> run_scan().
  The core has zero awareness of any GUI or scheduler.

- Docker deployment: data/ is the volume mount. config.yaml mounts separately.
  docker run -v ./data:/app/data -v ./config.yaml:/app/config.yaml inspectarr

- The entry point is inspectarr.py (renamed from watchdog.py on 2026-06-07).
  The Dockerfile still references watchdog.py and needs updating before
  Docker deployment:  CMD ["python", "inspectarr.py"]

---

## Git Remotes

Both remotes configured on Optiplex at /home/o51r15/scripts/inspectarr/

| Remote name | URL | Notes |
|---|---|---|
| origin | https://git.sickbot.org/o51r15/inspectarr.git | Gitea (primary) |
| github | https://github.com/o51r15/inspectarr.git | GitHub (mirror) |

**Initial commit:** 43c7861 — "initial commit: inspectarr v1" (2026-06-07)
**Branch:** main

### Pushing updates (run from Optiplex)
```bash
cd /home/o51r15/scripts/inspectarr
source venv/bin/activate
git add .
git commit -m "your message"
git push origin main      # Gitea
git push github main      # GitHub
```

### GitHub email privacy note
GitHub blocks pushes that expose private email addresses.
The global git config on the Optiplex was updated to use the GitHub
no-reply address to resolve this:
  git config --global user.email "XXXXXXXX+o51r15@users.noreply.github.com"
  (replace XXXXXXXX with the actual numeric prefix from github.com/settings/emails)


---

## Git Author Cleanup (2026-06-07)

Early commits were authored with the private gmail address (kermit.01@gmail.com)
and a placeholder no-reply number (12345678+...), which GitHub resolved to an
unrelated user (shubh2294) as a phantom contributor.

Fixed by setting the correct no-reply address and rewriting all commit history:
  git config --global user.email "5066225+o51r15@users.noreply.github.com"
  git filter-branch -f --env-filter '
    CORRECT_EMAIL="5066225+o51r15@users.noreply.github.com"
    export GIT_COMMITTER_EMAIL="$CORRECT_EMAIL"
    export GIT_AUTHOR_EMAIL="$CORRECT_EMAIL"
  ' --tag-name-filter cat -- --branches --tags
  git push origin main --force
  git push github main --force

**The correct GitHub no-reply address for this account is:**
  5066225+o51r15@users.noreply.github.com

GitHub contributor display can take several minutes to refresh after a force push.

---

## WEB UI — v1 (2026-06-07)

A Flask-based web interface was added. The core (core/) was extended but its
design principle is intact: core has zero awareness of the UI.

### New file structure
```
inspectarr/
├── web.py                        # Flask entry point + app factory + scheduler boot
├── ui/
│   ├── __init__.py
│   ├── scheduler.py              # Background daemon thread
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── dashboard.py          # GET / and /status (JSON poll)
│   │   ├── config.py             # GET/POST /config, /config/save, /config/test/*
│   │   ├── logs.py               # GET /logs, /logs/data, POST /logs/clear
│   │   └── scheduler.py          # /scheduler, /scheduler/toggle, /run, /status
│   ├── templates/
│   │   ├── base.html             # Sidebar nav + dark theme layout
│   │   ├── dashboard.html
│   │   ├── config.html
│   │   ├── logs.html
│   │   └── scheduler.html
│   └── static/
│       ├── style.css             # Dark theme (GitHub-dark inspired palette)
│       └── app.js                # Polling, rules builder, test-connection, log refresh
```

### Pages
- Dashboard (/) — scheduler status, last-scan stats (checked/flagged/actioned),
  last flagged torrent, recent run history. JS polls /status every 5s for live updates.
- Scheduler (/scheduler) — daemon status, poll interval, last/next run, run history,
  Start/Stop/Run Now controls.
- Logs (/logs) — paginated JSON Lines viewer (100/page), level filter
  (ALL/ACTION/ERROR/DRY_RUN/INFO/DEBUG), color-coded badges, auto-refresh toggle
  (10s), clear-log button.
- Config (/config) — full form-based editor for every config option, plus a
  raw-YAML edit mode toggle for advanced users. Test Connection buttons for
  qBittorrent and Sonarr. Rules are a dynamic add/remove builder with a tag-style
  bad-extensions input.

### Scheduler daemon (ui/scheduler.py)
- Runs as a background thread inside the Flask process (single container, no
  separate process needed).
- Config is reloaded from disk before EVERY scan, so UI config changes take
  effect on the next cycle with no restart.
- Starts OFF — must be enabled from the dashboard or scheduler page.
- start() / stop() / trigger() (run-now) / get_status().
- Keeps last_result + run_history (last 10 runs) in memory for the dashboard.

### Core changes made to support the UI
- core/config.py: added WebConfig dataclass (port, default 8585), added
  poll_interval_seconds (default 300) to AppConfig, wired both into _parse_config.
  AppConfig field ordering kept valid (non-default fields before defaulted ones).
- core/scanner.py:
  - run_scan() now RETURNS a stats dict {torrents_checked, flagged, actioned,
    last_flagged} and writes a "scan_complete" log event. This is useful for any
    consumer, not just the UI.
  - _evaluate_torrent() now returns (flagged, actioned) tuple.
  - startup() was SPLIT: startup() does full startup (prune + log + Pushover
    startup notification) for CLI and the daemon's FIRST scan only; prepare()
    does prune-only (no notification) and is called on every subsequent daemon
    cycle. This prevents the scheduler from firing a startup notification on
    every poll interval.

### New config keys (added to config.example.yaml)
```yaml
poll_interval_seconds: 300   # daemon scan interval (CLI single-shot ignores this)
web:
  port: 8585
```

### New dependency
- flask>=3.0.0 (added to requirements.txt)

### Entry points
- web.py   → Flask UI + scheduler daemon (new primary for Docker)
- inspectarr.py → CLI single-shot (unchanged, still works)

### Deployment notes for the web UI
On the Optiplex:
```bash
cd /home/o51r15/scripts/inspectarr
source venv/bin/activate
pip install -r requirements.txt      # installs flask
python3 web.py                       # serves on http://0.0.0.0:8585
```
Then open http://192.168.1.192:8585

IMPORTANT: web.py uses template_folder="ui/templates" / static_folder="ui/static"
which resolve relative to web.py's location. ALWAYS launch from the project root
(cd into the inspectarr dir first). The Docker CMD and any systemd unit must set
the working directory to /app (Docker) or the project dir accordingly.

### Code review pass (2026-06-07) — issues found and fixed before transfer
1. NOTIFICATION SPAM BUG (significant): scheduler called startup() every cycle,
   which would fire a Pushover "startup" notification + startup log on every poll.
   Fixed by splitting startup()/prepare() as described above; daemon calls
   startup() only on its first scan (is_first flag in _execute_scan).
2. __import__("datetime") hack in run_scan() replaced with a proper top-level
   import (from datetime import datetime, timezone) and the redundant
   torrents_checked counter was simplified to a single unconditional increment.

Verified OK during review: SonarrClient(url, api_key) and
QBittorrentClient(url, username, password) constructors match their call sites in
the test-connection routes; SQLite is thread-safe as used (fresh connection per
operation, each within its own thread); AppConfig dataclass field ordering is
valid; all UI route imports reference core.* correctly; no lingering "watchdog"
references in the Python.

### Web UI backlog (v2)
- Authentication (basic auth, config-driven — web.username / web.password)
- Radarr fields are present in the form but disabled (v2)
- Toast notifications for save/test feedback (currently inline text)
- Mobile responsive layout
- Persist run_history across restarts (currently in-memory only)


---

## FULL CODE REVIEW (2026-06-07) — findings + fixes

A complete file-by-file review was performed (every file read end to end,
execution paths traced). The following issues were found and FIXED on Thor.
Nothing was pushed; changes are local to E:\docker\configs\inspectarr.

### Fixed — retry subsystem correctness
1. Retry exhaustion never stopped (notification spam). get_due_retries() had no
   cap on attempt_count, so an exhausted entry (attempt_count >= max,
   resolved=0) kept being picked up when due, retried forever, and fired the
   "retry_exhausted" alert every cycle.
   FIX: get_due_retries(max_attempts) now filters AND attempt_count < max.
   Exhausted entries stop being retried; the exhaustion alert fires once.
   process_retries() passes self.config.retry.max_attempts.

2. Abort-mode items bypassed retry timing. With on_arr_failure=abort, a failed
   torrent stayed in qBit and was recorded action="failed". is_processed()
   treats only "deleted" as terminal, so run_scan() reprocessed it EVERY poll
   cycle, ignoring the retry interval and spamming error notifications.
   FIX: added StateManager.has_active_retry(hash) (True if an unresolved
   retry_queue row exists). _evaluate_torrent() now skips a torrent when
   retry.enabled and has_active_retry(h) — the retry queue owns the timing.

3. retry.enabled=false orphaned entries. _attempt_action queued retries even
   when retry was disabled, but process_retries() is skipped when disabled, so
   those entries were never handled (and with fix #2 would be skipped forever).
   FIX: the queue_retry calls in _attempt_action are now gated by
   self.config.retry.enabled. When retry is disabled, failures are recorded as
   "failed" and reprocessed next cycle (old behavior), no orphan entries.

### Fixed — deployment
4. Dockerfile CMD referenced inspectarr.py which did not exist on Thor (only
   watchdog.py did), and pointed at the CLI single-shot. Updated:
   - CMD is now ["python", "web.py"] (web UI + built-in scheduler = the
     intended container entry point).
   - Added EXPOSE 8585.
   - Comment notes CLI one-off via: docker exec <container> python inspectarr.py

5. watchdog.py -> inspectarr.py rename drift. Thor still had watchdog.py while
   the git repo / Optiplex had inspectarr.py. RENAMED on Thor to inspectarr.py
   and updated its docstring/usage text. --daemon now points users to web.py;
   redundant `except (ValueError, KeyError, Exception)` simplified to
   `except Exception`.

### Fixed — robustness
6. web.py used relative template_folder="ui/templates" / static_folder, which
   breaks (TemplateNotFound) if launched from any dir other than the project
   root. FIX: paths are now absolute, derived from
   os.path.dirname(os.path.abspath(__file__)). CWD-independent (safe for
   systemd/Docker).

7. .gitignore was missing venv/ (Thor copy out of sync with Optiplex). Added.

### Reviewed — known limitations (NOT changed, acceptable for v1)
- Sonarr history fallback (blocklist_from_history): the downloadId filter on
  /api/v3/history IS valid (confirmed against the Servarr codebase —
  FindByDownloadId), so it will not fail an unrelated download. Residual: when
  the torrent is NOT in the queue, records[0] (most recent by date) may be a
  non-"grabbed" event of the correct download; /history/failed/{id} still
  targets the right download. This fallback path is untested in the field; the
  primary (queue) path is tested and solid.
- Scheduler manual "Run Now" can overlap a scheduled scan (the route guards
  with is_scanning() but it is a TOCTOU check; _execute_scan holds no scan
  lock). Two concurrent scans could collide on SQLite or both try to delete the
  same torrent. Low probability for single-user homelab use. v2 hardening:
  a real scan lock.
- _parse_config accesses raw["qbittorrent"] before validation, so a totally
  missing qbittorrent section raises a raw KeyError instead of the friendly
  validation message. Minor.
- Templates hardcode /static and / paths; would need url_for if ever served
  under a reverse-proxy subpath. v2.
- run_history / last_result are in-memory only (lost on restart). v2.

### Verified OK
- config.py dataclass field ordering valid (non-default before defaulted).
- SQLite thread-safe as used (fresh connection per op, each in its own thread).
- qBit 403 re-auth path correct; delete_torrent bool vs raise handled upstream.
- rules.py path handling correct on the Linux target (posixpath).
- notifier emergency-priority (2) retry/expire handling consistent.
- All UI route field names match config.py _form_to_config parsing.
- base.html static/nav paths correct for a root-mounted app.
- No remaining "watchdog" references in Python after the rename.

### Entry points (current, after review)
- web.py        -> Flask web UI + scheduler daemon (Docker CMD, primary)
- inspectarr.py -> CLI single-shot (manual/testing); --daemon points to web.py

### TRANSFER NOTE for next deployment to Optiplex
Thor is now the source of truth with all fixes. When transferring:
- inspectarr.py replaces inspectarr.py cleanly (no more watchdog.py).
- Bring over: inspectarr.py, web.py, ui/ (whole dir), core/config.py,
  core/scanner.py, core/state.py, Dockerfile, requirements.txt, .gitignore,
  config.example.yaml.
- On Optiplex: source venv/bin/activate && pip install -r requirements.txt
  (flask), then python web.py, open http://192.168.1.192:8585.
- If a stray watchdog.py still exists on the Optiplex from before, delete it:
  rm /home/o51r15/scripts/inspectarr/watchdog.py
