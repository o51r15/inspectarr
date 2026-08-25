"""
core/ollama_registry.py -- model identity and update checking for Ollama.

Two questions, deliberately separated because they have very different costs
and very different failure modes:

  local_digest()     which exact build is installed here?   one LAN GET
  registry_digest()  which build does upstream publish?     one internet GET

Neither pulls anything. That distinction is the whole reason this module
exists: the first implementation of update checking called /api/pull to find
out whether an update existed, and Ollama has no dry-run -- so it genuinely
pulled, on every Settings page load, starting multi-gigabyte downloads that
were abandoned at the timeout (B-06).

How the comparison works
------------------------
Ollama's /api/tags reports a `digest` per model. Measured against the live
registry on 2026-08-25, that value is exactly:

    sha256(raw bytes of the registry manifest for that model:tag)

Verified on 8 locally-installed models including a third-party namespaced one
(vanilj/phi-4-unsloth:Q3_K_M) -- 8/8 matched. So an update check is one
unauthenticated GET plus a hash, with nothing changed on the host and no
credentials involved.

Not from /api/show: that response has no `digest` key at all. Reading it
there returned an empty string and silently defeated every staleness check
built on it (B-05).
"""
import hashlib
import logging

import requests

log = logging.getLogger("inspectarr")

REGISTRY = "https://registry.ollama.ai"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"

# Short, because this feeds an advisory badge. A slow registry must never be
# something the user waits on.
LOCAL_TIMEOUT = 10
REGISTRY_TIMEOUT = 15


def split_model(model: str) -> tuple[str, str, str]:
    """
    "qwen2.5-coder:7b"            -> ("library", "qwen2.5-coder", "7b")
    "vanilj/phi-4-unsloth:Q3_K_M" -> ("vanilj", "phi-4-unsloth", "Q3_K_M")
    "phi4"                        -> ("library", "phi4", "latest")
    """
    name, _, tag = (model or "").strip().partition(":")
    tag = tag or "latest"
    namespace = "library"
    if "/" in name:
        namespace, name = name.split("/", 1)
    return namespace, name, tag


def local_digest(url: str, model: str) -> str | None:
    """
    The digest of the model installed on this Ollama host, or None.

    Returns None on any failure rather than raising: every caller of this
    feeds a badge or a cache key, and neither is worth failing a scan over.
    """
    if not url or not model:
        return None
    try:
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=LOCAL_TIMEOUT)
        if r.status_code != 200:
            return None
        for m in r.json().get("models", []):
            if m.get("name") == model or m.get("model") == model:
                d = (m.get("digest") or "").replace("sha256:", "").strip()
                return d or None
    except Exception as exc:
        log.debug(f"local_digest({model}) failed: {exc}")
    return None


def registry_digest(model: str) -> str | None:
    """
    The digest upstream publishes for this model:tag, or None.

    Hashes the raw manifest bytes because the registry sends no
    Docker-Content-Digest header -- checked, it is absent on both GET and
    HEAD, so there is no cheaper form available.

    None means "could not tell", never "no update". A caller must not treat
    a network failure as a verdict.
    """
    if not model:
        return None
    namespace, name, tag = split_model(model)
    url = f"{REGISTRY}/v2/{namespace}/{name}/manifests/{tag}"
    try:
        r = requests.get(url, headers={"Accept": MANIFEST_ACCEPT},
                         timeout=REGISTRY_TIMEOUT)
        if r.status_code != 200:
            log.debug(f"registry_digest({model}): HTTP {r.status_code}")
            return None
        return hashlib.sha256(r.content).hexdigest()
    except Exception as exc:
        log.debug(f"registry_digest({model}) failed: {exc}")
        return None


def check_for_update(url: str, model: str) -> dict:
    """
    Compare the installed build against what upstream publishes.

    Returns a dict that always carries `update_available` as a real boolean,
    plus `known` saying whether the answer is trustworthy. "Could not reach
    the registry" and "you are up to date" are different states and must not
    collapse into the same False.
    """
    local = local_digest(url, model)
    remote = registry_digest(model)

    if not local:
        return {"model": model, "update_available": False, "known": False,
                "local": None, "remote": remote,
                "message": "Model is not installed on this Ollama host"}
    if not remote:
        return {"model": model, "update_available": False, "known": False,
                "local": local, "remote": None,
                "message": "Could not reach the registry for this model"}

    same = local == remote
    return {
        "model": model,
        "update_available": not same,
        "known": True,
        "local": local,
        "remote": remote,
        "message": ("Up to date" if same
                    else "A newer build is published upstream"),
    }
