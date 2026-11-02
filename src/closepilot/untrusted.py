"""Helpers for presenting untrusted input (CSV cells, commentary, labels) safely.

Values from a close package are data, never instructions. Wherever such a value is
echoed into a message that a person or an agent will read, it is quoted and truncated
so it cannot pass for system text or flood the output.
"""

from __future__ import annotations

MAX_ECHO_CHARS = 40


def quote(value: str) -> str:
    """Return ``value`` quoted with ``repr`` and truncated to ``MAX_ECHO_CHARS``."""
    if len(value) > MAX_ECHO_CHARS:
        value = value[:MAX_ECHO_CHARS] + "..."
    return repr(value)
