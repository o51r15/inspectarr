from .base import AbstractArrClient


class LidarrClient(AbstractArrClient):
    """
    Lidarr v2 API client (/api/v1 endpoint prefix).

    API structure is identical to Sonarr/Radarr for queue/history/blocklist,
    with two differences captured by the class attributes below:
      - Endpoint prefix is /api/v1 (not /api/v3)
      - Queue unknown-items param is includeUnknownArtistItems

    All endpoint logic lives in AbstractArrClient (IMP-1).
    """
    APP_NAME = "lidarr"
    API_PREFIX = "/api/v1"
    QUEUE_UNKNOWN_PARAM = "includeUnknownArtistItems"
