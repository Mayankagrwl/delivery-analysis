from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx


class McpError(Exception):
    pass


class McpClient(Protocol):
    def list_tools(self) -> list[str]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    def close(self) -> None: ...


def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def unwrap_rpc(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if payload.get("error"):
        err = payload["error"]
        if isinstance(err, dict):
            message = str(err.get("message") or err)
        else:
            message = str(err)
        raise McpError(message)
    if "result" in payload:
        return payload["result"]
    return payload


def unwrap_tool_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise McpError(_tool_error_text(result))
    content = result.get("content")
    if isinstance(content, list) and content:
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {None, "text"}:
                texts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                texts.append(item)
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
        if texts:
            return texts
    return result


def _tool_error_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("text") or "tool error")
        return str(first)
    return "tool error"


class SseMcpClient:
    """Minimal MCP JSON-RPC client over SSE (PRD §6.1)."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._verify = verify
        self._rpc_id = 0
        self._endpoint: str | None = None
        self._session_id: str | None = None
        self._messages: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._reader_error: Exception | None = None
        self._thread: threading.Thread | None = None
        headers = {"Accept": "text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._headers = headers
        self._stream = httpx.Client(
            verify=verify, timeout=timeout, follow_redirects=True, headers=headers
        )
        post_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            post_headers["Authorization"] = f"Bearer {token}"
        self._rpc = httpx.Client(
            verify=verify, timeout=timeout, follow_redirects=True, headers=post_headers
        )

    def connect(self) -> None:
        self._thread = threading.Thread(target=self._read_sse, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._reader_error:
                raise McpError("MCP SSE connection failed") from self._reader_error
            if self._endpoint:
                break
            time.sleep(0.05)
        if not self._endpoint:
            raise McpError(
                f"MCP SSE endpoint event not received ({urlparse(self._url).hostname})"
            )
        self._initialize()

    def _read_sse(self) -> None:
        try:
            with self._stream.stream("GET", self._url) as response:
                response.raise_for_status()
                for event, data in iter_sse_events(response.iter_lines()):
                    if self._stop.is_set():
                        return
                    if event == "endpoint":
                        self._endpoint = urljoin(self._url, data.strip())
                        continue
                    try:
                        payload = json.loads(data) if data else None
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    with self._condition:
                        self._messages.append(payload)
                        self._condition.notify_all()
        except Exception as exc:
            self._reader_error = exc
            with self._condition:
                self._condition.notify_all()

    def _initialize(self) -> None:
        result = self._rpc_call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "delivery-analysis", "version": "s2"},
            },
        )
        if isinstance(result, dict):
            session = result.get("sessionId") or result.get("session_id")
            if isinstance(session, str):
                self._session_id = session
        self._rpc_call("notifications/initialized", None, notification=True)

    def list_tools(self) -> list[str]:
        result = unwrap_tool_result(self._rpc_call("tools/list", {}))
        names: list[str] = []
        tools = result
        if isinstance(result, dict):
            tools = result.get("tools") or []
        if isinstance(tools, list):
            for item in tools:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
        return names

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raw = self._rpc_call(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return unwrap_tool_result(raw)

    def _rpc_call(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        notification: bool = False,
    ) -> Any:
        if not self._endpoint:
            raise McpError("MCP SSE client is not connected")
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        rpc_id = None
        if not notification:
            self._rpc_id += 1
            rpc_id = self._rpc_id
            body["id"] = rpc_id
        if params is not None:
            body["params"] = params
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = self._rpc.post(self._endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            host = urlparse(self._url).hostname or "mcp"
            raise McpError(f"MCP POST failed ({host})") from exc
        if notification:
            return None
        if response.status_code == 200 and response.content:
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("id") == rpc_id:
                return unwrap_rpc(payload)
        return unwrap_rpc(self._wait_for_id(rpc_id))

    def _wait_for_id(self, rpc_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout
        with self._condition:
            while time.monotonic() < deadline:
                if self._reader_error:
                    raise McpError("MCP SSE stream failed") from self._reader_error
                for index, payload in enumerate(self._messages):
                    if payload.get("id") == rpc_id:
                        return self._messages.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
        raise McpError(f"MCP RPC {rpc_id} timed out")

    def close(self) -> None:
        self._stop.set()
        self._stream.close()
        self._rpc.close()

    def __enter__(self) -> SseMcpClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def mcp_host(url: str) -> str:
    return urlparse(url).hostname or ""
