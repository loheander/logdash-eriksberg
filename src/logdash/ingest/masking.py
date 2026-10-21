"""Mask variable tokens (IPs, UUIDs, hex, numbers, paths) in log payloads.

The same rules exist as a Python function and as a DuckDB SQL expression so that
masking can run inside the database for speed. Tests assert that they agree.
"""

from __future__ import annotations

import re

# Order matters: more specific tokens are replaced first. Patterns stay within the
# subset shared by Python ``re`` (ASCII mode) and DuckDB's RE2.
MASK_RULES: tuple[tuple[str, str], ...] = (
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<UUID>"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?", "<IP>"),
    (r"\b(?:0x[0-9a-fA-F]+|[0-9a-fA-F]{12,})\b", "<HEX>"),
    (r"(?:/[\w.\-]+){2,}", "<PATH>"),
    (r"\b\d+(?:\.\d+)?\b", "<NUM>"),
)

_COMPILED = tuple((re.compile(p, re.ASCII), repl) for p, repl in MASK_RULES)


def mask(text: str) -> str:
    """Replace variable tokens in ``text`` with placeholders."""
    for pattern, repl in _COMPILED:
        text = pattern.sub(repl, text)
    return text


def mask_sql(expr: str) -> str:
    """Return a DuckDB expression equivalent to ``mask`` applied to ``expr``."""
    for pattern, repl in MASK_RULES:
        expr = f"regexp_replace({expr}, '{pattern}', '{repl}', 'g')"
    return expr
