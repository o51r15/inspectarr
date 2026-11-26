"""
What Inspectarr sends to Ollama: thinking off, low temperature, the exact
answer schema, and never more context than the model supports.

No real server: requests.post / requests.get are replaced by recorders that
answer the way Ollama 0.40 does for the shapes checked on a real server
(gemma4: thinking values [false, true]; gpt-oss: levels only; llama3: none).
"""
import json

import pytest

from core import llm_client, model_validator, ollama_models


SHOW = {
    "gemma4:latest": {"model_info": {"general.architecture": "gemma4",
                                     "gemma4.context_length": 131072},
                      "capabilities": ["completion", "tools", "thinking"],
                      "thinking": {"values": [False, True], "default": True}},
    "gpt-oss:20b": {"model_info": {"general.architecture": "gptoss",
                                   "gptoss.context_length": 131072},
                    "capabilities": ["completion", "tools", "thinking"],
                    "thinking": {"values": ["low", "medium", "high"], "default": "medium"}},
    "llama3-groq-tool-use:8b": {"model_info": {"general.architecture": "llama",
                                               "llama.context_length": 8192},
                                "capabilities": ["completion", "tools"]},
    "tiny:1b": {"model_info": {"general.architecture": "llama",
                               "llama.context_length": 2048},
                "capabilities": ["completion"]},
    "deepseek-r1:1.5b": {"model_info": {"general.architecture": "qwen2",
                                        "qwen2.context_length": 131072},
                         "capabilities": ["completion", "thinking"]},
    "nomic-embed-text:latest": {"model_info": {"general.architecture": "nomic-bert",
                                               "nomic-bert.context_length": 2048},
                                "capabilities": ["embedding"]},
}

GOOD_ANSWER = json.dumps({"scores": [
    {"indexer_id": 1, "health_score": 90, "reasoning": "Reliable and fast."}]})


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama. Returns the list of /api/generate payloads sent."""
    ollama_models.forget_cache()
    sent = []
    state = {"refuse_schema": False, "answer": GOOD_ANSWER, "done_reason": "stop"}

    def post(url, json=None, timeout=None):
        if url.endswith("/api/show"):
            show = SHOW.get(json["model"])
            return Resp(200, show) if show else Resp(404, {"error": "not found"})
        if url.endswith("/api/generate"):
            sent.append(dict(json))  # a copy: the caller may change it and resend
            if state["refuse_schema"] and isinstance(json.get("format"), dict):
                return Resp(400, {"error": "invalid format"})
            return Resp(200, {"response": state["answer"], "done_reason": state["done_reason"],
                              "eval_count": 24, "eval_duration": 1e9})
        return Resp(404, {})

    def get(url, timeout=None):
        if url.endswith("/api/tags"):
            return Resp(200, {"models": [{"name": n} for n in SHOW]})
        if url.endswith("/api/ps"):
            return Resp(200, {"models": []})
        return Resp(404, {})

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    yield type("Fake", (), {"sent": sent, "state": state})
    ollama_models.forget_cache()


URL = "http://ollama.test:11434"


# --- how each model thinks ------------------------------------------------------

@pytest.mark.parametrize("model, expected", [
    ("gemma4:latest", False),            # can be switched off: off
    ("gpt-oss:20b", "low"),              # levels only: the lowest
    ("deepseek-r1:1.5b", False),         # capability without details: a toggle
    ("llama3-groq-tool-use:8b", None),   # does not think: send nothing
])
def test_thinking_is_off_or_lowest(ollama, model, expected):
    settings = ollama_models.request_settings(URL, model, 8192)
    if expected is None:
        assert "think" not in settings
    else:
        assert settings["think"] == expected


def test_level_model_that_can_be_switched_off_is_switched_off():
    profile = {"thinking": "levels", "think_levels": ["off", "low", "high"]}
    assert ollama_models.recommended_think(profile) is False


def test_unknown_model_gets_no_think_field_and_keeps_its_context(ollama):
    # A failed lookup must not stop scoring or invent a limit.
    settings = ollama_models.request_settings(URL, "not-installed", 16384)
    assert "think" not in settings
    assert settings["options"]["num_ctx"] == 16384


# --- context ------------------------------------------------------------------------

def test_context_is_capped_at_the_models_limit(ollama):
    assert ollama_models.request_settings(URL, "llama3-groq-tool-use:8b", 16384)["options"]["num_ctx"] == 8192
    assert ollama_models.effective_context(URL, "tiny:1b", 8192) == 2048
    assert ollama_models.effective_context(URL, "gemma4:latest", 16384) == 16384


@pytest.mark.parametrize("max_context, expected", [
    (131072, 8192), (8192, 8192), (2048, 2048), (None, 8192)])
def test_a_chosen_model_gets_8k_or_its_maximum(max_context, expected):
    assert ollama_models.context_for_model(max_context) == expected


def test_slider_stops_end_exactly_at_the_models_maximum():
    assert ollama_models.context_stops(32768)[-1] == 32768
    assert ollama_models.context_stops(128000) == [2048, 4096, 8192, 16384, 32768, 65536, 128000]
    assert ollama_models.context_stops(None)[-1] == 262144


def test_embedding_models_are_marked_unable_to_score(ollama):
    names = {m["name"]: m for m in ollama_models.list_models(URL)}
    assert names["nomic-embed-text:latest"]["can_score"] is False
    assert names["gemma4:latest"]["can_score"] is True
    assert names["gemma4:latest"]["max_context"] == 131072


# --- the scoring request --------------------------------------------------------------

def _score(model, ctx=8192, n=1):
    rows = [{"indexer_id": i + 1, "name": f"ix{i}", "total_queries": 10} for i in range(n)]
    return llm_client.ollama_score_indexers(rows, URL, model, timeout=30, context_window=ctx)


def test_scoring_sends_schema_think_temperature_and_capped_context(ollama):
    result = _score("gemma4:latest")
    assert result[1]["health_score"] == 90
    p = ollama.sent[0]
    assert p["format"] == llm_client.SCORE_SCHEMA
    assert p["think"] is False
    assert p["options"]["temperature"] == ollama_models.SCORING_TEMPERATURE
    assert p["options"]["num_ctx"] == 8192
    assert p["options"]["num_predict"] == llm_client.answer_budget(1)


def test_scoring_never_sends_more_context_than_the_model_has(ollama):
    _score("llama3-groq-tool-use:8b", ctx=16384)
    assert ollama.sent[0]["options"]["num_ctx"] == 8192


def test_models_that_must_think_get_room_to_think(ollama):
    _score("gpt-oss:20b")
    assert ollama.sent[0]["think"] == "low"
    # A thinking model gets the room the context has left, not a fixed cap.
    budget = ollama.sent[0]["options"]["num_predict"]
    assert 6000 < budget < 8192


def test_thinking_on_gets_the_room_too(ollama):
    llm_client.ollama_score_indexers([{"indexer_id": 1}], URL, "gemma4:latest", 30,
                                     context_window=16384, think=True)
    assert ollama.sent[0]["think"] is True
    assert ollama.sent[0]["options"]["num_predict"] > 14000


def test_a_stored_think_value_is_not_sent_to_a_model_that_cannot_think(ollama):
    llm_client.ollama_score_indexers([{"indexer_id": 1}], URL, "llama3-groq-tool-use:8b", 30,
                                     think="medium")
    assert "think" not in ollama.sent[0]


def test_old_server_that_refuses_the_schema_falls_back_to_plain_json(ollama):
    ollama.state["refuse_schema"] = True
    result = _score("gemma4:latest")
    assert [p["format"] for p in ollama.sent] == [llm_client.SCORE_SCHEMA, "json"]
    assert 1 in result


def test_cut_off_answer_is_logged(ollama, caplog):
    ollama.state["done_reason"] = "length"
    _score("gemma4:latest")
    assert "cut off" in caplog.text


def test_prompt_bigger_than_the_budget_uses_the_smallest_batch():
    rows = [{"indexer_id": i} for i in range(10)]
    assert llm_client._batch_size(rows, overhead_tokens=10_000, context_window=2048) == 1


def test_narration_turns_thinking_off_but_leaves_temperature(ollama):
    llm_client.narrate(URL, "gemma4:latest", "Summarise.", timeout=30)
    p = ollama.sent[0]
    assert p["think"] is False
    assert "temperature" not in p["options"]
    assert "format" not in p


# --- calibration runs the way production does ---------------------------------------

def test_warmup_loads_at_the_test_context_with_thinking_off(ollama):
    model_validator._warmup(URL, "gemma4:latest", 8192)
    p = ollama.sent[0]
    assert p["options"]["num_ctx"] == 8192 and p["think"] is False




def test_batch_search_starts_at_the_real_count_and_is_not_capped_by_the_slider(ollama, monkeypatch):
    tried = []

    def probe(url, model, timeout, prompt, count, ctx, batch):
        tried.append(batch)
        return {"batch": batch, "passed": batch <= 8, "per_call_s": 1.0,
                "missing": 0, "invented": 0, "within_timeout": True}
    monkeypatch.setattr(model_validator, "_probe_batch", probe)
    out = model_validator._calibrate_batch(URL, "gemma4:latest", 60, "", 8, 8192,
                                           ceiling=model_validator.BATCH_LADDER[0])
    assert tried[0] == 8 and out["max_safe_batch"] == 8
