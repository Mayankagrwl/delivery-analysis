"""Scrub secrets from text. Never persist credentials into reports."""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT = "***REDACTED***"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:ghp|gho|ghs)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-(?:live|proj|svcacct)-[A-Za-z0-9_-]+"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r'(?i)\b(password|token|secret|api[_-]?key)\s*[:=]\s*([^\s"\']+)',
    ),
    re.compile(
        r"(?i)(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)[A-Z0-9_]*\s*=\s*\S+"
    ),
]


def redact_text(text: str) -> tuple[str, int]:
    """Return (redacted_text, number of substitutions)."""
    total = 0
    out = text
    for compiled in _PATTERNS:
        out, n = compiled.subn(REPLACEMENT, out)
        total += n
    return out, total


def redact_walk(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        items = []
        total = 0
        for item in value:
            redacted, n = redact_walk(item)
            items.append(redacted)
            total += n
        return items, total
    if isinstance(value, dict):
        mapped: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            redacted, n = redact_walk(item)
            mapped[key] = redacted
            total += n
        return mapped, total
    return value, 0
