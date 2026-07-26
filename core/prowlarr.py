"""
core/prowlarr.py — Prowlarr API client

Handles indexer list, history, backoff status, and priority writes.
Only torrent indexers (protocol == "torrent") are exposed — NZB indexers
are never touched by Inspectarr.
"""
import logging
import requests

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

    def get_torrent_indexers(self, include_disabled: bool = False) -> list[dict]:
        """Return torrent indexers sorted by priority ascending.
        By default only enabled indexers. Set include_disabled=True for auto-manage.
        """
        all_idx = self._get("indexer")
        torrent = [
            i for i in all_idx
            if i.get("protocol") == "torrent" and (include_disabled or i.get("enable", True))
        ]
        return sorted(torrent, key=lambda x: x.get("priority", 999))

    def get_indexer_status(self) -> dict[int, dict]:
        """Return dict of indexer_id → status for indexers currently in backoff."""
        statuses = self._get("indexerstatus")
        return {s["indexerId"]: s for s in statuses}

    def get_indexer_stats(self) -> dict[int, dict]:
        """
        Return per-indexer stats from /api/v1/indexerstats, keyed by indexer_id.
        Provides averageResponseTime plus per-type query and failure counts.
        NZB indexers are included in the response but are never looked up by the
        scorer (only torrent indexer IDs are passed in).
        """
        data = self._get("indexerstats")
        return {s["indexerId"]: s for s in data.get("indexers", [])}

    def sync_to_apps(self) -> bool:
        """
        Trigger Prowlarr to push the current indexer list to all connected
        applications (Sonarr, Radarr, Whisparr, etc.) via the
        ApplicationIndexerSync command.
        Returns True if the command was accepted, False on any error.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/command",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"name": "ApplicationIndexerSync"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except requests.HTTPError as exc:
            body = exc.response.text[:300] if exc.response is not None else ""
            log.warning(
                f"Prowlarr ApplicationIndexerSync failed "
                f"(HTTP {exc.response.status_code if exc.response is not None else '?'}): {body}"
            )
            return False
        except Exception as exc:
            log.warning(f"Prowlarr ApplicationIndexerSync failed: {exc}")
            return False

    def set_indexer_enabled(self, indexer: dict, enabled: bool) -> bool:
        """
        Enable or disable an indexer in Prowlarr.
        Uses forceSave=true (same rationale as set_indexer_priority).
        Returns True on success.
        """
        updated = dict(indexer)
        updated["enable"] = enabled
        try:
            resp = requests.put(
                f"{self.base_url}/api/v1/indexer/{indexer['id']}",
                headers={**self.headers, "Content-Type": "application/json"},
                params={"forceSave": "true"},
                json=updated,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            action = "enabled" if enabled else "disabled"
            log.info(f"Prowlarr indexer '{indexer.get('name')}' {action}")
            return True
        except requests.HTTPError as exc:
            body = exc.response.text[:300] if exc.response is not None else ""
            log.warning(
                f"Prowlarr enable/disable failed for '{indexer.get('name')}' "
                f"(HTTP {exc.response.status_code if exc.response is not None else '?'}): {body}"
            )
            return False
        except Exception as exc:
            log.warning(f"Prowlarr enable/disable failed for '{indexer.get('name')}': {exc}")
            return False

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
