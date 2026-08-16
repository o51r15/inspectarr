"""
core/llm_client.py — Ollama integration for AI-powered indexer scoring

Optional feature. When prowlarr.ollama.url and prowlarr.ollama.model are set
in config.yaml, the scorer calls ollama_score_indexers() after computing
deterministic scores. Ollama receives ALL available data about each indexer
(including the deterministic score) and produces the final 0-100 health score.

If Ollama is not configured, unreachable, times out, or returns garbage,
this module returns an empty dict and the caller keeps deterministic scores.
This must never block or crash the scoring pipeline.

Design mirrors trackarr's scoring.py / quality_assessment.py pattern.
"""

import json
import logging
import re

import requests

log = logging.getLogger("inspectarr")

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """\
You are scoring Prowlarr torrent indexers for health and reliability.
Produce a score from 0-100 for each indexer using these strict point allocations:

RELIABILITY (40 points max):
- Success rate 99%+: 40pts | 95-99%: 35pts | 90-95%: 25pts | 80-90%: 15pts | <80%: 0-10pts
- Grab failures weigh more heavily than query failures.
- Auth failures indicate credential or access problems — penalize heavily.

PERFORMANCE (25 points max):
- Avg response <500ms: 25pts | 500-1000ms: 20pts | 1-2s: 15pts | 2-3s: 10pts | 3-5s: 5pts | >5s: 0pts

TRUST (25 points max):
- Start at 25. Deduct 8 points per malicious hit. Minimum 0.

AVAILABILITY (10 points max):
- Not in backoff: 10pts | Currently in backoff: 0pts

ADJUSTMENTS:
- Low data (few total queries): score conservatively, pull toward 50.
- If failure counts are disproportionately high relative to successes, penalize further.
- RSS-heavy indexers with few grabs: note but do not heavily penalize.

You are also given each indexer's deterministic health score for reference.
Your score REPLACES it — use your judgment across all the data provided.

Return ONLY valid JSON matching this EXACT schema — no extra fields:
{"scores": [{"indexer_id": <int>, "health_score": <int 0-100>, "reasoning": "<one sentence explaining your score>"}]}

CRITICAL: Each object MUST have exactly three keys: indexer_id, health_score, reasoning.
Do NOT echo back the input data. Do NOT include weight, avg_response_time, or any other input fields.
The "reasoning" field MUST be a non-empty string explaining why you gave that score.
"""


def _parse_response(raw: str) -> list[dict]:
    """
    Parse Ollama's response text into a list of score dicts.
    Strips <think> blocks (reasoning models) and markdown fences.
    Returns [] on any parse failure.
    """
    cleaned = THINK_BLOCK.sub("", raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: find the outermost [...] in the response
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    # Handle {"scores": [...]} or {"indexers": [...]} or any wrapper object
    if isinstance(parsed, dict):
        # Try common key names first, then fall back to first list value
        for key in ("scores", "results", "indexers", "data"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        # Fall back: find the first value that is a list
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def ollama_score_indexers(
    indexer_data: list[dict],
    ollama_url: str,
    model: str,
    timeout: int = 120,
    custom_prompt: str = "",
) -> dict[int, dict]:
    """
    Send all indexer data to Ollama in a single batch prompt.

    Returns {indexer_id: {"health_score": float, "reasoning": str}} on success,
    empty dict on any failure. Never raises.
    """
    if not ollama_url or not model or not indexer_data:
        return {}

    system_prompt = custom_prompt.strip() if custom_prompt and custom_prompt.strip() else SYSTEM_PROMPT
    prompt = (
        system_prompt
        + "\nIndexer data:\n"
        + json.dumps(indexer_data, indent=2)
    )

    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.warning(
                "Ollama scoring returned HTTP %d: %s",
                resp.status_code, resp.text[:300],
            )
            return {}
        data = resp.json()
    except requests.Timeout:
        log.warning("Ollama scoring timed out after %ds", timeout)
        return {}
    except Exception as exc:
        log.warning("Ollama scoring request failed: %s", exc)
        return {}

    raw_response = data.get("response", "")
    if not raw_response:
        log.warning("Ollama returned empty response")
        return {}

    log.debug("Raw Ollama response (first 2000 chars): %s", repr(raw_response[:2000]))

    parsed = _parse_response(raw_response)
    if not parsed:
        log.warning("Could not parse Ollama response as JSON array")
        log.warning("Raw Ollama response (first 2000 chars): %s", repr(raw_response[:2000]))
        return {}

    results = {}
    for item in parsed:
        iid = item.get("indexer_id")
        score = item.get("health_score")
        if iid is None or score is None:
            continue
        try:
            results[int(iid)] = {
                "health_score": max(0.0, min(100.0, float(score))),
                "reasoning": str(item.get("reasoning", "")),
            }
        except (TypeError, ValueError):
            continue

    if results:
        log.info("Ollama scored %d indexer(s) successfully", len(results))
    else:
        log.warning("Ollama response parsed but contained no valid scores")
        log.warning("Parsed items: %s", repr(parsed[:3]) if parsed else "[]")
        log.warning("Raw Ollama response (first 2000 chars): %s", repr(raw_response[:2000]))

    return results
