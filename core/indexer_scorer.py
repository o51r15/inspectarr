"""
core/indexer_scorer.py — Prowlarr indexer health scoring and auto-reorder

Health score (0–100%):
  rt_score  = 100 × (1 − log(1+avg_ms) / log(1+WORST_MS))   [logarithmic curve]
  fr_score  = (1 − weighted_failure_rate) × 100               [auth 3×, grab 2×, query 1×, RSS 0.5×]
  m_score   = clamp(100 − (malicious_hits/grabs) × 100 × penalty, 0, 100)
  gr_score  = grab_success_rate × 100                         [distinct grab sub-score]

  Health = rt×w_rt + fr×w_fr + m×w_m + gr×w_gr − backoff − trend
  Final value clamped to [0, 100].

Reorder model (torrent indexers only):
  All torrent indexers are scored. The best-scoring indexer is assigned
  `base_priority`, the next `base_priority + 1`, and so on. Ignored indexers
  keep their current priority value and are skipped — free indexers fill the
  remaining numbers around them.
"""
import hashlib
import json
import math
import logging
from datetime import datetime, timezone
from .prowlarr    import ProwlarrClient
from .state       import StateManager
from .config      import ProwlarrConfig
from .llm_client  import ollama_score_indexers
from .ollama_registry import local_digest as ollama_local_digest


WORST_RESPONSE_MS = 5000.0

# A run scoring this share of indexers at exactly zero is not assessing them.
# The failed run measured here put 8 of 37 (22%) at 0.0 while its own
# reasoning for those rows read "Low data, but no failures."
#
# Zero is the end that matters: it is what trips auto-disable. A model being
# generous costs nothing, so there is deliberately no matching check at 100.
MAX_ZERO_FRACTION = 0.15

# Below this many comparable indexers there is not enough to judge a batch by,
# and a strict check would reject small legitimate runs.
MIN_COHERENCE_SAMPLE = 5


def assess_ai_coherence(scored, ai_results, max_mean_deviation):
    """
    Does this AI run look like an assessment, or like a model that lost the
    plot? Returns a verdict dict; never raises.

    Judged on the batch, not the indexer, because that is where the signal
    is. One indexer disagreeing with its deterministic score is ordinary and
    is what the AI is for. Thirty-seven disagreeing at once is not the fleet
    changing -- indexer health does not move together -- it is the model
    failing, and the deterministic scores are the trustworthy ones.

    Deliberately measured on the RAW AI output, before any clamping. Clamping
    first would cap the deviation at the clamp and destroy the very signal
    this reads.
    """
    pairs = [(s["health_score"], ai_results[s["id"]]["health_score"])
             for s in scored
             if s["id"] in ai_results
             and s.get("health_score") is not None
             and ai_results[s["id"]].get("health_score") is not None]

    v = {"accepted": True, "reason": None, "n": len(pairs),
         "mean_deviation": None, "zero_fraction": None}
    if len(pairs) < MIN_COHERENCE_SAMPLE:
        v["reason"] = "too few indexers to judge (%d)" % len(pairs)
        return v

    ai_vals = [a for _, a in pairs]
    v["mean_deviation"] = round(
        sum(abs(a - d) for d, a in pairs) / len(pairs), 1)
    v["zero_fraction"] = round(
        sum(1 for a in ai_vals if a == 0) / len(ai_vals), 3)

    if len(set(ai_vals)) == 1:
        v["accepted"] = False
        v["reason"] = ("every indexer scored %s -- the model returned one "
                       "value for the whole batch" % ai_vals[0])
    elif v["zero_fraction"] > MAX_ZERO_FRACTION:
        v["accepted"] = False
        v["reason"] = ("%.0f%% of indexers scored exactly 0 (%d of %d)"
                       % (100 * v["zero_fraction"],
                          sum(1 for a in ai_vals if a == 0), len(ai_vals)))
    elif max_mean_deviation and v["mean_deviation"] > max_mean_deviation:
        v["accepted"] = False
        v["reason"] = ("the whole batch disagrees with its deterministic "
                       "scores by %.1f points on average (limit %.0f)"
                       % (v["mean_deviation"], max_mean_deviation))
    return v
log = logging.getLogger("inspectarr")


# -------------------------------------------------------------------------
# Score calculation (pure function — easy to unit-test)
# -------------------------------------------------------------------------

def compute_health_score(
    avg_response_ms: float,
    weighted_failure_rate: float,  # 0.0 – 1.0 (weighted across failure types)
    malicious_rate: float,         # malicious_hits / total_grabs (0.0 – 1.0)
    grab_success_rate: float,      # 0.0 – 1.0
    in_backoff: bool,
    cfg: ProwlarrConfig,
    trend: float | None = None,
) -> float:
    s = cfg.scoring
    # Logarithmic response time curve — gentle on fast, harsh on slow
    if avg_response_ms <= 0:
        rt_score = 100.0
    else:
        rt_score = max(0.0, 100.0 * (1.0 - math.log1p(avg_response_ms) / math.log1p(WORST_RESPONSE_MS)))
    # Weighted failure rate (auth > grab > query > RSS)
    fr_score = (1.0 - weighted_failure_rate) * 100.0
    # Malicious rate instead of raw count
    m_score = max(0.0, 100.0 - malicious_rate * 100.0 * s.malicious_penalty_per_hit)
    # Grab success as distinct sub-score
    gr_score = grab_success_rate * 100.0
    raw = (
        rt_score * s.response_time_weight
        + fr_score * s.failure_rate_weight
        + m_score  * s.malicious_weight
        + gr_score * s.grab_success_weight
    )
    if in_backoff:
        raw -= s.backoff_penalty
    if trend is not None:
        raw += trend
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
        indexers       = self.prowlarr.get_torrent_indexers(include_disabled=True)
        backoff_map    = self.prowlarr.get_indexer_status()
        stats_by_id    = {s["indexer_id"]: s for s in self.state.get_all_indexer_stats()}
        prowlarr_stats = self.prowlarr.get_indexer_stats()
        scored = [self._score_one(idx, backoff_map, stats_by_id, prowlarr_stats) for idx in indexers]
        if not skip_ai:
            scored = self._apply_ai_scoring(scored, prowlarr_stats)

        # Persist scores and record history for trend analysis.
        #
        # BUG-21: a deterministic-only pass must never overwrite a stored AI
        # verdict. This path runs after EVERY scan cycle (auto-manage), while
        # the AI pass runs only once per reorder_interval_hours. Persisting
        # deterministic values unconditionally meant an AI score and its
        # reasoning survived at most one scan before being blanked, so the
        # Indexers page showed empty reasoning for nearly every indexer.
        #
        # The only AI text that survived belonged to indexers with no data at
        # all: their deterministic health_score is None, so the old
        # "is not None" guard skipped the write and froze their reasoning
        # forever. That is why the surviving summaries all read like
        # "No data available" -- the bug preserved exactly the least useful
        # verdicts and destroyed all the others.
        #
        # The three AI fields are written as a SET or not at all. A
        # health_score from one source carrying reasoning from another is
        # worse than either alone, because it reads as an explanation of a
        # number it does not explain.
        prior = self.state.get_cached_scores()

        for s in scored:
            existing = prior.get(s["id"])
            ai_held  = bool(existing and existing["ai_scored"])

            if skip_ai and ai_held:
                # Keep the AI verdict whole, and reflect it back to the caller
                # so auto_manage and the UI act on the same number the DB holds.
                s["health_score"] = existing["health_score"]
                s["ai_scored"]    = True
                s["ai_reasoning"] = existing["ai_reasoning"]
                continue

            if s["health_score"] is None:
                # Not enough data to score. Only an AI pass may clear a stored
                # AI verdict: reaching here with not-skip_ai means the model
                # ran and declined to score this indexer, so the old verdict no
                # longer stands and must not be displayed as if it were fresh.
                if ai_held:
                    self.state.save_cached_score(s["id"], s["name"], None, False, "")
                continue

            self.state.save_cached_score(
                s["id"], s["name"], s["health_score"],
                s.get("ai_scored", False), s.get("ai_reasoning", ""),
            )
            self.state.record_score_history(s["id"], s["health_score"])
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
    # Auto-manage: disable/re-enable indexers based on health
    # ------------------------------------------------------------------

    def auto_manage(self, scored: list[dict]) -> dict:
        """
        After scoring, check each indexer against the auto-manage rules.
        Disable indexers below threshold for N consecutive runs.
        Re-enable auto-disabled indexers after cooldown if score recovered.
        Returns {"disabled": [...], "re_enabled": [...]}.
        """
        am = self.cfg.auto_manage
        if not am.enabled:
            return {"disabled": [], "re_enabled": []}

        disabled = []
        re_enabled = []

        for s in scored:
            if s["health_score"] is None or s.get("ignored"):
                continue

            # BUG-22: never auto-disable an indexer we have no data about.
            #
            # This guard used to be implicit and accidental. An indexer below
            # min_grabs_before_scoring gets health_score = None from
            # _score_one, so the check above skipped it. BUG-21 changed
            # score_all to carry a stored AI verdict into the returned dict,
            # which filled that field in and silently removed the guard --
            # three indexers were auto-disabled on the next scan for scores
            # the model had derived from missing data ("invalid performance
            # data", "no data to evaluate").
            #
            # Disabling on absent evidence is self-sealing: a disabled indexer
            # runs no queries, so it can never accumulate the data that would
            # clear it. The threshold is meant to catch indexers that perform
            # badly, not ones we have not measured yet.
            if not s.get("has_enough_data", True):
                continue

            iid = s["id"]
            ams = self.state.get_auto_manage_state(iid)

            if s["health_score"] < am.disable_threshold:
                if ams.get("disabled_by_auto"):
                    # Already off by our own hand. Counting further low runs
                    # measures nothing -- it cannot be disabled twice, and the
                    # figure would only inflate the message when it eventually
                    # recovers.
                    continue
                count = self.state.increment_consecutive_low(iid)
                if count >= am.consecutive_runs:
                    ok = self.prowlarr.set_indexer_enabled(s["_raw"], False)
                    if ok:
                        self.state.mark_auto_disabled(iid)
                        disabled.append(s["name"])
                        log.warning(
                            f"Auto-disabled indexer '{s['name']}' — "
                            f"score {s['health_score']} below {am.disable_threshold} "
                            f"for {count} consecutive run(s)"
                        )
                    else:
                        log.warning(
                            f"Wanted to disable '{s['name']}' "
                            f"(score {s['health_score']} below "
                            f"{am.disable_threshold}) but Prowlarr rejected "
                            f"the change"
                        )
                else:
                    # Below the line but not yet for long enough. Without this
                    # the wait is invisible and reads as the threshold not
                    # working.
                    log.info(
                        f"Indexer '{s['name']}' scored {s['health_score']}, "
                        f"below {am.disable_threshold} "
                        f"({count} of {am.consecutive_runs} runs needed to "
                        f"disable)"
                    )
            else:
                self.state.reset_consecutive_low(iid)
                # Re-enable if it was auto-disabled and cooldown has passed.
                #
                # This branch used to be entirely silent when the cooldown had
                # not elapsed, which is indistinguishable from being broken: the
                # score visibly recovers, the indexer stays off, and nothing
                # anywhere says why. cooldown_hours is an anti-flap guard and
                # worth keeping, but a guard that never explains itself gets
                # reported as a bug -- so a deferral is now logged once per
                # pass, with the time remaining.
                if ams.get("disabled_by_auto") and ams.get("disabled_at"):
                    try:
                        disabled_dt = datetime.fromisoformat(ams["disabled_at"])
                        elapsed_h = (datetime.now(timezone.utc) - disabled_dt).total_seconds() / 3600
                    except (ValueError, TypeError):
                        # An unparseable timestamp must not strand an indexer
                        # off forever. Treat it as "cooldown already served":
                        # the score has recovered, which is the condition that
                        # actually matters, and the clock is only there to stop
                        # flapping.
                        log.warning(
                            f"Indexer '{s['name']}' has an unreadable "
                            f"disabled_at ({ams.get('disabled_at')!r}); "
                            f"treating its cooldown as elapsed"
                        )
                        elapsed_h = float(am.cooldown_hours)

                    if elapsed_h >= am.cooldown_hours:
                        ok = self.prowlarr.set_indexer_enabled(s["_raw"], True)
                        if ok:
                            self.state.clear_auto_disabled(iid)
                            re_enabled.append(s["name"])
                            log.info(
                                f"Auto-re-enabled indexer '{s['name']}' — "
                                f"score recovered to {s['health_score']}"
                            )
                        else:
                            # Prowlarr refused. Say so: the alternative is an
                            # indexer that looks eligible every pass and never
                            # comes back, with nothing in the log.
                            log.warning(
                                f"Wanted to re-enable '{s['name']}' "
                                f"(score {s['health_score']}, cooldown served) "
                                f"but Prowlarr rejected the change"
                            )
                    else:
                        log.info(
                            f"Indexer '{s['name']}' has recovered to "
                            f"{s['health_score']} but stays disabled for "
                            f"another {am.cooldown_hours - elapsed_h:.1f}h "
                            f"(auto_manage.cooldown_hours = {am.cooldown_hours})"
                        )

        return {"disabled": disabled, "re_enabled": re_enabled}

    # ------------------------------------------------------------------
    # AI scoring overlay
    # ------------------------------------------------------------------

    def _apply_ai_scoring(
        self, scored: list[dict], prowlarr_stats: dict[int, dict],
    ) -> list[dict]:
        """
        If Ollama is configured, send all indexer data in one batch and
        overwrite health_score with the AI result. Uses content-hash caching
        to skip Ollama when input hasn't changed within the TTL.
        On any failure, returns the original deterministic scores unchanged.
        """
        ocfg = self.cfg.ollama
        # is_active() covers the master switch as well as url/model being set.
        if not ocfg.is_active():
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
                "weighted_success_rate_pct": s["success_rate"],
                "grab_success_pct":         s.get("grab_success", 0),
                "total_queries":            ps.get("numberOfQueries", 0),
                "failed_queries":           ps.get("numberOfFailedQueries", 0),
                "total_grabs":              ps.get("numberOfGrabs", 0),
                "failed_grabs":             ps.get("numberOfFailedGrabs", 0),
                "total_rss_queries":        ps.get("numberOfRssQueries", 0),
                "failed_rss_queries":       ps.get("numberOfFailedRssQueries", 0),
                "total_auth_queries":       ps.get("numberOfAuthQueries", 0),
                "failed_auth_queries":      ps.get("numberOfFailedAuthQueries", 0),
                "malicious_hits":           s["malicious_hits"],
                "malicious_rate_pct":       s.get("malicious_rate", 0),
                "in_backoff":               s["in_backoff"],
                "total_records":            s["total_records"],
                "priority":                 s["priority"],
                "trend":                    s.get("trend"),
            })

        # Content-hash cache: skip Ollama if identical input was scored recently.
        #
        # The key MUST include the model and system prompt, not just the
        # indexer stats. Keying on stats alone meant swapping models returned
        # the old model's cached scores under the new model's name, silently
        # invalidating any A/B comparison between models for a full TTL.
        #
        # It must also include the model's DIGEST, for the same reason one
        # level down: `ollama pull qwen2.5:7b` can replace the build behind a
        # name without the name changing. Without the digest that produces a
        # byte-identical key, and the previous build's scores are served
        # under the new build's name until the TTL expires.
        #
        # In the key rather than invalidated on detection, deliberately.
        # Invalidation only helps if the update check happened to have run;
        # a key carrying the digest cannot be hit by a stale entry at all.
        #
        # A digest we cannot read (host down, model absent) becomes None and
        # simply participates as None -- the cache still works, it just does
        # not gain this protection for that run. Failing the scan over an
        # advisory lookup would be the worse trade.
        model_digest = ollama_local_digest(ocfg.url, ocfg.model)
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "payload":       payload,
                    "model":         ocfg.model,
                    "model_digest":  model_digest,
                    "system_prompt": ocfg.system_prompt,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

        cached = self.state.get_llm_cache(content_hash, ocfg.cache_ttl_hours)
        if cached:
            log.info("AI scoring cache hit — reusing previous result")
            ai_results = json.loads(cached)
        else:
            previous_model = self.state.get_recent_cache_model()
            if previous_model and previous_model != ocfg.model:
                log.info(
                    f"AI scoring model changed ({previous_model} -> "
                    f"{ocfg.model}) — cache bypassed, scoring fresh"
                )
            ai_results_raw = ollama_score_indexers(
                payload, ocfg.url, ocfg.model, ocfg.timeout,
                custom_prompt=ocfg.system_prompt,
                context_window=getattr(ocfg, "context_window", 4096),
                max_indexers_per_call=getattr(
                    ocfg, "max_indexers_per_call", 25),
            )
            if not ai_results_raw:
                log.info("AI scoring unavailable — using deterministic scores")
                return scored
            # Cache the result
            self.state.save_llm_cache(
                content_hash,
                json.dumps({int(k): v for k, v in ai_results_raw.items()}),
                model=ocfg.model,
            )
            ai_results = ai_results_raw

        # Convert keys back to int if loaded from cache (JSON keys are strings)
        if isinstance(ai_results, dict):
            ai_results = {int(k): v for k, v in ai_results.items()}

        # Is this run trustworthy at all? Checked before anything is applied,
        # and on the raw values -- see assess_ai_coherence.
        verdict = assess_ai_coherence(
            scored, ai_results, getattr(ocfg, "max_mean_deviation", 25.0))
        self.last_ai_verdict = verdict
        if not verdict["accepted"]:
            log.error(
                "AI scoring run REJECTED: %s. Keeping deterministic scores "
                "for all %d indexers. Auto-manage is skipped this cycle -- "
                "nothing will be disabled on evidence this run produced.",
                verdict["reason"], len(scored))
            return scored

        max_adj = float(getattr(ocfg, "max_adjustment", 30.0) or 0)
        clamped = []

        log_entries = []
        for s in scored:
            ai = ai_results.get(s["id"])
            if ai is not None:
                raw = ai["health_score"]
                value = raw
                # Clamp AFTER the batch verdict, never before. An indexer with
                # no deterministic score has no baseline to clamp against, so
                # it is left alone -- auto_manage skips those anyway (B-25).
                if max_adj and s.get("health_score") is not None:
                    lo = s["health_score"] - max_adj
                    hi = s["health_score"] + max_adj
                    value = max(lo, min(hi, raw))
                    if value != raw:
                        clamped.append((s["name"], raw, value,
                                        s["health_score"]))
                log_entries.append({
                    "indexer_id": s["id"],
                    "indexer_name": s["name"],
                    "deterministic_score": s["health_score"],
                    "ai_score": value,
                    "ai_reasoning": ai.get("reasoning", ""),
                })
                s["health_score"] = value
                s["ai_scored"]    = True
                s["ai_reasoning"] = ai.get("reasoning", "")

        if clamped:
            # Worth a line each: a clamp means the model wanted to move an
            # indexer further than we allow, which is a weaker version of the
            # same failure the batch gate catches.
            for name, raw, value, det in clamped[:10]:
                log.warning(
                    "Clamped '%s': AI said %s against a deterministic %s; "
                    "limited to %s (max_adjustment %.0f)",
                    name, raw, det, value, max_adj)
            if len(clamped) > 10:
                log.warning("... and %d more clamped this run",
                            len(clamped) - 10)

        # Persist the scoring run for the LLM Logs page
        if log_entries:
            try:
                self.state.record_llm_scoring_run(
                    log_entries, ocfg.model, cache_hit=cached is not None,
                )
            except Exception as exc:
                log.warning(f"Failed to record LLM scoring log: {exc}")

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

        avg_ms = float(ps.get("averageResponseTime", 0))
        s = self.cfg.scoring

        # Raw counts per failure type
        n_query      = ps.get("numberOfQueries", 0)
        n_grab       = ps.get("numberOfGrabs", 0)
        n_rss        = ps.get("numberOfRssQueries", 0)
        n_auth       = ps.get("numberOfAuthQueries", 0)
        nf_query     = ps.get("numberOfFailedQueries", 0)
        nf_grab      = ps.get("numberOfFailedGrabs", 0)
        nf_rss       = ps.get("numberOfFailedRssQueries", 0)
        nf_auth      = ps.get("numberOfFailedAuthQueries", 0)

        total_queries = n_query + n_grab + n_rss + n_auth

        # Weighted failure rate: each type multiplied by its severity weight
        weighted_failed = (
            nf_query * s.query_failure_mult
            + nf_grab * s.grab_failure_mult
            + nf_rss  * s.rss_failure_mult
            + nf_auth * s.auth_failure_mult
        )
        weighted_total = (
            n_query * s.query_failure_mult
            + n_grab * s.grab_failure_mult
            + n_rss  * s.rss_failure_mult
            + n_auth * s.auth_failure_mult
        )
        weighted_failure_rate = (weighted_failed / weighted_total) if weighted_total > 0 else 0.0

        # Malicious rate: hits per grab (not raw count)
        malicious_hits = db_stats.get("malicious_hits", 0)
        malicious_rate = (malicious_hits / n_grab) if n_grab > 0 else 0.0

        # Grab success rate as distinct signal
        grab_success_rate = ((n_grab - nf_grab) / n_grab) if n_grab > 0 else 1.0

        has_enough = total_queries >= self.cfg.min_grabs_before_scoring
        in_backoff = iid in backoff_map

        trend = self.state.get_score_trend(iid) if has_enough else None
        health = (
            compute_health_score(
                avg_ms, weighted_failure_rate, malicious_rate,
                grab_success_rate, in_backoff, self.cfg, trend,
            )
            if has_enough else None
        )

        return {
            "id":              iid,
            "name":            idx["name"],
            "priority":        idx.get("priority", 0),
            "avg_response_ms": round(avg_ms, 1),
            "success_rate":    round((1.0 - weighted_failure_rate) * 100, 1),
            "grab_success":    round(grab_success_rate * 100, 1),
            "total_records":   total_queries,
            "malicious_hits":  malicious_hits,
            "malicious_rate":  round(malicious_rate * 100, 1),
            "health_score":    health,
            "trend":           trend,
            "in_backoff":      in_backoff,
            "ignored":         bool(db_stats.get("ignored", False)),
            "pinned_position": db_stats.get("pinned_position"),
            "has_enough_data": has_enough,
            "ai_scored":       False,
            "ai_reasoning":    "",
            "enabled":         idx.get("enable", True),
            "_raw":            idx,
        }
