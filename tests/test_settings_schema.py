"""
ui/settings_schema: the form can change only what it shows, and every field
it shows is accounted for.
"""
import re

import pytest
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash

from core.config import _parse_config
from tests.conftest import read_config
from tests.formparse import form_fields
from ui.settings_schema import (
    FIELDS, FIELDS_BY_NAME, NOT_FIELDS, RENDERED, RULE_PREFIX, FieldError, form_to_updates,
)


def _html(client):
    return client.get("/config").get_data(as_text=True)


def _form(client):
    return form_fields(_html(client), form_id="config-form")


def _save(client, fields):
    return client.post("/config/save", data=fields)


# --- the schema covers the form, and the form marks its checkboxes ---------

def test_every_form_field_is_in_the_schema(client):
    """A field missing from FIELDS would silently never be saved."""
    names = set(_form(client).keys())
    unknown = sorted(n for n in names
                     if n not in FIELDS_BY_NAME and n not in NOT_FIELDS
                     and not n.startswith(RULE_PREFIX))
    assert unknown == []


def test_every_checkbox_is_marked_rendered(client):
    """An unmarked checkbox could never be saved as unticked."""
    html = _html(client)
    form_html = html[html.index('id="config-form"'):]
    boxes = set(re.findall(r'type="checkbox" name="([a-z_]+)"', form_html))
    marked = set(re.findall(r'name="_rendered" value="([a-z_]+)"', form_html))
    assert boxes and boxes <= marked, sorted(boxes - marked)
    assert "rules" in marked


@pytest.mark.parametrize("field", FIELDS, ids=lambda f: f.name)
def test_every_schema_path_exists_in_the_parsed_config(field):
    """Catches a typo in a path, which would write a key nothing reads."""
    cfg = _parse_config({
        "qbittorrent": {"url": "http://q", "username": "u", "password": "p"},
        "transmission": {"url": "http://t"}, "deluge": {"url": "http://d"},
    })
    node = cfg
    for key in field.path:
        assert hasattr(node, key), f"{field.name}: no '{key}' in {type(node).__name__}"
        node = getattr(node, key)


# --- behaviour ----------------------------------------------------------------

def test_unticked_checkbox_saves_false(client, config_path):
    fields = _form(client)
    assert "retry_enabled" in fields                 # ticked in the fixture
    fields.poplist("retry_enabled")
    assert _save(client, fields).status_code == 302
    assert read_config(config_path)["retry"]["enabled"] is False


def test_pane_not_on_the_page_is_left_alone():
    """No `_rendered` marker and no value: the field is not touched at all."""
    updates = form_to_updates(MultiDict({"qbit_url": "http://x"}))
    assert updates == {"qbittorrent": {"url": "http://x"}}


def test_bad_number_is_refused_and_nothing_written(client, config_path):
    before = read_config(config_path)
    fields = _form(client)
    fields["retry_max_attempts"] = "ten"
    resp = _save(client, fields)
    assert resp.status_code == 200
    assert "is not a whole number" in resp.get_data(as_text=True)
    assert read_config(config_path) == before


def test_slider_values_are_clamped_not_refused():
    updates = form_to_updates(MultiDict({"ollama_max_indexers_per_call": "0",
                                         "ollama_context_window": "999999"}))
    assert updates["prowlarr"]["ollama"] == {"max_indexers_per_call": 1, "context_window": 262144}


def test_blank_operating_mode_keeps_stored_mode():
    assert form_to_updates(MultiDict({"remediation_operating_mode": "  "})) == {}


def test_password_is_hashed_and_blank_keeps_it():
    updates = form_to_updates(MultiDict({"auth_password": "hunter2"}))
    assert check_password_hash(updates["web"]["auth"]["password"], "hunter2")
    assert form_to_updates(MultiDict({"auth_password": ""})) == {}
    stored = "scrypt:32768:8:1$abc$def"
    assert form_to_updates(MultiDict({"auth_password": stored}))["web"]["auth"]["password"] == stored


def test_ollama_url_is_tidied():
    updates = form_to_updates(MultiDict({"ollama_url": " http://ai:11434/ "}))
    assert updates["prowlarr"]["ollama"]["url"] == "http://ai:11434"


def test_bad_rule_size_is_refused():
    form = MultiDict([(RENDERED, "rules"), ("rule_name_0", "TV"), ("rule_min_size_mb_0", "big")])
    with pytest.raises(FieldError, match="Rule TV"):
        form_to_updates(form)


def test_legacy_poll_interval_is_not_written_and_survives(client, config_path, base_config):
    from tests.conftest import write_config
    base_config["poll_interval_seconds"] = 123
    write_config(config_path, base_config)
    assert _save(client, _form(client)).status_code == 302
    after = read_config(config_path)
    assert after["poll_interval_seconds"] == 123          # left exactly as it was


# --- a key the file does not mention stays unmentioned, unless it changes ----

def _write_text(path, data):
    import yaml
    from core.config import invalidate_config_cache
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml.safe_dump(data, sort_keys=False))
    invalidate_config_cache()


def test_trimmed_config_is_byte_identical_after_unchanged_save(client, config_path):
    """A hand-trimmed file must not grow deluge:, transmission:, defaults..."""
    minimal = {
        "qbittorrent": {"url": "http://qbit.test:8080", "username": "u", "password": "p"},
        "on_arr_failure": "delete",
        "logging": {"log_file": "./data/inspectarr.log.json"},
        "state": {"db_file": "./data/inspectarr.db"},
    }
    _write_text(config_path, minimal)
    before = open(config_path, encoding="utf-8").read()
    assert _save(client, _form(client)).status_code == 302
    assert open(config_path, encoding="utf-8").read() == before


def test_changing_an_unmentioned_setting_writes_it(client, config_path):
    minimal = {"qbittorrent": {"url": "http://q", "username": "u", "password": "p"},
               "logging": {"log_file": "./data/inspectarr.log.json"},
               "state": {"db_file": "./data/inspectarr.db"}}
    _write_text(config_path, minimal)
    fields = _form(client)
    fields["retry_max_attempts"] = "4"
    assert _save(client, fields).status_code == 302
    assert read_config(config_path)["retry"] == {"max_attempts": 4}


def test_ai_can_be_switched_off_when_enabled_was_implied(client, config_path, base_config):
    """With no `enabled:` key the parser turns AI on because a URL is set.
    Unticking must therefore write `enabled: false`, not skip it as a default."""
    from tests.conftest import write_config
    from core.config import load_config
    ollama = base_config["prowlarr"]["ollama"]
    ollama.pop("enabled")
    ollama["url"] = "http://ai.test:11434"
    write_config(config_path, base_config)
    fields = _form(client)
    assert "ollama_enabled" in fields                     # shown ticked, as the parser sees it
    fields.poplist("ollama_enabled")
    assert _save(client, fields).status_code == 302
    assert read_config(config_path)["prowlarr"]["ollama"]["enabled"] is False
    assert load_config(config_path).prowlarr.ollama.enabled is False


def test_legacy_interval_alone_is_kept_in_effect(client, config_path, base_config):
    from tests.conftest import write_config
    from core.config import load_config
    del base_config["scanning"]
    base_config["poll_interval_seconds"] = 123
    write_config(config_path, base_config)
    assert _save(client, _form(client)).status_code == 302
    assert load_config(config_path).scanning.polling.interval_seconds == 123
    assert "scanning" not in read_config(config_path)


def test_notify_on_in_another_order_is_not_a_change(client, config_path, base_config):
    from tests.conftest import write_config
    base_config["notifications"]["apprise"]["notify_on"] = ["quarantine", "error", "action"]
    write_config(config_path, base_config)
    assert _save(client, _form(client)).status_code == 302
    assert read_config(config_path)["notifications"]["apprise"]["notify_on"] == \
        ["quarantine", "error", "action"]
