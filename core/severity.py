"""
core/severity.py -- classify what a rule match actually means.

The rule engine answers "did something match". It cannot answer "how bad is
it", and those are different questions. A single bad_extensions list routinely
mixes both: an .exe inside a TV download is unambiguously malicious, while a
.rar is how a large share of legitimate releases ship. Treating them
identically is what makes automatic deletion feel unsafe.

Severity is assigned per SIGNAL OCCURRENCE, not per rule, because one rule
match can produce several findings of different weight -- and one file can
carry more than one signal at once (keygen.exe fires both bad_extension and
bad_filename_pattern).

Nothing here decides anything on its own. It produces a level; the caller
decides what a level is worth.
"""
import logging

log = logging.getLogger("inspectarr")

CRITICAL = "CRITICAL"
HIGH     = "HIGH"
MEDIUM   = "MEDIUM"
LOW      = "LOW"

# Ordered weakest -> strongest. Aggregation takes the max, never an average:
# averaging lets a pile of low-severity noise dilute one genuinely dangerous
# finding, which is the exact failure the report warned about.
ORDER = [LOW, MEDIUM, HIGH, CRITICAL]
_RANK = {level: i for i, level in enumerate(ORDER)}


def rank(level: str) -> int:
    """Numeric rank for comparison. Unknown levels sort as LOW."""
    return _RANK.get((level or "").upper(), 0)


def is_valid(level: str) -> bool:
    return (level or "").upper() in _RANK


# Executables and scripts. Anything here can run code on the machine that
# opens it, so its presence in a media download is never incidental.
EXECUTABLE_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".hta", ".cpl", ".jar",
    ".apk", ".dmg", ".app", ".pkg", ".deb", ".rpm", ".run", ".sh", ".bin",
    ".lnk", ".url", ".reg", ".dll", ".sys", ".msc", ".gadget",
}

# Archives. Genuinely suspicious in some contexts and completely routine in
# others -- a large share of legitimate scene releases ship as .rar or .zip.
# HIGH rather than CRITICAL: worth surfacing, not worth deleting on sight.
ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".cab",
    ".arj", ".lzh", ".ace", ".img",
}

# Default severity per signal, used when the extension is not classified.
SIGNAL_DEFAULTS = {
    "bad_extension":        MEDIUM,   # unclassified extension: flag, don't panic
    "min_file_size":        HIGH,     # undersized primary file = likely fake
    "bad_filename_pattern": MEDIUM,   # heuristic; higher false-positive rate
}

DEFAULT_SEVERITY = MEDIUM


def severity_for_finding(finding, overrides: dict = None) -> str:
    """
    Severity for a single Finding.

    Precedence, most specific first:
      1. explicit override for this exact extension  (".rar": "LOW")
      2. explicit override for this signal            ("min_file_size": "LOW")
      3. built-in extension classification
      4. built-in signal default

    Overrides are user config, so an unrecognised level is ignored with a
    warning rather than raising -- a typo in config must not stop a scan.
    """
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    signal = getattr(finding, "signal", "") or ""

    ext = ""
    if signal == "bad_extension":
        # Finding.detail carries the matched extension for this signal.
        ext = (getattr(finding, "detail", "") or "").lower().strip()
        if ext and not ext.startswith("."):
            ext = "." + ext

    for key in (ext, signal):
        if not key:
            continue
        val = overrides.get(key)
        if val is None:
            continue
        if is_valid(val):
            return val.upper()
        log.warning(
            f"Ignoring invalid severity {val!r} for {key!r} "
            f"(expected one of {', '.join(ORDER)})")

    if ext:
        if ext in EXECUTABLE_EXTENSIONS:
            return CRITICAL
        if ext in ARCHIVE_EXTENSIONS:
            return HIGH

    return SIGNAL_DEFAULTS.get(signal, DEFAULT_SEVERITY)


def assess(findings, overrides: dict = None) -> dict:
    """
    Score a set of findings.

    Returns:
      severities  -- one level per finding, positionally aligned with input
      risk_level  -- the aggregate (max), or None when there are no findings
      risk_score  -- 0-100, for ordering and display only
      counts      -- how many findings landed at each level

    The aggregate is the MAX, never a mean. One executable among twenty
    routine findings is still an executable; averaging would bury it. This is
    the "certain signals should be hard overrides" requirement -- expressed
    as the aggregation rule rather than as a special case bolted on later.
    """
    findings = list(findings or [])
    severities = [severity_for_finding(f, overrides) for f in findings]

    counts = {level: 0 for level in ORDER}
    for level in severities:
        counts[level] = counts.get(level, 0) + 1

    if not severities:
        return {"severities": [], "risk_level": None, "risk_score": 0,
                "counts": counts}

    top = max(severities, key=rank)
    return {
        "severities": severities,
        "risk_level": top,
        "risk_score": _score(top, counts),
        "counts": counts,
    }


# Base score per level, spaced so no amount of corroboration can push one
# band into the next: a LOW finding must never present as a HIGH one.
_BASE = {LOW: 10, MEDIUM: 35, HIGH: 60, CRITICAL: 90}


def _score(top: str, counts: dict) -> int:
    """
    0-100 score for display and ordering. Deliberately NOT the authority for
    any decision -- risk_level is. Corroborating findings at the top level
    nudge the score up a little, capped inside the band.
    """
    base = _BASE.get(top, 35)
    corroboration = max(0, counts.get(top, 1) - 1)
    return int(min(100, base + min(corroboration * 2, 9)))


# Decisions. Only `remediate` and `record` are reachable today; `quarantine`
# is defined here so item 25 has a name to fill in rather than reshaping this.
REMEDIATE  = "remediate"
NOTIFY     = "notify"
QUARANTINE = "quarantine"
RECORD     = "record"


def decide(risk_level: str, min_severity: str = LOW,
           remediate_at: str = LOW) -> str:
    """
    Map an aggregate risk level to what should happen.

    Two thresholds, three bands:

        risk < min_severity                     -> RECORD
        min_severity <= risk < remediate_at     -> QUARANTINE
        risk >= remediate_at                    -> REMEDIATE

    Both default to LOW, which collapses the quarantine band to nothing and
    reproduces the behaviour from before either existed: everything flagged
    is remediated. Raising remediate_at to HIGH holds low- and medium-severity
    catches for review while still deleting anything that finds an executable.

    Quarantine is deliberately the MIDDLE band rather than a separate mode.
    The dangerous failure is not "held something that was fine" -- it is
    "deleted something that was fine", and a band between record and delete
    is what removes that cliff.
    """
    if not risk_level:
        return RECORD
    if not is_valid(min_severity):
        log.warning(f"Invalid min_severity {min_severity!r}; using {LOW}")
        min_severity = LOW
    if not is_valid(remediate_at):
        log.warning(f"Invalid remediate_at {remediate_at!r}; using {LOW}")
        remediate_at = LOW

    # A remediate floor below the record floor is contradictory. Trust the
    # more conservative of the two rather than guessing which was intended.
    if rank(remediate_at) < rank(min_severity):
        remediate_at = min_severity

    r = rank(risk_level)
    if r < rank(min_severity):
        return RECORD
    if r < rank(remediate_at):
        return QUARANTINE
    return REMEDIATE


def explain(risk_level: str, counts: dict) -> str:
    """One-line summary for logs and the UI."""
    if not risk_level:
        return "no findings"
    parts = [f"{counts.get(l, 0)} {l.lower()}" for l in reversed(ORDER)
             if counts.get(l)]
    return f"{risk_level} ({', '.join(parts)})"
