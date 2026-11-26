"""
3.4: the browser only ever holds a mask.

No stored secret -- passwords, API keys, the webhook secret, the web
password's hash, or the token inside an Apprise URL -- may appear anywhere in
the Settings HTML. Saving with the mask keeps the stored value; the Test
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
APPRISE = ["discord://WEBHOOKID777/TOKEN777", "tgram://BOTTOKEN888/CHAT888"]


@pytest.fixture
def secret_config(config_path, base_config):
    for path, value in TOKENS.items():
        node = base_config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    base_config["transmission"] = {"url": "http://tr.test", "username": "t",
                                   "password": "TRANSMISSION-SECRET-999"}
    base_config["notifications"]["apprise"]["urls"] = list(APPRISE)
    write_config(config_path, base_config)
    return base_config


def _changed(a, b, path=()):
    """Paths whose values differ -- names only, so a failure is readable."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for k in a.keys() | b.keys() for p in _changed(a.get(k), b.get(k), path + (k,))]
    return [".".join(map(str, path))] if a != b else []


def _leaks(html):
    secrets = list(TOKENS.values()) + ["TRANSMISSION-SECRET-999", "HASHSALT666",
                                       "WEBHOOKID777", "TOKEN777", "BOTTOKEN888", "CHAT888"]
    return [s for s in secrets if s in html]


def test_no_secret_appears_in_settings_html(client, secret_config):
    html = client.get("/config").get_data(as_text=True)
    assert _leaks(html) == []
    assert SECRET_MASK in html
    assert "discord://" + SECRET_MASK + " (saved 1)" in html       # scheme still readable


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
    lines = fields["apprise_urls"].splitlines()
    fields["apprise_urls"] = "\n".join([lines[1], "mailto://new@example.test"])  # drop #1, keep #2
    assert client.post("/config/save", data=fields).status_code == 302
    after = read_config(config_path)
    assert after["arrs"]["sonarr"]["api_key"] == "NEW-KEY"
    assert after["prowlarr"]["api_key"] == ""
    assert after["qbittorrent"]["password"] == "QBIT-SECRET-111"
    assert after["notifications"]["apprise"]["urls"] == [APPRISE[1], "mailto://new@example.test"]


def test_mask_for_a_url_that_no_longer_exists_is_refused(client, config_path, secret_config):
    before = read_config(config_path)
    fields = form_fields(client.get("/config").get_data(as_text=True), form_id="config-form")
    fields["apprise_urls"] = "discord://" + SECRET_MASK + " (saved 9)"
    resp = client.post("/config/save", data=fields)
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


def test_apprise_test_with_masked_lines_uses_stored_urls(client, secret_config, monkeypatch):
    added = []

    class FakeApprise:
        def add(self, url):
            added.append(url)

        def notify(self, **_):
            return True

    import apprise
    monkeypatch.setattr(apprise, "Apprise", FakeApprise)
    body = {"urls": "discord://" + SECRET_MASK + " (saved 1)\nmailto://x@example.test"}
    assert client.post("/config/test/apprise", json=body).get_json()["ok"] is True
    assert added == [APPRISE[0], "mailto://x@example.test"]
