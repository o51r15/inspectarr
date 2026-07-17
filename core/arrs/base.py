import requests
from abc import ABC
from typing import Optional


class ArrClientError(Exception):
    pass


class AbstractArrClient(ABC):
    """
    Base class for all *arr API clients.

    The queue/history/blocklist endpoints are structurally identical across
    Sonarr v4 and Radarr v3 (/api/v3) and Lidarr v2 (/api/v1), so the full
    implementation lives here (IMP-1). Subclasses only set class attributes:

      APP_NAME            — appName reported by /system/status (lowercase)
      API_PREFIX          — endpoint prefix, e.g. "/api/v3"
      QUEUE_UNKNOWN_PARAM — queue param exposing unknown items
                            (includeUnknownSeriesItems / MovieItems / ArtistItems)

    To add a new arr:
      1. Create a new module in core/arrs/
      2. Subclass AbstractArrClient and set the three class attributes
      3. Wire the app name in scanner._build_arr_client()
    """

    APP_NAME: str = ""
    API_PREFIX: str = "/api/v3"
    QUEUE_UNKNOWN_PARAM: str = ""

    # BUG-19: safety cap on queue pagination — a misbehaving server that
    # keeps returning full pages must not spin find_in_queue forever.
    MAX_QUEUE_PAGES = 50

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        resp = requests.get(
            f"{self.url}{self.API_PREFIX}{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, endpoint: str, params: dict = None) -> bool:
        resp = requests.delete(
            f"{self.url}{self.API_PREFIX}{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def _post(self, endpoint: str, json_body: dict = None) -> dict:
        resp = requests.post(
            f"{self.url}{self.API_PREFIX}{endpoint}",
            headers=self._headers(),
            json=json_body or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def find_in_queue(self, infohash: str) -> Optional[dict]:
        """Paginate the queue searching for this infohash (case-insensitive)."""
        hash_upper = infohash.upper()
        page = 1
        page_size = 100
        while page <= self.MAX_QUEUE_PAGES:
            data = self._get("/queue", params={
                "page": page,
                "pageSize": page_size,
                self.QUEUE_UNKNOWN_PARAM: "true",
            })
            records = data.get("records", [])
            for item in records:
                # BUG-19: downloadId can be null in arr responses
                if (item.get("downloadId") or "").upper() == hash_upper:
                    return item
            if len(records) < page_size:
                break
            page += 1
        return None

    def blocklist_from_queue(self, queue_id: int) -> bool:
        """
        Remove from the arr queue and blocklist the release.
        removeFromClient=false — inspectarr handles qbit deletion separately.
        """
        return self._delete(f"/queue/{queue_id}", params={
            "removeFromClient": "false",
            "blocklist": "true",
            "skipRedownload": "false",
        })

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def find_in_history(self, infohash: str) -> Optional[dict]:
        """Search history by downloadId. Returns the most recent match."""
        records = self.get_history_records_by_hash(infohash)
        return records[0] if records else None

    def get_history_records_by_hash(self, infohash: str) -> list[dict]:
        """
        Return ALL history records for this infohash.

        Important for malicious-hit attribution: after blocklisting, the arr
        appends a new "downloadFailed" event, making it records[0]. The
        original "grabbed" event — which carries data.indexer — is still
        present but no longer the most recent. Iterating all records finds it.
        """
        data = self._get("/history", params={
            "pageSize": 100,
            "sortKey": "date",
            "sortDir": "desc",
            "downloadId": infohash.upper(),
        })
        return data.get("records", [])

    def blocklist_from_history(self, history_id: int) -> bool:
        """
        Mark a history item as failed. The arr blocklists the release and
        triggers a re-search — acceptable since the release is confirmed bad.
        """
        self._post(f"/history/failed/{history_id}")
        return True

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def blocklist(self, infohash: str) -> bool:
        """
        Orchestrate blocklisting: check queue first, then history.
        Returns True if blocklisted (or not found — caller logs the nuance).
        """
        queue_item = self.find_in_queue(infohash)
        if queue_item:
            return self.blocklist_from_queue(queue_item["id"])

        history_item = self.find_in_history(infohash)
        if history_item:
            return self.blocklist_from_history(history_item["id"])

        # Not tracked by the arr at all — may have been a manual qbit add.
        # Return True so the caller doesn't treat this as an arr failure.
        return True

    def get_grab_indexer(self, infohash: str) -> str | None:
        """
        Return the indexer name that served this grab, or None.
        Iterates ALL history records and returns the indexer field from the
        first record that carries it (see get_history_records_by_hash).
        """
        for record in self.get_history_records_by_hash(infohash):
            indexer = record.get("data", {}).get("indexer")
            if indexer:
                return indexer
        return None

    def test_connection(self) -> bool:
        try:
            data = self._get("/system/status")
            return isinstance(data, dict) and data.get("appName", "").lower() == self.APP_NAME
        except Exception:
            return False
