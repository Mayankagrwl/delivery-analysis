from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from .config import Settings
from .models import KeyMap

URN_PATTERN = re.compile(r"strn:distribution:DeliveryRequest:\d+")
_TRAILING_ID = re.compile(r"(\d+)\s*$")
DATEISH_PATTERN = re.compile(
    r"(?:\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?)?)"
    r"|(?:\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?)",
    re.IGNORECASE,
)
URN_CANDIDATES = ("urn", "_urn", "id")
TIMESTAMP_CANDIDATES = ("updated.on", "_updated.on", "updatedOn", "lastUpdated")
STATE_CANDIDATES = ("state",)


class SrmError(Exception):
    pass


def normalize_request_id(raw: str | None) -> str | None:
    """Extract the numeric DeliveryRequest id from a bare number or full URN.

    Accepts ``123456`` or ``strn:distribution:DeliveryRequest:123456`` (and any
    string ending in the numeric id). Returns the id as a string, or ``None`` if
    no trailing number is present.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = _TRAILING_ID.search(text)
    return match.group(1) if match else None


def urn_request_id(urn: str | None) -> str | None:
    """Return the trailing numeric id of a URN string, robust to prefixes."""
    if not urn:
        return None
    match = _TRAILING_ID.search(str(urn))
    return match.group(1) if match else None


def safe_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def fetch_delivery_requests(
    url: str,
    username: str,
    password: str,
    *,
    verify: bool | str = True,
    timeout: float = 30.0,
) -> Any:
    try:
        with httpx.Client(
            auth=(username, password),
            verify=verify,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise SrmError("SRM response is not valid JSON") from exc
    except httpx.HTTPStatusError as exc:
        raise SrmError(f"SRM HTTP {exc.response.status_code}") from exc
    except httpx.TimeoutException as exc:
        raise SrmError("SRM request timed out") from exc
    except httpx.RequestError as exc:
        host = urlparse(url).hostname or "unknown-host"
        raise SrmError(f"SRM connection failed ({host})") from exc


_WRAPPER_KEYS = ("items", "data", "resources", "results", "value")


def as_objects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in _WRAPPER_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return list(value)
        looks_like_record = any(
            field in payload for field in ("state", "urn", "_urn")
        )
        if not looks_like_record:
            for value in payload.values():
                if (
                    isinstance(value, list)
                    and value
                    and all(isinstance(item, dict) for item in value)
                ):
                    return list(value)
        return [payload]
    return []


def get_path(obj: Any, path: str | None) -> Any:
    if not path or not isinstance(obj, dict):
        return None
    if path in obj:
        return obj[path]
    current: Any = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def walk_paths(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.append((path, value))
            found.extend(walk_paths(value, path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            found.append((path, value))
            found.extend(walk_paths(value, path))
    return found


def key_inventory(objects: list[dict[str, Any]], limit: int = 5) -> list[str]:
    keys: set[str] = set()
    for obj in objects[:limit]:
        for path, value in walk_paths(obj):
            if "[" in path:
                continue
            if isinstance(value, (dict, list)):
                continue
            keys.add(path)
            parent = path.split(".")[0]
            keys.add(parent)
    return sorted(keys)


def _first_candidate_path(
    obj: dict[str, Any], candidates: tuple[str, ...]
) -> str | None:
    for candidate in candidates:
        value = get_path(obj, candidate)
        if value is not None and value != "":
            return candidate
    return None


def _find_state_path(obj: dict[str, Any], states: tuple[str, ...]) -> str | None:
    path = _first_candidate_path(obj, STATE_CANDIDATES)
    if path:
        return path
    for walked, value in walk_paths(obj):
        if walked.split(".")[-1] == "state":
            return walked
        if isinstance(value, str) and value.strip() in states:
            return walked
    return None


def _looks_like_urn(value: Any) -> bool:
    if isinstance(value, str) and URN_PATTERN.search(value):
        return True
    return False


def _find_urn_path(obj: dict[str, Any]) -> str | None:
    for candidate in URN_CANDIDATES:
        value = get_path(obj, candidate)
        if value is None or value == "":
            continue
        if candidate == "id" and not _looks_like_urn(value):
            continue
        return candidate
    for walked, value in walk_paths(obj):
        if _looks_like_urn(value):
            return walked
    for candidate in URN_CANDIDATES:
        value = get_path(obj, candidate)
        if value is not None and value != "":
            return candidate
    return None


def _looks_like_timestamp(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or URN_PATTERN.search(text):
        return False
    return bool(DATEISH_PATTERN.search(text))


def _find_timestamp_path(obj: dict[str, Any]) -> str | None:
    for candidate in TIMESTAMP_CANDIDATES:
        value = get_path(obj, candidate)
        if value is not None and value != "":
            return candidate
    for walked, value in walk_paths(obj):
        if _looks_like_timestamp(value):
            return walked
    return None


def discover_key_map(
    objects: list[dict[str, Any]],
    *,
    states: tuple[str, ...] = ("SUBMITTED", "GRANTED"),
) -> KeyMap:
    state_path = None
    urn_path = None
    ts_path = None
    for obj in objects:
        if state_path is None:
            state_path = _find_state_path(obj, states)
        if urn_path is None:
            urn_path = _find_urn_path(obj)
        if ts_path is None:
            ts_path = _find_timestamp_path(obj)
        if state_path and urn_path and ts_path:
            break
    return KeyMap(state=state_path, urn=urn_path, updated_on=ts_path)


def require_credentials(settings: Settings) -> tuple[str, str]:
    user = settings.srm_basic_user
    password = settings.srm_basic_password
    if not user or not password:
        raise SrmError("missing SRM_BASIC_USER or SRM_BASIC_PASSWORD")
    return user, password
