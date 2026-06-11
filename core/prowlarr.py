"""
core/prowlarr.py — Prowlarr API client

Handles indexer list, history, backoff status, and priority writes.
Only torrent indexers (protocol == "torrent") are exposed — NZB indexers
are never touched by Inspectarr.
"""
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("inspectarr")


class ProwlarrClient:

    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        self.headers  = {"X-Api-Key": api_key}
        self.timeout  = 15

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None):
        resp = requests.get(
            f"{self.base_url}/api/v1/{path}",
            headers=self.headers,
            params=params or {},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict):
        resp = requests.put(
            f"{self.base_url}/api/v1/{path}",
            headers={**self.headers, "Content-Type": "application/json"},
            json=data,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def test_connection(self) -> bool:
        try:
            self._get("indexer")
            return True
        except Exception:
            return False

    def get_torrent_indexers(self) -> list[dict]:
        """Return all enabled torrent indexers sorted by priority ascending."""
        all_idx = self._get("indexer")
        torrent = [
            i for i in all_idx
            if i.get("protocol") == "torrent" and i.get("enable", True)
        ]
        return sorted(torrent, key=lambda x: x.get("priority", 999))

    def get_indexer_status(self) -> dict[int, dict]:
        """Return dict of indexer_id → status for indexers currently in backoff."""
        statuses = self._get("indexerstatus")
        return {s["indexerId"]: s for s in statuses}

    def get_indexer_history(
        self,
        indexer_id: int,
        days: int = 90,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Return history records for one indexer within a rolling window.
        Paginates automatically; stops at the window cutoff or the record limit.
        """
        cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
        records: list[dict] = []
        page    = 1

        while len(records) < limit:
            data  = self._get("history", params={
                "indexerId":     indexer_id,
                "pageSize":      100,
                "page":          page,
                "sortKey":       "date",
                "sortDirection": "descending",
            })
            batch = data.get("records", [])
            if not batch:
                break
            for r in batch:
                rec_dt = _parse_dt(r.get("date"))
                if rec_dt is not None and rec_dt < cutoff:
                    return records
                records.append(r)
            if len(batch) < 100:
                break
            page += 1

        return records

    def set_indexer_priority(self, indexer: dict, new_priority: int) -> bool:
        """
        Write a new priority for an indexer.

        Uses forceSave=true so Prowlarr persists the change WITHOUT running its
        live connectivity test. This is essential: an indexer that is currently
        unreachable (e.g. CloudFlare-blocked) would otherwise return HTTP 400 and
        block the save — and those failing indexers are exactly the ones we most
        need to reprioritise.

        Returns True on success, False on any error (error is logged, not raised).
        """
        updated = dict(indexer)
        updated["priority"] = new_priority
        try:
            resp = requests.put(
                f"{self.base_url}/api/v1/indexer/{indexer['id']}",
                headers={**self.headers, "Content-Type": "application/json"},
                params={"forceSave": "true"},
                json=updated,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except requests.HTTPError as exc:
            body = exc.response.text[:300] if exc.response is not None else ""
            log.warning(
                f"Prowlarr priority write failed for '{indexer.get('name')}' "
                f"(HTTP {exc.response.status_code if exc.response is not None else '?'}): {body}"
            )
            return False
        except Exception as exc:
            log.warning(f"Prowlarr priority write failed for '{indexer.get('name')}': {exc}")
            return False


def _parse_dt(value: str | None) -> datetime | None:
    """
    Parse a Prowlarr ISO date (e.g. '2026-06-10T15:55:40Z') into an
    offset-aware UTC datetime. Returns None if unparseable.
    """
    if not value:
        return None
    try:
        # Normalise trailing Z to +00:00 for fromisoformat
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
