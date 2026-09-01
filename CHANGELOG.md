# Changelog

All notable changes to Inspectarr are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Unreleased]

### Fixed - A deletion is not a deletion until something checks

`delete_torrent()` returned `True` whenever the HTTP request did not raise.
qBittorrent answers `POST /torrents/delete` with `200` in every case - including
for a hash it does not hold, and when the payload could not be unlinked - so the
return value proved only that the request was sent. Inspectarr recorded
`qbit_success=1`, wrote a terminal `action='deleted'` row, and pushed a
notification saying the torrent was gone.

On the live deployment that produced four malicious `.exe` payloads sitting on
the share, ~4.8 GB, while `processed_hashes` reported nine clean deletions. Two
were files re-grabbed within twenty seconds of a genuine delete; two were
orphans whose torrent had already left the client, so the delete removed
nothing. Because `'deleted'` is terminal in `is_processed()`, none of them were
ever looked at again.

`delete_torrent()` now confirms its own work before reporting success:

- the client must actually hold the torrent when asked - deleting an unknown
  hash is a no-op, not a success
- the torrent must be gone from the client afterwards (polled, 20s)
- when `INSPECTARR_PATH_MAP` maps the client's paths onto ones Inspectarr can
  see, the payload must be gone from disk (polled, 10s - a CIFS share caches
  attributes, so a single `stat` right after the unlink can still return the
  old entry)

Anything short of that returns `False`, which routes the torrent onto the
failure path that already existed and was until now unreachable: a
`client_delete_failed` event, an error notification, `action='failed'`
(re-eligible on the next scan) and the retry queue.

Payload verification is skipped, not assumed, when unconfigured.
`docker-compose.yml` now mounts the download tree read-only and sets
`INSPECTARR_PATH_MAP`.

The same verification now covers **all three** clients. `delete_torrent()` was
returning `True` on any request that did not raise in `transmission.py` and
`deluge.py` as well; the confirmation logic (`_precheck_delete`,
`_confirm_deleted`, `_local_path`, `_payload_path`) now lives on
`AbstractTorrentClient` and every client uses it. Verified live against real
Transmission and Deluge daemons (real torrent deleted and confirmed gone from
client and disk; orphan hash reported as failure where stock returned `True`),
plus 21 stubbed cases across the three.


### Added — Batch Size Calibration

Inspectarr can now **measure** how many indexers your model handles reliably in one request, instead of assuming a number.

The old default of 25 per call was measured against a single model on a GPU host. It does not generalise. Measured on one CPU-only host, 39 indexers, identical settings:

| Model | 25 per call | 10 per call |
|---|---|---|
| `phi4:latest` | 39 of 39 | 39 of 39 |
| `securelogsummarizer:secure` | 39 of 39 | 39 of 39 |
| `gemma:latest` | **24 of 39 — 15 dropped** | 39 of 39 |
| `phi4-mini:latest` | **37 of 39 — 2 dropped** | 39 of 39 |

One machine, four models, opposite outcomes. Nothing errors when indexers are dropped: they quietly keep their deterministic score.

**Settings → AI** now has two actions rather than one, and they are not alternatives — **Calibrate does everything Quick Check does, and then measures the ceiling**:

- **Quick Check** (1–3 min) — discrimination, schema, context capacity
- **Calibrate** (10–40 min) — the same three, then a search for the largest safe batch size

The search descends 25 → 20 → 15 → 10 → 5 and takes the first size that comes back **clean twice in a row**. Twice, because silent omission is intermittent: one model passed a single-pass check at 39 indexers and dropped 15 the next day on the same host with nothing changed. A candidate must also finish comfortably inside your timeout, not merely produce the right answer — a batch of 25 once scored 39 of 39 while taking 269–295 s against a 300 s limit.

Indexers per scoring call is now a slider in Settings → AI, defaulted to whatever calibration measured. It is coloured by measurement: moving it at or below the measured ceiling is silent, and only crossing above it asks for confirmation.

A scoring call that finishes above 80% of your timeout now warns, and names the setting to change. Three of four calls on that CPU host were landing within 10 seconds of the limit, and the log said nothing until one broke.

### Added — Notification History

**System → Notifications** records every notification Inspectarr tried to send and whether it got through.

Apprise is fire-and-forget: once a push left there was no record it happened, none of one that *failed*, and none of one *suppressed* because notifications were switched off. "Nobody told me" and "notifications are off" looked identical.

Each row is `sent`, `failed`, `suppressed`, or `recorded` (a system event that is not itself a push), filterable by status and channel. A `sent` row means Apprise accepted the message — not that the service delivered it, and the page says so.

With digest mode enabled most events buffer and leave as a single `digest` push, so that is what the history shows; the individual events are inside that message.

### Changed — A Validation Result Belongs to a Host

`validated_models` records which Ollama server each verdict was earned against, along with the measured throughput and how much of the model sat on the GPU.

Previously, changing `prowlarr.ollama.url` silently carried every stored "supported" across to hardware the model had never been tested on. That is not a technicality — the same model and batch size took **436 seconds on one host and 237 on another**, and a model clean at 25 per call on one dropped indexers on the other.

Selecting a model whose only passing result came from a different server is now refused, and the message names both servers. Records written before this existed read as *unknown host*, not *wrong host* — absence of evidence is not evidence of a different machine, and treating it otherwise would invalidate an install's whole history on upgrade.

The model list is now colour-coded: untested, red (failed, forced, or validated elsewhere), amber (validated but not calibrated), green (validated and calibrated). Colour is never the only signal — every state carries a symbol and a word, because some browsers ignore colour inside a dropdown entirely.

### Fixed — A Broken AI Scoring Run Could Disable Healthy Indexers

On one run the model returned scores that contradicted its own reasoning:

| Indexer | Deterministic | AI | The model's own words |
|---|---|---|---|
| MVGroup Main | 90.4 | **0.0** | "Low data, but no failures." |
| Sexy-Pics | 93.4 | **0.0** | "Low data, but no failures." |

That run scored 8 of 37 indexers at exactly zero. Forty minutes later, auto-manage disabled two healthy indexers because of it.

Inspectarr now checks whether a scoring run is coherent **before** using it. The premise is that indexer health does not move for the whole fleet at once — if every indexer suddenly disagrees with its deterministic score, the model is wrong, not the fleet. Measured across eleven healthy runs the disagreement sat between 7.4 and 15.3 points; the failed run sat at 44.9.

A run is rejected when the batch disagrees by more than `max_mean_deviation` (25) on average, when more than 15% of indexers score exactly zero, or when every indexer gets the identical value. Zero is the end that matters because it is what trips auto-disable; a generous model costs nothing, so there is no matching check at 100.

**A rejected run keeps the deterministic scores and skips auto-manage entirely for that cycle.** Disabling needs evidence, and the run's evidence has just been declared untrustworthy. It is recorded in the notification history and sent as an error notification — silence is the worst outcome, because scoring quietly reverts to deterministic and nothing looks broken.

Separately, the AI may now move a single indexer at most `max_adjustment` (30) points from its deterministic score. Healthy runs disagree by 7–16 on average, so this rarely binds; it clips a lone outlier without muzzling the model.

```yaml
prowlarr:
  ollama:
    max_adjustment: 30.0        # per-indexer limit
    max_mean_deviation: 25.0    # whole-run limit; 0 disables
```

### Fixed — Auto-Manage Acted on the Wrong Scores, and Never Explained Itself

Two long-standing complaints, two different causes.

**An indexer would visibly drop below the threshold and stay enabled, then turn up disabled later.** Auto-manage ran its own deterministic scoring pass; the AI rescore — the numbers shown on the Indexers page — never evaluated disable or re-enable at all. The decision and the number on screen were never the same event. When a reorder was due, the cycle also scored twice, which could advance the consecutive-low counter twice for one cycle's evidence.

Scoring now happens once per cycle and auto-manage judges those numbers.

**An indexer would recover above the threshold and stay disabled.** That is `cooldown_hours` working as configured, but it was completely silent — indistinguishable from broken. Every deferral now logs the hours remaining and names the setting.

Also now visible rather than silent: waiting for more low runs (`n of N runs needed`), and a disable or re-enable that Prowlarr **rejects**. An already-disabled indexer no longer keeps incrementing its counter, and an unreadable `disabled_at` no longer strands one off permanently.

### Fixed — Notifications Arriving as Raw JSON

A daily summary went out as the model's raw output — a full JSON object, braces and all.

Asking for prose does not guarantee prose: a model fine-tuned on a structured task answers in its own schema regardless of the prompt. The code checked only that the request succeeded and the reply was not empty, both of which were true.

Inspectarr now checks the shape of the reply. Prose is sent as-is; JSON carrying a readable summary field has that field extracted; JSON with nothing readable, or a reply cut off mid-object, falls back to the built-in plain summary. Falling back is not a failure — the plain summary is always correct and strictly better than a wall of JSON — and the reason is logged.

Length is capped rather than merely requested, and the narration prompt now sends `num_ctx` like the scorer does. Without it Ollama applies its own default and silently truncates, and a model that lost its instructions is precisely the one that answers in the wrong format.

### Added — Ollama Model Updates

- **Registry update checking.** Inspectarr now tells you when a newer build of your model is published upstream, and offers a one-click pull.

  The check is one unauthenticated request and a hash — it never pulls anything to find out. Ollama's `/api/tags` digest turns out to be exactly `sha256(the raw registry manifest)`, verified across 8 installed models including a third-party namespaced one, so comparing them is cheap and side-effect-free.

  The badge says **which** update it found, because the two call for opposite reactions: *local* (the installed build changed since validation → re-validate) or *upstream* (a newer build exists → pull). "Could not reach the registry" reports as unavailable, never as up-to-date.

  `prowlarr.ollama.auto_update_check` (default on) controls this. It is the one check that leaves your network, so it is opt-out — air-gapped installs set it false and lose nothing else.

- **One-click model pull**, in the background with real byte-level progress. A pull is gigabytes; doing it in-request would tie up a worker and time out. Afterwards it says plainly that the new build is **not validated** — a different build of the same name can score differently, which is the whole reason the validation gate exists.

- **Cached AI scores can no longer go stale across a model rebuild.** The scoring cache key now includes the model's digest, not just its name. Previously `ollama pull qwen2.5:7b` could replace the build behind a name and the old build's scores would keep being served under the new one for a full cache TTL.

  In the key rather than an invalidation step, deliberately: invalidation only helps if a check happened to have run, whereas a key carrying the digest can never be hit by a stale entry at all.

### Fixed — AI Scoring on Small Context Windows

Scoring sent every indexer in one prettified JSON prompt. At 37 indexers that is ~5,500 tokens, so a 4k-context model lost the instructions and returned fabricated data rather than an error.

The prompt is now compact and short-keyed — ~3,000 tokens for the same 37 — and Inspectarr splits the work into several calls when needed, merging the results. Both are invisible: nothing about how you configure scoring changes.

**Fitting the window turned out not to be enough**, which is the more useful finding. Measured on `qwen2.5-coder:7b`:

| Indexers | Window | Result |
|---|---|---|
| 25 | 4k | all 25 scored |
| 30 | 4k | **echoes the input back** instead of scoring — in 3 seconds, window half full |
| 37 | 8k | well-formed JSON that **silently omits five** |

That last row is the one worth worrying about: it looks exactly like success. Nothing errors, the JSON parses, and the missing indexers quietly keep their deterministic score.

So batching is capped by item count as well as tokens, and the two limits are nowhere near each other. `prowlarr.ollama.max_indexers_per_call` (default 25) is that cap — a property of your model rather than of Inspectarr, and the validation run on the AI settings page is how to find the right number for yours.

`prowlarr.ollama.context_window` (default 4096) is also now sent to Ollama as `num_ctx`, so it uses the window being budgeted for instead of its own default.

The same 37 indexers that scored **0 of 37** at 4k now score **37 of 37**.

### Fixed — AI Reasoning Vanishing From the Indexers Page

If AI scoring was on, almost every indexer showed a health score with **no reasoning at all** — and the handful that did show reasoning all said some variation of "No data available."

Two passes write scores. The deterministic one runs after **every** scan cycle; the AI one runs once per `reorder_interval_hours`. The deterministic pass was overwriting the score, the AI flag and the reasoning unconditionally, so an AI verdict survived at most one scan before being blanked.

The exception is what made it confusing. An indexer with no data at all has no deterministic score, so the old guard skipped writing that row entirely — and froze its stored reasoning permanently. The bug therefore destroyed every informative AI summary and preserved only the useless ones, which is precisely the opposite of what you want, and why the symptom looked like a model problem rather than a persistence one.

A deterministic pass now leaves a stored AI verdict alone. Only an AI pass may replace or retire one, and when it does it writes the score, the flag and the reasoning together — a score from one source carrying an explanation from another is worse than either on its own.

Nothing to configure, and no database change. The next AI pass repopulates reasoning for every indexer, and it now stays there.

A follow-up guard came with it. `auto_manage` decides whether to disable an underperforming indexer, and it was relying on the same `None` to skip indexers with too little data to judge. Carrying a stored AI verdict into that decision filled the field in and removed the guard by accident, so indexers were briefly disabled on scores the model had derived from missing data. The check is now explicit: an indexer below `min_grabs_before_scoring` is never auto-disabled. Disabling on absent evidence is self-sealing anyway — a disabled indexer runs no queries, so it can never gather the data that would clear it.

---

## [v2.0.0] — 2026-08-25

The release the safety work was building toward. Inspectarr stops being a
tool that detects bad releases and deletes them, and becomes one that grades
what it finds, holds what it is unsure about, and records what happened next.

**Nothing breaks.** Every default reproduces v1.6.0 behaviour exactly:
severity thresholds ship at `LOW` (which collapses the quarantine band to
nothing), `operating_mode` ships at `automatic`, and AI ships **off** but
stays on for any install that already had it configured. Upgrading changes
what Inspectarr can do, not what it does.

The three things worth turning on deliberately:

| Set this | To get |
|---|---|
| `remediation.remediate_at: CRITICAL` | Archives and filename matches held for review; executables still deleted on sight |
| `remediation.operating_mode: monitor` | Watch what your rules would do before letting them do it |
| `prowlarr.ollama.enabled: true` | AI indexer scoring, once a model has passed validation |

New: 5 database tables, 8 config keys, a Quarantine page, an AI settings
pane, a `--test` CLI flag, and `GET /api/health`. All created automatically
on first run — there is no migration step.

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

### Added — Operating Modes


- **Operating modes** (`remediation.operating_mode`) — a single control for how far Inspectarr may act, shown in Settings → Rules and on a banner across every page whenever it is not set to automatic.

  | Mode | Behaviour |
  |---|---|
  | `monitor` | Findings are inspected, graded and recorded. Nothing is paused, blocklisted or deleted. |
  | `quarantine` | Nothing is deleted automatically — including by a quarantine timeout. Anything that would have been remediated is held for review. You can still delete deliberately from the review page. |
  | `automatic` | The remediation thresholds are applied exactly as written. **Default** — existing installs are unchanged. |

  There are three modes rather than the four originally planned: "Monitor" and "Dry Run" turned out to describe the same behaviour under two names. The `--dry-run` CLI flag remains the way to get it for a single run, and still takes precedence over the configured mode.

  The mode is a **ceiling**, not a preset. It never rewrites `min_severity` or `remediate_at` — those keep the values you gave them, so switching to monitor and back is lossless. It can only ever reduce an outcome, never escalate one. That property is asserted directly rather than inferred, because a "safety" control that could raise a decision would be worse than no control at all.

  Because both readings stay true at once, the settings page can be specific rather than vague: the band explainer says what the thresholds decide *and* what the mode does to that, including the case where the mode prevents any deletion at all.

- **`inspections.operating_mode` is now populated.** The column has existed and been NULL since structured inspections landed. Every terminal path — deleted, dry-run, recorded, failed — now records the posture the scan ran under, so a historical row can answer "why was this not deleted".

- **New log event `capped_by_operating_mode`**, carrying both `decision_before_mode` and `decision`. It is deliberately distinct from `below_severity_floor`: one means the thresholds said record, the other means the thresholds said more and the mode refused. Reporting the wrong one would send anyone debugging it to the wrong setting.

### Added — Replacement Outcome Tracking


- **Replacement tracking** (`remediation.track_replacements`, default on) — after a release is deleted, Inspectarr watches the *arr to see whether a replacement arrives, which indexer served it, and whether that one survives inspection too. Results appear on **Indexers → Stats**, grouped by the indexer that served the *bad* release.

  | Outcome | Meaning |
  |---|---|
  | `imported` | A replacement was grabbed and imported cleanly. |
  | `rejected` | The replacement was itself flagged — the cycle repeated. |
  | `abandoned` | The window closed without a conclusion. |

  The useful distinction is one the health score cannot draw: an indexer whose bad releases are quickly replaced by good ones is a very different proposition from one whose bad releases are never replaced at all. Watches are opened at the moment of *rejection*, so "nothing ever arrived" is a recorded answer rather than merely an absence of data.

  Purely observational — it never causes or prevents an action.

- **Recovery rate per indexer**, counting settled cases only. Watches still open are excluded rather than counted as failures, so a fresh install shows a dash instead of a damning 0%.

- **New config keys:** `remediation.track_replacements`, `remediation.replacement_window_hours` (default 72). **New table:** `replacements`. **New log events:** `replacement_grabbed`, `replacement_imported`, `replacement_rejected`, `replacement_abandoned`.

- **Lidarr is excluded from replacement tracking** and says so explicitly rather than failing quietly. Detection, blocklisting, quarantine and everything else are unaffected. See below for why.

### Added — AI Settings & Model Validation


- **Dedicated Settings → AI pane** — AI is optional, so it got its own pane. Exposes `ollama.url`, `timeout`, `cache_ttl_hours`, and `update_check_hours`; four of six keys previously had no UI at all, `url` among them, so AI scoring could not be enabled without hand-editing `config.yaml`.
- **Master AI enable/disable switch**, shipping **off** by default. While off, indexer AI scoring, AI notification digests, and periodic summaries are all skipped and no Ollama request is made. Existing installs are not broken: `enabled` defaults to whether a URL is configured, so an install that worked before this key existed keeps working. An explicit `enabled: false` always wins.
- **Model validation engine** (`core/model_validator.py`) — three tests against the real scoring path: **discrimination** (good vs bad indexer, requires a ≥25-point spread, catches constant output and inverted scales), **schema compliance** (catches the echo bug and malformed scores), and **context capacity** at the real indexer count.
- **Validation UI with a hard gate** — `/config/ai/model` returns 409 unless the model passed; `force=true` requires an explicit "Apply anyway" confirmation and is recorded as `forced`, distinct from `failed`. Runs in a background thread with polling, because a multi-minute validation in-request would hold a waitress worker.
- **Model comparison table** listing every validated model with its test results and average response time.

### Added — CLI Connection Test


- **`inspectarr.py --test`** — tests every configured connection and exits, using the same checks as **System → Status** so the two can never disagree.

  ```
  Torrent Client (qbittorrent)  OK
  Sonarr                        OK
  Lidarr                        skipped (not configured)
  Prowlarr                      OK
  ```

  Three exit codes, because they mean different things: **0** everything configured is reachable, **1** something configured is not, **2** the config could not be loaded or failed validation. A disabled service reports as skipped and does not fail the run; a config with nothing configured at all exits 1 rather than reporting success at having verified nothing.

  Safe to run at any time against a live install: it is dispatched before anything opens the database or starts a scan.

- **`core/connections.py`** — the connection checks moved out of the web layer. They never contained any Flask, and a command-line test should not import a Flask blueprint to reach them.

### Performance


- **The config file is no longer reparsed several times per request.** Measured first: a parse costs 9.61 ms, of which 99.5% is `yaml.safe_load`. A single page render did **four parses of the same unchanged file** — about 38 ms, which was 52% of the dashboard's total time and 70% of the quarantine page's.

  The **parse** is cached, not the config object — callers legitimately mutate what they get back, so a shared instance would leak one request's changes into the next. Rebuilding the dataclasses costs 0.12 ms and keeps every caller's copy private.

  The cache revalidates against the file's `(mtime_ns, size, inode)` on every read, at 0.002 ms — so **editing `config.yaml` by hand still takes effect immediately**, exactly as before. A cache that only noticed the app's own saves would have turned that documented behaviour into a baffling bug. Saves invalidate explicitly as well.

  Result: `/` went 74 ms → 3.7 ms and `/quarantine` 41 ms → 2.4 ms. Pages dominated by outbound API calls, such as Indexers, are unchanged — config was never their bottleneck.

### Changed


- **LLM score cache key now includes model and system prompt.** It hashed indexer stats only, so swapping models served the previous model's scores under the new model's name for a full TTL. `llm_cache` gained a `model` column.
- **`OllamaConfig.is_active()`** is now the single question every consumer asks ("enabled AND url AND model"). The URL on disk is never blanked to represent "off"; disabling and re-enabling round-trips the configured value untouched.
- **System → Status** distinguishes "Ollama (disabled)" from "not configured".
- **New config keys:** `prowlarr.ollama.enabled`, `remediation.min_severity`, `remediation.remediate_at`, `remediation.severity_overrides`, `remediation.quarantine_timeout_minutes`, `remediation.quarantine_timeout_action`.
- **New tables:** `inspections`, `inspection_reasons`, `quarantine`, `validated_models`.

### Notes — the *arr history API


Finding a replacement means asking "what happened to this episode/movie", which needs history scoped to one media item — the global feed is 318,000 records on a real install.

The endpoint that does this is **different per app, and the symmetric guess is wrong in both directions.** Measured against live Sonarr v4 and Radarr v3:

| Call | Result |
|---|---|
| Sonarr `GET /history?episodeId=N` | Correctly scoped |
| Sonarr `GET /history/series?episodeId=N` | **episodeId ignored** — returns the whole series |
| Radarr `GET /history/movie?movieId=N` | Correctly scoped |
| Radarr `GET /history?movieId=N` | **movieId ignored** — returns unfiltered global history |

Both wrong forms return HTTP 200 with entirely plausible data; nothing in the response indicates the filter was dropped. So every response is verified to actually carry the id it was asked for, and rejected if not — a wrong answer here would be attributed to a real indexer as a real replacement. Lidarr stays off because it could not be verified against a live instance, and this is precisely the API where guessing has now been demonstrated wrong twice.

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

### Fixed — Audit of the Above


An adversarial review of this release found ten bugs in it. They shared
one shape: **every one lived in a code path that was not the scan path.**
The tests shipped alongside each feature all passed, because they were
written alongside that feature and looked where it was written.

- **The operating-mode ceiling was applied in one of three paths that delete torrents.** `operating_mode: quarantine` combined with a quarantine timeout set to `remediate` **deleted torrents on a timer** — in the one mode documented as never deleting anything. The retry queue had the same gap: a failure queued while in `automatic` still deleted after switching to `monitor`, which is exactly when someone reaches for that switch. Both now consult the ceiling.

  A person clicking **Delete** on the review page is deliberately still not capped. The mode governs *automatic* action; capping a deliberate human decision would make quarantine mode a queue you cannot empty.

- **Replacement tracking and indexer malicious-hit attribution were dead in quarantine mode.** Watches were opened only on the scan path, but in quarantine mode every remediation goes through the manual Delete button, which did neither. Turning on the new safety mode silently disabled both, and the Indexers tab would have shown nothing — indistinguishable from having found no bad releases. All three rejection paths now share one entry point.

- **Quarantine events vanished from the plain notification digest** — a scan that only quarantined sent "Scan complete — no events." The AI-narrated digest did see them, so the two paths disagreed. Held torrents now lead the digest, and one that could not be paused is flagged.

- **The Settings form silently stripped `quarantine` from `notify_on`.** The checkbox list omitted it while the save wrote the form values verbatim, so anyone who followed the docs and added it by hand lost it on their next save, with no error.

- **One-shot CLI runs never advanced the quarantine or replacement sweeps** — and that is the documented cron deployment, so a cron install accumulated watches it never resolved and never fired a timeout.

- **`was_imported()` credited imports it could not attribute**, so a hand-import or an unrelated release completing for the same episode was recorded as a clean replacement and fed to indexer reputation. Also fixed: two ISO timestamp dialects were compared as strings.

- Smaller: the replacement sweep's per-pass cap counted list positions rather than checks (starving due watches) and did not count failures at all (making an unreachable *arr unbounded); `/api/health?deps=1` kept a duplicate service list; a bare `remediation:` key in YAML returned HTTP 500 for the whole Settings page.

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
