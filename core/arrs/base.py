import requests
from abc import ABC
from datetime import datetime, timezone
from typing import Optional


class ArrClientError(Exception):
    pass


def _iso_after(a: str, b: str) -> bool:
    """
    Is timestamp `a` strictly later than `b`?

    Parsed rather than string-compared, because the two sides are different
    ISO dialects: the arrs emit "...T12:00:00Z" while our own timestamps are
    datetime.isoformat() -> "...T12:00:00.123456+00:00". Lexically 'Z'
    (0x5A) sorts above '.' (0x2E), so a same-second grab compared as
    strictly later and would have been picked up as its own replacement.

    Falls back to string comparison if either side will not parse, which is
    no worse than what it replaces.
    """
    try:
        pa = datetime.fromisoformat(a.replace("Z", "+00:00"))
        pb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=timezone.utc)
        if pb.tzinfo is None:
            pb = pb.replace(tzinfo=timezone.utc)
        return pa > pb
    except Exception:
        return a > b


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

    # ------------------------------------------------------------------
    # Per-media history (ROADMAP item 27)
    # ------------------------------------------------------------------
    #
    # Answering "did a replacement arrive for the thing we rejected" needs
    # history scoped to one episode/movie. The global feed is 318k records
    # on a real install, so paging it is not an option.
    #
    # The endpoint that does this is DIFFERENT per app, and the symmetric
    # guess is wrong in BOTH directions. Verified against live Sonarr v4 and
    # Radarr v3 on 2026-08-23:
    #
    #   Sonarr  GET /history?episodeId=N        scoped correctly
    #   Sonarr  GET /history/series?episodeId=  episodeId IGNORED -- returns
    #                                           the entire series (185 rows
    #                                           across 57 episodes)
    #   Radarr  GET /history/movie?movieId=N    scoped correctly
    #   Radarr  GET /history?movieId=N          movieId IGNORED -- returns
    #                                           unfiltered global history
    #                                           (50 rows across 19 movies)
    #
    # Both wrong forms return HTTP 200 with plausible-looking data. Nothing
    # about the response says the filter was dropped. That is why the guard
    # below exists: every returned record must actually carry the id we
    # asked for, or we raise instead of reporting a replacement that belongs
    # to a different film.

    MEDIA_ID_FIELD: str = ""      # "episodeId" | "movieId" | "albumId"
    MEDIA_HISTORY_VERIFIED: bool = False

    def media_id_of(self, history_record: dict):
        """The id identifying which episode/movie/album a record is about."""
        if not self.MEDIA_ID_FIELD:
            return None
        return (history_record or {}).get(self.MEDIA_ID_FIELD)

    def _fetch_media_history(self, media_id) -> list[dict]:
        """App-specific fetch. Subclasses override; see the table above."""
        raise NotImplementedError(
            f"{self.APP_NAME}: per-media history endpoint not implemented")

    def get_media_history(self, media_id) -> list[dict]:
        """
        All history records for one episode/movie/album, newest first.

        Raises ArrClientError if the server ignored the filter, rather than
        returning records for other media. A wrong answer here would be
        attributed to a real indexer as a real replacement, so failing is
        strictly better than guessing.
        """
        if media_id in (None, ""):
            return []
        if not self.MEDIA_HISTORY_VERIFIED:
            raise ArrClientError(
                f"{self.APP_NAME}: per-media history is not verified for this "
                f"app; refusing to guess an endpoint")

        records = self._fetch_media_history(media_id) or []

        field = self.MEDIA_ID_FIELD
        foreign = {r.get(field) for r in records if r.get(field) != media_id}
        if foreign:
            raise ArrClientError(
                f"{self.APP_NAME}: history filter {field}={media_id} was "
                f"ignored by the server -- got {len(foreign)} other "
                f"{field}(s) back. Refusing to use this response.")

        records.sort(key=lambda r: r.get("date") or "", reverse=True)
        return records

    def find_replacement_grab(self, media_id, after_iso: str,
                              exclude_hash: str = None) -> dict | None:
        """
        The first grab for this media strictly after `after_iso`.

        Returns a normalised dict, or None while nothing has been grabbed
        yet -- which is the common case and is not an error: the arr may
        still be searching, or there may be no other release available.

        `exclude_hash` drops the rejected release itself, which stays in
        history and would otherwise look like its own replacement.
        """
        exclude = (exclude_hash or "").upper()
        candidates = []
        for r in self.get_media_history(media_id):
            if r.get("eventType") != "grabbed":
                continue
            date = r.get("date") or ""
            if not date or not _iso_after(date, after_iso):
                continue
            data = r.get("data") or {}
            # torrentInfoHash is the reliable one; downloadId is the infohash
            # for torrents but a UUID for usenet grabs.
            h = (data.get("torrentInfoHash") or r.get("downloadId") or "").upper()
            if exclude and h == exclude:
                continue
            candidates.append({
                "history_id":    r.get("id"),
                "hash":          h or None,
                "title":         r.get("sourceTitle"),
                "indexer":       data.get("indexer"),
                "release_group": data.get("releaseGroup"),
                "protocol":      data.get("protocol"),
                "grabbed_at":    date,
            })
        # Oldest first: the first thing grabbed after the rejection is the
        # replacement. Later grabs are replacements for the replacement.
        candidates.sort(key=lambda c: c["grabbed_at"])
        return candidates[0] if candidates else None

    def was_imported(self, media_id, after_iso: str,
                     download_hash: str = None) -> bool:
        """
        Did a grab for this media actually get imported after `after_iso`?

        This is the success signal for a replacement. Inspectarr only records
        inspections for FLAGGED releases, so a clean replacement produces no
        evidence of its own -- the arr importing it is the observable proof
        that it downloaded and passed.
        """
        want = (download_hash or "").upper()
        for r in self.get_media_history(media_id):
            if r.get("eventType") != "downloadFolderImported":
                continue
            if not _iso_after(r.get("date") or "", after_iso):
                continue
            if want:
                got = ((r.get("data") or {}).get("torrentInfoHash")
                       or r.get("downloadId") or "").upper()
                # Require a POSITIVE match, not merely the absence of a
                # contradiction. The old `if got and got != want` treated an
                # unidentifiable import as a match, so a hand-import or an
                # unrelated release completing for the same episode was
                # recorded as "the replacement from indexer X imported
                # cleanly" -- and then fed to indexer reputation as fact.
                #
                # This module already refuses to guess about scoping; it
                # should hold itself to the same standard about attribution.
                if got != want:
                    continue
            return True
        return False

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
