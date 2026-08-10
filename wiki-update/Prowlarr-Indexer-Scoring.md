Inspectarr can score your torrent indexers by health and automatically reorder them in Prowlarr so the best performers get the highest priority. NZB indexers are never touched.

## How it works

1. **Data collection** — Inspectarr pulls stats from Prowlarr's API: average response time, query/grab/RSS/auth counts and failures, and backoff status. It also tracks malicious content hits from its own scan results.
2. **Scoring** — Each indexer gets a health score from 0–100% based on four weighted sub-scores plus penalties and trend data.
3. **Reorder** — The healthiest indexer is assigned `base_priority`, the next gets `base_priority + 1`, and so on. Ignored indexers keep their current priority.
4. **Auto-manage** — Optionally, indexers that consistently score below a threshold are automatically disabled, then re-enabled after a cooldown.

## Health score formula

```
Health = rt×w_rt + fr×w_fr + m×w_m + gr×w_gr − backoff − trend
```

Clamped to [0, 100].

### Sub-scores

| Sub-score | Weight | Formula | Notes |
|---|---|---|---|
| Response time (`rt`) | 0.25 | `100 × (1 − log(1+avg_ms) / log(1+5000))` | Logarithmic curve — gentle on fast indexers, harsh on slow ones |
| Failure rate (`fr`) | 0.30 | `(1 − weighted_failure_rate) × 100` | Weighted across failure types (see multipliers below) |
| Malicious (`m`) | 0.20 | `100 − (malicious_hits/grabs) × 100 × penalty_per_hit` | Uses malicious *rate*, not raw count |
| Grab success (`gr`) | 0.25 | `grab_success_rate × 100` | Successful grabs ÷ total grabs |

### Failure type multipliers

Not all failures are equal. Auth failures are weighted 6× more heavily than RSS failures:

| Type | Multiplier | Rationale |
|---|---|---|
| Auth | 3.0 | Indexer is fundamentally broken |
| Grab | 2.0 | Direct impact on downloads |
| Query | 1.0 | Baseline |
| RSS | 0.5 | Least impactful |

### Penalties

| Penalty | Default | Effect |
|---|---|---|
| `backoff_penalty` | 20 | Flat deduction if the indexer is in Prowlarr's backoff state |
| `malicious_penalty_per_hit` | 10 | Multiplied by the malicious rate in the malicious sub-score |

### Trend

Inspectarr records each indexer's health score over time. The trend value (positive = improving, negative = declining) is added directly to the raw score before clamping. This means an indexer on a downward trend gets penalized even if its current metrics look okay, and a recovering indexer gets a small boost.

![Indexer Health page](images/indexers.jpg)

## AI scoring (Ollama)

When an Ollama endpoint and model are configured under `prowlarr.ollama`, Inspectarr sends all indexer data in a single batch to the LLM, which returns its own health scores and reasoning. The AI scores replace the deterministic ones.

If Ollama is unreachable or returns an error, the deterministic scores are used automatically — no manual intervention needed.

**LLM result caching:** To avoid hammering Ollama on every rescore, results are cached using a content hash of the input data. If the indexer stats haven't changed, the cached AI scores are reused until `cache_ttl_hours` expires (default 24 hours).

**LLM Logs:** Every AI scoring run is recorded to the database. The [LLM Logs](LLM-Logs) page under System shows the full report card with per-indexer reasoning, score trend charts over time, and a history of all runs.

## Auto-manage

When `prowlarr.auto_manage.enabled` is `true`, Inspectarr runs auto-manage checks after every scan cycle (independent of the reorder interval):

1. If an indexer scores below `disable_threshold` for `consecutive_runs` consecutive cycles, it is automatically disabled in Prowlarr.
2. After `cooldown_hours`, if the indexer's score has recovered above the threshold, it is automatically re-enabled.

You can always manually enable or disable an indexer from the [Indexers](http://localhost:8585/indexers) page — manual overrides are respected.

## Position model

Inspectarr uses a two-tier position model:

- **Protocol filter** — NZB indexers are filtered out at the data layer (`protocol == "torrent"`). They are never scored, reordered, or touched.
- **Ignored** — Check the "Ignore" box on the Indexers page to lock a torrent indexer at its current priority. Ignored indexers are skipped during reorder; free indexers fill the remaining priority numbers around them.

## Indexers page

The web UI Indexers page shows all torrent indexers with their current scores. Actions available:

- **Rescore** — Recalculates health scores without changing anything in Prowlarr
- **Reorder & Sync** — Applies scores as Prowlarr priorities, then syncs the new order to Sonarr, Radarr, and Whisparr
- **Ignore toggle** — Locks an indexer at its current priority
- **Reset** — Clears grabs, malicious hits, and cached scores for an indexer
- **Enable/Disable** — Manually toggle an indexer's enabled state in Prowlarr
