import requests
from typing import Optional
from .base import AbstractArrClient, ArrClientError


class LidarrClient(AbstractArrClient):
    """
    Lidarr v2 API client (/api/v1 endpoint prefix).

    API structure is identical to Sonarr/Radarr for queue/history/blocklist,
    with two differences:
      - Endpoint prefix is /api/v1 (not /api/v3)
      - Queue unknown-items param is includeUnknownArtistItems
    """

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        resp = requests.get(
            f"{self.url}/api/v1{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, endpoint: str, params: dict = None) -> bool:
        resp = requests.delete(
            f"{self.url}/api/v1{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def _post(self, endpoint: str, json_body: dict = None) -> dict:
        resp = requests.post(
            f"{self.url}/api/v1{endpoint}",
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
                "includeUnknownArtistItems": "true",
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
        Remove from Lidarr queue and blocklist the release.
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

    def blocklist_from_history(self, history_id: int) -> bool:
        """
        Mark history item as failed. Lidarr blocklists the release and
        triggers a re-search for the album — acceptable since the release
        is confirmed bad.
        """
        self._post(f"/history/failed/{history_id}")
        return True

    def test_connection(self) -> bool:
        try:
            self._get("/system/status")
            return True
        except Exception:
            return False
