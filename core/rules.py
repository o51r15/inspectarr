import os
from .config import Rule


def evaluate_rule(rule: Rule, files: list[dict]) -> tuple[bool, list[str]]:
    """
    Evaluate a rule against a qBittorrent file list.

    Each file dict contains at minimum:
        name: str   — full relative path within the torrent
        size: int   — file size in bytes

    Returns (flagged: bool, bad_filenames: list[str])
    """
    if not files:
        return False, []

    exts = rule.conditions.bad_extensions   # already lower-cased at load time

    if rule.conditions.match_mode == "any":
        return _match_any(files, exts)
    if rule.conditions.match_mode == "primary":
        return _match_primary(files, exts)
    return False, []


def _match_any(files: list[dict], bad_exts: list[str]) -> tuple[bool, list[str]]:
    """Flag if ANY file has a bad extension."""
    bad: list[str] = []
    for f in files:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in bad_exts:
            bad.append(os.path.basename(f["name"]))
    return bool(bad), bad


def _match_primary(files: list[dict], bad_exts: list[str]) -> tuple[bool, list[str]]:
    """Flag only if the LARGEST file has a bad extension."""
    largest = max(files, key=lambda f: f.get("size", 0))
    ext  = os.path.splitext(largest["name"])[1].lower()
    name = os.path.basename(largest["name"])
    if ext in bad_exts:
        return True, [name]
    return False, []
