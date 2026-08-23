from .base import AbstractArrClient


class RadarrClient(AbstractArrClient):
    """
    Radarr v3 API client (/api/v3 endpoint prefix).

    API structure is identical to Sonarr v4 for queue/history/blocklist.
    Blocklisting from history triggers a re-search for the movie —
    acceptable since the release is confirmed bad.

    All endpoint logic lives in AbstractArrClient (IMP-1).
    """
    APP_NAME = "radarr"
    API_PREFIX = "/api/v3"
    QUEUE_UNKNOWN_PARAM = "includeUnknownMovieItems"

    # ROADMAP item 27. Verified against live Radarr v3 on 2026-08-23.
    MEDIA_ID_FIELD = "movieId"
    MEDIA_HISTORY_VERIFIED = True

    def _fetch_media_history(self, media_id) -> list[dict]:
        """
        The MIRROR of Sonarr, not the same shape. Radarr's paged /history
        endpoint accepts movieId and silently ignores it -- measured
        returning 50 records spanning 19 different movies. /history/movie
        filters correctly and returns a bare list rather than a page object.
        """
        return self._get("/history/movie", params={"movieId": media_id}) or []
