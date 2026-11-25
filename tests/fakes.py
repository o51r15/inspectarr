"""
tests/fakes.py -- stand-ins for external services.

FakeTorrentClient subclasses the real ABC on purpose: if a method is ever
added to AbstractTorrentClient, instantiating the fake fails and the tests
say so, instead of the fake silently drifting from the interface it mimics.
"""
from core.torrent_client import AbstractTorrentClient


class FakeTorrentClient(AbstractTorrentClient):
    """Answers every call with an empty-but-valid result. Records actions."""

    def __init__(self, torrents=None, categories=None):
        self.torrents = list(torrents or [])
        self.categories = dict(categories or {"tv-sonarr": {"name": "tv-sonarr"}})
        self.calls: list[tuple] = []

    def test_connection(self) -> bool:
        return True

    def get_all_torrents(self, hash: str | None = None) -> list[dict]:
        if hash:
            return [t for t in self.torrents if t.get("hash") == hash]
        return list(self.torrents)

    def get_torrents_by_category(self, category: str) -> list[dict]:
        return [t for t in self.torrents if t.get("category") == category]

    def get_categories(self) -> dict:
        return dict(self.categories)

    def pause_torrent(self, hash: str) -> bool:
        self.calls.append(("pause", hash))
        return True

    def resume_torrent(self, hash: str) -> bool:
        self.calls.append(("resume", hash))
        return True

    def delete_torrent(self, hash: str, delete_files: bool = True,
                       verify: bool = True, verify_timeout: float = 20.0) -> bool:
        self.calls.append(("delete", hash, delete_files))
        return True

    def set_torrent_category(self, hash: str, category: str) -> bool:
        self.calls.append(("set_category", hash, category))
        return True

    def get_torrent_files(self, hash: str) -> list[dict]:
        return []

    def get_torrent_properties(self, hash: str) -> dict:
        return {}

    def get_torrent_trackers(self, hash: str) -> list[dict]:
        return []
