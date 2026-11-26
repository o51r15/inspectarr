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
    names = re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([a-z_]+)"', form)
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
