"""Scoring prompts: the locked Default, people's own prompts, and the old
prowlarr.ollama.system_prompt setting moving out of config.yaml."""
import contextlib

import pytest

from core import scoring_prompts as sp
from core.llm_client import SYSTEM_PROMPT
from tests.conftest import read_config, write_config

CUSTOM = "You score torrent indexers. Prefer fast, reliable ones and explain briefly why."


@pytest.fixture
def state(app):
    """A state with no prompts: create_app already migrated conftest's
    system_prompt into Prompt 1 (test_app_start_migrates checks that)."""
    st = app.config["STATE"]
    for row in st.list_scoring_prompts():
        st.delete_scoring_prompt(row["id"])
    st.set_app_state(sp.ACTIVE_KEY, "0")
    return st


# --- the split -------------------------------------------------------------------

def test_default_is_sent_byte_for_byte():
    assert sp.build() == SYSTEM_PROMPT
    assert sp.build(None) == SYSTEM_PROMPT


def test_split_points_rebuild_the_default():
    assert sp.ANSWER_FORMAT.startswith("Return ONLY valid JSON")
    assert sp.DEFAULT_INSTRUCTIONS and sp.ANSWER_FORMAT not in sp.DEFAULT_INSTRUCTIONS
    assert sp.is_default(sp.DEFAULT_INSTRUCTIONS)
    assert sp.is_default(SYSTEM_PROMPT)                 # an old full copy counts too


def test_answer_format_is_always_added():
    full = sp.build(CUSTOM)
    assert full.startswith(CUSTOM)
    assert full.rstrip().endswith(sp.ANSWER_FORMAT)


def test_pasted_answer_format_is_not_doubled():
    text = sp.clean_text(CUSTOM + "\n\n" + sp.ANSWER_FORMAT)
    assert text == CUSTOM
    assert sp.build(text).count("Return ONLY valid JSON") == 1


def test_clean_text_limits():
    with pytest.raises(sp.PromptError):
        sp.clean_text("too short")
    with pytest.raises(sp.PromptError):
        sp.clean_text("x" * (sp.MAX_CHARS + 1))
    assert sp.clean_text("  " + CUSTOM + "  \r\n") == CUSTOM


# --- storing and choosing --------------------------------------------------------

def test_create_update_use_delete(state):
    assert sp.active(state) is None and sp.active_system(state) == ("Default", SYSTEM_PROMPT)
    pid = state.create_scoring_prompt(CUSTOM, "mine")
    row = state.get_scoring_prompt(pid)
    assert row["number"] == 1 and row["revision"] == 1
    assert sp.label(row) == "Prompt 1 (mine)"

    state.update_scoring_prompt(pid, CUSTOM, "renamed")          # name only: same revision
    assert state.get_scoring_prompt(pid)["revision"] == 1
    state.update_scoring_prompt(pid, CUSTOM + " Be strict.", "renamed")
    assert state.get_scoring_prompt(pid)["revision"] == 2

    sp.use(state, pid)
    label, system = sp.active_system(state)
    assert label == "Prompt 1 (renamed)" and system == sp.build(CUSTOM + " Be strict.")

    sp.delete(state, pid)                                        # in use -> back to Default
    assert sp.active(state) is None and state.get_scoring_prompt(pid) is None


def test_numbers_reuse_the_lowest_free(state):
    a = state.create_scoring_prompt(CUSTOM)
    b = state.create_scoring_prompt(CUSTOM)
    state.delete_scoring_prompt(a)
    c = state.create_scoring_prompt(CUSTOM)
    assert state.get_scoring_prompt(b)["number"] == 2
    assert state.get_scoring_prompt(c)["number"] == 1


def test_use_rejects_a_missing_prompt(state):
    with pytest.raises(sp.PromptError):
        sp.use(state, 999)


# --- the old config setting ------------------------------------------------------

def _with_prompt(config_path, value):
    data = read_config(config_path)
    data["prowlarr"]["ollama"]["system_prompt"] = value
    write_config(config_path, data)


def test_migration_default_copy_is_just_removed(state, config_path):
    _with_prompt(config_path, SYSTEM_PROMPT + "\n\n")
    assert "Default" in sp.migrate_config_prompt(state, config_path)
    assert "system_prompt" not in read_config(config_path)["prowlarr"]["ollama"]
    assert state.list_scoring_prompts() == [] and sp.active(state) is None


def test_migration_custom_becomes_a_prompt_in_use(state, config_path):
    _with_prompt(config_path, CUSTOM + "\n\n" + sp.ANSWER_FORMAT)
    sp.migrate_config_prompt(state, config_path)
    cfg = read_config(config_path)
    assert "system_prompt" not in cfg["prowlarr"]["ollama"]
    assert cfg["prowlarr"]["ollama"]["model"] == "phi4:latest"           # the rest untouched
    row = sp.active(state)
    assert row and row["text"] == CUSTOM and row["name"] == "from config.yaml"
    assert sp.migrate_config_prompt(state, config_path) is None          # once only

    _with_prompt(config_path, CUSTOM)                                    # an old config restored
    sp.migrate_config_prompt(state, config_path)
    assert len(state.list_scoring_prompts()) == 1                        # reused, not copied


def test_app_start_migrates(app, config_path):
    # conftest's config carries system_prompt "custom prompt"; create_app moved it.
    assert "system_prompt" not in read_config(config_path)["prowlarr"]["ollama"]
    assert sp.active(app.config["STATE"])["text"] == "custom prompt"


# --- routes ----------------------------------------------------------------------

def test_routes(client, state):
    d = client.get("/config/ai/prompts").get_json()
    assert d["ok"] and d["default_instructions"] == sp.DEFAULT_INSTRUCTIONS
    assert d["answer_format"] == sp.ANSWER_FORMAT

    new = client.post("/config/ai/prompts", json={}).get_json()
    assert new["ok"]
    assert state.get_scoring_prompt(new["id"])["text"] == sp.DEFAULT_INSTRUCTIONS

    bad = client.post(f"/config/ai/prompts/{new['id']}", json={"text": "short", "name": ""})
    assert bad.get_json()["ok"] is False
    ok = client.post(f"/config/ai/prompts/{new['id']}", json={"text": CUSTOM, "name": "x"})
    assert ok.get_json()["ok"]

    used = client.post("/config/ai/prompts/use", json={"id": new["id"]}).get_json()
    assert used["ok"] and sp.active(state)["id"] == new["id"]
    assert client.post("/config/ai/prompts/use", json={"id": 0}).get_json()["ok"]
    assert sp.active(state) is None

    assert client.get(f"/config/ai/prompts/{new['id']}/tests").get_json()["tests"] == []
    assert client.post(f"/config/ai/prompts/{new['id']}/delete").get_json()["ok"]
    assert state.get_scoring_prompt(new["id"]) is None


def test_test_route_needs_an_ai_server(client, state):
    pid = state.create_scoring_prompt(CUSTOM)
    r = client.post(f"/config/ai/prompts/{pid}/test", json={"models": ["m"]}).get_json()
    assert r["ok"] is False                                   # no Ollama URL in the test config


# --- a prompt test run -----------------------------------------------------------

def test_prompt_test_run_saves_results(app, state, config_path, monkeypatch):
    import core.ai_turn
    import core.model_quality
    import core.ollama_models
    from core.calibration_runs import CalibrationRunner

    seen = {}

    def fake_quality(url, model, timeout, ctx, think=None, prompt=None):
        seen[model] = prompt
        return {"score": 80.0, "complete": model == "good", "detail": "ok"}

    monkeypatch.setattr(core.model_quality, "run_quality_test", fake_quality)
    monkeypatch.setattr(core.ai_turn, "ai_turn", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(core.ollama_models, "unload", lambda *a, **k: None)
    monkeypatch.setattr(core.ollama_models, "model_profile", lambda *a, **k: {"max_context": 32768})

    pid = state.create_scoring_prompt(CUSTOM)
    url = "http://ollama.test:11434"
    run_id = state.create_calibration_run(url, ["good", "bad"], kind="prompt", prompt_id=pid)
    runner = CalibrationRunner(state, config_path)
    runner._run(state.get_calibration_run(run_id))

    assert seen["good"] == sp.build(CUSTOM)
    assert state.get_calibration_run(run_id)["status"] == "done"
    tests = {t["model"]: t for t in state.get_prompt_tests(pid, url)}
    assert tests["good"]["complete"] and not tests["bad"]["complete"]
    assert tests["good"]["prompt_rev"] == 1
    assert state.get_validations(url) == []                   # never touches the ranking
