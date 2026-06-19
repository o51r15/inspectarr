import requests
from typing import Optional
from .base import AbstractArrClient, ArrClientError


class SonarrClient(AbstractArrClient):
    """
    Sonarr v4 API client (/api/v3 endpoint prefix).

    Queue path  (primary):  DELETE /queue/{id}?blocklist=true
    History path (fallback): POST /history/failed/{id}
      — marks the grab as failed and blocklists it; also triggers a
        re-search for the episode, which is acceptable since we've
        confirmed the release is bad.
    """

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        resp = requests.get(
            f"{self.url}/api/v3{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, endpoint: str, params: dict = None) -> bool:
        resp = requests.delete(
            f"{self.url}/api/v3{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def _post(self, endpoint: str, json_body: dict = None) -> dict:
        resp = requests.post(
            f"{self.url}/api/v3{endpoint}",
            headers=self._headers(),
            json=json_body or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}


    def find_in_queue(self, infohash: str) -> Optional[dict]:
        """Paginate the queue searching for this infohash (case-insensitive)."""
        hash_upper = infohash.upper()
        page = 1
        page_size = 100
        while True:
            data = self._get("/queue", params={
                "page": page,
                "pageSize": page_size,
                "includeUnknownSeriesItems": "true",
            })
            records = data.get("records", [])
            for item in records:
                if item.get("downloadId", "").upper() == hash_upper:
                    return item
            if len(records) < page_size:
                break
            page += 1
        return None

    def blocklist_from_queue(self, queue_id: int) -> bool:
        """
        Remove from Sonarr queue and blocklist the release.
        removeFromClient=false — inspectarr handles qbit deletion separately.
        """
        return self._delete(f"/queue/{queue_id}", params={
            "removeFromClient": "false",
            "blocklist": "true",
            "skipRedownload": "false",
        })

    def find_in_history(self, infohash: str) -> Optional[dict]:
        """Search history by downloadId. Returns the most recent match."""
        data = self._get("/history", params={
            "pageSize": 100,
            "sortKey": "date",
            "sortDir": "desc",
            "downloadId": infohash.upper(),
        })
        records = data.get("records", [])
        return records[0] if records else None

    def get_history_records_by_hash(self, infohash: str) -> list[dict]:
        """Return ALL history records for this infohash (used for indexer attribution)."""
        data = self._get("/history", params={
            "pageSize": 100,
            "sortKey": "date",
            "sortDir": "desc",
            "downloadId": infohash.upper(),
        })
        return data.get("records", [])

    def blocklist_from_history(self, history_id: int) -> bool:
        """
        Mark history item as failed. Sonarr v4 blocklists the release and
        triggers a re-search for the episode — acceptable since the release
        is confirmed bad.
        """
        self._post(f"/history/failed/{history_id}")
        return True

    def test_connection(self) -> bool:
        try:
            data = self._get("/system/status")
            return isinstance(data, dict) and data.get("appName", "").lower() == "sonarr"
        except Exception:
            return False
