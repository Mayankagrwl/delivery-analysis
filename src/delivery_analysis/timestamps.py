from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser

UTC = timezone.utc


def parse_srm_timestamp(value: Any, *, tz: timezone = UTC) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty timestamp")
        dt = date_parser.parse(text, dayfirst=False)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    dt = date_parser.isoparse(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
