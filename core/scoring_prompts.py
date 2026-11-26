"""
core/scoring_prompts.py -- the instructions the AI scores indexers with.

The built-in Default can be read but not edited, so Inspectarr updates can
keep improving it. People can add Prompt 1, 2, 3..., each starting as a copy
of the Default's instructions, and choose exactly one for scoring
(DockLlama's design).

A prompt is two parts:

  instructions   who the model is, the points table, the adjustment rules --
                 the part a person may rewrite;
  answer format  the exact JSON shape and the "three keys, no echo" rules.
                 Never editable: it is added after the instructions of every
                 prompt, the Default included, because it is what Inspectarr
                 needs to read the answer at all.

The Default is sent exactly as llm_client.SYSTEM_PROMPT, byte for byte, so
results and caches from before this split stay comparable.

Calibrate, the model ranking and the better-model banner always use the
Default: scores must be comparable between models and over time. A custom
prompt is tested from its own screen (test results kept per prompt revision).
"""
from __future__ import annotations

import logging

from .llm_client import SYSTEM_PROMPT

log = logging.getLogger("inspectarr")

_FORMAT_MARK = "Return ONLY valid JSON"
_AT = SYSTEM_PROMPT.index(_FORMAT_MARK)
ANSWER_FORMAT = SYSTEM_PROMPT[_AT:].strip()
DEFAULT_INSTRUCTIONS = SYSTEM_PROMPT[:_AT].strip()

ACTIVE_KEY = "scoring_prompt_active"
MIN_CHARS, MAX_CHARS = 50, 20000


class PromptError(ValueError):
    """A user-facing reason a prompt cannot be saved."""


def build(instructions: str | None = None) -> str:
    """The full prompt sent to the model: the Default as-is, or the given
    instructions followed by the locked answer format."""
    if instructions is None:
        return SYSTEM_PROMPT
    return instructions.strip() + "\n\n" + ANSWER_FORMAT + "\n"


def strip_format(text: str) -> str:
    """Instructions only: drop an answer-format block someone pasted in (an
    old full custom prompt carried one), since it is added anyway."""
    text = (text or "").replace("\r\n", "\n")
    at = text.find(_FORMAT_MARK)
    return (text[:at] if at >= 0 else text).strip()


def clean_text(text: str) -> str:
    text = strip_format(text)
    if len(text) < MIN_CHARS:
        raise PromptError("The instructions are too short to tell the AI how to score an indexer.")
    if len(text) > MAX_CHARS:
        raise PromptError(f"The instructions are too long (over {MAX_CHARS:,} characters). Long "
                          "instructions leave less room for your indexers.")
    return text


def _norm(s: str) -> str:
    return "\n".join(line.rstrip() for line in (s or "").strip().splitlines())


def is_default(text: str) -> bool:
    """Is this (an old full custom prompt, or instructions) just the Default?"""
    return _norm(strip_format(text)) == _norm(DEFAULT_INSTRUCTIONS)


def label(row: dict | None) -> str:
    if not row:
        return "Default"
    return f"Prompt {row['number']}" + (f" ({row['name']})" if row.get("name") else "")


# --- which prompt scores -----------------------------------------------------

def active(state) -> dict | None:
    """The prompt row in use for scoring, or None for the Default."""
    if state is None:
        return None
    try:
        pid = int(state.get_app_state(ACTIVE_KEY) or 0)
    except (TypeError, ValueError):
        pid = 0
    return state.get_scoring_prompt(pid) if pid else None


def active_system(state) -> tuple[str, str]:
    """(label, full prompt) for live scoring."""
    row = active(state)
    return label(row), build(row["text"] if row else None)


def use(state, prompt_id: int) -> None:
    if prompt_id and state.get_scoring_prompt(prompt_id) is None:
        raise PromptError("That prompt doesn't exist any more.")
    state.set_app_state(ACTIVE_KEY, str(int(prompt_id or 0)))


def delete(state, prompt_id: int) -> None:
    row = active(state)
    if row and row["id"] == prompt_id:
        state.set_app_state(ACTIVE_KEY, "0")            # back to the Default
    state.delete_scoring_prompt(prompt_id)


# --- the old single setting ------------------------------------------------------

def migrate_config_prompt(state, config_path: str) -> str | None:
    """Move prowlarr.ollama.system_prompt out of config.yaml.

    Before prompts were kept here, a custom prompt was one config setting
    holding the WHOLE prompt (answer format included). Run at every start:

      * the same as the Default (a saved copy) -> nothing is created;
      * different -> it becomes the next Prompt N and is put in use;

    and either way the key is removed from config.yaml, so it cannot sit
    there looking as if it still did something. Returns what happened, for
    the log. Never raises: a failed migration leaves the key and the Default.
    """
    try:
        from . import config_store
        doc = config_store.load_document(config_path)
        ollama = ((doc.data or {}).get("prowlarr") or {}).get("ollama")
        if not ollama or "system_prompt" not in ollama:
            return None
        old = str(ollama.get("system_prompt") or "")
        outcome = "removed an empty system_prompt setting"
        if old.strip() and not is_default(old):
            text = strip_format(old)
            # A restored old config.yaml brings the key back: reuse the prompt
            # it already became instead of adding a copy at every start.
            same = next((p for p in state.list_scoring_prompts()
                         if _norm(p["text"]) == _norm(text)), None)
            pid = same["id"] if same else state.create_scoring_prompt(text, name="from config.yaml")
            if pid:
                use(state, pid)
                outcome = (f"moved the custom scoring prompt from config.yaml into "
                           f"{label(state.get_scoring_prompt(pid))}, in use")
        elif old.strip():
            outcome = "removed system_prompt from config.yaml: it was the Default"
        del ollama["system_prompt"]
        config_store.save_document(config_path, doc)
        log.info(f"Scoring prompts: {outcome}")
        return outcome
    except Exception as exc:
        log.warning(f"Scoring prompts: could not migrate system_prompt from config.yaml: {exc}")
        return None
