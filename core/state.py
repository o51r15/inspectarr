import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path


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
        self._init_db()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_dirs(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
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
            conn.commit()


    # ------------------------------------------------------------------
    # processed_hashes
    # ------------------------------------------------------------------

    def is_processed(self, hash: str) -> bool:
        """
        True if this hash was previously actioned (deleted or dry_run with
        dry_run still active). dry_run records are NOT skipped once dry_run
        is turned off — they re-evaluate on the next scan.
        """
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM retry_queue WHERE hash = ? AND resolved = 0",
                (hash,)
            ).fetchone()
        return row is not None

    def get_retry_count(self, hash: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM retry_queue WHERE hash = ?", (hash,)
            ).fetchone()
        return row["attempt_count"] if row else 0

    def resolve_retry(self, hash: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE retry_queue SET resolved = 1 WHERE hash = ?", (hash,)
            )
            conn.commit()


    # ------------------------------------------------------------------
    # Retention + log
    # ------------------------------------------------------------------

    def prune_old_records(self):
        cutoff = (_now_dt() - timedelta(days=self.retention_days)).isoformat()
        with self._conn() as conn:
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
        with open(self.log_path, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")

    def write_log(self, entry: dict):
        """Append a single event to the JSON Lines log file."""
        if "timestamp" not in entry:
            entry["timestamp"] = _now_iso()
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat()
