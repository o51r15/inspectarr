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



# ---------------------------------------------------------------------
# Context budgeting (ROADMAP item 19)
# ---------------------------------------------------------------------
#
# Measured against the real payload on 2026-08-25 (37 indexers, 20 fields
# each). These are the ACTUAL prompt, captured by intercepting what the
# scorer hands this module -- not a reconstruction. A first attempt did
# rebuild it by hand and came out 41% low, which is the same mistake the
# roadmap already recorded once.
#
#   system prompt                      ~391 tok
#   payload indent=2 (shipped)        ~5147 tok   TOTAL ~5538  EXCEEDS 4k
#   payload compact                   ~3981 tok   TOTAL ~4372  EXCEEDS 4k
#   payload compact + short keys      ~2520 tok   TOTAL ~2977  fits 4k
#
# Per indexer: 139 tok pretty, 108 compact, 68 short.
#
# So compact JSON alone does NOT fit 4k -- it saves 23%, which is real but
# not enough. Short keys are the large win, because with 20 fields per row
# the KEYS outweigh the values. Together they take the ceiling from about
# 16 indexers to about 40.
#
# Batching still exists because indexer count is unbounded: 40 is a much
# better ceiling than 16, but it is still a ceiling.
#
# Note the response schema is deliberately NOT shortened. Only the INPUT
# keys change, so _parse_response needs no matching update and the model
# still answers in the same self-describing shape it always did. The
# roadmap assumed both ends had to move; only one does.

# Short names for the input payload. Every field the scorer sends is mapped;
# an unmapped one would silently keep its long name and quietly undo the
# saving, so this is asserted in the tests rather than trusted.
SHORT_KEYS = {
    "indexer_id":                 "id",
    "name":                       "n",
    "priority":                   "pri",
    "deterministic_health_score": "det",
    "avg_response_ms":            "ms",
    "weighted_success_rate_pct":  "succ",
    "grab_success_pct":           "gs",
    "malicious_rate_pct":         "malpct",
    "total_queries":              "q",
    "failed_queries":             "qf",
    "total_rss_queries":          "rss",
    "failed_rss_queries":         "rssf",
    "total_auth_queries":         "auth",
    "failed_auth_queries":        "authf",
    "total_grabs":                "g",
    "failed_grabs":               "gf",
    "malicious_hits":             "mal",
    "in_backoff":                 "bo",
    "total_records":              "rec",
    "trend":                      "tr",
}

# Roughly four characters per token. Crude, and deliberately so: a real
# tokenizer would be another dependency to serve a budgeting decision that
# is already padded. It errs high for JSON, which is the safe direction.
CHARS_PER_TOKEN = 4.0

# How much of the window the prompt may claim. The model has to emit a
# reasoning sentence per indexer, and a prompt that fills the context leaves
# nothing to answer with -- which presents as truncated or invented JSON
# rather than as an error.
PROMPT_BUDGET_FRACTION = 0.66

# Warn only when the prompt is genuinely near the window. Batches are sized
# to PROMPT_BUDGET_FRACTION, so that is the expected size, not a problem.
# Hard cap on items per call, independent of the token budget.
#
# Measured on qwen2.5-coder:7b: 25 indexers score correctly, 30 makes it echo
# the input instead of scoring (in 3 seconds, at half the window), and 37 in
# an 8k window returns well-formed JSON that silently omits five of them.
#
# That last one is the dangerous case: it looks exactly like success. So the
# limit that matters is how many items the model will actually reason about,
# not how many fit -- and the two are nowhere near each other.
#
# A property of the model rather than of Inspectarr. Raise it if yours copes;
# core/model_validator.py's context test is how to find out.
MAX_INDEXERS_PER_CALL = 25

OVERFLOW_WARN_FRACTION = 0.9

DEFAULT_CONTEXT_WINDOW = 4096


def _estimate_tokens(text: str) -> int:
    """Approximate token count for a string."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def _shorten(rows: list[dict]) -> list[dict]:
    """Rename input fields to their short forms. Unknown fields pass through."""
    return [{SHORT_KEYS.get(k, k): v for k, v in row.items()} for row in rows]


def _key_legend() -> str:
    """The mapping, for the prompt. Without it the model is guessing."""
    return ("Field keys: "
            + ", ".join(f"{v}={k}" for k, v in SHORT_KEYS.items())
            + "\n")


def _encode_payload(rows: list[dict]) -> str:
    """Compact, short-keyed JSON."""
    return json.dumps(_shorten(rows), separators=(",", ":"))


def _batch_size(rows: list[dict], overhead_tokens: int,
                context_window: int,
                max_items: int = MAX_INDEXERS_PER_CALL) -> int:
    """
    How many indexers go in one call.

    The SMALLER of two limits, because they constrain different things:

      tokens  what fits in the window, measured from this payload rather
              than a hardcoded number, since field counts change
      items   how many the model will actually reason about

    The second is not derivable from the first and is usually much lower --
    measured at 25 for qwen2.5-coder:7b, where 30 causes it to echo the input
    and 37 (in a window twice as large) causes it to silently drop five.

    Always at least 1. A single indexer that does not fit is a real problem,
    but splitting it further is not the fix for it.
    """
    if not rows:
        return 0
    per = _estimate_tokens(_encode_payload(rows)) / len(rows)
    budget = context_window * PROMPT_BUDGET_FRACTION - overhead_tokens
    by_tokens = len(rows) if (per <= 0 or budget <= 0) else int(budget / per)
    return max(1, min(by_tokens, max_items or len(rows)))


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
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    max_indexers_per_call: int = MAX_INDEXERS_PER_CALL,
) -> dict[int, dict]:
    """
    Score every indexer, splitting into several calls if they will not fit.

    Returns {indexer_id: {"health_score": float, "reasoning": str}} on
    success, empty dict on total failure. Never raises.

    Batching is invisible to the caller by design: indexer_scorer computes
    its cache hash over the whole payload before calling, so splitting here
    cannot affect caching, and the merged result is indistinguishable from a
    single call's.

    A partial result is returned rather than discarded. If three batches of
    four succeed and one fails, twelve indexers scored is plainly better than
    none -- and indexer_scorer already falls back to the deterministic score
    for anything absent from the dict.
    """
    if not ollama_url or not model or not indexer_data:
        return {}

    system_prompt = (custom_prompt.strip()
                     if custom_prompt and custom_prompt.strip()
                     else SYSTEM_PROMPT)
    overhead = _estimate_tokens(system_prompt + _key_legend()
                                + "\nIndexer data:\n")
    size = _batch_size(indexer_data, overhead, context_window,
                       max_indexers_per_call)

    if size >= len(indexer_data):
        return _score_one_batch(indexer_data, ollama_url, model, timeout,
                                system_prompt, context_window)

    batches = [indexer_data[i:i + size]
               for i in range(0, len(indexer_data), size)]
    log.info(
        "Scoring %d indexers in %d batches of up to %d "
        "(context window %d tokens)",
        len(indexer_data), len(batches), size, context_window)

    merged: dict[int, dict] = {}
    failed = 0
    for n, batch in enumerate(batches, 1):
        part = _score_one_batch(batch, ollama_url, model, timeout,
                                system_prompt, context_window)
        if not part:
            failed += 1
            log.warning("Batch %d/%d returned nothing", n, len(batches))
        merged.update(part)

    if failed:
        # Never let a partial result look complete. The scorer will fall back
        # to deterministic scores for the gaps, and the operator should know
        # that happened rather than wonder why some indexers look different.
        log.warning(
            "%d of %d scoring batches failed; %d of %d indexers scored by AI",
            failed, len(batches), len(merged), len(indexer_data))
    return merged


def _score_one_batch(
    indexer_data: list[dict],
    ollama_url: str,
    model: str,
    timeout: int,
    system_prompt: str,
    context_window: int,
) -> dict[int, dict]:
    """One Ollama call. Same contract as the public function."""
    prompt = (
        system_prompt
        + "\n" + _key_legend()
        + "Indexer data:\n"
        + _encode_payload(indexer_data)
    )

    est = _estimate_tokens(prompt)
    if est > context_window * OVERFLOW_WARN_FRACTION:
        # Genuinely close to the window, not merely at the batching target.
        # This means batching could not help: a single indexer whose own row
        # is enormous, or a very long custom system prompt. Say so plainly --
        # the alternative failure is the model quietly losing its
        # instructions and inventing plausible scores, which is much worse
        # than a warning.
        #
        # Threshold is deliberately NOT PROMPT_BUDGET_FRACTION: batches are
        # sized to exactly that, so warning there fired on every correctly
        # sized batch. A warning that cries on the normal path teaches people
        # to ignore it, and then the real one goes unread too.
        log.warning(
            "Scoring prompt is ~%d tokens against a %d-token window; the "
            "model may drop the instructions. Raise prowlarr.ollama."
            "context_window if this model supports more.", est, context_window)

    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                # Budgeting for a window Ollama is not actually using would
                # be pointless: without num_ctx it applies its own default
                # (4096 for most models) and silently truncates anything
                # past it -- which is precisely the failure this item exists
                # to remove.
                "options": {"num_ctx": context_window},
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

    return results
