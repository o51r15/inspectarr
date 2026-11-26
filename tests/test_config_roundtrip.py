"""
Settings must not lose data.

The central test: render /config, submit exactly what a browser would send if
the user pressed Save without touching anything, and check that nothing the
form does not own has changed. Every past data-loss bug in this area
(BUG-10/11, B-14, B-21) would have failed it.
"""
import pytest
import yaml

from tests.conftest import read_config, write_config
from tests.formparse import form_fields


def _unchanged_form(client):
    html = client.get("/config").get_data(as_text=True)
    return form_fields(html, form_id="config-form")


def _save(client, fields):
    resp = client.post("/config/save", data=fields)
    if resp.status_code != 302:
        body = resp.get_data(as_text=True)
        start = body.find("alert-error")
        snippet = body[start:start + 400] if start >= 0 else body[:400]
        pytest.fail(f"save did not redirect ({resp.status_code}): {snippet}")
    return resp


def test_unchanged_submit_preserves_config(client, config_path):
    before = read_config(config_path)
    _save(client, _unchanged_form(client))
    after = read_config(config_path)

    # Keys with no form field must survive a form save.
    assert after["prowlarr"]["scoring"]["response_time_weight"] == 0.4
    assert after["prowlarr"]["ollama"]["model"] == "phi4:latest"
    # system_prompt is moved out of config.yaml into a stored prompt at start
    # (core/scoring_prompts.migrate_config_prompt), so it is gone, not lost.
    assert "system_prompt" not in after["prowlarr"]["ollama"]
    assert after["remediation"]["severity_overrides"] == {".rar": "LOW"}
    assert after["remediation"]["track_replacements"] is True

    # Keys the form does own must come back with the same value.
    assert after["remediation"]["operating_mode"] == "monitor"
    assert after["remediation"]["remediate_at"] == "HIGH"
    assert after["scanning"]["polling"]["interval_seconds"] == 900
    assert after["rules"] == before["rules"]
    assert after["arrs"] == before["arrs"]
    assert after["qbittorrent"] == before["qbittorrent"]
    # Notification events now live on each place to send to (notify_targets)
    # and the form no longer owns them, so a form save can't drop one (B-14:
    # it used to strip "quarantine"). test_notify_targets covers the places.
    assert after["notifications"]["apprise"]["targets"] == \
        before["notifications"]["apprise"].get("targets", [])


def test_second_unchanged_save_is_a_no_op(client, config_path):
    """After the first save normalises the file, saving again changes nothing."""
    _save(client, _unchanged_form(client))
    once = read_config(config_path)
    _save(client, _unchanged_form(client))
    assert read_config(config_path) == once


def test_deleting_middle_rule_keeps_later_rules(client, config_path, base_config):
    base_config["rules"] = [
        {"name": n, "category": "tv-sonarr", "app": "sonarr",
         "conditions": {"match_mode": "any", "bad_extensions": [".exe"]}}
        for n in ("first", "middle", "last")
    ]
    write_config(config_path, base_config)
    fields = _unchanged_form(client)
    for key in [k for k in fields.keys() if k.startswith("rule_") and k.endswith("_1")]:
        fields.poplist(key)
    _save(client, fields)
    assert [r["name"] for r in read_config(config_path)["rules"]] == ["first", "last"]


def test_yaml_mode_rejects_empty_document(client, config_path):    # BUG-05
    before = read_config(config_path)
    resp = client.post("/config/save", data={"edit_mode": "yaml", "raw_yaml": ""})
    assert resp.status_code == 200
    assert read_config(config_path) == before


def test_yaml_mode_rejects_misspelled_mode(client, config_path):
    """A typo in the setting that stops deletions must be refused, never absorbed."""
    data = read_config(config_path)
    data["remediation"]["operating_mode"] = "monitr"
    resp = client.post("/config/save",
                       data={"edit_mode": "yaml", "raw_yaml": yaml.safe_dump(data)})
    assert resp.status_code == 200
    assert read_config(config_path)["remediation"]["operating_mode"] == "monitor"


def test_unknown_top_level_key_survives(client, config_path, base_config):    # S2
    base_config["my_note"] = "kept by hand"
    write_config(config_path, base_config)
    _save(client, _unchanged_form(client))
    assert read_config(config_path).get("my_note") == "kept by hand"


def _with_comments(config_path, base_config):
    """The fixture config, re-written with a header and an inline comment."""
    text = "# my homelab notes -- please keep\n" + yaml.safe_dump(base_config, sort_keys=False)
    lines = text.splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("on_arr_failure:"))
    lines[i] += "   # think before changing"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_form_save_keeps_comments(client, config_path, base_config):
    _with_comments(config_path, base_config)
    _save(client, _unchanged_form(client))
    text = open(config_path, encoding="utf-8").read()
    assert text.startswith("# my homelab notes -- please keep\n")
    assert "# think before changing" in text


def test_yaml_editor_shows_and_keeps_comments(client, config_path, base_config):
    _with_comments(config_path, base_config)
    html = client.get("/config").get_data(as_text=True)
    assert "# my homelab notes -- please keep" in html        # editor shows the real file
    raw = open(config_path, encoding="utf-8").read().replace("# think before", "# THINK before")
    resp = client.post("/config/save", data={"edit_mode": "yaml", "raw_yaml": raw})
    assert resp.status_code == 302
    assert "# THINK before changing" in open(config_path, encoding="utf-8").read()
