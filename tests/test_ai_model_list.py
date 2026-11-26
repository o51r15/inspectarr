"""
The AI tab's model list: rich model details, the graphics-card question per
server, calibration history, and the thinking form field.
"""
import pytest
from werkzeug.datastructures import MultiDict

from tests.conftest import read_config, write_config

A = "http://192.168.1.125:11434"
B = "http://192.168.1.97:11434"


def _server(config_path, url=A, **ollama):
    data = read_config(config_path)
    data["prowlarr"]["ollama"].update({"enabled": True, "url": url, **ollama})
    write_config(config_path, data)


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


def test_model_list_carries_size_limits_and_abilities(client, config_path, monkeypatch):
    _server(config_path, model="gemma4:latest")
    import requests
    import core.ollama_models as om
    om.forget_cache()
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: Resp({"models": [
        {"name": "gemma4:latest", "size": 6 * 2**30, "details": {"parameter_size": "7.5B", "family": "gemma4"}},
        {"name": "nomic-embed-text:latest", "size": 2**28, "details": {}}]}))
    monkeypatch.setattr(om, "list_models", lambda url: [
        {"name": "gemma4:latest", "max_context": 131072, "thinking": "toggle", "think_levels": [], "can_score": True},
        {"name": "nomic-embed-text:latest", "max_context": 2048, "thinking": "none", "think_levels": [],
         "can_score": False}])
    d = client.get("/config/ollama/models").get_json()
    assert d["ok"] and d["current"] == "gemma4:latest"
    g = {m["name"]: m for m in d["details"]}
    assert g["gemma4:latest"]["size_gb"] == 6.0 and g["gemma4:latest"]["max_context"] == 131072
    assert g["gemma4:latest"]["thinking"] == "toggle"
    assert g["nomic-embed-text:latest"]["can_score"] is False


def test_hardware_answer_is_kept_per_server(client, config_path):
    _server(config_path, A)
    assert client.get("/config/ai/hardware").get_json()["hardware"] == ""
    assert client.post("/config/ai/hardware", json={"hardware": "gpu16"}).get_json()["hardware"] == "gpu16"
    assert client.get("/config/ai/hardware").get_json()["hardware"] == "gpu16"
    _server(config_path, B)
    assert client.get("/config/ai/hardware").get_json()["hardware"] == ""


def test_hardware_rejects_unknown_answers(client, config_path):
    _server(config_path)
    assert client.post("/config/ai/hardware", json={"hardware": "gpu999"}).status_code == 400


def test_history_keeps_every_run_newest_first(app, client, config_path):
    _server(config_path)
    st = app.config["STATE"]
    st.save_validation("gemma4:latest", {"passed": True, "quality_score": 80.0}, ollama_url=A)
    st.save_validation("gemma4:latest", {"passed": True, "quality_score": 91.0,
                                         "settings": {"context_window": 16384}}, ollama_url=A)
    st.save_validation("gemma4:latest", {"passed": False}, ollama_url=B)       # another server
    h = client.get("/config/ai/history?model=gemma4:latest").get_json()["history"]
    assert [r["quality_score"] for r in h] == [91.0, 80.0]
    assert h[0]["settings"]["context_window"] == 16384


@pytest.mark.parametrize("form, stored", [("", None), ("off", False), ("on", True),
                                          ("medium", "medium"), ("low", "low")])
def test_thinking_field(form, stored):
    from ui.settings_schema import form_to_updates
    upd = form_to_updates(MultiDict({"ollama_think": form}), {"prowlarr": {"ollama": {"think": "high"}}})
    assert upd["prowlarr"]["ollama"]["think"] == stored


def test_thinking_field_rejects_nonsense():
    from ui.settings_schema import FieldError, form_to_updates
    with pytest.raises(FieldError):
        form_to_updates(MultiDict({"ollama_think": "turbo"}), {})


def test_ai_pane_has_the_list_and_no_dropdown(client, config_path):
    _server(config_path)
    html = client.get("/config").get_data(as_text=True)
    assert 'id="ai-model-list"' in html and 'id="ai-hardware"' in html
    assert 'name="ollama_think"' in html and 'id="ollama-context-slider"' in html
    assert "ollama-model-select" not in html
    assert html.index("js/pages/ai-models.js") < html.index("js/pages/settings.js")
