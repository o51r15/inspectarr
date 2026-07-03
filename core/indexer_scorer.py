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
from .prowlarr    import ProwlarrClient
from .state       import StateManager
from .config      import ProwlarrConfig
from .llm_client  import ollama_score_indexers


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

    def score_all(self, skip_ai: bool = False) -> list[dict]:
        """
        Score all torrent indexers. Returns a list of score dicts sorted by
        current Prowlarr priority (ascending = best first).
        Does NOT reorder — purely informational.

        skip_ai=True returns deterministic scores only (fast path for UI).
        """
        indexers       = self.prowlarr.get_torrent_indexers()
        backoff_map    = self.prowlarr.get_indexer_status()
        stats_by_id    = {s["indexer_id"]: s for s in self.state.get_all_indexer_stats()}
        prowlarr_stats = self.prowlarr.get_indexer_stats()
        scored = [self._score_one(idx, backoff_map, stats_by_id, prowlarr_stats) for idx in indexers]
        if not skip_ai:
            scored = self._apply_ai_scoring(scored, prowlarr_stats)
        # Persist scores so reorder can use them without rescoring
        for s in scored:
            if s["health_score"] is not None:
                self.state.save_cached_score(
                    s["id"], s["name"], s["health_score"],
                    s.get("ai_scored", False), s.get("ai_reasoning", ""),
                )
        return scored

    def reorder(self) -> int:
        """
        Reorder torrent indexers in Prowlarr using cached health scores.

        Uses the most recent scores from the database (written by score_all).
        Does NOT rescore — call score_all() first if fresh scores are needed.

        The best-scoring indexer is assigned `base_priority`, the next
        `base_priority + 1`, and so on. Ignored indexers keep their current
        priority value and are skipped.

        Returns the number of priority changes written to Prowlarr.
        """
        indexers = self.prowlarr.get_torrent_indexers()   # sorted by priority asc

        if not indexers:
            log.info("No torrent indexers found — nothing to reorder.")
            return 0

        cached      = self.state.get_cached_scores()
        stats_by_id = {s["indexer_id"]: s for s in self.state.get_all_indexer_stats()}

        scored = []
        for idx in indexers:
            iid      = idx["id"]
            db_stats = stats_by_id.get(iid, {})
            cache    = cached.get(iid, {})
            scored.append({
                "id":           iid,
                "name":         idx["name"],
                "priority":     idx.get("priority", 0),
                "health_score": cache.get("health_score"),
                "ignored":      bool(db_stats.get("ignored", False)),
                "_raw":         idx,
            })

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
    # AI scoring overlay
    # ------------------------------------------------------------------

    def _apply_ai_scoring(
        self, scored: list[dict], prowlarr_stats: dict[int, dict],
    ) -> list[dict]:
        """
        If Ollama is configured, send all indexer data in one batch and
        overwrite health_score with the AI result. On any failure, returns
        the original deterministic scores unchanged.
        """
        ocfg = self.cfg.ollama
        if not ocfg.url or not ocfg.model:
            return scored

        # Build the payload — everything we have per indexer
        payload = []
        for s in scored:
            ps = prowlarr_stats.get(s["id"], {})
            payload.append({
                "indexer_id":               s["id"],
                "name":                     s["name"],
                "deterministic_health_score": s["health_score"],
                "avg_response_ms":          s["avg_response_ms"],
                "success_rate_pct":         s["success_rate"],
                "total_queries":            ps.get("numberOfQueries", 0),
                "failed_queries":           ps.get("numberOfFailedQueries", 0),
                "total_grabs":              ps.get("numberOfGrabs", 0),
                "failed_grabs":             ps.get("numberOfFailedGrabs", 0),
                "total_rss_queries":        ps.get("numberOfRssQueries", 0),
                "failed_rss_queries":       ps.get("numberOfFailedRssQueries", 0),
                "total_auth_queries":       ps.get("numberOfAuthQueries", 0),
                "failed_auth_queries":      ps.get("numberOfFailedAuthQueries", 0),
                "malicious_hits":           s["malicious_hits"],
                "in_backoff":               s["in_backoff"],
                "total_records":            s["total_records"],
                "priority":                 s["priority"],
            })

        ai_results = ollama_score_indexers(
            payload, ocfg.url, ocfg.model, ocfg.timeout,
        )

        if not ai_results:
            log.info("AI scoring unavailable — using deterministic scores")
            return scored

        for s in scored:
            ai = ai_results.get(s["id"])
            if ai is not None:
                s["health_score"] = ai["health_score"]
                s["ai_scored"]    = True
                s["ai_reasoning"] = ai.get("reasoning", "")

        return scored

    # ------------------------------------------------------------------
    # Per-indexer scoring
    # ------------------------------------------------------------------

    def _score_one(
        self,
        idx: dict,
        backoff_map: dict[int, dict],
        stats_by_id: dict[int, dict],
        prowlarr_stats: dict[int, dict],
    ) -> dict:
        iid      = idx["id"]
        db_stats = stats_by_id.get(iid, {})
        ps       = prowlarr_stats.get(iid, {})

        # Per-indexer response time and query counts from /api/v1/indexerstats.
        # This replaces the per-indexer history fetch, which did not filter by
        # indexer correctly and returned a global average for every indexer.
        avg_ms = float(ps.get("averageResponseTime", 0))

        total_queries = (
            ps.get("numberOfQueries",     0) +
            ps.get("numberOfGrabs",       0) +
            ps.get("numberOfRssQueries",  0) +
            ps.get("numberOfAuthQueries", 0)
        )
        total_failed = (
            ps.get("numberOfFailedQueries",     0) +
            ps.get("numberOfFailedGrabs",       0) +
            ps.get("numberOfFailedRssQueries",  0) +
            ps.get("numberOfFailedAuthQueries", 0)
        )

        has_enough   = total_queries >= self.cfg.min_grabs_before_scoring
        success_rate = (1.0 - total_failed / total_queries) if total_queries > 0 else 1.0

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
            "total_records":   total_queries,
            "malicious_hits":  malicious_hits,
            "health_score":    health,
            "in_backoff":      in_backoff,
            "ignored":         bool(db_stats.get("ignored", False)),
            "pinned_position": db_stats.get("pinned_position"),
            "has_enough_data": has_enough,
            "ai_scored":       False,
            "ai_reasoning":    "",
            "_raw":            idx,   # full Prowlarr object needed for PUT
        }
