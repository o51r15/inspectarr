"""
Tests of the test harness itself.

A green suite is only evidence if the harness can fail. These prove the two
guarantees everything else leans on: no test can reach the network, and the
form parser really sees the Settings form (a parser that silently found a
handful of fields would make the round-trip test pass vacuously).
"""
import pytest

from tests.formparse import form_fields


@pytest.mark.parametrize("url", [
    "http://sonarr.test:8989/api/v3/system/status",   # hostname: blocked at DNS
    "http://127.0.0.1:9/",                            # IP literal: blocked at connect
])
def test_network_guard_blocks_outbound_calls(url):
    import requests
    with pytest.raises(Exception) as exc:
        requests.get(url, timeout=1)
    assert "network access attempted in a test" in repr(exc.value)


def test_settings_form_is_fully_parsed(client):
    fields = form_fields(client.get("/config").get_data(as_text=True), "config-form")
    names = set(fields.keys())
    # A spread from every pane: connections, rules, remediation, prowlarr,
    # notifications, general, advanced.
    for expected in ("qbit_url", "sonarr_api_key", "rule_name_0", "rule_extensions_0",
                     "remediation_operating_mode", "prowlarr_url", "notify_on",
                     "polling_interval_seconds", "auth_username", "log_level"):
        assert expected in names, f"{expected} missing from parsed form"
    assert len(names) >= 50, f"only {len(names)} distinct fields parsed"


def test_fake_client_is_used(client, fake_client):
    client.get("/torrents")
    # The fake was asked for torrents rather than a real client being built.
    assert fake_client.torrents == []
