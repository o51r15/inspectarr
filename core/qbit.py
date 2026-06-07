import requests


class QBittorrentError(Exception):
    pass


class QBittorrentClient:
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

    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        """Delete torrent from qBittorrent. Returns True on success."""
        try:
            self._req(
                "POST", "/api/v2/torrents/delete",
                data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
            )
            return True
        except requests.HTTPError:
            return False

    def test_connection(self) -> bool:
        """Verify credentials and connectivity."""
        try:
            self._login()
            return True
        except Exception:
            return False
