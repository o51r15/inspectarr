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


# ---------------------------------------------------------------------
# Operating modes (ROADMAP item 26)
# ---------------------------------------------------------------------
#
# A mode is a CEILING on how far the system may act. It is deliberately NOT
# a preset that rewrites min_severity/remediate_at, and NOT an override that
# replaces them.
#
# The thresholds answer "which band does this finding belong in".
# The mode answers "how far am I allowed to act at all".
# Those are different questions, and a config that answers them with one
# control ends up lying about at least one of them: pick "monitor" from a
# preset and your remediate_at silently becomes CRITICAL on disk, so the
# stored config no longer describes what you configured.
#
# Keeping them separate means both stay true at once, and the UI can say
# something honest and specific: "thresholds say DELETE, mode caps at
# record -- recorded only".
#
# There are three modes, not the four originally sketched. "Monitor" and
# "Dry Run" turned out to describe the same behaviour under two names --
# compute the decision, record it, act on nothing -- so they are one mode.
# The CLI --dry-run flag remains the way to get it for a single run.

MODE_MONITOR    = "monitor"      # compute and record; never pause, never delete
MODE_QUARANTINE = "quarantine"   # never delete; remediate is held instead
MODE_AUTOMATIC  = "automatic"    # thresholds honoured exactly (current behaviour)

MODES = [MODE_MONITOR, MODE_QUARANTINE, MODE_AUTOMATIC]
DEFAULT_MODE = MODE_AUTOMATIC

# How far each mode permits the system to go.
_MODE_CEILING = {
    MODE_MONITOR:    RECORD,
    MODE_QUARANTINE: QUARANTINE,
    MODE_AUTOMATIC:  REMEDIATE,
}

# Decisions ordered by how much they actually do. NOTIFY is deliberately
# absent: it is defined in this module but never produced, and giving an
# unreachable value a rank here would let it be silently escalated.
_DECISION_ORDER = [RECORD, QUARANTINE, REMEDIATE]
_DECISION_RANK = {d: i for i, d in enumerate(_DECISION_ORDER)}


def is_valid_mode(mode: str) -> bool:
    return (mode or "").lower().strip() in _MODE_CEILING


def cap_for_mode(decision: str, mode: str = DEFAULT_MODE) -> str:
    """
    Reduce a decision to what the operating mode permits.

    This only ever moves a decision DOWN. It cannot turn a RECORD into a
    delete, whatever the mode -- a ceiling that could raise the outcome
    would not be a safety control.

        decision    monitor     quarantine   automatic
        RECORD      RECORD      RECORD       RECORD
        QUARANTINE  RECORD      QUARANTINE   QUARANTINE
        REMEDIATE   RECORD      QUARANTINE   REMEDIATE

    Unrecognised input fails toward doing less, not more: an unknown
    decision becomes RECORD, and an unknown mode falls back to the default
    rather than guessing. config.py rejects an invalid mode at load time,
    so the mode fallback here is defence in depth rather than the real
    guard -- a mistyped safety setting should be reported, not absorbed.
    """
    mode = (mode or "").lower().strip() or DEFAULT_MODE
    if mode not in _MODE_CEILING:
        log.warning(
            f"Unknown operating_mode {mode!r}; using {DEFAULT_MODE}. "
            f"Expected one of {', '.join(MODES)}")
        mode = DEFAULT_MODE

    if decision not in _DECISION_RANK:
        log.warning(
            f"Unrecognised decision {decision!r}; recording only. "
            f"Expected one of {', '.join(_DECISION_ORDER)}")
        return RECORD

    ceiling = _MODE_CEILING[mode]
    if _DECISION_RANK[decision] <= _DECISION_RANK[ceiling]:
        return decision
    return ceiling


def describe_mode(mode: str) -> str:
    """One-line summary for the UI banner and logs."""
    mode = (mode or "").lower().strip() or DEFAULT_MODE
    return {
        MODE_MONITOR:    "Monitor - findings are recorded, nothing is paused or deleted",
        MODE_QUARANTINE: "Quarantine - matches are held for review, nothing is deleted automatically",
        MODE_AUTOMATIC:  "Automatic - the remediation thresholds are applied as configured",
    }.get(mode, f"Unknown mode {mode!r} - treated as {DEFAULT_MODE}")


def explain(risk_level: str, counts: dict) -> str:
    """One-line summary for logs and the UI."""
    if not risk_level:
        return "no findings"
    parts = [f"{counts.get(l, 0)} {l.lower()}" for l in reversed(ORDER)
             if counts.get(l)]
    return f"{risk_level} ({', '.join(parts)})"
