import sqlite3
import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("inspectarr")


class StateManager:
    """
    Manages SQLite state (processed hashes + retry queue) and the
    JSON Lines action log. Single class owns all persistence.
    """

    def __init__(self, db_path: str, log_path: str, retention_days: int):
        self.db_path = db_path
        self.log_path = log_path
        self.retention_days = retention_days
        self._ensure_dirs()
        # BUG-13: the single shared connection is used from Flask request
        # threads, the scheduler loop, and manual-scan threads. The RLock
        # serializes every multi-statement operation; WAL + busy_timeout
        # (BUG-12) prevent "database is locked" errors across the separate
        # connections still created per Scanner/per request.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError as exc:
            log.warning(f"Could not set SQLite pragmas: {exc}")
        self._init_db()

    def close(self):
        """L-03: explicitly close the SQLite connection."""
        try:
            self._db.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_dirs(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        """Return the persistent shared connection."""
        return self._db

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_hashes (
                    hash         TEXT PRIMARY KEY,
                    torrent_name TEXT,
                    category     TEXT,
                    rule_name    TEXT,
                    action       TEXT,
                    actioned_at  TEXT,
                    arr_success  INTEGER,
                    qbit_success INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retry_queue (
                    hash           TEXT PRIMARY KEY,
                    torrent_name   TEXT,
                    category       TEXT,
                    rule_name      TEXT,
                    attempt_count  INTEGER DEFAULT 0,
                    last_attempt   TEXT,
                    next_attempt   TEXT,
                    failure_reason TEXT,
                    resolved       INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_start       TEXT,
                    scan_end         TEXT,
                    duration_seconds REAL,
                    torrents_checked INTEGER,
                    flagged          INTEGER,
                    actioned         INTEGER,
                    error            TEXT,
                    last_flagged     TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS error_state (
                    context    TEXT PRIMARY KEY,
                    last_error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS indexer_stats (
                    indexer_id       INTEGER PRIMARY KEY,
                    indexer_name     TEXT,
                    malicious_hits   INTEGER DEFAULT 0,
                    last_reorder     TEXT,
                    ignored          INTEGER DEFAULT 0,
                    pinned_position  INTEGER,
                    last_scored      TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_torrents (
                    hash         TEXT PRIMARY KEY,
                    indexer_id   INTEGER,
                    indexer_name TEXT,
                    first_seen   TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS score_history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    indexer_id   INTEGER NOT NULL,
                    health_score REAL NOT NULL,
                    scored_at    TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_score_history_indexer
                ON score_history (indexer_id, scored_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_cache (
                    content_hash TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    scored_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auto_manage_state (
                    indexer_id          INTEGER PRIMARY KEY,
                    consecutive_low     INTEGER DEFAULT 0,
                    disabled_at         TEXT,
                    disabled_by_auto    INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_scoring_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    scored_at           TEXT NOT NULL,
                    indexer_id          INTEGER NOT NULL,
                    indexer_name        TEXT NOT NULL,
                    deterministic_score REAL,
                    ai_score            REAL NOT NULL,
                    ai_reasoning        TEXT NOT NULL DEFAULT '',
                    model_used          TEXT NOT NULL DEFAULT '',
                    cache_hit           INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_log_scored
                ON llm_scoring_log (scored_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_log_indexer
                ON llm_scoring_log (indexer_id, scored_at DESC)
            """)
            conn.commit()
        # Migrate indexer_stats to add total_grabs if not present
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "ALTER TABLE indexer_stats ADD COLUMN total_grabs INTEGER DEFAULT 0"
                )
                conn.commit()
        except sqlite3.OperationalError as exc:
            # BUG-16: only "duplicate column" is expected — anything else is a
            # real migration failure and must not be silently swallowed.
            if "duplicate column" not in str(exc).lower():
                log.warning(f"indexer_stats migration failed (total_grabs): {exc}")
        # Migrate indexer_stats to add cached score columns
        for col, default in [
            ("health_score REAL", "NULL"),
            ("ai_scored INTEGER", "0"),
            ("ai_reasoning TEXT", "''"),
        ]:
            try:
                with self._lock, self._conn() as conn:
                    conn.execute(
                        f"ALTER TABLE indexer_stats ADD COLUMN {col} DEFAULT {default}"
                    )
                    conn.commit()
            except sqlite3.OperationalError as exc:
                # BUG-16: see above — log real failures, ignore duplicates.
                if "duplicate column" not in str(exc).lower():
                    log.warning(f"indexer_stats migration failed ({col}): {exc}")


    # ------------------------------------------------------------------
    # processed_hashes
    # ------------------------------------------------------------------

    def is_processed(self, hash: str) -> bool:
        """
        True if this hash was previously actioned (deleted or dry_run with
        dry_run still active). dry_run records are NOT skipped once dry_run
        is turned off — they re-evaluate on the next scan.
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT action FROM processed_hashes WHERE hash = ?", (hash,)
            ).fetchone()
        if row is None:
            return False
        # "dry_run" and "failed" are re-eligible; only "deleted" is terminal
        return row["action"] == "deleted"

    def record_action(
        self, hash: str, torrent_name: str, category: str,
        rule_name: str, action: str, arr_success: bool, qbit_success: bool
    ):
        with self._lock, self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_hashes
                    (hash, torrent_name, category, rule_name, action,
                     actioned_at, arr_success, qbit_success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (hash, torrent_name, category, rule_name, action,
                  _now_iso(), int(arr_success), int(qbit_success)))
            conn.commit()

    # ------------------------------------------------------------------
    # retry_queue
    # ------------------------------------------------------------------

    def queue_retry(
        self, hash: str, torrent_name: str, category: str,
        rule_name: str, failure_reason: str, interval_seconds: int
    ) -> int:
        """Upsert a retry entry. Returns the new attempt_count."""
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT attempt_count FROM retry_queue WHERE hash = ?", (hash,)
            ).fetchone()
            count = (existing["attempt_count"] + 1) if existing else 1
            now = _now_dt()
            next_dt = (now + timedelta(seconds=interval_seconds)).isoformat()
            conn.execute("""
                INSERT OR REPLACE INTO retry_queue
                    (hash, torrent_name, category, rule_name, attempt_count,
                     last_attempt, next_attempt, failure_reason, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (hash, torrent_name, category, rule_name, count,
                  now.isoformat(), next_dt, failure_reason))
            conn.commit()
        return count

    def get_due_retries(self, max_attempts: int) -> list[dict]:
        """
        Return retry entries that are due and NOT yet exhausted.
        Exhausted entries (attempt_count >= max_attempts) are excluded so we
        stop hammering them — they remain in the table as a permanent record.
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM retry_queue
                WHERE resolved = 0
                  AND next_attempt <= ?
                  AND attempt_count < ?
            """, (_now_iso(), max_attempts)).fetchall()
        return [dict(r) for r in rows]

    def get_all_unresolved_retries(self) -> list[dict]:
        """
        Return ALL unresolved retries regardless of timing or attempt count.
        Used by --retry-now to force-flush everything including exhausted entries.
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM retry_queue WHERE resolved = 0"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_active_retry(self, hash: str) -> bool:
        """
        True if an unresolved retry entry exists for this hash (pending OR
        exhausted). Used so the normal scan skips it and lets the retry queue
        own the timing — preventing reprocessing on every poll cycle.
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM retry_queue WHERE hash = ? AND resolved = 0",
                (hash,)
            ).fetchone()
        return row is not None

    def get_retry_count(self, hash: str) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM retry_queue WHERE hash = ?", (hash,)
            ).fetchone()
        return row["attempt_count"] if row else 0

    def resolve_retry(self, hash: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE retry_queue SET resolved = 1 WHERE hash = ?", (hash,)
            )
            conn.commit()


    # ------------------------------------------------------------------
    # run_history
    # ------------------------------------------------------------------

    def save_run(self, result: dict):
        """Persist a scan result. Keeps the latest 50 rows."""
        lf = result.get("last_flagged")
        with self._lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO run_history
                    (scan_start, scan_end, duration_seconds,
                     torrents_checked, flagged, actioned, error, last_flagged)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.get("scan_start"),
                result.get("scan_end"),
                result.get("duration_seconds"),
                result.get("torrents_checked"),
                result.get("flagged"),
                result.get("actioned"),
                result.get("error"),
                json.dumps(lf) if lf else None,
            ))
            # Trim to latest 50
            conn.execute("""
                DELETE FROM run_history
                WHERE id NOT IN (
                    SELECT id FROM run_history ORDER BY id DESC LIMIT 50
                )
            """)
            conn.commit()

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        """Return the most recent scan results, newest first."""
        with self._lock, self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM run_history ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("last_flagged"):
                try:
                    entry["last_flagged"] = json.loads(entry["last_flagged"])
                except (json.JSONDecodeError, TypeError):
                    entry["last_flagged"] = None
            results.append(entry)
        return results

    def get_total_stats(self) -> dict:
        """Return lifetime totals from processed_hashes (canonical record)."""
        with self._lock, self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS total FROM processed_hashes
            """).fetchone()
        total = row["total"] if row else 0
        return {"total_flagged": total, "total_actioned": total}

    def get_last_flagged_torrent(self) -> dict | None:
        """Return the most recently flagged torrent from processed_hashes."""
        with self._lock, self._conn() as conn:
            row = conn.execute("""
                SELECT torrent_name, rule_name, actioned_at
                FROM processed_hashes
                ORDER BY actioned_at DESC LIMIT 1
            """).fetchone()
        if not row:
            return None
        return {
            "torrent_name": row["torrent_name"],
            "rule":         row["rule_name"],
            "timestamp":    row["actioned_at"],
        }

    def get_last_detection(self) -> dict | None:
        """
        Return the last_flagged dict from the most recent scan that had
        a flagged torrent, regardless of how many clean scans have run since.
        Returns None if no flagged run exists.
        """
        with self._lock, self._conn() as conn:
            row = conn.execute("""
                SELECT last_flagged FROM run_history
                WHERE flagged > 0 AND last_flagged IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """).fetchone()
        if not row or not row["last_flagged"]:
            return None
        try:
            return json.loads(row["last_flagged"])
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------
    # error_state — dedup repeated identical error notifications
    # ------------------------------------------------------------------

    def get_error_state(self, context: str) -> Optional[str]:
        """Return the last recorded error for this context, or None."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT last_error FROM error_state WHERE context = ?", (context,)
            ).fetchone()
        return row["last_error"] if row else None

    def set_error_state(self, context: str, error: Optional[str]):
        """
        Record the current error for this context.
        Pass error=None to clear (e.g. when the operation succeeds again).
        """
        with self._lock, self._conn() as conn:
            if error is None:
                conn.execute(
                    "DELETE FROM error_state WHERE context = ?", (context,)
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO error_state (context, last_error) VALUES (?, ?)",
                    (context, error),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # indexer_stats — Prowlarr indexer health tracking
    # ------------------------------------------------------------------

    def get_all_indexer_stats(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM indexer_stats").fetchall()
        return [dict(r) for r in rows]

    def _upsert_indexer(self, conn, indexer_id: int, indexer_name: str):
        """Ensure a row exists for this indexer; update name if it changed."""
        conn.execute(
            "INSERT OR IGNORE INTO indexer_stats (indexer_id, indexer_name) VALUES (?, ?)",
            (indexer_id, indexer_name),
        )
        conn.execute(
            "UPDATE indexer_stats SET indexer_name = ? WHERE indexer_id = ?",
            (indexer_name, indexer_id),
        )

    def increment_malicious_hit(self, indexer_id: int, indexer_name: str) -> int:
        """Increment malicious_hits for this indexer. Returns the new count."""
        with self._lock, self._conn() as conn:
            self._upsert_indexer(conn, indexer_id, indexer_name)
            conn.execute(
                "UPDATE indexer_stats SET malicious_hits = malicious_hits + 1 WHERE indexer_id = ?",
                (indexer_id,),
            )
            row = conn.execute(
                "SELECT malicious_hits FROM indexer_stats WHERE indexer_id = ?",
                (indexer_id,),
            ).fetchone()
            conn.commit()
        return row["malicious_hits"] if row else 1

    def set_indexer_ignored(
        self,
        indexer_id: int,
        indexer_name: str,
        ignored: bool,
        pinned_position: int | None = None,
    ):
        """Set or clear the ignore/pin state for an indexer."""
        with self._lock, self._conn() as conn:
            self._upsert_indexer(conn, indexer_id, indexer_name)
            conn.execute(
                "UPDATE indexer_stats SET ignored = ?, pinned_position = ? WHERE indexer_id = ?",
                (int(ignored), pinned_position, indexer_id),
            )
            conn.commit()

    def update_last_reorder(self, indexer_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE indexer_stats SET last_reorder = ? WHERE indexer_id = ?",
                (_now_iso(), indexer_id),
            )
            conn.commit()

    def update_last_scored(self, indexer_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE indexer_stats SET last_scored = ? WHERE indexer_id = ?",
                (_now_iso(), indexer_id),
            )
            conn.commit()

    def save_cached_score(
        self,
        indexer_id: int,
        indexer_name: str,
        health_score: float | None,
        ai_scored: bool = False,
        ai_reasoning: str = "",
    ):
        """Persist a health score so reorder can use it without rescoring."""
        with self._lock, self._conn() as conn:
            self._upsert_indexer(conn, indexer_id, indexer_name)
            conn.execute(
                """UPDATE indexer_stats
                   SET health_score = ?, ai_scored = ?, ai_reasoning = ?, last_scored = ?
                   WHERE indexer_id = ?""",
                (health_score, int(ai_scored), ai_reasoning, _now_iso(), indexer_id),
            )
            conn.commit()

    def get_cached_scores(self) -> dict[int, dict]:
        """Return {indexer_id: {health_score, ai_scored, ai_reasoning}} for all scored indexers."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT indexer_id, health_score, ai_scored, ai_reasoning FROM indexer_stats WHERE health_score IS NOT NULL"
            ).fetchall()
        return {
            r["indexer_id"]: {
                "health_score": r["health_score"],
                "ai_scored": bool(r["ai_scored"]),
                "ai_reasoning": r["ai_reasoning"] or "",
            }
            for r in rows
        }

    def reset_indexer_stats(self, indexer_id: int):
        """
        Reset grab count, malicious hits, and cached health scores for a
        single indexer. Leaves the row in place (preserving ignored/pinned
        state) so the indexer re-enters scoring with a clean slate.
        """
        with self._lock, self._conn() as conn:
            conn.execute(
                """UPDATE indexer_stats
                   SET malicious_hits = 0,
                       total_grabs = 0,
                       health_score = NULL,
                       ai_scored = 0,
                       ai_reasoning = '',
                       last_scored = NULL
                   WHERE indexer_id = ?""",
                (indexer_id,),
            )
            conn.commit()

    def record_score_history(self, indexer_id: int, health_score: float):
        """Append a score snapshot for trend analysis. Keeps last 30 per indexer."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO score_history (indexer_id, health_score, scored_at) VALUES (?, ?, ?)",
                (indexer_id, health_score, _now_iso()),
            )
            # Trim to latest 30 per indexer
            conn.execute("""
                DELETE FROM score_history
                WHERE indexer_id = ? AND id NOT IN (
                    SELECT id FROM score_history
                    WHERE indexer_id = ? ORDER BY id DESC LIMIT 30
                )
            """, (indexer_id, indexer_id))
            conn.commit()

    def get_score_trend(self, indexer_id: int, window: int = 5) -> float | None:
        """
        Return a trend factor based on the last `window` score snapshots.
        Positive = improving, negative = declining, None = not enough data.
        Value is the slope of a simple linear regression, scaled to ±10 max.
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT health_score FROM score_history WHERE indexer_id = ? ORDER BY id DESC LIMIT ?",
                (indexer_id, window),
            ).fetchall()
        if len(rows) < 3:
            return None
        scores = [r["health_score"] for r in reversed(rows)]  # oldest first
        n = len(scores)
        x_mean = (n - 1) / 2.0
        y_mean = sum(scores) / n
        num = sum((i - x_mean) * (s - y_mean) for i, s in enumerate(scores))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        slope = num / den
        # Clamp to ±10 so trend never dominates the score
        return round(max(-10.0, min(10.0, slope)), 2)

    def get_score_history_all(self, limit: int = 30) -> list[dict]:
        """Return the last `limit` score snapshots per indexer, for charting."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT sh.indexer_id, ist.indexer_name, sh.health_score, sh.scored_at
                   FROM score_history sh
                   JOIN indexer_stats ist ON ist.indexer_id = sh.indexer_id
                   ORDER BY sh.scored_at ASC""",
            ).fetchall()
        return [dict(r) for r in rows]

    def increment_total_grabs(self, indexer_id: int, indexer_name: str) -> int:
        """Increment total_grabs for this indexer. Returns the new count."""
        with self._lock, self._conn() as conn:
            self._upsert_indexer(conn, indexer_id, indexer_name)
            conn.execute(
                "UPDATE indexer_stats SET total_grabs = total_grabs + 1 WHERE indexer_id = ?",
                (indexer_id,),
            )
            row = conn.execute(
                "SELECT total_grabs FROM indexer_stats WHERE indexer_id = ?",
                (indexer_id,),
            ).fetchone()
            conn.commit()
        return row["total_grabs"] if row else 1

    # ------------------------------------------------------------------
    # seen_torrents — per-torrent indexer attribution (dedup grab counts)
    # ------------------------------------------------------------------

    def get_torrent_seen(self, hash: str) -> dict | None:
        """Return the seen_torrents record for this hash, or None."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM seen_torrents WHERE hash = ?", (hash,)
            ).fetchone()
        return dict(row) if row else None

    def record_torrent_seen(
        self, hash: str, indexer_id: int, indexer_name: str
    ) -> bool:
        """
        Record a torrent as seen (attributed to an indexer).
        Returns True if newly inserted, False if already existed.
        Uses INSERT OR IGNORE so it is safe to call multiple times.
        """
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM seen_torrents WHERE hash = ?", (hash,)
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """INSERT INTO seen_torrents
                   (hash, indexer_id, indexer_name, first_seen)
                   VALUES (?, ?, ?, ?)""",
                (hash, indexer_id, indexer_name, _now_iso()),
            )
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # app_state — small persistent key/value store (e.g. last reorder time)
    # ------------------------------------------------------------------

    def get_app_state(self, key: str) -> Optional[str]:
        """Return the stored value for this key, or None."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_app_state(self, key: str, value: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # LLM cache — content-hash based dedup for AI scoring
    # ------------------------------------------------------------------

    def get_llm_cache(self, content_hash: str, ttl_hours: int = 24) -> str | None:
        """Return cached JSON response if fresh, else None."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT response_json, scored_at FROM llm_cache WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        if not row:
            return None
        try:
            scored = datetime.fromisoformat(row["scored_at"])
            if (_now_dt() - scored).total_seconds() > ttl_hours * 3600:
                return None
        except (ValueError, TypeError):
            return None
        return row["response_json"]

    def save_llm_cache(self, content_hash: str, response_json: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (content_hash, response_json, scored_at) VALUES (?, ?, ?)",
                (content_hash, response_json, _now_iso()),
            )
            conn.commit()

    def prune_llm_cache(self, ttl_hours: int = 24):
        cutoff = (_now_dt() - timedelta(hours=ttl_hours)).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM llm_cache WHERE scored_at < ?", (cutoff,))
            conn.commit()

    # ------------------------------------------------------------------
    # LLM scoring log — per-run record of AI scoring results
    # ------------------------------------------------------------------

    def record_llm_scoring_run(
        self,
        results: list[dict],
        model_used: str,
        cache_hit: bool = False,
    ):
        """
        Record one AI scoring run.  `results` is a list of dicts, each with:
        indexer_id, indexer_name, deterministic_score, ai_score, ai_reasoning.
        """
        now = _now_iso()
        with self._lock, self._conn() as conn:
            for r in results:
                conn.execute(
                    """INSERT INTO llm_scoring_log
                       (scored_at, indexer_id, indexer_name,
                        deterministic_score, ai_score, ai_reasoning,
                        model_used, cache_hit)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now,
                        r["indexer_id"],
                        r["indexer_name"],
                        r.get("deterministic_score"),
                        r["ai_score"],
                        r.get("ai_reasoning", ""),
                        model_used,
                        int(cache_hit),
                    ),
                )
            conn.commit()

    def get_llm_scoring_runs(self, limit: int = 50) -> list[dict]:
        """
        Return the most recent scoring runs grouped by scored_at timestamp.
        Each entry: {scored_at, model_used, cache_hit, indexers: [{...}]}.
        """
        with self._lock, self._conn() as conn:
            # Get distinct run timestamps
            ts_rows = conn.execute(
                """SELECT DISTINCT scored_at FROM llm_scoring_log
                   ORDER BY scored_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            if not ts_rows:
                return []
            runs = []
            for ts_row in ts_rows:
                ts = ts_row["scored_at"]
                rows = conn.execute(
                    """SELECT indexer_id, indexer_name, deterministic_score,
                              ai_score, ai_reasoning, model_used, cache_hit
                       FROM llm_scoring_log WHERE scored_at = ?
                       ORDER BY indexer_name""",
                    (ts,),
                ).fetchall()
                if rows:
                    runs.append({
                        "scored_at": ts,
                        "model_used": rows[0]["model_used"],
                        "cache_hit": bool(rows[0]["cache_hit"]),
                        "indexers": [dict(r) for r in rows],
                    })
        return runs

    def get_llm_score_history_for_indexer(
        self, indexer_id: int, limit: int = 30,
    ) -> list[dict]:
        """Return AI score history for one indexer, oldest first."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT scored_at, deterministic_score, ai_score, ai_reasoning,
                          model_used, cache_hit
                   FROM llm_scoring_log
                   WHERE indexer_id = ?
                   ORDER BY scored_at DESC LIMIT ?""",
                (indexer_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_llm_scoring_log(self, keep_days: int = 30):
        """Remove LLM scoring log entries older than keep_days."""
        cutoff = (_now_dt() - timedelta(days=keep_days)).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM llm_scoring_log WHERE scored_at < ?", (cutoff,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Auto-manage state — track consecutive low scores for auto-disable
    # ------------------------------------------------------------------

    def get_auto_manage_state(self, indexer_id: int) -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM auto_manage_state WHERE indexer_id = ?",
                (indexer_id,),
            ).fetchone()
        return dict(row) if row else {"indexer_id": indexer_id, "consecutive_low": 0, "disabled_at": None, "disabled_by_auto": 0}

    def increment_consecutive_low(self, indexer_id: int) -> int:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO auto_manage_state (indexer_id) VALUES (?)",
                (indexer_id,),
            )
            conn.execute(
                "UPDATE auto_manage_state SET consecutive_low = consecutive_low + 1 WHERE indexer_id = ?",
                (indexer_id,),
            )
            row = conn.execute(
                "SELECT consecutive_low FROM auto_manage_state WHERE indexer_id = ?",
                (indexer_id,),
            ).fetchone()
            conn.commit()
        return row["consecutive_low"] if row else 1

    def reset_consecutive_low(self, indexer_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO auto_manage_state (indexer_id) VALUES (?)",
                (indexer_id,),
            )
            conn.execute(
                "UPDATE auto_manage_state SET consecutive_low = 0 WHERE indexer_id = ?",
                (indexer_id,),
            )
            conn.commit()

    def mark_auto_disabled(self, indexer_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO auto_manage_state (indexer_id) VALUES (?)",
                (indexer_id,),
            )
            conn.execute(
                "UPDATE auto_manage_state SET disabled_at = ?, disabled_by_auto = 1 WHERE indexer_id = ?",
                (_now_iso(), indexer_id),
            )
            conn.commit()

    def clear_auto_disabled(self, indexer_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE auto_manage_state SET disabled_at = NULL, disabled_by_auto = 0, consecutive_low = 0 WHERE indexer_id = ?",
                (indexer_id,),
            )
            conn.commit()

    def get_all_auto_disabled(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_manage_state WHERE disabled_by_auto = 1"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Retention + log
    # ------------------------------------------------------------------

    def prune_old_records(self):
        cutoff = (_now_dt() - timedelta(days=self.retention_days)).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM processed_hashes WHERE actioned_at < ?", (cutoff,)
            )
            conn.execute(
                "DELETE FROM retry_queue WHERE last_attempt < ? AND resolved = 1",
                (cutoff,)
            )
            conn.commit()
        self._prune_log_file(cutoff)

    def _prune_log_file(self, cutoff_iso: str):
        # BUG-18: hold the lock so a concurrent write_log() append can't be
        # lost between read and rewrite, and replace atomically so a crash
        # mid-write can't truncate the log.
        with self._lock:
            if not os.path.exists(self.log_path):
                return
            kept: list[str] = []
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("timestamp", "") >= cutoff_iso:
                            kept.append(line)
                    except (json.JSONDecodeError, KeyError):
                        kept.append(line)   # preserve malformed lines
            tmp_path = self.log_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                if kept:
                    f.write("\n".join(kept) + "\n")
            os.replace(tmp_path, self.log_path)

    def write_log(self, entry: dict):
        """Append a single event to the JSON Lines log file."""
        if "timestamp" not in entry:
            entry["timestamp"] = _now_iso()
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat()
