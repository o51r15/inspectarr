import logging
import time

import requests

from .torrent_client import AbstractTorrentClient, TorrentClientError

log = logging.getLogger("inspectarr")

# How long to keep trying when qBittorrent cannot be reached at all.
#
# This is not a general-purpose retry. It exists for one specific, expected
# condition: qBittorrent is routed through a VPN sidecar (the usual gluetun
# setup puts it in the sidecar's network namespace), and every VPN reconnect
# takes its network away for a few seconds. During that window every call
# fails with a connection error, and a deletion that happens to land in it
# was being reported as a hard failure.
#
# One delay per entry, so the total worst case is the sum plus the request
# timeouts -- currently ~17s of waiting across four attempts.
CONNECT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)

# Only these are retried. An HTTP error means qBittorrent answered and said
# no, which retrying will not change; a connection error means nothing
# answered at all.
TRANSIENT_ERRORS = (requests.ConnectionError, requests.Timeout)


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
        """
        Perform a request, riding out a brief loss of the connection.

        Every endpoint this client uses is idempotent -- listing torrents,
        setting a category, pausing, and deleting a hash all have the same
        effect applied twice as applied once -- so a retry cannot double up
        an action. That is what makes retrying safe here, and it is the
        reason not to copy this wrapper somewhere that is not idempotent.

        When the attempts run out this raises QBittorrentError rather than
        the underlying requests exception, so callers can tell "the client is
        not there" apart from "the client refused".
        """
        last = None
        attempts = len(CONNECT_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            try:
                return self._req_once(method, endpoint, **kwargs)
            except TRANSIENT_ERRORS as exc:
                last = exc
                # The socket, and possibly the whole session, is gone. Force
                # a fresh login on the next attempt rather than reusing a
                # cookie against a connection that no longer exists.
                self._authenticated = False
                if attempt == attempts - 1:
                    break
                delay = CONNECT_BACKOFF_SECONDS[attempt]
                log.warning(
                    "qBittorrent unreachable (%s) -- retrying in %.0fs "
                    "(attempt %d of %d)",
                    exc.__class__.__name__, delay, attempt + 1, attempts,
                )
                time.sleep(delay)
        raise QBittorrentError(
            "qBittorrent unreachable after %d attempts over ~%.0fs: %s"
            % (attempts, sum(CONNECT_BACKOFF_SECONDS), last)
        ) from last

    def _req_once(self, method: str, endpoint: str, **kwargs) -> requests.Response:
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

    def _payload_path(self, hash: str) -> str | None:
        try:
            info = self._req("GET", "/api/v2/torrents/info",
                             params={"hashes": hash}).json()
            return info[0].get("content_path") or None if info else None
        except Exception:
            return None

    def delete_torrent(self, hash: str, delete_files: bool = True,
                       verify: bool = True, verify_timeout: float = 20.0) -> bool:
        """Delete a torrent from qBittorrent and confirm it actually went away."""
        content_path = self._payload_path(hash) if delete_files else None
        if verify and not self._precheck_delete(hash):
            return False
        try:
            self._req(
                "POST", "/api/v2/torrents/delete",
                data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
            )
        except (requests.HTTPError, TorrentClientError) as exc:
            log.warning("qBit delete failed for %s: %s", hash[:12], exc)
            return False
        return True if not verify else self._confirm_deleted(
            hash, content_path, verify_timeout)

    def test_connection(self) -> bool:
        """Verify credentials and connectivity."""
        try:
            self._login()
            return True
        except Exception as exc:
            log.warning("qBit test_connection failed: %s", exc)
            return False
