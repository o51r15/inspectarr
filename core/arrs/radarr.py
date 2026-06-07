from typing import Optional
from .base import AbstractArrClient


class RadarrClient(AbstractArrClient):
    """
    Radarr API client — STUB.
    Not implemented in v1. Wire up by mirroring sonarr.py against
    Radarr's /api/v3 queue and history endpoints (same structure as Sonarr).
    """

    def find_in_queue(self, infohash: str) -> Optional[dict]:
        raise NotImplementedError("Radarr support not implemented in v1")

    def blocklist_from_queue(self, queue_id: int) -> bool:
        raise NotImplementedError("Radarr support not implemented in v1")

    def find_in_history(self, infohash: str) -> Optional[dict]:
        raise NotImplementedError("Radarr support not implemented in v1")

    def blocklist_from_history(self, history_id: int) -> bool:
        raise NotImplementedError("Radarr support not implemented in v1")
