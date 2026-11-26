"""
3.4: the browser only ever holds a mask.

No stored secret -- passwords, API keys, the webhook secret, the web
password's hash, or a notification place's token, password or webhook --
may appear anywhere in the Settings HTML or the Notifications data. Saving with the mask keeps the stored value; the Test
buttons swap the mask for the stored value on the server.
"""
import pytest
import yaml

from tests.conftest import read_config, write_config
from tests.formparse import form_fields
from ui.settings_schema import SECRET_MASK

TOKENS = {
    ("qbittorrent", "password"): "QBIT-SECRET-111",
    ("arrs", "sonarr", "api_key"): "SONARR-SECRET-222",
    ("arrs", "radarr", "api_key"): "RADARR-SECRET-333",
    ("prowlarr", "api_key"): "PROWLARR-SECRET-444",
    ("scanning", "webhooks", "secret"): "WEBHOOK-SECRET-555",
    ("web", "auth", "password"): "scrypt:32768:8:1$HASHSALT666$HASHBODY666",
}
# Two places to send to: an Apprise URL with a token in it, and Telegram.
TARGETS = [
    {"id": 1, "service": "apprise", "fields": {"url": "discord://WEBHOOKID777/TOKEN777"},
     "events": ["action", "error"], "enabled": True},
    {"id": 2, "service": "telegram", "fields": {"bot_token": "123456:BOTTOKEN888", "chat_id": "888"},
     "events": ["summary"], "enabled": True},
]


@pytest.fixture
def secret_config(config_path, base_config):
    for path, value in TOKENS.items():
        node = base_config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    base_config["transmission"] = {"url": "http://tr.test", "username": "t",
                                   "password": "TRANSMISSION-SECRET-999"}
    base_config["notifications"]["apprise"] = {"targets": [dict(t) for t in TARGETS]}
    write_config(config_path, base_config)
    return base_config


def _changed(a, b, path=()):
    """Paths whose values differ -- names only, so a failure is readable."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for k in a.keys() | b.keys() for p in _changed(a.get(k), b.get(k), path + (k,))]
    return [".".join(map(str, path))] if a != b else []


def _leaks(html):
    secrets = list(TOKENS.values()) + ["TRANSMISSION-SECRET-999", "HASHSALT666",
                                       "WEBHOOKID777", "TOKEN777", "BOTTOKEN888"]
    return [s for s in secrets if s in html]


def test_no_secret_appears_in_settings_html(client, secret_config):
    html = client.get("/config").get_data(as_text=True)
    assert _leaks(html) == []
    assert SECRET_MASK in html


def test_notification_places_never_reach_the_browser(client, secret_config):
    body = client.get("/config/notify").get_data(as_text=True)
    assert _leaks(body) == []
    data = client.get("/config/notify").get_json()
    assert [t["description"] for t in data["targets"]] == ["Apprise address (discord://…)",
                                                            "Telegram chat 888"]


def test_error_page_does_not_leak_either(client, secret_config):
    fields = form_fields(client.get("/config").get_data(as_text=True), form_id="config-form")
    fields["retry_max_attempts"] = "ten"
    resp = client.post("/config/save", data=fields)
    assert resp.status_code == 200 and _leaks(resp.get_data(as_text=True)) == []


def test_unchanged_save_keeps_every_secret(client, config_path, secret_config):
    before = read_config(config_path)
    fields = form_fields(client.get("/config").get_data(as_text=True), form_id="config-form")
    assert client.post("/config/save", data=fields).status_code == 302
    assert _changed(before, read_config(config_path)) == []


def test_typing_replaces_and_clearing_clears(client, config_path, secret_config):
    fields = form_fields(client.get("/config").get_data(as_text=True), form_id="config-form")
    fields["sonarr_api_key"] = "NEW-KEY"
    fields["prowlarr_api_key"] = ""
    assert client.post("/config/save", data=fields).status_code == 302
    after = read_config(config_path)
    assert after["arrs"]["sonarr"]["api_key"] == "NEW-KEY"
    assert after["prowlarr"]["api_key"] == ""
    assert after["qbittorrent"]["password"] == "QBIT-SECRET-111"
    assert after["notifications"]["apprise"]["targets"] == TARGETS     # not the form's to change


def test_mask_for_a_place_that_no_longer_exists_is_refused(client, config_path, secret_config):
    """In the file editor, a masked token whose place id was changed has nothing
    behind it: refused, never saved as bullets."""
    before = read_config(config_path)
    data = read_config(config_path)
    data["notifications"]["apprise"]["targets"][1]["id"] = 9
    data["notifications"]["apprise"]["targets"][1]["fields"]["bot_token"] = SECRET_MASK
    resp = client.post("/config/save",
                       data={"edit_mode": "yaml", "raw_yaml": yaml.safe_dump(data, allow_unicode=True)})
    assert resp.status_code == 200 and "no longer exists" in resp.get_data(as_text=True)
    assert _changed(before, read_config(config_path)) == []


def test_yaml_editor_is_masked_and_restores_on_save(client, config_path, secret_config):
    before = read_config(config_path)
    html = client.get("/config").get_data(as_text=True)
    assert _leaks(html) == []
    raw = form_fields(html, form_id="config-form").get("raw_yaml")
    if raw is None:                                    # editor may live in its own form
        start = html.index('name="raw_yaml"')
        start = html.index(">", start) + 1
        raw = html[start:html.index("</textarea>", start)]
        import html as h
        raw = h.unescape(raw).lstrip("\n")
    assert SECRET_MASK in raw
    resp = client.post("/config/save", data={"edit_mode": "yaml", "raw_yaml": raw})
    assert resp.status_code == 302
    assert _changed(before, read_config(config_path)) == []


def test_yaml_editor_can_still_change_a_secret(client, config_path, secret_config):
    data = read_config(config_path)
    data["arrs"]["sonarr"]["api_key"] = SECRET_MASK
    data["qbittorrent"]["password"] = "TYPED-IN-YAML"
    resp = client.post("/config/save",
                       data={"edit_mode": "yaml", "raw_yaml": yaml.safe_dump(data, allow_unicode=True)})
    assert resp.status_code == 302
    after = read_config(config_path)
    assert after["arrs"]["sonarr"]["api_key"] == "SONARR-SECRET-222"
    assert after["qbittorrent"]["password"] == "TYPED-IN-YAML"


def test_test_button_with_mask_uses_stored_key(client, secret_config, monkeypatch):
    seen = {}

    class FakeSonarr:
        def __init__(self, url, api_key):
            seen["api_key"] = api_key

        def test_connection(self):
            return True

    import core.arrs.sonarr
    monkeypatch.setattr(core.arrs.sonarr, "SonarrClient", FakeSonarr)
    resp = client.post("/config/test/sonarr", json={"url": "http://s", "api_key": SECRET_MASK})
    assert resp.get_json()["ok"] is True and seen["api_key"] == "SONARR-SECRET-222"
    client.post("/config/test/sonarr", json={"url": "http://s", "api_key": "TYPED"})
    assert seen["api_key"] == "TYPED"
