"""
3.5: Settings is split into panes. Moving markup between pane files must
never lose a field, duplicate one, or leave a tab with no pane behind it.
"""
import re
from collections import Counter

from tests.formparse import form_fields
from ui.settings_schema import FIELDS, RENDERED

TABS = ["connections", "protection", "scanning", "notifications", "indexers", "ai", "system"]
# Shown only while true, so it can be switched off; otherwise Watch only covers it.
CONDITIONAL = {"dry_run"}


def _html(client):
    return client.get("/config").get_data(as_text=True)


def test_every_schema_field_is_on_the_settings_form(client):
    """The reverse of test_every_form_field_is_in_the_schema: a field the
    schema knows about but no pane renders can never be changed in the UI."""
    fields = form_fields(_html(client), form_id="config-form")
    rendered = set(fields.getlist(RENDERED))
    missing = [f.name for f in FIELDS if f.name not in fields and f.name not in rendered
               and f.name not in CONDITIONAL]
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
    assert tabs == TABS
    assert sorted(panes) == sorted(TABS + ["file"])      # the config file opens from System


def test_no_tab_or_pane_is_hidden_behind_advanced(client):
    """Show advanced reveals sections inside tabs, never whole tabs."""
    html = _html(client)
    assert not re.search(r'class="subnav-item"[^>]*data-level', html)
    assert not re.search(r'class="settings-pane[^"]*"[^>]*data-level', html)
    assert 'data-level="advanced"' in html                # but some sections are advanced


def test_dry_run_is_only_shown_while_it_is_on(client, config_path, base_config):
    from tests.conftest import read_config, write_config
    assert 'name="dry_run"' not in _html(client)
    base_config["dry_run"] = True
    write_config(config_path, base_config)
    fields = form_fields(_html(client), form_id="config-form")
    assert "dry_run" in fields
    fields.poplist("dry_run")                            # untick it
    assert client.post("/config/save", data=fields).status_code == 302
    assert read_config(config_path)["dry_run"] is False


def test_a_custom_interval_is_kept_not_rounded(client, config_path, base_config):
    from tests.conftest import read_config, write_config
    base_config["scanning"]["polling"]["interval_seconds"] = 420     # not one of the choices
    write_config(config_path, base_config)
    html = _html(client)
    assert "Custom (420 seconds)" in html
    fields = form_fields(html, form_id="config-form")
    assert fields["polling_interval_seconds"] == "420"
    assert client.post("/config/save", data=fields).status_code == 302
    assert read_config(config_path)["scanning"]["polling"]["interval_seconds"] == 420


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
    assert pills == ["Torrent Client", "Sonarr", "Radarr", "Lidarr", "Prowlarr", "Ollama"]


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
