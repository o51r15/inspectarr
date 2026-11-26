"""
3.5: Settings is split into panes. Moving markup between pane files must
never lose a field, duplicate one, or leave a tab with no pane behind it.
"""
import re
from collections import Counter

from tests.formparse import form_fields
from ui.settings_schema import FIELDS, RENDERED

BASIC = ["connections", "protection", "scanning", "notifications", "security", "data"]
ADVANCED = ["indexers", "ai", "server", "file"]


def _html(client):
    return client.get("/config").get_data(as_text=True)


def test_every_schema_field_is_on_the_settings_form(client):
    """The reverse of test_every_form_field_is_in_the_schema: a field the
    schema knows about but no pane renders can never be changed in the UI."""
    fields = form_fields(_html(client), form_id="config-form")
    rendered = set(fields.getlist(RENDERED))
    missing = [f.name for f in FIELDS if f.name not in fields and f.name not in rendered]
    assert missing == []


def test_no_field_is_rendered_twice(client):
    html = _html(client)
    form = html[html.index('id="config-form"'):html.index('id="file-form"')]
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*>', form)
    radios = {re.search(r'name="([a-z_]+)"', c).group(1) for c in controls
              if 'type="radio"' in c and 'name="' in c}
    names = [re.search(r'\bname="([a-z_]+)"', c).group(1) for c in controls
             if re.search(r'\bname="[a-z_]+"', c) and 'type="radio"' not in c]
    names += sorted(radios)                                     # a radio group is one field
    repeated = {n: c for n, c in Counter(names).items()
                if c > 1 and n not in (RENDERED, "notify_on")}     # notify_on: one per event
    assert repeated == {}


def test_tabs_and_panes_match(client):
    html = _html(client)
    tabs = re.findall(r'class="subnav-item"[^>]*data-pane="([a-z]+)"', html)
    panes = re.findall(r'class="settings-pane[^"]*" data-pane="([a-z]+)"', html)
    assert tabs == BASIC + ADVANCED
    assert sorted(panes) == sorted(tabs)


def test_advanced_panes_are_marked(client):
    html = _html(client)
    for name in ADVANCED:
        assert re.search(rf'data-pane="{name}"[^>]*data-level="advanced"', html), name
    for name in BASIC:
        assert not re.search(rf'class="settings-pane[^"]*" data-pane="{name}"[^>]*data-level', html), name


def test_config_file_editor_is_its_own_form(client):
    html = _html(client)
    main = html[html.index('id="config-form"'):html.index("</form>")]
    assert 'name="raw_yaml"' not in main                     # not posted with every form save
    file_form = form_fields(html, form_id="file-form")
    assert file_form["edit_mode"] == "yaml" and "raw_yaml" in file_form


def test_yaml_error_reopens_the_config_file_pane(client):
    resp = client.post("/config/save", data={"edit_mode": "yaml", "raw_yaml": "a: [unclosed"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "YAML error" in body
    assert '"initial_pane": "file"' in body


# --- 3.6 Connections as cards -------------------------------------------------

def test_torrent_client_radio_submits_the_saved_choice(client, config_path, base_config):
    from tests.conftest import write_config
    base_config["torrent_client"] = "transmission"
    base_config["transmission"] = {"url": "http://tr.test:9091", "username": "", "password": ""}
    write_config(config_path, base_config)
    fields = form_fields(_html(client), form_id="config-form")
    assert fields.getlist("torrent_client") == ["transmission"]


def test_every_service_card_has_a_status_pill(client):
    html = _html(client)
    pills = re.findall(r'data-status-for="([^"]+)"', html)
    assert pills == ["Torrent Client", "Sonarr", "Radarr", "Lidarr"]


def test_credentials_open_only_when_nothing_is_stored(client, config_path, base_config):
    from tests.conftest import write_config
    base_config["arrs"]["radarr"]["api_key"] = ""             # nothing stored
    write_config(config_path, base_config)                      # sonarr has a key
    html = _html(client)

    def creds_open(field):
        i = html.index(f'name="{field}"')
        start = html.rindex("<details", 0, i)
        return " open" in html[start:html.index(">", start)]

    assert creds_open("sonarr_api_key") is False
    assert creds_open("radarr_api_key") is True
