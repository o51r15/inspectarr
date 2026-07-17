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
