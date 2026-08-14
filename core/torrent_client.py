"""
core/torrent_client.py — Abstract base class for torrent client integrations.

All torrent clients (qBittorrent, Transmission, Deluge) implement this
interface so that scanner.py, the UI, and the retry system can work with
any backend without conditional logic.

Normalised field contract
-------------------------
get_all_torrents() and get_torrents_by_category() return dicts with at least:
    hash      : str   — 40-char lower-case info-hash
    name      : str
    size      : int   — total bytes
    progress  : float — 0.0 … 1.0
    state     : str   — one of the NORMALISED_STATES below
    category  : str   — may be "" if uncategorised
    dlspeed   : int   — bytes/sec
    upspeed   : int   — bytes/sec

get_torrent_files() returns dicts with at least:
    name : str
    size : int
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig


# States every client must map to
NORMALISED_STATES = {
    "downloading", "seeding", "paused", "stopped",
    "checking", "error", "queued", "stalled", "unknown",
}


class TorrentClientError(Exception):
    """Raised on auth failures, connection errors, or unexpected API responses."""
    pass


class AbstractTorrentClient(ABC):
    """Interface that qBittorrent, Transmission, and Deluge clients implement."""

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @abstractmethod
    def test_connection(self) -> bool:
        """Verify credentials and connectivity. Returns True on success."""

    # ------------------------------------------------------------------
    # Torrent listing
    # ------------------------------------------------------------------

    @abstractmethod
    def get_all_torrents(self, hash: str | None = None) -> list[dict]:
        """Return normalised torrent dicts. If hash given, filter to that one."""

    @abstractmethod
    def get_torrents_by_category(self, category: str) -> list[dict]:
        """Return normalised torrent dicts in a given category."""

    @abstractmethod
    def get_categories(self) -> dict:
        """Return category map: {name: {name, savePath?}}."""

    # ------------------------------------------------------------------
    # Torrent actions
    # ------------------------------------------------------------------

    @abstractmethod
    def pause_torrent(self, hash: str) -> bool:
        """Pause / stop a torrent. Returns True on success."""

    @abstractmethod
    def resume_torrent(self, hash: str) -> bool:
        """Resume / start a torrent. Returns True on success."""

    @abstractmethod
    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        """Delete torrent (and optionally its data). Returns True on success."""

    @abstractmethod
    def set_torrent_category(self, hash: str, category: str) -> bool:
        """Set the category/label on a torrent."""

    # ------------------------------------------------------------------
    # Torrent detail
    # ------------------------------------------------------------------

    @abstractmethod
    def get_torrent_files(self, hash: str) -> list[dict]:
        """Return file list: [{name, size}, ...]."""

    @abstractmethod
    def get_torrent_properties(self, hash: str) -> dict:
        """Return extended properties (save_path, trackers, etc.)."""

    @abstractmethod
    def get_torrent_trackers(self, hash: str) -> list[dict]:
        """Return tracker list: [{url, status, ...}, ...]."""

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        """L-20: close the underlying HTTP session to release sockets."""
        session = getattr(self, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def build_torrent_client(config: AppConfig) -> AbstractTorrentClient:
    """
    Factory: return the correct torrent client based on config.torrent_client.

    HOW IT WORKS
    The torrent_client field in config.yaml selects the backend:
    - "qbittorrent" (default) → QBittorrentClient
    - "transmission"          → TransmissionClient
    - "deluge"                → DelugeClient

    WHAT COULD GO WRONG
    - If torrent_client is set to "transmission" but no transmission block exists
      in config.yaml, we raise TorrentClientError (config validation should catch
      this earlier, but we guard here too).
    - Imports are deferred to avoid circular imports with config.py.
    """
    tc = getattr(config, "torrent_client", "qbittorrent")

    if tc == "qbittorrent":
        from .qbit import QBittorrentClient
        return QBittorrentClient(
            config.qbittorrent.url,
            config.qbittorrent.username,
            config.qbittorrent.password,
        )

    if tc == "transmission":
        from .transmission import TransmissionClient
        if not config.transmission:
            raise TorrentClientError("torrent_client is 'transmission' but no transmission config block found")
        return TransmissionClient(
            config.transmission.url,
            config.transmission.username,
            config.transmission.password,
        )

    if tc == "deluge":
        from .deluge import DelugeClient
        if not config.deluge:
            raise TorrentClientError("torrent_client is 'deluge' but no deluge config block found")
        return DelugeClient(
            config.deluge.url,
            config.deluge.password,
        )

    raise TorrentClientError(f"Unknown torrent_client: {tc!r}")
