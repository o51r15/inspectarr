"""
Model validations are kept per Ollama server.

A verdict describes a model ON A SERVER. Keyed by model alone, validating on
one server overwrote the same model's result from every other.
"""
import sqlite3

import pytest

from core.state import StateManager

A = "http://192.168.1.97:11434"
B = "http://192.168.1.125:11434"

OLD_SCHEMA = """
CREATE TABLE validated_models (
    model TEXT PRIMARY KEY, validated_at TEXT NOT NULL, status TEXT NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0, indexer_count INTEGER, avg_response_ms INTEGER,
    model_digest TEXT, results_json TEXT, ollama_url TEXT, tok_per_s REAL,
    gpu_offload_pct INTEGER, max_safe_batch INTEGER, calibrated INTEGER NOT NULL DEFAULT 0
)"""


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "s.db"), log_path=str(tmp_path / "l.json"),
                        retention_days=30)


def _ok(**extra):
    return {"passed": True, "indexer_count": 40, "avg_response_ms": 900, **extra}


def test_same_model_keeps_a_result_per_server(tmp_path):
    st = _state(tmp_path)
    st.save_validation("phi4", _ok(calibrated=True, max_safe_batch=25), ollama_url=A)
    st.save_validation("phi4", {"passed": False}, ollama_url=B)
    assert st.get_validation("phi4", A)["status"] == "supported"
    assert st.get_validation("phi4", B)["status"] == "failed"
    assert st.get_validation("phi4", "http://other:11434") is None
    st.close()


def test_lookups_normalise_the_url(tmp_path):
    st = _state(tmp_path)
    st.save_validation("phi4", _ok(), ollama_url="HTTP://192.168.1.97:11434/")
    assert st.get_validation("phi4", A) is not None
    st.close()


def test_listing_one_server_hides_the_others(tmp_path):
    st = _state(tmp_path)
    st.save_validation("phi4", _ok(), ollama_url=A)
    st.save_validation("qwen", _ok(), ollama_url=B)
    assert [v["model"] for v in st.get_validations(A)] == ["phi4"]
    assert sorted(v["model"] for v in st.get_validations()) == ["phi4", "qwen"]
    st.close()


def test_delete_only_touches_that_server(tmp_path):
    st = _state(tmp_path)
    st.save_validation("phi4", _ok(), ollama_url=A)
    st.save_validation("phi4", _ok(), ollama_url=B)
    st.delete_validation("phi4", A)
    assert st.get_validation("phi4", A) is None
    assert st.get_validation("phi4", B) is not None
    st.close()


def test_a_verdict_without_a_server_is_refused(tmp_path):
    st = _state(tmp_path)
    assert st.save_validation("phi4", _ok(), ollama_url="") is False
    assert st.mark_model_forced("phi4", ollama_url=None) is False
    assert st.get_validations() == []
    st.close()


def test_upgrade_keeps_tracked_rows_and_deletes_untracked(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute(OLD_SCHEMA)
    rows = [("phi4", A, "supported", 1, 1, 25), ("qwen", B, "failed", 0, 0, None),
            ("llama", None, "supported", 1, 0, None), ("gemma", "", "failed", 0, 0, None)]
    for model, url, status, passed, cal, batch in rows:
        conn.execute("INSERT INTO validated_models (model, validated_at, status, passed, "
                     "ollama_url, calibrated, max_safe_batch, results_json) "
                     "VALUES (?, '2026-11-01T00:00:00', ?, ?, ?, ?, ?, '{}')",
                     (model, status, passed, url, cal, batch))
    conn.commit()
    conn.close()

    st = _state(tmp_path)
    kept = {(v["model"], v["ollama_url"]) for v in st.get_validations()}
    assert kept == {("phi4", A), ("qwen", B)}                   # untracked rows gone
    phi = st.get_validation("phi4", A)
    assert phi["calibrated"] == 1 and phi["max_safe_batch"] == 25   # data carried over
    # and the new key works: a second server's verdict no longer overwrites
    st.save_validation("phi4", {"passed": False}, ollama_url=B)
    assert st.get_validation("phi4", A)["status"] == "supported"
    st.close()

    pk = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(validated_models)") if r[5]]
    assert sorted(pk) == ["model", "ollama_url"]


def test_upgrade_runs_once(tmp_path):
    st = _state(tmp_path)
    st.save_validation("phi4", _ok(), ollama_url=A)
    st.close()
    st = _state(tmp_path)                                        # second start: no-op
    assert st.get_validation("phi4", A) is not None
    st.close()


# --- the Settings endpoints answer for the connected server only -------------

@pytest.fixture
def on_server_a(app, config_path, base_config):
    from tests.conftest import write_config
    base_config["prowlarr"]["ollama"].update({"enabled": True, "url": A})
    write_config(config_path, base_config)
    st = app.config["STATE"]
    st.save_validation("phi4", _ok(calibrated=True, max_safe_batch=20), ollama_url=A)
    st.save_validation("qwen", _ok(), ollama_url=B)              # another server
    st.save_validation("phi4", {"passed": False}, ollama_url=B)
    return st


def test_list_shows_only_this_servers_results(client, on_server_a):
    d = client.get("/config/ai/validations").get_json()
    assert d["server"] == A
    assert [(v["model"], v["status"]) for v in d["validations"]] == [("phi4", "supported")]
    assert d["other_servers"] == 1
    assert "ollama_url" not in d["validations"][0] and "host_state" not in d["validations"][0]


def test_apply_gate_uses_this_servers_verdict(client, on_server_a, monkeypatch):
    import ui.routes.config as routes
    monkeypatch.setattr(routes, "_ollama_digest", lambda url, model: None)
    ok = client.post("/config/ai/model", json={"model": "phi4"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    # qwen only passed on the OTHER server: here it is untested
    blocked = client.post("/config/ai/model", json={"model": "qwen"})
    assert blocked.status_code == 409 and blocked.get_json()["status"] == "untested"


def test_forcing_records_against_this_server(client, on_server_a, monkeypatch):
    import ui.routes.config as routes
    monkeypatch.setattr(routes, "_ollama_digest", lambda url, model: None)
    r = client.post("/config/ai/model", json={"model": "qwen", "force": True})
    assert r.get_json()["forced"] is True
    assert on_server_a.get_validation("qwen", A)["status"] == "forced"
    assert on_server_a.get_validation("qwen", B)["status"] == "supported"   # untouched


def test_calibration_reads_this_servers_record(client, on_server_a, config_path):
    from tests.conftest import read_config, write_config
    cfg = read_config(config_path)
    cfg["prowlarr"]["ollama"]["model"] = "phi4"
    write_config(config_path, cfg)
    d = client.get("/config/ai/calibration").get_json()
    assert d["calibrated"] is True and d["max_safe_batch"] == 20


def test_clearing_a_result_leaves_other_servers_alone(client, on_server_a):
    r = client.post("/config/ai/validation/delete", json={"model": "phi4"})
    assert r.get_json()["ok"] is True
    assert on_server_a.get_validation("phi4", A) is None
    assert on_server_a.get_validation("phi4", B) is not None


def test_switching_back_brings_results_back(client, on_server_a, config_path):
    from tests.conftest import read_config, write_config
    cfg = read_config(config_path)
    cfg["prowlarr"]["ollama"]["url"] = B
    write_config(config_path, cfg)
    d = client.get("/config/ai/validations").get_json()
    assert sorted((v["model"], v["status"]) for v in d["validations"]) == \
        [("phi4", "failed"), ("qwen", "supported")]
