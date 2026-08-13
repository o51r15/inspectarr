"""Shared helpers for route modules."""

import re


def safe_error(exc: Exception) -> str:
    """
    M-01: Return a sanitized error message safe for client display.
    Strips filesystem paths, stack traces, and caps length.
    """
    msg = str(exc)
    msg = re.sub(r'(/[^\s:]+)', '[path]', msg)       # strip Unix paths
    msg = re.sub(r'([A-Z]:\\[^\s:]+)', '[path]', msg) # strip Windows paths
    msg = msg.split('\n')[0]                           # first line only
    return msg[:200]
