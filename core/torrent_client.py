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

import logging
import os
import time
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


log = logging.getLogger("inspectarr")


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
    def delete_torrent(self, hash: str, delete_files: bool = True,
                       verify: bool = True, verify_timeout: float = 20.0) -> bool:
        """
        Delete torrent (and optionally its data). Returns True only when the
        deletion has been CONFIRMED -- see _confirm_deleted(). A True here is
        reported to the user as "the file is gone", so implementations must
        not return it on the strength of a request that merely did not raise.
        """

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
    # Deletion verification (shared by every client)
    # ------------------------------------------------------------------
    #
    # Every one of these clients answers a delete request affirmatively
    # without promising anything: qBittorrent returns 200 for a hash it does
    # not hold, and the Transmission and Deluge RPCs likewise succeed on a
    # no-op. Callers report the result to the user as "the file is gone", so
    # the return value of delete_torrent() has to be checked, not assumed.

    # Format: "<path as the client sees it>:<path as inspectarr sees it>",
    # comma-separated. Unset means the payload is not reachable from this
    # process and only the client-side checks run.
    _PATH_MAP_ENV = "INSPECTARR_PATH_MAP"

    def _local_path(self, client_path: str) -> str | None:
        """
        Translate a client-side path into one this process can stat. Returns
        None when no mapping applies or the mapped root is not mounted here --
        which means "cannot verify", never "already gone".
        """
        raw = os.environ.get(self._PATH_MAP_ENV, "").strip()
        if not raw or not client_path:
            return None
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            src, dst = (p.strip() for p in pair.split(":", 1))
            if src and dst and client_path.startswith(src):
                root = dst.rstrip("/") or "/"
                return client_path.replace(src, dst, 1) if os.path.isdir(root) else None
        return None

    def _payload_path(self, hash: str) -> str | None:
        """
        Where this torrent's data lives, as the CLIENT sees it. Subclasses
        override; the default disables filesystem verification.
        """
        return None

    def _torrent_present(self, hash: str) -> bool | None:
        """True/False if known, None if the client could not be queried."""
        try:
            return bool(self.get_all_torrents(hash))
        except Exception as exc:
            log.warning("could not query %s before deleting it: %s", hash[:12], exc)
            return None

    def _precheck_delete(self, hash: str) -> bool:
        """
        False if we must not claim a deletion: the client does not hold this
        torrent (so the delete is a no-op that leaves any payload orphaned on
        disk), or we cannot tell.
        """
        present = self._torrent_present(hash)
        if present is False:
            log.error(
                "asked to delete %s but the client does not hold that torrent "
                "-- nothing was deleted and any payload on disk is unaccounted "
                "for; reporting failure", hash[:12],
            )
            return False
        if present is None:
            log.error(
                "could not confirm whether %s is in the client, so a delete "
                "cannot be vouched for; reporting failure", hash[:12],
            )
            return False
        return True

    def _confirm_deleted(self, hash: str, content_path: str | None,
                         verify_timeout: float = 20.0) -> bool:
        """Poll until the torrent has left the client and, where visible, the
        payload has left the disk. False if either is still there."""
        deadline = time.monotonic() + verify_timeout
        while True:
            try:
                still = bool(self.get_all_torrents(hash))
            except Exception as exc:
                log.error("could not verify deletion of %s: %s", hash[:12], exc)
                return False
            if not still:
                break
            if time.monotonic() >= deadline:
                log.error(
                    "delete of %s was accepted but the torrent is STILL in the "
                    "client after %.0fs -- reporting failure",
                    hash[:12], verify_timeout,
                )
                return False
            time.sleep(1.0)

        if not content_path:
            return True
        local = self._local_path(content_path)
        if local is None:
            log.info(
                "delete verified for %s (client only; payload path %r is not "
                "visible to inspectarr -- set %s to verify the filesystem too)",
                hash[:12], content_path, self._PATH_MAP_ENV,
            )
            return True

        # A network share caches attributes, so a stat taken immediately after
        # the unlink can still return the old entry. Re-check briefly.
        fs_deadline = time.monotonic() + 10.0
        while os.path.exists(local) and time.monotonic() < fs_deadline:
            time.sleep(1.0)
        if os.path.exists(local):
            log.error(
                "torrent %s was removed but the payload is STILL on disk: %s "
                "-- reporting failure", hash[:12], local,
            )
            return False
        log.info("delete verified for %s (torrent and payload both gone)", hash[:12])
        return True

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
