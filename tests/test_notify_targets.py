"""Notifications: places to send to (DockLlama's design), the old-config
conversion, per-place routing, and the stopped / stopped-unexpectedly messages."""
import pytest

from core import notify_targets as nt
from tests.conftest import read_config, write_config

NTFY = {"topic": "inspectarr-abc123", "server": "https://ntfy.sh"}
HOOK = "https://discord.com/api/webhooks/123/abcDEF-ghi"


# --- building addresses ------------------------------------------------------

@pytest.mark.parametrize("service,fields,url", [
    ("ntfy", NTFY, "ntfys://ntfy.sh/inspectarr-abc123"),
    ("ntfy", {"topic": "t1", "server": "http://box:8080", "token": "tk n"}, "ntfy://box:8080/t1?token=tk%20n"),
    ("discord", {"webhook": HOOK}, HOOK),
    ("telegram", {"bot_token": "123:AbC", "chat_id": "-42"}, "tgram://123:AbC/-42"),
    ("gotify", {"server": "gotify.lan", "app_token": "T"}, "gotifys://gotify.lan/T"),
    ("pushover", {"user_key": "U", "app_token": "A"}, "pover://U@A"),
    ("email", {"address": "me@example.com", "password": "p@ss"},
     "mailtos://me:p%40ss@example.com?to=me@example.com"),
    ("apprise", {"url": "json://host/x"}, "json://host/x"),
])
def test_build_url(service, fields, url):
    assert nt.build_url(service, fields) == url


@pytest.mark.parametrize("service,fields,words", [
    ("ntfy", {"topic": "has space"}, "letters, numbers"),
    ("discord", {"webhook": "https://example.com/x"}, "isn't a Discord webhook"),
    ("telegram", {"bot_token": "nope", "chat_id": "1"}, "123456789:"),
    ("email", {"address": "nope", "password": "x"}, "email address"),
    ("pushover", {"user_key": "U"}, "Fill in the app token"),
    ("carrier-pigeon", {}, "Pick one"),
])
def test_build_url_explains_mistakes(service, fields, words):
    with pytest.raises(nt.NotifyError, match=words):
        nt.build_url(service, fields)


def test_descriptions_never_show_secrets():
    assert nt.describe("discord", {"webhook": HOOK}) == "Discord webhook …EF-ghi"
    assert "p@ss" not in nt.describe("email", {"address": "me@example.com", "password": "p@ss"})
    assert nt.describe("apprise", {"url": "tgram://SECRET/1"}) == "Apprise address (tgram://…)"
    assert nt.describe("pushover", {"user_key": "USERKEY99"}) == "Pushover user …EY99"


def test_failures_in_plain_words():
    log = "Sending ntfy notification. Failed to send ntfy notification: error=401."
    assert "token or password" in nt.explain_failure(log, "ntfys://ntfy.sh/x")
    log = "Socket Exception: ... host='gotify.lan' ... Name or service not known"
    assert "doesn't resolve" in nt.explain_failure(log, "gotify://gotify.lan/T")
    assert nt.explain_failure("", "pover://a@b") == nt.GENERIC_FAILURE


def test_topic_is_hard_to_guess():
    a, b = nt.new_ntfy_topic(), nt.new_ntfy_topic()
    assert a != b and a.startswith("inspectarr-") and len(a) > 20


# --- reading config ----------------------------------------------------------

def test_old_config_is_read_as_places():
    old = {"enabled": True, "urls": ["json://a/1", "json://b/2"], "notify_on": ["action", "quarantine"]}
    targets = nt.parse_targets(old, summary_enabled=True)
    assert [t["url"] for t in targets] == ["json://a/1", "json://b/2"]
    assert targets[0]["events"] == ["action", "quarantine", "summary"]     # summary went to every URL
    assert all(t["enabled"] and t["service"] == "apprise" for t in targets)


def test_old_config_switched_off_stays_off():
    targets = nt.parse_targets({"enabled": False, "urls": ["json://a/1"]})
    assert targets[0]["enabled"] is False and targets[0]["events"] == ["action", "error"]


def test_incomplete_place_is_kept_but_sends_nothing():
    t = nt.parse_targets({"targets": [{"id": 3, "service": "discord", "fields": {}, "events": ["error"]}]})[0]
    assert t["url"] is None and not nt.wants(t, "error")


def test_wants():
    t = {"enabled": True, "url": "json://x", "events": ["action", "error", "startup"]}
    assert nt.wants(t, "action") and nt.wants(t, "quarantine")          # a hold also goes to "removals"
    assert nt.wants(t, "retry_exhausted") and nt.wants(t, "shutdown") and nt.wants(t, "unexpected_stop")
    assert not nt.wants(t, "summary") and not nt.wants({**t, "enabled": False}, "action")


def test_config_parser_works_out_the_old_fields():
    from core.config import _parse_config
    from tests.conftest import BASE_CONFIG
    import copy
    raw = copy.deepcopy(BASE_CONFIG)
    raw["notifications"]["apprise"] = {"targets": [
        {"id": 1, "service": "apprise", "fields": {"url": "json://a/1"}, "events": ["action", "summary"]},
        {"id": 2, "service": "apprise", "fields": {"url": "json://b/2"}, "events": ["error"], "enabled": False}]}
    cfg = _parse_config(raw)
    assert cfg.notifications.apprise.enabled is True
    assert cfg.notifications.apprise.urls == ["json://a/1"]
    assert cfg.notifications.summary.enabled is True                    # a place asked for it


# --- rewriting an old config.yaml -----------------------------------------------

def test_migration_rewrites_once(config_path):
    data = read_config(config_path)
    data["notifications"]["apprise"] = {"enabled": True, "urls": ["json://a/1"],
                                        "notify_on": ["action", "quarantine"]}
    data["notifications"]["summary"] = {"enabled": True, "schedule": "weekly", "use_ollama": True}
    write_config(config_path, data)
    assert "1" in nt.migrate_config(config_path)
    after = read_config(config_path)
    assert after["notifications"]["apprise"] == {"targets": [
        {"id": 1, "service": "apprise", "fields": {"url": "json://a/1"},
         "events": ["action", "quarantine", "summary"], "enabled": True}]}
    assert after["notifications"]["summary"] == {"schedule": "weekly", "use_ollama": True}
    assert after["rules"] == data["rules"]                               # the rest untouched
    assert nt.migrate_config(config_path) is None                        # once only


# --- sending -----------------------------------------------------------------------

class _State:
    def __init__(self):
        self.status, self.notices = {}, []

    def set_notify_status(self, tid, ok, problem=""):
        self.status[tid] = (ok, problem)

    def record_notice(self, *a):
        self.notices.append(a)


def _notifier(monkeypatch, targets, sent, fail=()):
    from core.config import _parse_config
    from core.notifier import Notifier
    from tests.conftest import BASE_CONFIG
    import copy
    import core.notifier
    raw = copy.deepcopy(BASE_CONFIG)
    raw["notifications"]["apprise"] = {"targets": targets}

    def fake_send(url, title, body):
        sent.append((url, title))
        return (False, "it said no") if url in fail else (True, "")
    monkeypatch.setattr(core.notifier, "send_now", fake_send)
    state = _State()
    return Notifier(_parse_config(raw), state=state), state


PLACES = [
    {"id": 1, "service": "apprise", "fields": {"url": "json://phone/1"}, "events": ["action", "error"]},
    {"id": 2, "service": "apprise", "fields": {"url": "json://mail/2"}, "events": ["summary"]},
    {"id": 3, "service": "apprise", "fields": {"url": "json://off/3"}, "events": ["action"], "enabled": False},
]


def test_each_event_goes_only_where_it_was_asked_for(monkeypatch):
    sent = []
    n, state = _notifier(monkeypatch, PLACES, sent)
    n.notify_action("Bad.Release", ["x.exe"], True, True, "sonarr")
    assert [u for u, _ in sent] == ["json://phone/1"]                   # not mail, not the paused one
    assert state.status == {1: (True, "")}
    sent.clear()
    n._send("Daily summary", "body", event="summary")
    assert [u for u, _ in sent] == ["json://mail/2"]


def test_one_place_failing_is_recorded_on_that_place(monkeypatch):
    sent = []
    places = [dict(PLACES[0]), {**PLACES[1], "events": ["error"]}]
    n, state = _notifier(monkeypatch, places, sent, fail={"json://mail/2"})
    n.notify_error("Sonarr", "unreachable")
    assert state.status == {1: (True, ""), 2: (False, "it said no")}
    # record_notice(channel, event, title, body, status, detail)
    assert state.notices[-1][4] == "sent" and "some places failed" in state.notices[-1][5]


def test_nobody_asked_is_recorded_as_suppressed(monkeypatch):
    sent = []
    n, state = _notifier(monkeypatch, PLACES, sent)
    n.notify_dry_run("Bad.Release", ["x.exe"])
    assert sent == [] and state.notices == []                            # dry_run: nobody wants it
    n._send("t", "b", event="dry_run")
    assert state.notices[-1][4] == "suppressed"


def test_digest_sends_each_place_only_its_events(monkeypatch):
    sent = []
    places = [{"id": 1, "service": "apprise", "fields": {"url": "json://a/1"}, "events": ["action"]},
              {"id": 2, "service": "apprise", "fields": {"url": "json://b/2"}, "events": ["error"]}]
    n, _ = _notifier(monkeypatch, places, sent)
    n.digest_cfg.enabled = True
    n.notify_action("Bad.Release", ["x.exe"], True, True)
    n.notify_error("Radarr", "down")
    n.flush_digest()
    assert sorted(u for u, _ in sent) == ["json://a/1", "json://b/2"]   # one message each


# --- stopped / stopped unexpectedly ----------------------------------------------

class _AppState(dict):
    def get_app_state(self, k):
        return self.get(k)

    def set_app_state(self, k, v):
        self[k] = v


def test_unexpected_stop_is_noticed_at_the_next_start(monkeypatch, config_path):
    from core import lifecycle
    told = []
    import core.notifier
    monkeypatch.setattr(core.notifier.Notifier, "notify_unexpected_stop",
                        lambda self, since: told.append(since))
    monkeypatch.setattr(core.notifier.Notifier, "notify_shutdown", lambda self, reason="": told.append("stop"))
    st = _AppState()
    assert lifecycle.on_start(st, config_path) is False                  # first ever start
    assert st[lifecycle.RUNNING_KEY] == "1"
    assert lifecycle.on_start(st, config_path) is True                   # never said goodbye
    assert len(told) == 1
    lifecycle.on_stop(st, config_path, "asked to stop")
    assert told[-1] == "stop" and st[lifecycle.RUNNING_KEY] == "0"
    assert lifecycle.on_start(st, config_path) is False                  # clean stop: no alarm


# --- the page's endpoints ----------------------------------------------------------

@pytest.fixture
def sends(monkeypatch):
    import core.notify_targets
    out = []

    def fake(url, title, body):
        out.append(url)
        return (True, "")
    monkeypatch.setattr(core.notify_targets, "send_now", fake)
    return out


def test_routes_add_change_test_remove(client, config_path, app, sends):
    d = client.get("/config/notify").get_json()
    assert d["targets"] == [] and [s["id"] for s in d["services"]][0] == "ntfy"
    bad = client.post("/config/notify/targets", json={"service": "discord", "fields": {"webhook": "nope"}})
    assert bad.status_code == 400 and "Discord webhook" in bad.get_json()["message"]

    added = client.post("/config/notify/targets", json={
        "service": "ntfy", "fields": {**NTFY, "junk": "dropped"}, "events": ["action", "nonsense"]}).get_json()
    assert added["ok"] and added["id"] == 1
    stored = read_config(config_path)["notifications"]["apprise"]["targets"][0]
    assert stored == {"id": 1, "service": "ntfy", "fields": NTFY, "events": ["action"], "enabled": True}

    assert client.post("/config/notify/targets/1", json={"events": {"error": True, "action": False}}).get_json()["ok"]
    assert client.post("/config/notify/targets/1", json={"enabled": False}).get_json()["ok"]
    stored = read_config(config_path)["notifications"]["apprise"]["targets"][0]
    assert stored["events"] == ["error"] and stored["enabled"] is False

    r = client.post("/config/notify/targets/1/test").get_json()
    assert r["ok"] and sends == ["ntfys://ntfy.sh/inspectarr-abc123"]
    view = client.get("/config/notify").get_json()["targets"][0]
    assert view["last_ok"] and view["last_error"] is None and view["subscribe"].endswith("/inspectarr-abc123")

    assert client.post("/config/notify/test", json={"service": "pushover", "fields": {"user_key": "U"}}
                       ).get_json() == {"ok": False, "problem": "Fill in the app token."}
    assert client.post("/config/notify/targets/1/delete").get_json()["ok"]
    assert read_config(config_path)["notifications"]["apprise"]["targets"] == []
    assert client.post("/config/notify/targets/1/delete").status_code == 404


def test_qr_only_for_links(client):
    assert client.get("/config/notify/qr?text=javascript:alert(1)").status_code == 400
    r = client.get("/config/notify/qr?text=https://ntfy.sh/inspectarr-abc")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.data
