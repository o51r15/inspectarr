import logging
import os
import time

import requests

from .torrent_client import AbstractTorrentClient, TorrentClientError

log = logging.getLogger("inspectarr")


class QBittorrentError(TorrentClientError):
    pass


class QBittorrentClient(AbstractTorrentClient):
    """
    Thin wrapper around qBittorrent Web API v2.
    Handles session cookie auth with automatic re-auth on 403.
    """

    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self._authenticated = False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self):
        resp = self.session.post(
            f"{self.url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        if resp.text.strip().lower() not in ("ok.", "ok"):
            raise QBittorrentError(f"Login rejected: {resp.text.strip()!r}")
        self._authenticated = True

    def _req(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        if not self._authenticated:
            self._login()
        resp = self.session.request(
            method, f"{self.url}{endpoint}", timeout=15, **kwargs
        )
        if resp.status_code == 403:
            # Session expired — re-auth once
            self._authenticated = False
            self._login()
            resp = self.session.request(
                method, f"{self.url}{endpoint}", timeout=15, **kwargs
            )
        resp.raise_for_status()
        return resp


    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def get_all_torrents(self, hash: str | None = None) -> list[dict]:
        """
        Return all torrents. If hash is provided, returns only that torrent.
        Each item includes: hash, name, size, progress, state, category,
        dlspeed, upspeed, ratio, num_seeds, num_leechs, added_on, eta.
        """
        params = {}
        if hash:
            params["hashes"] = hash
        resp = self._req("GET", "/api/v2/torrents/info", params=params)
        return resp.json()

    def get_categories(self) -> dict:
        """
        Return qBittorrent's configured categories.
        Response is a dict keyed by category name:
          {"tv-sonarr": {"name": "tv-sonarr", "savePath": "/downloads/tv"}, ...}
        An empty string key represents the uncategorized bucket in some versions.
        """
        resp = self._req("GET", "/api/v2/torrents/categories")
        return resp.json()

    def set_torrent_category(self, hash: str, category: str) -> bool:
        """Set the category for a torrent. Pass category="" to uncategorize."""
        try:
            self._req(
                "POST", "/api/v2/torrents/setCategory",
                data={"hashes": hash, "category": category},
            )
            return True
        except Exception as exc:
            log.warning("qBit set_category failed for %s: %s", hash[:12], exc)
            return False

    def pause_torrent(self, hash: str) -> bool:
        """
        Pause/stop a torrent. Tries the qBit 5.x /stop endpoint first,
        falls back to the 4.x /pause endpoint for older installations.
        """
        try:
            self._req("POST", "/api/v2/torrents/stop", data={"hashes": hash})
            return True
        except Exception:
            try:
                self._req("POST", "/api/v2/torrents/pause", data={"hashes": hash})
                return True
            except Exception as exc:
                log.warning("qBit pause failed for %s: %s", hash[:12], exc)
                return False

    def resume_torrent(self, hash: str) -> bool:
        """
        Resume a torrent. Tries the qBit 5.x /start endpoint first,
        falls back to the 4.x /resume endpoint for older installations.
        """
        try:
            self._req("POST", "/api/v2/torrents/start", data={"hashes": hash})
            return True
        except Exception:
            try:
                self._req("POST", "/api/v2/torrents/resume", data={"hashes": hash})
                return True
            except Exception as exc:
                log.warning("qBit resume failed for %s: %s", hash[:12], exc)
                return False

    def get_torrent_properties(self, hash: str) -> dict:
        """
        Return detailed properties for a single torrent.
        Includes: save_path, addition_date, completion_date, share_ratio,
        dl_speed_avg, up_speed_avg, nb_connections, total_wasted, eta, comment.
        """
        resp = self._req("GET", "/api/v2/torrents/properties", params={"hash": hash})
        return resp.json()

    def get_torrent_trackers(self, hash: str) -> list[dict]:
        """
        Return tracker list for a torrent.
        Each item includes: url, status (0=disabled,1=not contacted,2=working,
        3=too many requests,4=not working,5=not registered), num_peers,
        num_seeds, num_leeches, msg.
        """
        resp = self._req("GET", "/api/v2/torrents/trackers", params={"hash": hash})
        return resp.json()

    def get_torrents_by_category(self, category: str) -> list[dict]:
        """Return all torrents in a given category."""
        resp = self._req("GET", "/api/v2/torrents/info", params={"category": category})
        return resp.json()

    def get_torrent_files(self, hash: str) -> list[dict]:
        """
        Return file list for a torrent.
        Each item contains at minimum: name (str), size (int).
        """
        resp = self._req("GET", "/api/v2/torrents/files", params={"hash": hash})
        return resp.json()

    # Environment hook for filesystem verification. Format is a
    # comma-separated list of "<path as the client sees it>:<path as
    # inspectarr sees it>", e.g. "/downloads:/downloads". Empty (the default)
    # means the payload path is not reachable from this process and only the
    # client-side check runs.
    _PATH_MAP_ENV = "INSPECTARR_PATH_MAP"

    def _local_path(self, client_path: str) -> str | None:
        """
        Translate a path as the torrent client sees it into one this process
        can stat. Returns None when no mapping applies or the mapped root is
        not mounted here -- which means "cannot verify", not "not deleted".
        """
        raw = os.environ.get(self._PATH_MAP_ENV, "").strip()
        if not raw or not client_path:
            return None
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            src, dst = pair.split(":", 1)
            src, dst = src.strip(), dst.strip()
            if src and dst and client_path.startswith(src):
                root = dst.rstrip("/") or "/"
                if not os.path.isdir(root):
                    return None
                return client_path.replace(src, dst, 1)
        return None

    def delete_torrent(
        self,
        hash: str,
        delete_files: bool = True,
        verify: bool = True,
        verify_timeout: float = 20.0,
    ) -> bool:
        """
        Delete a torrent from qBittorrent and confirm it actually went away.

        qBittorrent answers POST /torrents/delete with 200 whatever happens --
        including for a hash it does not hold, and when it could not unlink the
        payload -- so the HTTP status is not evidence of anything. Callers read
        the return value of this method as "the bad thing is gone" and report it
        to the user in exactly those words, so it has to mean that:

          0. the client actually held the torrent when we asked (deleting a
             hash qBittorrent does not have is a no-op that still answers
             200, and leaves the payload orphaned on disk -- this is how
             untracked .exe files survived a "deleted" notification),
          1. the torrent is no longer listed by the client, and
          2. when delete_files is set and the payload path is reachable from
             this process, the payload is no longer on disk.

        Returns True only when every check available to us passed. A False
        return puts the caller on its existing failure path: a
        ``client_delete_failed`` event, an error notification, ``action=
        'failed'`` (which stays re-eligible for the next scan) and the retry
        queue -- rather than a terminal 'deleted' record for a file that is
        still there.
        """
        # --- check 0: does the client actually hold this torrent? ---------
        # A delete for an unknown hash succeeds loudly and does nothing. If we
        # skip this, an orphaned payload reads as a clean deletion.
        content_path = None
        present = None
        try:
            info = self._req(
                "GET", "/api/v2/torrents/info", params={"hashes": hash}
            ).json()
            present = bool(info)
            if info:
                content_path = info[0].get("content_path") or None
        except Exception as exc:
            log.warning(
                "qBit: could not query %s before deleting it: %s", hash[:12], exc
            )

        if verify and present is False:
            log.error(
                "qBit was asked to delete %s but the client does not hold that "
                "torrent -- nothing was deleted and any payload on disk is "
                "unaccounted for; reporting failure", hash[:12],
            )
            return False

        if verify and present is None:
            log.error(
                "qBit: could not confirm whether %s is in the client, so a "
                "delete cannot be vouched for; reporting failure", hash[:12],
            )
            return False

        try:
            self._req(
                "POST", "/api/v2/torrents/delete",
                data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
            )
        except requests.HTTPError as exc:
            log.warning("qBit delete failed for %s: %s", hash[:12], exc)
            return False

        if not verify:
            return True

        # --- check 1: the torrent left the client -------------------------
        deadline = time.monotonic() + verify_timeout
        gone = False
        while True:
            try:
                still = self._req(
                    "GET", "/api/v2/torrents/info", params={"hashes": hash}
                ).json()
            except Exception as exc:
                log.error(
                    "qBit: could not verify deletion of %s: %s", hash[:12], exc
                )
                return False
            if not still:
                gone = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)

        if not gone:
            log.error(
                "qBit accepted the delete for %s but the torrent is STILL in "
                "the client after %.0fs -- reporting failure",
                hash[:12], verify_timeout,
            )
            return False

        # --- check 2: the payload left the disk ---------------------------
        if content_path:
            local = self._local_path(content_path)
            if local is None:
                log.info(
                    "qBit delete verified for %s (client only; payload path %r "
                    "is not visible to inspectarr -- set %s to verify the "
                    "filesystem too)",
                    hash[:12], content_path, self._PATH_MAP_ENV,
                )
                return True

            # A network share caches attributes (this deployment mounts the
            # download tree over CIFS), so a stat taken immediately after the
            # unlink can still return the old entry. Re-check for a few
            # seconds before calling it a failure.
            fs_deadline = time.monotonic() + 10.0
            while os.path.exists(local) and time.monotonic() < fs_deadline:
                time.sleep(1.0)
            if os.path.exists(local):
                log.error(
                    "qBit removed torrent %s but the payload is STILL on disk: "
                    "%s -- reporting failure", hash[:12], local,
                )
                return False
            log.info(
                "qBit delete verified for %s (torrent and payload both gone)",
                hash[:12],
            )
        return True

    def test_connection(self) -> bool:
        """Verify credentials and connectivity."""
        try:
            self._login()
            return True
        except Exception as exc:
            log.warning("qBit test_connection failed: %s", exc)
            return False
