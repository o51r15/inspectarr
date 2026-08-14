"""
core/transmission.py — Transmission RPC client (v4.x JSON-RPC 2.0).

Implements AbstractTorrentClient for Transmission.

Auth: HTTP Basic (optional, configured per-instance).
CSRF: X-Transmission-Session-Id header; on 409, capture the header and retry.
Default URL: http://host:9091/transmission/rpc

Transmission uses "labels" (array of strings) as its category equivalent.
We treat the first label as the category for Inspectarr's purposes.

Reference: https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md
"""

import logging
import requests

from .torrent_client import AbstractTorrentClient, TorrentClientError

log = logging.getLogger("inspectarr")


class TransmissionError(TorrentClientError):
    pass


# Transmission status codes → normalised states
_STATUS_MAP = {
    0: "stopped",
    1: "queued",      # queued to verify
    2: "checking",
    3: "queued",      # queued to download
    4: "downloading",
    5: "queued",      # queued to seed
    6: "seeding",
}


class TransmissionClient(AbstractTorrentClient):
    """
    Thin wrapper around Transmission's JSON-RPC 2.0 API.
    Handles CSRF token (X-Transmission-Session-Id) automatically.
    """

    # Fields we request from torrent_get — covers everything the UI and
    # scanner need.  Kept minimal to avoid pulling huge peer/piece arrays.
    _TORRENT_FIELDS = [
        "id", "hash_string", "name", "total_size", "percent_done",
        "status", "labels", "rate_download", "rate_upload",
        "added_date", "eta", "upload_ratio", "error", "error_string",
        "download_dir", "is_stalled", "peers_connected",
        "peers_sending_to_us", "peers_getting_from_us",
    ]

    _FILE_FIELDS = ["files"]

    def __init__(self, url: str, username: str = "", password: str = ""):
        self.url = url.rstrip("/")
        if not self.url.endswith("/rpc"):
            self.url += "/transmission/rpc"
        self.session = requests.Session()
        if username:
            self.session.auth = (username, password)
        self._csrf_token: str = ""

    # ------------------------------------------------------------------
    # Low-level RPC
    # ------------------------------------------------------------------

    def _rpc(self, method: str, arguments: dict | None = None) -> dict:
        """
        Send a JSON-RPC 2.0 request.  Handles 409 CSRF automatically.
        Returns the 'result' object from the response.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": 1,
        }
        if arguments:
            payload["params"] = arguments

        resp = self._do_post(payload)

        # 409 = need CSRF token
        if resp.status_code == 409:
            self._csrf_token = resp.headers.get(
                "X-Transmission-Session-Id", ""
            )
            if not self._csrf_token:
                raise TransmissionError("409 without X-Transmission-Session-Id header")
            resp = self._do_post(payload)

        if resp.status_code == 401:
            raise TransmissionError("Authentication failed (HTTP 401)")

        resp.raise_for_status()

        body = resp.json()

        # JSON-RPC 2.0 error
        if "error" in body:
            err = body["error"]
            msg = err.get("message", str(err))
            raise TransmissionError(f"RPC error: {msg}")

        return body.get("result", {})

    def _do_post(self, payload: dict) -> requests.Response:
        headers = {}
        if self._csrf_token:
            headers["X-Transmission-Session-Id"] = self._csrf_token
        return self.session.post(
            self.url, json=payload, headers=headers, timeout=15
        )

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def _normalise_torrent(self, t: dict) -> dict:
        """Convert Transmission torrent object to normalised format."""
        status_code = t.get("status", 0)
        state = _STATUS_MAP.get(status_code, "unknown")

        # Stalled detection
        if state == "downloading" and t.get("is_stalled"):
            state = "stalled"

        # Error override
        if t.get("error", 0) != 0:
            state = "error"

        labels = t.get("labels", [])
        category = labels[0] if labels else ""

        return {
            "hash": t.get("hash_string", "").lower(),
            "name": t.get("name", ""),
            "size": t.get("total_size", 0),
            "progress": round(t.get("percent_done", 0), 4),
            "state": state,
            "category": category,
            "dlspeed": t.get("rate_download", 0),
            "upspeed": t.get("rate_upload", 0),
            "added_on": t.get("added_date", 0),
            "eta": t.get("eta", -1),
            "ratio": t.get("upload_ratio", 0),
            "num_seeds": t.get("peers_sending_to_us", 0),
            "num_leechs": t.get("peers_getting_from_us", 0),
        }

    # ------------------------------------------------------------------
    # AbstractTorrentClient implementation
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        try:
            self._rpc("session_get", {"fields": ["version"]})
            return True
        except Exception as exc:
            log.warning("Transmission test_connection failed: %s", exc)
            return False

    def get_all_torrents(self, hash: str | None = None) -> list[dict]:
        args: dict = {"fields": self._TORRENT_FIELDS}
        if hash:
            args["ids"] = [hash]
        result = self._rpc("torrent_get", args)
        return [self._normalise_torrent(t) for t in result.get("torrents", [])]

    def get_torrents_by_category(self, category: str) -> list[dict]:
        all_torrents = self.get_all_torrents()
        return [t for t in all_torrents if t["category"] == category]

    def get_categories(self) -> dict:
        """
        Transmission has no first-class categories. We derive them from
        labels across all torrents.
        """
        all_torrents = self._rpc(
            "torrent_get", {"fields": ["labels"]}
        ).get("torrents", [])
        cats: dict[str, dict] = {}
        for t in all_torrents:
            for label in t.get("labels", []):
                if label and label not in cats:
                    cats[label] = {"name": label}
        return cats

    def set_torrent_category(self, hash: str, category: str) -> bool:
        """Set the first label on a torrent (replaces all labels)."""
        try:
            labels = [category] if category else []
            self._rpc("torrent_set", {
                "ids": [hash],
                "labels": labels,
            })
            return True
        except Exception as exc:
            log.warning("Transmission set_category failed for %s: %s", hash[:12], exc)
            return False

    def pause_torrent(self, hash: str) -> bool:
        try:
            self._rpc("torrent_stop", {"ids": [hash]})
            return True
        except Exception as exc:
            log.warning("Transmission pause failed for %s: %s", hash[:12], exc)
            return False

    def resume_torrent(self, hash: str) -> bool:
        try:
            self._rpc("torrent_start", {"ids": [hash]})
            return True
        except Exception as exc:
            log.warning("Transmission resume failed for %s: %s", hash[:12], exc)
            return False

    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        try:
            self._rpc("torrent_remove", {
                "ids": [hash],
                "delete_local_data": delete_files,
            })
            return True
        except Exception as exc:
            log.warning("Transmission delete failed for %s: %s", hash[:12], exc)
            return False

    def get_torrent_files(self, hash: str) -> list[dict]:
        result = self._rpc("torrent_get", {
            "ids": [hash],
            "fields": ["files"],
        })
        torrents = result.get("torrents", [])
        if not torrents:
            return []
        files = torrents[0].get("files", [])
        return [
            {"name": f.get("name", ""), "size": f.get("length", 0)}
            for f in files
        ]

    def get_torrent_properties(self, hash: str) -> dict:
        result = self._rpc("torrent_get", {
            "ids": [hash],
            "fields": [
                "id", "hash_string", "name", "total_size", "download_dir",
                "added_date", "done_date", "upload_ratio", "comment",
                "error", "error_string", "eta",
            ],
        })
        torrents = result.get("torrents", [])
        if not torrents:
            return {}
        t = torrents[0]
        return {
            "save_path": t.get("download_dir", ""),
            "addition_date": t.get("added_date", 0),
            "completion_date": t.get("done_date", 0),
            "share_ratio": t.get("upload_ratio", 0),
            "eta": t.get("eta", -1),
            "comment": t.get("comment", ""),
        }

    def get_torrent_trackers(self, hash: str) -> list[dict]:
        result = self._rpc("torrent_get", {
            "ids": [hash],
            "fields": ["trackers", "tracker_stats"],
        })
        torrents = result.get("torrents", [])
        if not torrents:
            return []
        stats = torrents[0].get("tracker_stats", [])
        return [
            {
                "url": s.get("announce", ""),
                "status": 2 if s.get("last_announce_succeeded") else 4,
                "num_peers": s.get("last_announce_peer_count", 0),
                "msg": s.get("last_announce_result", ""),
            }
            for s in stats
            if s.get("announce", "").startswith("http")
        ]
