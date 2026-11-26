"""
core/ollama_models.py -- what each Ollama model can do, and how much context to give it.

Ollama's /api/show reports a model's trained context length as
`<architecture>.context_length` in model_info (llama.context_length,
gemma4.context_length, ...) and what it can do in `capabilities`. Checked on
all 32 models of a real server: the key always follows that pattern; limits
run from 512 to 262,144 tokens and are not always powers of two (128,000).

Two rules live here so every caller applies them the same way:

  * A model's context is 8K by default, or the model's own maximum if that is
    smaller (DEFAULT_CONTEXT, context_for_model).
  * Never send more context than the model supports (effective_context).
    Ollama does not clamp num_ctx to the trained length itself, and anything
    past the trained length is either wasted memory or degraded output.

It also decides what is sent with every request (request_settings): thinking
off, or the lowest level for models that cannot turn it off (gpt-oss), and a
low temperature, so scoring is fast and repeatable. Checked on a real server:
gemma4 reports thinking ON by default, so without this it reasons at length
before every scoring answer.

Every lookup fails soft: if Ollama cannot be asked, callers carry on with the
value they were given. A failed lookup must never stop a scan or a page.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("inspectarr")

DEFAULT_CONTEXT = 8192
# Scoring is a judgement against a fixed points table, not creative writing:
# a low temperature makes the same indexer score the same way twice, which is
# also what lets calibration compare two runs fairly. Same value DockLlama uses.
SCORING_TEMPERATURE = 0.1
# Slider stops. A model whose limit is not one of these gets its exact limit
# as the last stop.
CONTEXT_STOPS = (2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144)
LOOKUP_TIMEOUT = 10
CACHE_SECONDS = 600

_cache: dict = {}
_cache_lock = threading.Lock()


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/")


def can_score(capabilities) -> bool:
    """Can this model do the scoring job (plain text completion)?

    Embedding-only models report e.g. ["embedding"] and cannot. A server too
    old to report capabilities at all gets the benefit of the doubt.
    """
    if not capabilities:
        return True
    return "completion" in capabilities


def model_profile(url: str, model: str) -> dict | None:
    """{"max_context": int | None, "capabilities": [...], "can_score": bool},
    or None if Ollama could not be asked. Cached per (server, model)."""
    url, model = _norm(url), (model or "").strip()
    if not url or not model:
        return None
    key = (url.lower(), model)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        r = requests.post(f"{url}/api/show", json={"model": model}, timeout=LOOKUP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:
        log.debug(f"model_profile({model}) failed: {exc}")
        return None
    info = data.get("model_info") or {}
    arch = info.get("general.architecture")
    ctx = info.get(f"{arch}.context_length") if arch else None
    if not isinstance(ctx, int):
        # Fall back to any *.context_length key before giving up.
        ctx = next((v for k, v in info.items()
                    if k.endswith(".context_length") and isinstance(v, int)), None)
    caps = list(data.get("capabilities") or [])
    thinking, levels = _thinking_of(data, caps)
    profile = {"max_context": ctx if isinstance(ctx, int) and ctx > 0 else None,
               "capabilities": caps, "can_score": can_score(caps),
               "thinking": thinking, "think_levels": levels}
    with _cache_lock:
        _cache[key] = (now, profile)
    return profile


def _thinking_of(show: dict, caps: list) -> tuple[str, list]:
    """How a model thinks, from /api/show: ("none" | "toggle" | "levels", levels).

    Ollama describes it in a `thinking` block when it knows more than the
    capability: gemma4 lists values [false, true]; gpt-oss lists only levels
    ["low", "medium", "high"] and cannot be switched off. Some models list
    the capability with no details (deepseek-r1, granite4.2): a plain toggle.
    """
    t = show.get("thinking")
    if isinstance(t, dict) and isinstance(t.get("values"), list):
        values = t["values"]
        strings = [v for v in values if isinstance(v, str)]
        if strings:
            return "levels", (["off"] if False in values else []) + strings
        if True in values:
            return "toggle", []
    if "thinking" in caps:
        return "toggle", []
    return "none", []


def recommended_think(profile: dict | None):
    """The `think` value to send: False where it can be turned off, the
    lowest level where it cannot, or None (send nothing) for models that do
    not think. Ollama 0.40 accepts `think: false` for a model that does not
    think, but older versions refused it, so None means the field is left
    out entirely.

    gpt-oss cannot be switched off at all: sent `false`, it still thinks
    briefly and answers exactly as at "low" (measured), so "low" is the
    honest value to send."""
    if not profile:
        return None
    mode, levels = profile.get("thinking", "none"), profile.get("think_levels") or []
    if mode == "levels":
        if "off" in levels:
            return False
        return "low" if "low" in levels else (levels[0] if levels else None)
    if mode == "toggle":
        return False
    return None


def valid_think(value) -> bool:
    """Is this a think value a user (or calibration) may store?"""
    return value is None or isinstance(value, bool) or value in ("low", "medium", "high")


def request_settings(url: str, model: str, context_window: int,
                     temperature: float | None = SCORING_TEMPERATURE,
                     num_predict: int | None = None,
                     think=None) -> dict:
    """The fields to merge into an /api/generate payload for this model.

    One place for it, so the scorer, the narrator and calibration send the
    same thing -- a test is only meaningful if it runs the way production
    does. Returns {"options": {...}} plus "think" when the model thinks.

    think=None uses the recommended value. Anything else is a choice made by
    calibration or the user (gpt-oss at "medium", say) and is sent as given,
    except to a model that does not think at all, which gets nothing.
    """
    profile = model_profile(url, model)
    limit = (profile or {}).get("max_context")
    ctx = context_window
    if limit and ctx > limit:
        log.info(f"Context {ctx} is more than {model} supports; using {limit}")
        ctx = limit
    options = {"num_ctx": ctx}
    if temperature is not None:
        options["temperature"] = temperature
    if num_predict:
        options["num_predict"] = num_predict
    out = {"options": options}
    if think is None or not valid_think(think):
        think = recommended_think(profile)
    elif (profile or {}).get("thinking", "none") == "none" and profile:
        think = None
    if think is not None:
        out["think"] = think        # top level, never inside options
    return out


def list_models(url: str) -> list[dict] | None:
    """Every model on the server with its profile, or None if unreachable.

    Profiles are fetched concurrently: a server with 30 models answers in
    about the time of one lookup instead of thirty.
    """
    url = _norm(url)
    if not url:
        return None
    try:
        r = requests.get(f"{url}/api/tags", timeout=LOOKUP_TIMEOUT)
        r.raise_for_status()
        names = sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))
    except Exception as exc:
        log.debug(f"list_models failed: {exc}")
        return None
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(names)))) as pool:
        profiles = list(pool.map(lambda n: model_profile(url, n), names))
    out = []
    for name, p in zip(names, profiles):
        p = p or {"max_context": None, "capabilities": [], "can_score": True,
                  "thinking": "none", "think_levels": []}
        out.append({"name": name, **p})
    return out


def context_for_model(max_context: int | None) -> int:
    """The context a model gets when it is chosen: 8K, or its maximum if smaller."""
    if max_context and max_context < DEFAULT_CONTEXT:
        return max_context
    return DEFAULT_CONTEXT


def context_stops(max_context: int | None) -> list[int]:
    """Slider stops up to the model's maximum, ending exactly at it."""
    if not max_context:
        return list(CONTEXT_STOPS)
    stops = [s for s in CONTEXT_STOPS if s <= max_context]
    if not stops or stops[-1] != max_context:
        stops.append(max_context)
    return stops


def effective_context(url: str, model: str, configured: int) -> int:
    """The context to actually send: the configured value, but never more
    than the model supports. Unknown limit: the configured value."""
    return request_settings(url, model, configured)["options"]["num_ctx"]


def unload(url: str, model: str) -> None:
    """Free the model's memory now (keep_alive 0). Best effort: never raises."""
    try:
        requests.post(f"{_norm(url)}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=LOOKUP_TIMEOUT)
    except Exception as exc:
        log.debug(f"unload({model}) failed: {exc}")


def forget_cache() -> None:
    """For tests, and after a model is pulled (its limit may change)."""
    with _cache_lock:
        _cache.clear()
