"""
core/indexer_scorer.py — Prowlarr indexer health scoring and auto-reorder

Health score (0–100%):
  response_time_score = clamp(100 × (1 − avg_ms / WORST_MS), 0, 100)
  failure_rate_score  = (successful / total) × 100
  malicious_score     = clamp(100 − malicious_hits × penalty, 0, 100)

  Health = rt_score × w_rt + fr_score × w_fr + m_score × w_m − backoff_penalty
  Final value clamped to [0, 100].

Reorder model (torrent indexers only):
  All torrent indexers are scored. The best-scoring indexer is assigned
  `base_priority`, the next `base_priority + 1`, and so on. Ignored indexers
  keep their current priority value and are skipped — free indexers fill the
  remaining numbers around them.
"""
import logging
from .prowlarr import ProwlarrClient
from .state    import StateManager
from .config   import ProwlarrConfig


WORST_RESPONSE_MS = 5000.0
log = logging.getLogger("inspectarr")


# -------------------------------------------------------------------------
# Score calculation (pure function — easy to unit-test)
# -------------------------------------------------------------------------

def compute_health_score(
    avg_response_ms: float,
    success_rate: float,        # 0.0 – 1.0
    malicious_hits: int,
    in_backoff: bool,
    cfg: ProwlarrConfig,
) -> float:
    s = cfg.scoring
    rt_score = max(0.0, 100.0 * (1.0 - avg_response_ms / WORST_RESPONSE_MS))
    fr_score = success_rate * 100.0
    m_score  = max(0.0, 100.0 - malicious_hits * s.malicious_penalty_per_hit)
    raw = (
        rt_score * s.response_time_weight
        + fr_score * s.failure_rate_weight
        + m_score  * s.malicious_weight
    )
    if in_backoff:
        raw -= s.backoff_penalty
    return round(max(0.0, min(100.0, raw)), 1)


# -------------------------------------------------------------------------
# Scorer class
# -------------------------------------------------------------------------

class IndexerScorer:

    def __init__(
        self,
        prowlarr: ProwlarrClient,
        state: StateManager,
        cfg: ProwlarrConfig,
    ):
        self.prowlarr = prowlarr
        self.state    = state
        self.cfg      = cfg

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def score_all(self) -> list[dict]:
        """
        Score all torrent indexers. Returns a list of score dicts sorted by
        current Prowlarr priority (ascending = best first).
        Does NOT reorder — purely informational.
        """
        indexers    = self.prowlarr.get_torrent_indexers()
        backoff_map = self.prowlarr.get_indexer_status()
        stats_by_id = {s["indexer_id"]: s for s in self.state.get_all_indexer_stats()}
        return [self._score_one(idx, backoff_map, stats_by_id) for idx in indexers]

    def reorder(self) -> int:
        """
        Reorder torrent indexers in Prowlarr by health score.

        All torrent indexers are scored. The best-scoring indexer is assigned
        `base_priority`, the next `base_priority + 1`, and so on. Ignored
        indexers keep their current priority value and are skipped — the free
        indexers fill the remaining numbers around them.

        Returns the number of priority changes written to Prowlarr.
        """
        indexers    = self.prowlarr.get_torrent_indexers()   # sorted by priority asc
        backoff_map = self.prowlarr.get_indexer_status()
        stats_by_id = {s["indexer_id"]: s for s in self.state.get_all_indexer_stats()}

        if not indexers:
            log.info("No torrent indexers found — nothing to reorder.")
            return 0

        scored = [self._score_one(i, backoff_map, stats_by_id) for i in indexers]

        base = self.cfg.base_priority

        # Ignored indexers hold their current priority value and are never moved.
        ignored_priorities = {
            s["priority"] for s in scored if s["ignored"]
        }
        free = [s for s in scored if not s["ignored"]]

        # Sort free indexers: highest health first; no-data scores go to the end.
        free.sort(key=lambda x: (
            0 if x["health_score"] is not None else 1,
            -(x["health_score"] or 0),
        ))

        # Walk priority numbers upward from `base`, skipping any number an
        # ignored indexer already occupies, assigning each free indexer in turn.
        changed = 0
        next_priority = base
        for s in free:
            while next_priority in ignored_priorities:
                next_priority += 1
            new_priority = next_priority
            next_priority += 1

            if s["priority"] != new_priority:
                ok = self.prowlarr.set_indexer_priority(s["_raw"], new_priority)
                if ok:
                    changed += 1
                    self.state.update_last_reorder(s["id"])
                    log.info(
                        f"Reordered indexer '{s['name']}': "
                        f"priority {s['priority']} → {new_priority}"
                    )

        log.info(
            f"Prowlarr reorder complete — {changed} indexer(s) moved."
            if changed else
            "Prowlarr reorder complete — no changes needed."
        )
        return changed

    # ------------------------------------------------------------------
    # Per-indexer scoring
    # ------------------------------------------------------------------

    def _score_one(
        self,
        idx: dict,
        backoff_map: dict[int, dict],
        stats_by_id: dict[int, dict],
    ) -> dict:
        iid      = idx["id"]
        db_stats = stats_by_id.get(iid, {})

        fetch_failed = False
        try:
            history = self.prowlarr.get_indexer_history(
                iid, days=self.cfg.history_window_days
            )
        except Exception as exc:
            log.warning(f"Could not fetch history for indexer '{idx['name']}': {exc}")
            history = []
            fetch_failed = True

        total = len(history)
        has_enough = (not fetch_failed) and total >= self.cfg.min_grabs_before_scoring

        if total > 0:
            avg_ms       = sum(
                float(r.get("data", {}).get("elapsedTime", 0)) for r in history
            ) / total
            success_rate = sum(1 for r in history if r.get("successful", False)) / total
        else:
            avg_ms       = 0.0
            # No data with a successful fetch → assume healthy (fail-open).
            # Fetch failure → assume unhealthy so it isn't promoted on bad data.
            success_rate = 0.0 if fetch_failed else 1.0

        malicious_hits = db_stats.get("malicious_hits", 0)
        in_backoff     = iid in backoff_map

        health = (
            compute_health_score(avg_ms, success_rate, malicious_hits, in_backoff, self.cfg)
            if has_enough else None
        )

        return {
            "id":              iid,
            "name":            idx["name"],
            "priority":        idx.get("priority", 0),
            "avg_response_ms": round(avg_ms, 1),
            "success_rate":    round(success_rate * 100, 1),
            "total_records":   total,
            "malicious_hits":  malicious_hits,
            "health_score":    health,
            "in_backoff":      in_backoff,
            "ignored":         bool(db_stats.get("ignored", False)),
            "pinned_position": db_stats.get("pinned_position"),
            "has_enough_data": has_enough,
            "_raw":            idx,   # full Prowlarr object needed for PUT
        }
