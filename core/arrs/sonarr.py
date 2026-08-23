from .base import AbstractArrClient


class SonarrClient(AbstractArrClient):
    """
    Sonarr v4 API client (/api/v3 endpoint prefix).

    Queue path  (primary):  DELETE /queue/{id}?blocklist=true
    History path (fallback): POST /history/failed/{id}
      — marks the grab as failed and blocklists it; also triggers a
        re-search for the episode, which is acceptable since we've
        confirmed the release is bad.

    All endpoint logic lives in AbstractArrClient (IMP-1).
    """
    APP_NAME = "sonarr"
    API_PREFIX = "/api/v3"
    QUEUE_UNKNOWN_PARAM = "includeUnknownSeriesItems"

    # ROADMAP item 27. Verified against live Sonarr v4 on 2026-08-23.
    MEDIA_ID_FIELD = "episodeId"
    MEDIA_HISTORY_VERIFIED = True

    def _fetch_media_history(self, media_id) -> list[dict]:
        """
        NOT /history/series -- that endpoint accepts an episodeId parameter
        and silently ignores it, returning every record for the whole series
        (185 records across 57 episodes when measured). The paged /history
        endpoint honours episodeId correctly.
        """
        data = self._get("/history", params={
            "pageSize": 200,
            "sortKey": "date",
            "sortDir": "desc",
            "episodeId": media_id,
        })
        return data.get("records", [])
