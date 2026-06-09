import os
import re
from .config import Rule


def evaluate_rule(rule: Rule, files: list[dict]) -> tuple[bool, list[str]]:
    """
    Evaluate a rule against a qBittorrent file list.

    Each file dict contains at minimum:
        name: str   — full relative path within the torrent
        size: int   — file size in bytes

    All configured conditions are OR'd together: if any fires, the torrent
    is flagged. bad_files accumulates hits from all conditions.

    Returns (flagged: bool, bad_filenames: list[str])
    """
    if not files:
        return False, []

    cond = rule.conditions
    bad: list[str] = []

    # --- bad_extensions ---
    if cond.bad_extensions:
        _, ext_bad = _check_extensions(files, cond.bad_extensions, cond.match_mode)
        bad.extend(ext_bad)

    # --- bad_filename_patterns ---
    if cond.bad_filename_patterns:
        _, pat_bad = _check_patterns(files, cond.bad_filename_patterns, cond.match_mode)
        for f in pat_bad:
            if f not in bad:
                bad.append(f)

    # --- min_file_size_mb (always checks primary file, ignores match_mode) ---
    if cond.min_file_size_mb is not None:
        _, size_bad = _check_min_size(files, cond.min_file_size_mb)
        bad.extend(size_bad)

    return bool(bad), bad


# ---------------------------------------------------------------------------
# Condition helpers
# ---------------------------------------------------------------------------

def _check_extensions(
    files: list[dict], bad_exts: list[str], match_mode: str
) -> tuple[bool, list[str]]:
    if match_mode == "primary":
        return _match_primary_ext(files, bad_exts)
    return _match_any_ext(files, bad_exts)


def _match_any_ext(files: list[dict], bad_exts: list[str]) -> tuple[bool, list[str]]:
    """Flag if ANY file has a bad extension."""
    bad: list[str] = []
    for f in files:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in bad_exts:
            bad.append(os.path.basename(f["name"]))
    return bool(bad), bad


def _match_primary_ext(files: list[dict], bad_exts: list[str]) -> tuple[bool, list[str]]:
    """Flag only if the LARGEST file has a bad extension."""
    largest = max(files, key=lambda f: f.get("size", 0))
    ext  = os.path.splitext(largest["name"])[1].lower()
    name = os.path.basename(largest["name"])
    if ext in bad_exts:
        return True, [name]
    return False, []


def _check_patterns(
    files: list[dict], patterns: list[str], match_mode: str
) -> tuple[bool, list[str]]:
    """
    Evaluate bad_filename_patterns against file basenames.
    Invalid regex patterns are skipped silently (validated at config load time).
    match_mode=any:     flag if any file's basename matches any pattern
    match_mode=primary: flag only if the largest file's basename matches
    """
    compiled: list[re.Pattern] = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            pass  # bad pattern — already caught by config validator
    if not compiled:
        return False, []

    if match_mode == "primary":
        largest = max(files, key=lambda f: f.get("size", 0))
        name = os.path.basename(largest["name"])
        if any(rx.search(name) for rx in compiled):
            return True, [name]
        return False, []

    # match_mode == "any"
    bad: list[str] = []
    for f in files:
        name = os.path.basename(f["name"])
        if any(rx.search(name) for rx in compiled):
            if name not in bad:
                bad.append(name)
    return bool(bad), bad


def _check_min_size(
    files: list[dict], min_size_mb: int
) -> tuple[bool, list[str]]:
    """
    Flag if the primary (largest) file is smaller than min_size_mb.
    Always checks the primary file regardless of match_mode — a suspiciously
    small main file suggests a fake/placeholder torrent.
    """
    largest = max(files, key=lambda f: f.get("size", 0))
    size_mb = largest.get("size", 0) / (1024 * 1024)
    if size_mb < min_size_mb:
        name  = os.path.basename(largest["name"])
        label = f"{name} [too small: {size_mb:.1f} MB < {min_size_mb} MB]"
        return True, [label]
    return False, []
