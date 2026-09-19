from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from .config import (
    DEFAULT_DASHBOARD_TITLE,
    DEFAULT_DASHBOARD_UID,
    DEFAULT_LINE_LIMIT,
    DEFAULT_MAX_QUERIES,
    LOGQL_TEMPLATES_PER_PACK,
)
from .mcp_sse import McpClient, McpError, mcp_host
from .models import SrmResult
from .timestamps import UTC

DEFAULT_DASHBOARD_FOLDER = "Distribution"
DEFAULT_LINE_CHARS = 500
WRITE_TOOL_PREFIXES = ("update_", "create_", "delete_")
WRITE_TOOLS = {
    "alerting_manage_rules",
    "create_incident",
    "update_dashboard",
    "create_folder",
    "create_datasource",
    "update_datasource",
    "update_incident",
}
LOGQL_TOOLS = {"query_loki_logs", "query_loki_stats", "query_loki_patterns"}
TOKEN_RE = re.compile(r"(?i)(bearer\s+)\S+")
URN_ID_RE = re.compile(r"strn:distribution:DeliveryRequest:(\d+)")
STALL_PAD = timedelta(hours=2)
MAX_COMBINED_STALL = timedelta(days=7)


class ToolCallLog(BaseModel):
    name: str
    status: str
    duration_ms: float = 0
    row_count: int | None = None
    error: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class GrafanaResult(BaseModel):
    skipped: bool = False
    skip_reason: str | None = None
    mcp_url_host: str | None = None
    transport: str = "sse"
    latency_ms: float | None = None
    dashboard_uid: str = DEFAULT_DASHBOARD_UID
    dashboard_title: str = DEFAULT_DASHBOARD_TITLE
    dashboard_folder: str = DEFAULT_DASHBOARD_FOLDER
    dashboard_url: str | None = None
    loki_datasource_uid: str | None = None
    loki_datasource_name: str | None = None
    logql: list[str] = Field(default_factory=list)
    time_range: dict[str, str] | None = None
    time_ranges: list[dict[str, str]] = Field(default_factory=list)
    lines_kept: int = 0
    lines_discarded: int = 0
    redactions: int = 0
    deeplinks: list[str] = Field(default_factory=list)
    tools: list[ToolCallLog] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    urn_is_label: bool | None = None
    filters: dict[str, str] = Field(default_factory=dict)


class TimePack(NamedTuple):
    start: str
    end: str
    label: str


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_time_packs(result: SrmResult) -> list[TimePack]:
    """now-24h plus stall window around stale updated_on (UTC)."""
    as_of = result.as_of.astimezone(UTC)
    hours = result.stale_hours if result.stale_hours else 24
    now_start = (as_of - timedelta(hours=hours)).astimezone(UTC)
    packs = [TimePack(_fmt_utc(now_start), _fmt_utc(as_of), "now-24h")]

    stale_times: list[datetime] = []
    stale_recs: list[Any] = []
    for rec in result.records:
        if rec.stale and rec.updated_on_utc is not None:
            when = rec.updated_on_utc.astimezone(UTC)
            stale_times.append(when)
            stale_recs.append(rec)
    if not stale_times:
        return packs

    stall_start = min(stale_times) - STALL_PAD
    stall_end = max(stale_times) + STALL_PAD
    if stall_end - stall_start <= MAX_COMBINED_STALL:
        packs.append(TimePack(_fmt_utc(stall_start), _fmt_utc(stall_end), "stall"))
        return packs

    for rec in stale_recs:
        when = rec.updated_on_utc.astimezone(UTC)
        packs.append(
            TimePack(
                _fmt_utc(when - STALL_PAD),
                _fmt_utc(when + STALL_PAD),
                f"urn:{rec.urn}",
            )
        )
    return packs


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_regex(value: str) -> str:
    return re.escape(value)


def stream_selector(
    filters: dict[str, str],
    *,
    urns: list[str] | None = None,
    urn_is_label: bool = False,
) -> str:
    parts: list[str] = []
    for key in ("env", "component", "level"):
        value = filters.get(key)
        if value:
            parts.append(f'{key}="{_escape_label(value)}"')
    if urn_is_label and urns:
        if len(urns) == 1:
            parts.append(f'urn="{_escape_label(urns[0])}"')
        else:
            joined = "|".join(_escape_regex(u) for u in urns)
            parts.append(f'urn=~"{joined}"')
    if not parts:
        parts.append('component=~".+"')
    return "{" + ", ".join(parts) + "}"


def urn_line_filter(selector: str, urns: list[str]) -> str:
    if len(urns) == 1:
        return f'{selector} |= "{_escape_label(urns[0])}"'
    ids = []
    for urn in urns:
        match = URN_ID_RE.search(urn)
        ids.append(match.group(1) if match else _escape_regex(urn))
    return (
        f'{selector} |~ "strn:distribution:DeliveryRequest:({"|".join(ids)})"'
    )


def urn_json_filter(selector: str, urns: list[str]) -> str:
    if len(urns) == 1:
        return f'{selector} |= "\\"urn\\":\\"{_escape_label(urns[0])}\\""'
    ids = []
    for urn in urns:
        match = URN_ID_RE.search(urn)
        ids.append(match.group(1) if match else _escape_regex(urn))
    return (
        f'{selector} |~ "\\"urn\\":\\"strn:distribution:DeliveryRequest:'
        f'({"|".join(ids)})\\""'
    )


def error_logql(selector: str) -> str:
    return f'{selector} |= "DeliveryRequest" |~ "(?i)error|fail|timeout|denied|exception"'


def stale_urns(result: SrmResult) -> list[str]:
    urns: list[str] = []
    for rec in result.records:
        if rec.stale and rec.urn:
            urns.append(rec.urn)
    return urns


def should_query_grafana(verdict: str, *, on_srm_error: bool = False) -> bool:
    if verdict == "STALE":
        return True
    if verdict == "SRM_ERROR" and on_srm_error:
        return True
    return False


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _find_loki_datasource(payload: Any) -> tuple[str | None, str | None]:
    items = payload
    if isinstance(payload, dict):
        for key in ("datasources", "data", "items", "result"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    for item in _as_list(items):
        if not isinstance(item, dict):
            continue
        dtype = str(item.get("type") or item.get("Type") or "").lower()
        if dtype == "loki":
            uid = item.get("uid") or item.get("UID")
            name = item.get("name") or item.get("Name")
            return (str(uid) if uid else None, str(name) if name else None)
    return None, None


def _label_names(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(x) for x in payload]
    if isinstance(payload, dict):
        for key in ("data", "names", "labels"):
            if isinstance(payload.get(key), list):
                return [str(x) for x in payload[key]]
    return []


def _label_values(payload: Any) -> list[str]:
    return _label_names(payload)


def _entry_ts(item: dict[str, Any]) -> str:
    for key in ("timestamp", "ts", "time"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _log_entries(payload: Any) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if payload is None:
        return entries
    if isinstance(payload, list):
        for item in payload:
            entries.extend(_log_entries(item))
        return entries
    if isinstance(payload, str):
        return [("", payload)]
    if not isinstance(payload, dict):
        return entries
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                line = item.get("line") or item.get("Line") or item.get("message")
                if line:
                    entries.append((_entry_ts(item), str(line)))
            elif isinstance(item, str):
                entries.append(("", item))
            else:
                entries.extend(_log_entries(item))
    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            for entry in stream.get("lines") or []:
                if isinstance(entry, dict) and entry.get("line"):
                    entries.append((_entry_ts(entry), str(entry["line"])))
                elif isinstance(entry, str):
                    entries.append(("", entry))
            for value in stream.get("values") or []:
                if isinstance(value, (list, tuple)) and len(value) >= 2:
                    entries.append((str(value[0]), str(value[1])))
    if not entries and payload.get("line"):
        entries.append((_entry_ts(payload), str(payload["line"])))
    return entries


def _log_lines(payload: Any) -> list[str]:
    return [line for _, line in _log_entries(payload)]


def _deeplink_url(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.startswith("http"):
        return payload
    if isinstance(payload, dict):
        for key in ("url", "deeplink", "link"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _dashboard_meta(payload: Any) -> tuple[str | None, str | None]:
    title = None
    folder = None
    if isinstance(payload, dict):
        title = payload.get("title") or payload.get("dashboardTitle")
        folder = payload.get("folderTitle") or payload.get("folder")
        dash = payload.get("dashboard")
        if isinstance(dash, dict):
            title = title or dash.get("title")
        meta = payload.get("meta")
        if isinstance(meta, dict):
            folder = folder or meta.get("folderTitle")
    return (
        str(title) if title else None,
        str(folder) if folder else None,
    )


def redact_text(text: str, extra: str | None = None) -> tuple[str, int]:
    count = 0

    def _mask(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}***"

    redacted = TOKEN_RE.sub(_mask, text)
    if extra:
        occurrences = redacted.count(extra)
        if occurrences:
            redacted = redacted.replace(extra, "***")
            count += occurrences
    return redacted, count


def truncate_line(line: str, limit: int = DEFAULT_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= limit:
        return line, False
    return line[:limit], True


class _Collector:
    def __init__(
        self,
        client: McpClient,
        *,
        settings: Any,
        srm: SrmResult,
        available: set[str] | None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.srm = srm
        self.available = available
        self.logql_used = 0
        self.max_queries = int(getattr(settings, "loki_max_queries", DEFAULT_MAX_QUERIES))
        self.line_limit = int(getattr(settings, "loki_line_limit", DEFAULT_LINE_LIMIT))
        self.line_chars = DEFAULT_LINE_CHARS
        self.timeout = float(getattr(settings, "mcp_tool_timeout", 30.0))
        self.filters = dict(getattr(settings, "grafana_dashboard_filters", {}) or {})
        self.urns = stale_urns(srm)
        self.packs = build_time_packs(srm)
        first = self.packs[0]
        self.start = first.start
        self.end = first.end
        self.result = GrafanaResult(
            mcp_url_host=mcp_host(getattr(settings, "grafana_mcp_url", "") or ""),
            transport="sse",
            dashboard_uid=getattr(settings, "grafana_dashboard_uid", DEFAULT_DASHBOARD_UID)
            or DEFAULT_DASHBOARD_UID,
            dashboard_title=getattr(settings, "grafana_dashboard_title", DEFAULT_DASHBOARD_TITLE)
            or DEFAULT_DASHBOARD_TITLE,
            loki_datasource_uid=getattr(settings, "grafana_loki_datasource_uid", None),
            time_range={"start": first.start, "end": first.end, "label": first.label},
            time_ranges=[],
            filters=self.filters,
        )
        self._token = getattr(settings, "grafana_mcp_token", None)
        self._seen_lines: set[tuple[str, str]] = set()
        self._range_keys: set[tuple[str, str]] = set()
        self._kept_lines: list[str] = []

    def _allowed(self, name: str) -> bool:
        if name in WRITE_TOOLS or name.startswith(WRITE_TOOL_PREFIXES):
            self.result.notes.append(f"refused write tool {name}")
            return False
        if self.available is not None and name not in self.available:
            return False
        return True

    def call(self, name: str, arguments: dict[str, Any]) -> Any | None:
        if not self._allowed(name):
            self.result.tools.append(
                ToolCallLog(name=name, status="skipped", arguments=_public_args(arguments))
            )
            return None
        if name in LOGQL_TOOLS and self.logql_used >= self.max_queries:
            self.result.tools.append(
                ToolCallLog(
                    name=name,
                    status="skipped",
                    arguments=_public_args(arguments),
                    error="LogQL query budget reached",
                )
            )
            return None
        started = time.perf_counter()
        try:
            payload = self.client.call_tool(name, arguments)
            duration = (time.perf_counter() - started) * 1000
            if name in LOGQL_TOOLS:
                self.logql_used += 1
                logql = arguments.get("logql")
                if isinstance(logql, str):
                    self.result.logql.append(logql)
                start = arguments.get("startRfc3339")
                end = arguments.get("endRfc3339")
                if isinstance(start, str) and isinstance(end, str):
                    self._record_time_range(start, end)
            rows = _row_count(name, payload)
            self.result.tools.append(
                ToolCallLog(
                    name=name,
                    status="ok",
                    duration_ms=round(duration, 1),
                    row_count=rows,
                    arguments=_public_args(arguments),
                )
            )
            return payload
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            if name in LOGQL_TOOLS:
                start = arguments.get("startRfc3339")
                end = arguments.get("endRfc3339")
                if isinstance(start, str) and isinstance(end, str):
                    self._record_time_range(start, end)
            message = str(exc)
            redacted, n = redact_text(message, self._token)
            self.result.redactions += n
            self.result.notes.append(f"{name}: {redacted}")
            self.result.tools.append(
                ToolCallLog(
                    name=name,
                    status="error",
                    duration_ms=round(duration, 1),
                    error=redacted,
                    arguments=_public_args(arguments),
                )
            )
            return None

    def _record_time_range(self, start: str, end: str) -> None:
        key = (start, end)
        if key in self._range_keys:
            return
        self._range_keys.add(key)
        label = ""
        for pack in self.packs:
            if pack.start == start and pack.end == end:
                label = pack.label
                break
        entry = {"start": start, "end": end}
        if label:
            entry["label"] = label
        self.result.time_ranges.append(entry)

    def _raise_query_budget(self, urn_is_label: bool) -> None:
        templates = LOGQL_TEMPLATES_PER_PACK
        if urn_is_label:
            templates = LOGQL_TEMPLATES_PER_PACK - 1
        required = templates * len(self.packs)
        if self.max_queries < required:
            self.result.notes.append(
                f"raised Loki query budget {self.max_queries} → {required} "
                f"({templates} templates × {len(self.packs)} time packs)"
            )
            self.max_queries = required

    def ingest_lines(self, payload: Any) -> list[str]:
        kept: list[str] = []
        if payload is None:
            return kept
        for ts, line in _log_entries(payload):
            trimmed, discarded = truncate_line(line, self.line_chars)
            redacted, n = redact_text(trimmed, self._token)
            self.result.redactions += n
            if discarded:
                self.result.lines_discarded += 1
            key = (ts, redacted)
            if key in self._seen_lines:
                self.result.lines_discarded += 1
                continue
            self._seen_lines.add(key)
            if len(kept) < self.line_limit:
                kept.append(redacted)
                self._kept_lines.append(redacted)
                self.result.lines_kept += 1
            else:
                self.result.lines_discarded += 1
        return kept

    def run(self) -> GrafanaResult:
        started = time.perf_counter()
        uid = self.result.dashboard_uid
        summary = self.call("get_dashboard_summary", {"uid": uid})
        if summary is None:
            summary = self.call("get_dashboard_by_uid", {"uid": uid})
        title, folder = _dashboard_meta(summary)
        if title:
            self.result.dashboard_title = title
        if folder:
            self.result.dashboard_folder = folder
        panel_args: dict[str, Any] = {"uid": uid}
        if self.filters:
            panel_args["variables"] = self.filters
        self.call("get_dashboard_panel_queries", panel_args)

        ds_payload = self.call("list_datasources", {"type": "loki"})
        if ds_payload is None:
            ds_payload = self.call("list_datasources", {})
        loki_uid, loki_name = _find_loki_datasource(ds_payload)
        if not self.result.loki_datasource_uid:
            self.result.loki_datasource_uid = loki_uid
        if loki_name:
            self.result.loki_datasource_name = loki_name

        ds = self.result.loki_datasource_uid
        if not ds:
            self.result.notes.append("Loki datasource UID not found")
            self.result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            return self.result

        time_args = {
            "datasourceUid": ds,
            "startRfc3339": self.start,
            "endRfc3339": self.end,
        }
        names_payload = self.call("list_loki_label_names", dict(time_args))
        names = {n.lower() for n in _label_names(names_payload)}
        urn_is_label = "urn" in names
        if urn_is_label:
            values = _label_values(
                self.call(
                    "list_loki_label_values",
                    {**time_args, "labelName": "urn"},
                )
            )
            urn_is_label = any(u in values for u in self.urns) if values else True
        self.result.urn_is_label = urn_is_label
        self._raise_query_budget(urn_is_label)

        stats_selector = stream_selector(
            self.filters, urns=self.urns if urn_is_label else None, urn_is_label=urn_is_label
        )
        for pack in self.packs:
            self._query_logql_pack(pack, ds, urn_is_label, stats_selector)

        if not self._kept_lines:
            self.call(
                "check_datasources_health",
                {"uids": [ds]},
            )

        dash_args: dict[str, Any] = {"resourceType": "dashboard", "dashboardUid": uid}
        if self.filters:
            dash_args["queryParams"] = {
                f"var-{key}": value for key, value in self.filters.items()
            }
        dash_link = self.call("generate_deeplink", dash_args)
        explore_link = self.call(
            "generate_deeplink",
            {
                "resourceType": "explore",
                "datasourceUid": ds,
                "timeRange": {"from": self.start, "to": self.end},
            },
        )
        for payload in (dash_link, explore_link):
            link = _deeplink_url(payload)
            if link:
                self.result.deeplinks.append(link)
                if "dashboard" in link or uid in link:
                    self.result.dashboard_url = self.result.dashboard_url or link

        self.result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        self.result.highlights = self._kept_lines[:8]
        self.result.lines_kept = len(self._kept_lines)
        return self.result

    def _query_logql_pack(
        self,
        pack: TimePack,
        ds: str,
        urn_is_label: bool,
        stats_selector: str,
    ) -> None:
        time_args = {
            "datasourceUid": ds,
            "startRfc3339": pack.start,
            "endRfc3339": pack.end,
        }
        self.call(
            "query_loki_stats",
            {**time_args, "logql": stats_selector},
        )

        if urn_is_label:
            logs_query = stream_selector(
                self.filters, urns=self.urns, urn_is_label=True
            )
        else:
            logs_query = urn_line_filter(
                stream_selector(self.filters),
                self.urns or ["strn:distribution:DeliveryRequest"],
            )
        logs = self.call(
            "query_loki_logs",
            {**time_args, "logql": logs_query, "limit": self.line_limit},
        )
        if logs is not None:
            self.ingest_lines(logs)

        if not urn_is_label and self.urns:
            json_query = urn_json_filter(stream_selector(self.filters), self.urns)
            json_logs = self.call(
                "query_loki_logs",
                {**time_args, "logql": json_query, "limit": self.line_limit},
            )
            if json_logs is not None:
                self.ingest_lines(json_logs)

        error_query = error_logql(stream_selector(self.filters))
        error_logs = self.call(
            "query_loki_logs",
            {**time_args, "logql": error_query, "limit": self.line_limit},
        )
        if error_logs is not None:
            self.ingest_lines(error_logs)

        self.call(
            "query_loki_patterns",
            {**time_args, "logql": stats_selector},
        )


def _public_args(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if "token" in lowered or "password" in lowered or "secret" in lowered:
            continue
        out[key] = value
    return out


def _row_count(name: str, payload: Any) -> int | None:
    if payload is None:
        return None
    if name in {"query_loki_logs", "query_loki_patterns"}:
        return len(_log_lines(payload) or _as_list(payload if not isinstance(payload, dict) else payload.get("data")))
    if name == "list_loki_label_names":
        return len(_label_names(payload))
    if name == "list_loki_label_values":
        return len(_label_values(payload))
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return len(payload["data"])
    return None


def skipped_grafana(*, reason: str, transport: str = "sse") -> GrafanaResult:
    return GrafanaResult(skipped=True, skip_reason=reason, transport=transport, notes=[reason])


def collect_grafana(
    srm: SrmResult,
    settings: Any,
    *,
    client: McpClient | None = None,
) -> GrafanaResult:
    url = getattr(settings, "grafana_mcp_url", None)
    transport = (getattr(settings, "grafana_mcp_transport", "sse") or "sse").lower()
    if client is None and transport != "sse":
        return skipped_grafana(
            reason=f"Grafana transport {transport} is not sse", transport=transport
        )
    if client is None and not url:
        return skipped_grafana(reason="GRAFANA_MCP_URL is not set")

    owned = client is None
    if owned:
        from .mcp_sse import SseMcpClient

        client = SseMcpClient(
            url,
            token=getattr(settings, "grafana_mcp_token", None),
            verify=getattr(settings, "tls_verify", True),
            timeout=float(getattr(settings, "mcp_tool_timeout", 30.0)),
        )
        try:
            client.connect()  # type: ignore[attr-defined]
        except Exception as exc:
            if hasattr(client, "close"):
                client.close()
            host = mcp_host(url)
            return GrafanaResult(
                skipped=False,
                mcp_url_host=host,
                transport="sse",
                notes=[f"MCP connect failed ({host}): {exc.__class__.__name__}"],
            )

    try:
        available: set[str] | None
        try:
            available = set(client.list_tools())
        except Exception:
            available = None
            # Fall through and attempt the known read-only pack.
        collector = _Collector(client, settings=settings, srm=srm, available=available)
        return collector.run()
    except McpError as exc:
        host = mcp_host(url)
        redacted, n = redact_text(str(exc), getattr(settings, "grafana_mcp_token", None))
        return GrafanaResult(
            mcp_url_host=host,
            transport="sse",
            redactions=n,
            notes=[f"MCP failure ({host}): {redacted}"],
        )
    except Exception as exc:
        host = mcp_host(url)
        return GrafanaResult(
            mcp_url_host=host,
            transport="sse",
            notes=[f"MCP failure ({host}): {exc.__class__.__name__}"],
        )
    finally:
        if owned and client is not None and hasattr(client, "close"):
            client.close()
