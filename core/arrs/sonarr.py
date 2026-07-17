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
