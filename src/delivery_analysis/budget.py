"""Token estimation and middle-trim (same behaviour as collector prompt.py)."""

from __future__ import annotations

_MIDDLE = "\n… middle elided …\n"


def token_count(text: str) -> int:
    return len(text) // 4


def trim_middle(text: str, cap_tokens: int) -> tuple[str, bool, int, int]:
    """Keep the ends of a block. Returns (text, trimmed, before, after)."""
    before = token_count(text)
    if cap_tokens <= 0 or before <= cap_tokens:
        return text, False, before, before
    keep_chars = cap_tokens * 4
    half = max(1, keep_chars // 2)
    if len(text) <= keep_chars:
        return text, False, before, before
    trimmed = text[:half] + _MIDDLE + text[-half:]
    return trimmed, True, before, token_count(trimmed)
