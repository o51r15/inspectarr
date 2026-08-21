import os
import re
from dataclasses import dataclass
from .config import Rule


@dataclass
class Finding:
    """
    One signal that fired during rule evaluation.

    Replaces the previous flat list of filenames. That list could say a
    torrent was bad but not WHY -- all three conditions appended into one
    list, so the triggering condition was lost before anything could store
    it. Severity is assigned per signal, so the signal has to survive.

    signal:    stable identifier for the condition that fired
    detail:    the specific thing that matched (extension, pattern, threshold)
    file_path: basename of the offending file
    file_size: bytes, when the signal knows it
    """
    signal:    str
    detail:    str
    file_path: str
    file_size: int | None = None

    @property
    def display(self) -> str:
        """
        Human-readable label, byte-identical to the strings the old flat list
        produced. Log events and notifications render this, so their output
        does not change.
        """
        if self.signal == "min_file_size":
            return f"{self.file_path} [{self.detail}]"
        return self.file_path


# Signal identifiers -- stable strings, stored in inspection_reasons.signal.
SIGNAL_BAD_EXTENSION = "bad_extension"
SIGNAL_BAD_PATTERN   = "bad_filename_pattern"
SIGNAL_MIN_FILE_SIZE = "min_file_size"


def findings_to_filenames(findings: list[Finding]) -> list[str]:
    """
    Collapse findings to the flat, de-duplicated display list that log events
    and notifications consumed before findings existed.
    """
    seen: list[str] = []
    for f in findings:
        label = f.display
        if label not in seen:
            seen.append(label)
    return seen


def evaluate_rule(rule: Rule, files: list[dict]) -> tuple[bool, list[Finding]]:
    """
    Evaluate a rule against a qBittorrent file list.

    Each file dict contains at minimum:
        name: str   -- full relative path within the torrent
        size: int   -- file size in bytes

    All configured conditions are OR'd together: if any fires, the torrent
    is flagged. Every condition that fires contributes its own Finding, so a
    single file can legitimately appear under more than one signal.

    Returns (flagged: bool, findings: list[Finding])
    """
    if not files:
        return False, []

    cond = rule.conditions
    findings: list[Finding] = []

    if cond.bad_extensions:
        findings.extend(
            _check_extensions(files, cond.bad_extensions, cond.match_mode)
        )

    if cond.bad_filename_patterns:
        findings.extend(
            _check_patterns(files, cond.bad_filename_patterns, cond.match_mode)
        )

    # min_file_size_mb always checks the primary file, ignoring match_mode.
    if cond.min_file_size_mb is not None:
        findings.extend(_check_min_size(files, cond.min_file_size_mb))

    return bool(findings), findings


# ---------------------------------------------------------------------------
# Condition helpers -- each returns a list of Finding (empty when it does not fire)
# ---------------------------------------------------------------------------

def _check_extensions(
    files: list[dict], bad_exts: list[str], match_mode: str
) -> list[Finding]:
    if match_mode == "primary":
        candidates = [max(files, key=lambda f: f.get("size", 0))]
    else:
        candidates = files

    out: list[Finding] = []
    for f in candidates:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in bad_exts:
            out.append(Finding(
                signal=SIGNAL_BAD_EXTENSION,
                detail=ext,
                file_path=os.path.basename(f["name"]),
                file_size=f.get("size"),
            ))
    return out


def _check_patterns(
    files: list[dict], patterns: list[str], match_mode: str
) -> list[Finding]:
    """
    Evaluate bad_filename_patterns against file basenames.
    Invalid regex is skipped silently -- the config validator already
    rejects it at load time.
    match_mode=any:     every matching file contributes a Finding
    match_mode=primary: only the largest file is considered
    """
    compiled: list[tuple[str, re.Pattern]] = []
    for p in patterns:
        try:
            compiled.append((p, re.compile(p, re.IGNORECASE)))
        except re.error:
            pass
    if not compiled:
        return []

    if match_mode == "primary":
        candidates = [max(files, key=lambda f: f.get("size", 0))]
    else:
        candidates = files

    out: list[Finding] = []
    for f in candidates:
        base = os.path.basename(f["name"])
        for raw, rx in compiled:
            if rx.search(base):
                out.append(Finding(
                    signal=SIGNAL_BAD_PATTERN,
                    detail=raw,
                    file_path=base,
                    file_size=f.get("size"),
                ))
                break  # one Finding per file; first matching pattern wins
    return out


def _check_min_size(files: list[dict], min_size_mb: int) -> list[Finding]:
    """
    Flag if the primary (largest) file is smaller than min_size_mb.
    Always checks the primary file regardless of match_mode -- a suspiciously
    small main file suggests a fake or placeholder torrent.
    """
    largest = max(files, key=lambda f: f.get("size", 0))
    size    = largest.get("size", 0)
    size_mb = size / (1024 * 1024)
    if size_mb >= min_size_mb:
        return []
    return [Finding(
        signal=SIGNAL_MIN_FILE_SIZE,
        detail=f"too small: {size_mb:.1f} MB < {min_size_mb} MB",
        file_path=os.path.basename(largest["name"]),
        file_size=size,
    )]
