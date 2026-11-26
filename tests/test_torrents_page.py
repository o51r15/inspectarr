"""Torrents list and detail render real data, and actions reach the client."""
import pytest

HASH = "A" * 40


@pytest.fixture
def with_torrent(fake_client):
    fake_client.torrents = [{
        "hash": HASH, "name": "Show.S01E01.1080p <b>x</b>", "state": "stalledDL",
        "category": "tv-sonarr", "size": 1_500_000_000, "progress": 0.42,
        "dlspeed": 0, "upspeed": 0, "ratio": 0.5, "eta": 7300,
        "num_seeds": 1, "num_complete": 9, "num_leechs": 0, "num_incomplete": 3,
    }]
    return fake_client


def test_list_shows_state_progress_and_escapes_names(client, with_torrent):
    html = client.get("/torrents").get_data(as_text=True)
    assert "Stalled" in html
    assert 'data-pct="42"' in html
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_detail_page(client, with_torrent):
    html = client.get(f"/torrents/{HASH}").get_data(as_text=True)
    assert "2 h 1 min" in html          # eta 7300 s through the duration filter
    assert "Stalled" in html


def test_detail_rejects_bad_hash(client):
    assert client.get("/torrents/not-a-hash").status_code == 400


def test_actions_reach_the_client(client, with_torrent):
    for action in ("pause", "resume", "delete"):
        r = client.post(f"/torrents/{action}", json={"hash": HASH})
        assert r.get_json()["ok"] is True
    assert [c[0] for c in with_torrent.calls] == ["pause", "resume", "delete"]


@pytest.mark.parametrize("secs,text", [(45, "45 s"), (600, "10 min"), (7300, "2 h 1 min"),
                                       (90000, "1 d 1 h"), (0, "—"), (None, "—"), ("x", "—")])
def test_duration_filter(app, secs, text):
    assert app.jinja_env.filters["duration"](secs) == text
