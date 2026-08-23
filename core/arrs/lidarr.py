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

    # ROADMAP item 27: replacement tracking is OFF for Lidarr.
    #
    # No Lidarr instance was available to verify against, and this is
    # precisely the API where guessing has already been shown to be wrong
    # twice: Sonarr and Radarr need different endpoints, and each app's
    # wrong form returns HTTP 200 with unfiltered data rather than an error.
    #
    # MEDIA_HISTORY_VERIFIED stays False, so get_media_history() raises and
    # the sweep skips Lidarr releases instead of recording replacements that
    # may belong to a different album. Everything else about Lidarr is
    # unaffected -- detection, blocklisting and quarantine all still work.
    #
    # To enable: confirm which of /history?albumId= or /history/album?albumId=
    # is actually scoped (check that every returned record carries the
    # albumId asked for), then set MEDIA_ID_FIELD, implement
    # _fetch_media_history, and flip this to True.
    MEDIA_ID_FIELD = "albumId"
    MEDIA_HISTORY_VERIFIED = False
