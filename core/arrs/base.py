from abc import ABC, abstractmethod
from typing import Optional


class ArrClientError(Exception):
    pass


class AbstractArrClient(ABC):
    """
    Base class for all *arr API clients.

    To add a new arr (e.g. Radarr, Lidarr):
      1. Create a new module in core/arrs/
      2. Subclass AbstractArrClient
      3. Implement all four abstract methods
      4. Wire the app name in scanner.build_arr_client()
    """

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    @abstractmethod
    def find_in_queue(self, infohash: str) -> Optional[dict]:
        """Find a torrent in the arr download queue by infohash.
        Returns the queue item dict (with an 'id' key) or None."""
        ...

    @abstractmethod
    def blocklist_from_queue(self, queue_id: int) -> bool:
        """Remove item from queue with blocklist=true.
        removeFromClient should be False — qbit handles the actual deletion."""
        ...

    @abstractmethod
    def find_in_history(self, infohash: str) -> Optional[dict]:
        """Find a completed/failed download in history by infohash.
        Returns the history record dict (with an 'id' key) or None."""
        ...

    @abstractmethod
    def blocklist_from_history(self, history_id: int) -> bool:
        """Mark a history item as failed, triggering arr's blocklist logic.
        Note: in Sonarr v4 this also triggers a re-search for the episode."""
        ...

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
        Uses find_in_history() which each subclass already implements.
        Called by Inspectarr when attributing malicious hits to Prowlarr indexers.
        """
        item = self.find_in_history(infohash)
        if item:
            return item.get("data", {}).get("indexer")
        return None
