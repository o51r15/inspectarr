"""
core/deluge.py — Deluge Web UI JSON-RPC client.

Implements AbstractTorrentClient for Deluge 2.x.

Auth: POST to /json with method "auth.login" + password.
All subsequent calls go through the same /json endpoint as JSON-RPC v1.

The Web UI proxies to the Deluge daemon's RPC API, so we can use
core.* methods (core.get_torrents_status, core.pause_torrent, etc.)
through the web interface.

Reference: https://deluge.readthedocs.io/en/latest/reference/webapi.html
           https://deluge.readthedocs.io/en/latest/reference/api.html
"""

import logging
import requests

from .torrent_client import AbstractTorrentClient, TorrentClientError

log = logging.getLogger("inspectarr")


class DelugeError(TorrentClientError):
    pass


# Deluge state strings → normalised states
_STATE_MAP = {
    "Downloading":  "downloading",
    "Seeding":      "seeding",
    "Paused":       "paused",
    "Checking":     "checking",
    "Queued":       "queued",
    "Error":        "error",
    "Moving":       "downloading",   # treat as active
    "Allocating":   "checking",
}


class DelugeClient(AbstractTorrentClient):
    """
    Thin wrapper around Deluge's Web UI JSON-RPC API.
    Handles cookie-based auth with automatic re-login on failure.
    """

    # Fields we ask for when listing torrents
    _LIST_FIELDS = [
        "hash", "name", "total_size", "progress", "state",
        "label", "download_payload_rate", "upload_payload_rate",
        "ratio", "eta", "time_added", "num_seeds", "num_peers",
    ]

    # Fields for detailed properties
    _DETAIL_FIELDS = [
        "hash", "name", "total_size", "save_path", "time_added",
        "comment", "ratio", "eta", "state", "tracker_host",
    ]

    def __init__(self, url: str, password: str):
        self.url = url.rstrip("/")
        self.password = password
        self.session = requests.Session()
        self._request_id = 0
        self._authenticated = False

    # ------------------------------------------------------------------
    # Low-level RPC
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(self, method: str, params: list | None = None) -> dict:
        """
        Send a JSON-RPC v1 request to the Deluge Web UI.
        Re-authenticates on failure.
        """
        if not self._authenticated:
            self._login()

        result = self._do_call(method, params or [])

        # If error suggests auth failure, retry once
        if result is None:
            self._authenticated = False
            self._login()
            result = self._do_call(method, params or [])

        return result

    def _do_call(self, method: str, params: list) -> dict | None:
        payload = {
            "method": method,
            "params": params,
            "id": self._next_id(),
        }
        try:
            resp = self.session.post(
                f"{self.url}/json",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise DelugeError(f"HTTP error: {exc}")

        body = resp.json()
        if body.get("error"):
            err = body["error"]
            msg = err.get("message", str(err))
            # Auth errors return code 1
            if err.get("code") == 1:
                return None
            raise DelugeError(f"RPC error: {msg}")

        return body.get("result")

    def _login(self):
        """Authenticate to the Deluge Web UI."""
        payload = {
            "method": "auth.login",
            "params": [self.password],
            "id": self._next_id(),
        }
        try:
            resp = self.session.post(
                f"{self.url}/json",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise DelugeError(f"Login HTTP error: {exc}")

        body = resp.json()
        if body.get("error"):
            raise DelugeError(f"Login error: {body['error']}")
        if not body.get("result"):
            raise DelugeError("Login rejected — check password")

        self._authenticated = True

        # Deluge Web UI requires connecting to a daemon
        # Try to auto-connect to the first available host
        try:
            hosts = self._do_call("web.get_hosts", [])
            if hosts:
                host_id = hosts[0][0]
                # Check if already connected
                connected = self._do_call("web.connected", [])
                if not connected:
                    self._do_call("web.connect", [host_id])
        except Exception:
            pass  # If already connected or single-daemon setup, this is fine

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def _normalise_torrent(self, hash: str, t: dict) -> dict:
        """Convert Deluge torrent status dict to normalised format."""
        state_raw = t.get("state", "")
        state = _STATE_MAP.get(state_raw, "unknown")

        return {
            "hash": hash.lower(),
            "name": t.get("name", ""),
            "size": t.get("total_size", 0),
            "progress": round(t.get("progress", 0) / 100.0, 4),
            "state": state,
            "category": t.get("label", ""),
            "dlspeed": int(t.get("download_payload_rate", 0)),
            "upspeed": int(t.get("upload_payload_rate", 0)),
            "added_on": int(t.get("time_added", 0)),
            "eta": t.get("eta", -1),
            "ratio": t.get("ratio", 0),
            "num_seeds": t.get("num_seeds", 0),
            "num_leechs": t.get("num_peers", 0),
        }

    # ------------------------------------------------------------------
    # AbstractTorrentClient implementation
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        try:
            self._authenticated = False
            self._login()
            return True
        except Exception as exc:
            log.warning("Deluge test_connection failed: %s", exc)
            return False

    def get_all_torrents(self, hash: str | None = None) -> list[dict]:
        filter_dict = {}
        if hash:
            filter_dict["id"] = [hash]
        result = self._rpc("core.get_torrents_status", [
            filter_dict, self._LIST_FIELDS
        ])
        if not result or not isinstance(result, dict):
            return []
        return [
            self._normalise_torrent(h, data)
            for h, data in result.items()
        ]

    def get_torrents_by_category(self, category: str) -> list[dict]:
        # Use label filter
        result = self._rpc("core.get_torrents_status", [
            {"label": category}, self._LIST_FIELDS
        ])
        if not result or not isinstance(result, dict):
            return []
        return [
            self._normalise_torrent(h, data)
            for h, data in result.items()
        ]

    def get_categories(self) -> dict:
        """
        Deluge uses the Label plugin. If enabled, get label list.
        Falls back to scanning all torrents for unique labels.
        """
        try:
            labels = self._rpc("label.get_labels", [])
            if labels and isinstance(labels, list):
                return {l: {"name": l} for l in labels if l}
        except Exception:
            pass

        # Fallback: scan torrents
        result = self._rpc("core.get_torrents_status", [
            {}, ["label"]
        ])
        if not result or not isinstance(result, dict):
            return {}
        cats: dict[str, dict] = {}
        for data in result.values():
            label = data.get("label", "")
            if label and label not in cats:
                cats[label] = {"name": label}
        return cats

    def set_torrent_category(self, hash: str, category: str) -> bool:
        """Set label on a torrent via the Label plugin."""
        try:
            self._rpc("label.set_torrent", [hash, category])
            return True
        except Exception as exc:
            log.warning("Deluge set_category failed for %s: %s", hash[:12], exc)
            return False

    def pause_torrent(self, hash: str) -> bool:
        try:
            self._rpc("core.pause_torrent", [[hash]])
            return True
        except Exception as exc:
            log.warning("Deluge pause failed for %s: %s", hash[:12], exc)
            return False

    def resume_torrent(self, hash: str) -> bool:
        try:
            self._rpc("core.resume_torrent", [[hash]])
            return True
        except Exception as exc:
            log.warning("Deluge resume failed for %s: %s", hash[:12], exc)
            return False

    def _payload_path(self, hash: str) -> str | None:
        try:
            result = self._rpc("core.get_torrents_status",
                               [{"id": [hash]}, ["name", "save_path"]])
            if not result or not isinstance(result, dict):
                return None
            d = next(iter(result.values()), None)
            if not d or not d.get("save_path") or not d.get("name"):
                return None
            return d["save_path"].rstrip("/") + "/" + d["name"]
        except Exception:
            return None

    def delete_torrent(self, hash: str, delete_files: bool = True,
                       verify: bool = True, verify_timeout: float = 20.0) -> bool:
        """Remove a torrent from Deluge and confirm it actually went away."""
        content_path = self._payload_path(hash) if delete_files else None
        if verify and not self._precheck_delete(hash):
            return False
        try:
            self._rpc("core.remove_torrent", [hash, delete_files])
        except Exception as exc:
            log.warning("Deluge delete failed for %s: %s", hash[:12], exc)
            return False
        return True if not verify else self._confirm_deleted(
            hash, content_path, verify_timeout)

    def get_torrent_files(self, hash: str) -> list[dict]:
        result = self._rpc("web.get_torrent_files", [hash])
        if not result:
            return []

        # Deluge returns a nested tree structure. Flatten it.
        files = []
        self._flatten_file_tree(result, files)
        return files

    def _flatten_file_tree(self, node: dict, out: list, prefix: str = ""):
        """Recursively flatten Deluge's nested file tree into [{name, size}]."""
        if "type" in node and node["type"] == "file":
            out.append({
                "name": prefix + node.get("filename", ""),
                "size": node.get("size", 0),
            })
            return

        contents = node.get("contents", {})
        for name, child in contents.items():
            child_prefix = f"{prefix}{name}/" if "contents" in child else prefix
            if "contents" in child:
                self._flatten_file_tree(child, out, child_prefix)
            else:
                out.append({
                    "name": prefix + name,
                    "size": child.get("size", 0),
                })

    def get_torrent_properties(self, hash: str) -> dict:
        result = self._rpc("core.get_torrent_status", [
            hash, self._DETAIL_FIELDS
        ])
        if not result:
            return {}
        return {
            "save_path": result.get("save_path", ""),
            "addition_date": int(result.get("time_added", 0)),
            "completion_date": 0,  # Deluge doesn't track this directly
            "share_ratio": result.get("ratio", 0),
            "eta": result.get("eta", -1),
            "comment": result.get("comment", ""),
        }

    def get_torrent_trackers(self, hash: str) -> list[dict]:
        result = self._rpc("core.get_torrent_status", [
            hash, ["trackers"]
        ])
        if not result:
            return []
        trackers = result.get("trackers", [])
        return [
            {
                "url": t.get("url", ""),
                "status": 2 if t.get("tier", 0) >= 0 else 4,
                "num_peers": 0,
                "msg": "",
            }
            for t in trackers
            if t.get("url", "").startswith("http")
        ]
