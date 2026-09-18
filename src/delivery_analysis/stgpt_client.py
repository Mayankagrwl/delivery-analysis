"""ST ChatGPT client-apps bridge. Never log api_key or auth token.

Vendored contract from ci-rca-collector tools/rca/stgpt_client.py (PRD §5 / §5.1.1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx

from .config import STGPT_SERVICE, STGPT_VERSION, resolve_stgpt_client_app_name, tls_verify

_LOG = logging.getLogger(__name__)
_DEFAULT_TIMEOUT = 60.0
_REDACT = "<redacted>"


class StgptError(Exception):
    """Transport-level failure talking to the ST ChatGPT bridge."""


class ChatResult(NamedTuple):
    status_code: int
    body: dict[str, Any]
    completion: str | None
    response_id: str | None
    url: str | None = None
    duration_ms: int | None = None
    user_message_chars: int | None = None
    client_app_name: str | None = None


def generate_auth_token(client: str, service: str, key: str, ts: str | int, nonce: str) -> str:
    """Return SHA1 hex of ``f"{client}_{service}_{key}_{ts}_{nonce}"``."""
    raw = f"{client}_{service}_{key}_{ts}_{nonce}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def flatten_user_content(messages: Sequence[Mapping[str, str]]) -> str:
    """Collapse chat messages into a single non-empty user prompt."""
    parts: list[str] = []
    for item in messages:
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        role = str(item.get("role") or "user")
        if role == "assistant":
            parts.append("Previous assistant reply:\n" + content)
        else:
            parts.append(content)
    return "\n\n".join(parts).strip()


def post_chat(
    url: str,
    api_key: str,
    client_app_name: str,
    persona: str,
    messages: Sequence[Mapping[str, str]],
    *,
    service: str = STGPT_SERVICE,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
    verify: bool | str | None = None,
    extra: Mapping[str, Any] | None = None,
    version: str = STGPT_VERSION,
    response_format: str = "json_object",
    _format_retry: bool = False,
) -> ChatResult:
    """POST a chat turn. HTTP error statuses are returned, not raised."""
    ts = timestamp if timestamp is not None else str(int(time.time()))
    nonce_value = nonce if nonce is not None else uuid.uuid4().hex
    client_app_name = (client_app_name or "").strip() or resolve_stgpt_client_app_name()
    api_key = (api_key or "").strip()
    token = generate_auth_token(client_app_name, service, api_key, ts, nonce_value)
    endpoint = url.strip().rstrip("/")
    user_content = flatten_user_content(messages)
    if not user_content:
        raise StgptError("prompt_empty")
    _LOG.info("ST ChatGPT user_message_chars=%s", len(user_content))
    _LOG.info(
        "stgpt clientAppName_repr=%r clientAppName_len=%s url=%s persona=%s",
        client_app_name,
        len(client_app_name),
        public_request_url(endpoint),
        persona,
    )
    fmt = _coerce_response_format(response_format)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "stchatgpt-auth-token": token,
        "stchatgpt-auth-nonce": nonce_value,
        "stchatgpt-auth-timestamp": str(ts),
    }
    payload: dict[str, Any] = {}
    if extra:
        payload.update(dict(extra))
    payload["version"] = version
    payload["clientAppName"] = client_app_name
    payload["service"] = "chat"
    payload["timestamp"] = str(ts)
    payload["persona"] = persona
    payload["messages"] = [{"role": "user", "content": user_content}]
    payload["responseFormat"] = fmt

    ssl_verify = tls_verify() if verify is None else verify
    if ssl_verify is False:
        _LOG.warning(
            "TLS verification disabled (RCA_SSL_VERIFY=false); ST ChatGPT %s",
            public_request_url(endpoint),
        )
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "verify": ssl_verify,
        "follow_redirects": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    started = time.perf_counter()
    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.post(endpoint, headers=headers, json=payload)
    except (httpx.HTTPError, OSError) as exc:
        raise StgptError(_public_error(exc, endpoint)) from None
    duration_ms = int(round((time.perf_counter() - started) * 1000))

    body = _json_object(response)
    if fmt == "json_object" and not _format_retry and response_format_rejected(body):
        return post_chat(
            url,
            api_key,
            client_app_name,
            persona,
            messages,
            service=service,
            timeout=timeout,
            transport=transport,
            verify=verify,
            extra=extra,
            version=version,
            response_format="text",
            _format_retry=True,
        )
    completion = extract_completion(body)
    response_id = _response_id(body)
    return ChatResult(
        response.status_code,
        body,
        completion,
        response_id,
        endpoint,
        duration_ms,
        len(user_content),
        client_app_name,
    )


def _coerce_response_format(value: str | None) -> str:
    """Only json_object or text. Never json / JSON / json-schema / empty."""
    if value == "text":
        return "text"
    return "json_object"


def response_format_rejected(body: Mapping[str, Any] | None) -> bool:
    message = bridge_error_message(body)
    return bool(message and message.startswith("responseFormat must"))


def bridge_error_message(body: Mapping[str, Any] | None) -> str | None:
    """API error payload (errorCode or responseFormat rejection). Not a completion."""
    if not body:
        return None
    message = body.get("message")
    text = message.strip() if isinstance(message, str) else ""
    if body.get("errorCode"):
        return text or str(body.get("errorCode"))
    if text.startswith("responseFormat must") or text.startswith("Invalid application name"):
        return text
    return None


def extract_completion(body: Mapping[str, Any] | None) -> str | None:
    """First non-empty of the documented ST ChatGPT / OpenAI answer paths."""
    if not body:
        return None
    if bridge_error_message(body):
        return None
    for value in (
        body.get("completion"),
        body.get("message"),
        body.get("text"),
        body.get("content"),
    ):
        got = _scalar_text(value)
        if got:
            return got
    data = body.get("data")
    if isinstance(data, Mapping):
        for key in ("completion", "message", "text"):
            got = _scalar_text(data.get(key))
            if got:
                return got
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        first = choices[0]
        message = first.get("message")
        if isinstance(message, Mapping):
            got = _scalar_text(message.get("content"))
            if got:
                return got
        got = _scalar_text(first.get("text"))
        if got:
            return got
    if any(
        key in body
        for key in ("root_cause", "rootCause", "suggested_fix", "suggestedFix")
    ):
        try:
            return json.dumps(dict(body), ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, Mapping):
        for key in ("content", "completion", "text", "message"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner
    return None


def public_request_url(url: str | None) -> str:
    """Host + path only. No query, fragment, or credentials."""
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1] if parsed.netloc else ""
    path = parsed.path or ""
    if parsed.scheme and host:
        return f"{parsed.scheme}://{host}{path}"
    return f"{host}{path}" or url.split("?", 1)[0].split("#", 1)[0]


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return {"raw": text[:500]} if text else {}
    return payload if isinstance(payload, dict) else {}


def _response_id(body: Mapping[str, Any]) -> str | None:
    for key in ("responseId", "response_id", "id"):
        value = body.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return None


def _public_error(exc: BaseException, endpoint: str) -> str:
    host = urlparse(endpoint).netloc or endpoint
    text = str(exc)
    for secret in _secrets_from(exc):
        if secret:
            text = text.replace(secret, _REDACT)
    return f"ST ChatGPT bridge error talking to {host}: {text}"


def _secrets_from(exc: BaseException) -> list[str]:
    found: list[str] = []
    request = getattr(exc, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        token = headers.get("stchatgpt-auth-token")
        if token:
            found.append(token)
    return found
