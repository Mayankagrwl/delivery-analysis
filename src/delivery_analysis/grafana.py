from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from .config import (
    DEFAULT_COMPONENT,
    DEFAULT_DASHBOARD_TITLE,
    DEFAULT_DASHBOARD_UID,
    DEFAULT_ENV,
    DEFAULT_LEVELS,
    DEFAULT_LINE_LIMIT,
    DEFAULT_MAX_QUERIES,
    LOGQL_TEMPLATES_PER_PACK,
)
from .mcp_sse import McpClient, McpError, mcp_host
from .models import SrmResult
from .timestamps import UTC

DEFAULT_DASHBOARD_FOLDER = "Distribution"
DEFAULT_LINE_CHARS = 8000
HIGHLIGHT_RADIUS = 200
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
LOGQL_TOOLS = {
    "query_loki_logs",
    "query_loki_stats",
}
TOKEN_RE = re.compile(r"(?i)(bearer\s+)\S+")
URN_ID_RE = re.compile(r"strn:distribution:DeliveryRequest:(\d+)")
DELIVERY_URN_RE = re.compile(r"strn:distribution:DeliveryRequest:\d+")
EVENTSENDER_PAIR_RE = re.compile(r"\b(Started|Sent)\b")
STALL_PAD_BEFORE = timedelta(hours=2)
STALL_PAD_AFTER = timedelta(hours=6)
MAX_COMBINED_STALL = timedelta(days=7)
LEVEL_WARN_FILTER = "LEVEL=(alert|error|warn|ALERT|ERROR|WARN)"
LEVEL_INFO_FILTER = "LEVEL=(INFO|info)"
LEVEL_DEBUG_FILTER = "LEVEL=(debug|DEBUG)"
SEVERITY_PATTERN = LEVEL_WARN_FILTER
DEBUG_LEVELS = {"debug"}
KEEP_LEVELS = {"alert", "error", "warn", "warning", "fatal", "critical"}
LEVEL_RE = re.compile(
    r'(?:LEVEL|level)[=:][\s"]*(alert|error|warn(?:ing)?|debug|info|fatal|critical)\b'
    r'|\[(alert|error|warn(?:ing)?|debug|info)\]',
    re.IGNORECASE,
)
COMPONENT_RE = re.compile(
    r'(?i)(?:component)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_.:-]+)'
)
SELECTOR_RE = re.compile(r"\{([^}]*)\}")
PANEL_EXPR_KEYS = {"expr", "logql", "query", "exprraw", "rawsql"}
VAR_BRACE_RE = re.compile(r"\$\{([^}]+)\}")


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
    env: str | None = None
    environment_label: str | None = None
    component_key: str = "component"
    component_order: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    level_counts: dict[str, int] = Field(default_factory=dict)
    component_counts: dict[str, int] = Field(default_factory=dict)
    distribution_lines: list[str] = Field(default_factory=list)
    other_lines: list[str] = Field(default_factory=list)
    level_pass: dict[str, str] = Field(default_factory=dict)
    lines_with_stale_urn: int = 0
    lines_truncated: int = 0
    # Notification success gate (request_id / single-DR mode only)
    notification_checked: bool = False
    notification_success: bool | None = None
    notification_success_lines: list[str] = Field(default_factory=list)
    notification_markers_matched: list[str] = Field(default_factory=list)
    notification_component: str | None = None
    notification_lookback_hours: float | None = None
    notification_request_id: str | None = None
    notification_lines_scanned: int = 0
    # Tempo trace cascade
    tempo_datasource_uid: str | None = None
    trace_id: str | None = None
    trace_spans: list[dict[str, Any]] = Field(default_factory=list)
    trace_deeplink: str | None = None


class TimePack(NamedTuple):
    start: str
    end: str
    label: str


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_time_packs(
    result: SrmResult,
    *,
    request_id: str | None = None,
    lookback_hours: float | None = None,
) -> list[TimePack]:
    """stall (updated_on-2h..+6h) then recent (now-24h..now), UTC.

    In request_id mode a wider ``lookback`` pack (now-lookback..now) is prepended
    so an older completed request is found; the redundant ``recent`` pack is then
    dropped since ``lookback`` covers it.
    """
    as_of = result.as_of.astimezone(UTC)
    hours = result.stale_hours if result.stale_hours else 24
    now_start = (as_of - timedelta(hours=hours)).astimezone(UTC)
    recent = TimePack(_fmt_utc(now_start), _fmt_utc(as_of), "recent")

    stale_times: list[datetime] = []
    stale_recs: list[Any] = []
    for rec in result.records:
        if rec.stale and rec.updated_on_utc is not None:
            when = rec.updated_on_utc.astimezone(UTC)
            stale_times.append(when)
            stale_recs.append(rec)

    if not stale_times:
        base = [recent]
    else:
        stall_start = min(stale_times) - STALL_PAD_BEFORE
        stall_end = max(stale_times) + STALL_PAD_AFTER
        if stall_end - stall_start <= MAX_COMBINED_STALL:
            base = [
                TimePack(_fmt_utc(stall_start), _fmt_utc(stall_end), "stall"),
                recent,
            ]
        else:
            base = [
                TimePack(
                    _fmt_utc(rec.updated_on_utc.astimezone(UTC) - STALL_PAD_BEFORE),
                    _fmt_utc(rec.updated_on_utc.astimezone(UTC) + STALL_PAD_AFTER),
                    f"urn:{rec.urn}",
                )
                for rec in stale_recs
            ]
            base.append(recent)

    if request_id and lookback_hours and lookback_hours > 0:
        lookback = TimePack(
            _fmt_utc((as_of - timedelta(hours=lookback_hours)).astimezone(UTC)),
            _fmt_utc(as_of),
            "lookback",
        )
        # lookback subsumes recent; keep stall/per-urn packs for stall detail.
        return [lookback] + [p for p in base if p.label != "recent"]
    return base


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_regex(value: str) -> str:
    return re.escape(value)


def _is_regex_value(value: str) -> bool:
    raw = value.strip()
    if raw.lower() in {"all", "*"}:
        return True
    if raw.startswith("~") or raw == ".+":
        return True
    return any(ch in raw for ch in "|*+")


def _label_pair(key: str, value: str) -> str:
    raw = value.strip()
    if raw.lower() in {"all", "*"}:
        raw = ".+"
    if raw.startswith("~"):
        raw = raw[1:].strip().strip('"')
    if _is_regex_value(value) or raw == ".+":
        return f'{key}=~"{raw}"'
    return f'{key}="{_escape_label(raw)}"'


def stream_selector(
    filters: dict[str, str],
    *,
    urns: list[str] | None = None,
    urn_is_label: bool = False,
    component_key: str = "component",
) -> str:
    """Explore-style selector: component only. Never env= or level= inside {}."""
    del urns, urn_is_label, component_key
    parts: list[str] = []
    comp = filters.get("component")
    if comp:
        parts.append(_label_pair("component", comp))
    if not parts:
        parts.append('component=~".+"')
    return "{" + ", ".join(parts) + "}"


def loki_environment_value(
    env: str | None, overrides: dict[str, str] | None = None
) -> str | None:
    """Map an SRM environment (test/int/qa/demo/prod) to the Loki `environment`
    label value. prod/production/empty -> "production"; others unchanged. An
    optional overrides map (from GRAFANA_ENV_LABEL_VALUES) wins when present.
    """
    normalized = (env or "").strip().lower()
    if not normalized:
        return None
    if overrides and normalized in overrides:
        return overrides[normalized]
    if normalized in {"prod", "production"}:
        return "production"
    return normalized


def component_selector(component_value: str, environment: str | None = None) -> str:
    parts = [_label_pair("component", component_value)]
    if environment:
        parts.append(_label_pair("environment", environment))
    return "{" + ", ".join(parts) + "}"


def ensure_environment_label(logql: str, environment: str | None) -> str:
    """Inject `environment="<value>"` into the first `{...}` selector of a LogQL
    query when missing, so panel-derived queries scope to the env too.
    """
    if not environment:
        return logql

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        if re.search(r"\benvironment\s*=", inner):
            return match.group(0)
        pair = _label_pair("environment", environment)
        inner = inner.strip()
        return "{" + (f"{inner}, {pair}" if inner else pair) + "}"

    return SELECTOR_RE.sub(repl, logql, count=1)


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
    return f'{selector} |= "DeliveryRequest" |~ "{LEVEL_WARN_FILTER}"'


def urn_match_filter(urns: list[str]) -> str:
    if not urns:
        return '|= "strn:distribution:DeliveryRequest"'
    if len(urns) == 1:
        return f'|= "{_escape_label(urns[0])}"'
    ids: list[str] = []
    for urn in urns:
        match = URN_ID_RE.search(urn)
        ids.append(match.group(1) if match else _escape_regex(urn))
    return f'|~ "strn:distribution:DeliveryRequest:({"|".join(ids)})"'


def severity_line_filter(level_filter: str = LEVEL_WARN_FILTER) -> str:
    return f'|~ "{level_filter}"'


def level_filter_for_pass(level_pass: str) -> str:
    if level_pass == "info":
        return LEVEL_INFO_FILTER
    if level_pass == "debug":
        return LEVEL_DEBUG_FILTER
    return LEVEL_WARN_FILTER


def preferred_component_value(filters: dict[str, str]) -> str:
    raw = (filters.get("component") or DEFAULT_COMPONENT).strip()
    if raw.lower() in {"all", "*", ".+"}:
        return ".+"
    if raw.startswith("~"):
        return raw[1:].strip().strip('"') or DEFAULT_COMPONENT
    return raw or DEFAULT_COMPONENT


def build_priority_logql(
    urns: list[str],
    *,
    env: str = DEFAULT_ENV,
    component_key: str = "component",
    preferred_component: str = DEFAULT_COMPONENT,
    include_per_urn: bool = True,
    level_pass: str = "warn",
    environment: str | None = None,
) -> list[str]:
    """Explore LogQL: {component=[, environment=]} then URN then LEVEL= line filter.

    When ``environment`` is given it is added as a Loki label matcher so queries
    scope to that environment's logs (multi-env runs); otherwise no env label is
    emitted (unchanged single-host behavior). ``env`` remains a dashboard-only
    variable and is not a label.
    """
    del env, component_key
    sev = severity_line_filter(level_filter_for_pass(level_pass))
    combined = urn_match_filter(urns)
    queries: list[str] = []
    pref = preferred_component or DEFAULT_COMPONENT
    skip_pref = pref in {".+", "all", "All", "*"}
    if not skip_pref:
        dist = component_selector(pref, environment=environment)
        queries.append(f"{dist} {combined} {sev}")
        if include_per_urn and len(urns) > 1:
            for urn in urns:
                queries.append(f'{dist} |= "{_escape_label(urn)}" {sev}')
        elif include_per_urn and len(urns) == 1:
            queries.append(f'{dist} |= "{_escape_label(urns[0])}" {sev}')
    all_sel = component_selector(".+", environment=environment)
    queries.append(f"{all_sel} {combined} {sev}")
    return queries


def classify_level(line: str) -> str | None:
    match = LEVEL_RE.search(line)
    if not match:
        return None
    value = (match.group(1) or match.group(2) or "").lower()
    if value == "warning":
        return "warn"
    return value or None


def classify_component(line: str) -> str | None:
    match = COMPONENT_RE.search(line)
    if not match:
        return None
    return match.group(1)


def _panel_logql(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if (
                    str(key).lower() in PANEL_EXPR_KEYS
                    and isinstance(value, str)
                    and "{" in value
                ):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    # preserve order, drop dupes
    out: list[str] = []
    seen: set[str] = set()
    for expr in found:
        if expr not in seen:
            seen.add(expr)
            out.append(expr)
    return out


def substitute_panel_vars(expr: str, variables: dict[str, str]) -> str:
    out = expr
    for key, value in variables.items():
        out = out.replace("${" + key + "}", value)
        out = out.replace("$" + key, value)
        out = out.replace("[[" + key + "]]", value)
    return out


def rewrite_panel_logql(
    expr: str,
    *,
    env: str,
    level: str,
    urns: list[str],
    variables: dict[str, str] | None = None,
    level_pass: str = "warn",
) -> str | None:
    del env, level
    vars_map = dict(variables or {})
    out = substitute_panel_vars(expr, vars_map)
    if "${" in out:
        return None

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        kept: list[str] = []
        for part in parts:
            key = part.split("=", 1)[0].strip().lstrip("~")
            if key.lower() in {"level", "service"}:
                continue
            if key.lower() == "env":
                _, _, value = part.partition("=")
                kept.append("environment=" + value if value else 'environment="production"')
                continue
            kept.append(part)
        if not kept:
            kept.append('component=~".+"')
        return "{" + ", ".join(kept) + "}"

    out = SELECTOR_RE.sub(repl, out, count=1)
    if "${" in out:
        return None
    urn_f = urn_match_filter(urns)
    if urns and "DeliveryRequest" not in out:
        out = f"{out} {urn_f}"
    sev = severity_line_filter(level_filter_for_pass(level_pass))
    if "LEVEL=" not in out:
        out = f"{out} {sev}"
    return out


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
    """Persist-cap a Loki line. Prefer a window around the DeliveryRequest URN."""
    if len(line) <= limit:
        return line, False
    span = _urn_span(line)
    if span is None:
        return line[:limit], True
    start, end = span
    extra = max(0, limit - (end - start))
    left = extra // 2
    right = extra - left
    a = max(0, start - left)
    b = min(len(line), end + right)
    if b - a < limit:
        if a == 0:
            b = min(len(line), a + limit)
        else:
            a = max(0, b - limit)
    return line[a:b], True


def _urn_span(line: str) -> tuple[int, int] | None:
    match = DELIVERY_URN_RE.search(line)
    if not match:
        return None
    return match.start(), match.end()


def highlight_snippet(line: str, *, radius: int = HIGHLIGHT_RADIUS) -> str:
    """Summary snippet centered on DeliveryRequest URN when present."""
    span = _urn_span(line)
    if span is None:
        cap = radius * 2
        return line if len(line) <= cap else line[:cap]
    start, end = span
    a = max(0, start - radius)
    b = min(len(line), end + radius)
    snippet = line[a:b]
    if a > 0:
        snippet = "…" + snippet
    if b < len(line):
        snippet = snippet + "…"
    return snippet


def _second_bucket(ts: str) -> str:
    text = (ts or "").strip()
    if not text:
        return ""
    if text.isdigit():
        n = int(text)
        if n >= 10**18:
            n //= 10**9
        elif n >= 10**15:
            n //= 10**6
        elif n >= 10**12:
            n //= 10**3
        return str(n)
    if "T" in text:
        return text.replace("Z", "")[:19]
    return text[:19]


def eventsender_dedup_key(ts: str, line: str) -> tuple[str, str] | None:
    if "EventSender" not in line:
        return None
    return (_second_bucket(ts), EVENTSENDER_PAIR_RE.sub("*", line))


def line_has_stale_urn(line: str, urns: list[str]) -> bool:
    for urn in urns:
        if urn and urn in line:
            return True
        match = URN_ID_RE.search(urn or "")
        if match and f"DeliveryRequest:{match.group(1)}" in line:
            return True
    return False


NOTIFICATION_SUCCESS_CAP = 8
MAX_NOTIFICATION_REQUEST_IDS = 3
TEMPO_TOOL_HINTS = ("tempo", "trace")
_HEX_16_OR_32 = r"([0-9a-fA-F]{32}|[0-9a-fA-F]{16})(?![0-9a-fA-F])"


def _request_id_re(field: str | None = None) -> re.Pattern[str]:
    """Match a correlation request id: the configured field key or variants.

    The notification success markers ("successfully processed", "mail sent to")
    are keyed by this id, not by the DeliveryRequest URN. Handles JSON
    (``"requestId":"aq0..."``) and ``key=value`` forms; the value is 8+ chars of
    ``[A-Za-z0-9_-]`` so it never matches a bare numeric id.
    """
    keys = ["request[_-]?id"]
    if field:
        esc = re.escape(field.strip())
        if esc and esc not in keys:
            keys.insert(0, esc)
    key_alt = "|".join(keys)
    return re.compile(
        rf'(?i)(?:{key_alt})["\']?\s*[:=]\s*["\']?([A-Za-z0-9][A-Za-z0-9_\-]{{7,}})'
    )


def extract_request_ids(lines: list[str], *, field: str = "requestId") -> list[str]:
    """Ordered, de-duplicated correlation request ids found in the given lines."""
    pattern = _request_id_re(field)
    ids: list[str] = []
    for line in lines:
        for match in pattern.finditer(line):
            rid = match.group(1)
            if rid and rid not in ids:
                ids.append(rid)
    return ids


def match_success_markers(
    lines: list[str], markers: list[str]
) -> tuple[list[str], list[str]]:
    """Return (matched_lines, matched_markers), case-insensitive substring match."""
    matched_lines: list[str] = []
    matched_markers: list[str] = []
    for line in lines:
        low = line.lower()
        for marker in markers:
            if marker and marker.lower() in low:
                if line not in matched_lines:
                    matched_lines.append(line)
                if marker not in matched_markers:
                    matched_markers.append(marker)
    return matched_lines, matched_markers


def _trace_id_re(field: str | None = None) -> re.Pattern[str]:
    """Match a trace id: the configured field key or common variants + hex value.

    Accepts ``trace_id``, ``traceId``, ``traceID``, ``trace-id`` (and the given
    ``field``) followed by a 16- or 32-char hex value.
    """
    keys = ["trace[_-]?id"]
    if field:
        esc = re.escape(field.strip())
        if esc and esc not in keys:
            keys.insert(0, esc)
    key_alt = "|".join(keys)
    return re.compile(
        rf'(?i)(?:{key_alt})["\']?\s*[:=]\s*["\']?{_HEX_16_OR_32}'
    )


def extract_trace_id(lines: list[str], *, field: str = "trace_id") -> str | None:
    pattern = _trace_id_re(field)
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _span_field(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _trace_span_candidates(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("spans", "data", "traces", "batches", "resourceSpans"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        trace = payload.get("trace")
        if isinstance(trace, dict) and isinstance(trace.get("spans"), list):
            return trace["spans"]
    return []


def parse_trace_spans(payload: Any) -> list[dict[str, Any]]:
    """Tolerantly parse a Tempo trace payload into ordered cascade spans."""
    spans: list[dict[str, Any]] = []
    for item in _trace_span_candidates(payload):
        if not isinstance(item, dict):
            continue
        service = _span_field(
            item, ("service", "component", "serviceName", "service_name")
        )
        if service is None and isinstance(item.get("process"), dict):
            service = item["process"].get("serviceName")
        name = _span_field(item, ("name", "operationName", "operation", "span"))
        status = _span_field(
            item, ("status", "statusCode", "status_code", "state")
        )
        duration = _span_field(
            item, ("duration_ms", "durationMs", "duration", "durationMillis")
        )
        spans.append(
            {
                "service": str(service) if service is not None else "",
                "name": str(name) if name is not None else "",
                "status": str(status) if status is not None else "",
                "duration_ms": duration
                if isinstance(duration, (int, float))
                else _to_float(duration),
            }
        )
    return spans


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
        # Scope Grafana/Loki queries to the environment being processed. The
        # per-env SRM environment (test/int/qa/demo/prod) maps to the Loki
        # `environment` label value (prod -> "production"); when absent (legacy
        # single-host runs), no env label is emitted and behavior is unchanged.
        self.srm_environment = getattr(srm, "environment", None)
        self.environment_value = loki_environment_value(
            self.srm_environment,
            getattr(settings, "grafana_env_label_values", None),
        )
        if self.environment_value:
            self.env = self.environment_value
        else:
            self.env = (
                self.filters.get("environment")
                or self.filters.get("env")
                or DEFAULT_ENV
            ).strip() or DEFAULT_ENV
        self.level = (self.filters.get("level") or DEFAULT_LEVELS).strip() or DEFAULT_LEVELS
        self.preferred_component = preferred_component_value(self.filters)
        self.component_key = "component"
        self.include_debug = bool(getattr(settings, "include_debug_logs", False))
        self.request_id = getattr(srm, "request_id", None)
        self.notification_component = (
            getattr(settings, "notification_component", None) or "notification"
        )
        self.notification_markers = list(
            getattr(settings, "notification_success_markers", None) or []
        )
        self.notification_request_id_field = (
            getattr(settings, "notification_request_id_field", None) or "requestId"
        )
        self.trace_id_field = getattr(settings, "trace_id_field", "trace_id") or "trace_id"
        self.lookback_hours = float(
            getattr(settings, "request_id_lookback_hours", 168.0) or 168.0
        )
        self.urns = stale_urns(srm)
        # In request_id mode scope Loki/Tempo to that URN even when no stale
        # record exists (verdict FRESH/NO_RECORDS), so completion logs are found.
        if self.request_id and not self.urns:
            self.urns = [f"strn:distribution:DeliveryRequest:{self.request_id}"]
        self.packs = build_time_packs(
            srm,
            request_id=self.request_id,
            lookback_hours=self.lookback_hours,
        )
        first = self.packs[0]
        self.start = first.start
        self.end = first.end
        level_list = [part.strip() for part in self.level.split("|") if part.strip()]
        pref_label = (
            ".+"
            if self.preferred_component in {".+", "all", "*"}
            else self.preferred_component
        )
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
            env=self.env,
            environment_label=self.environment_value,
            component_key=self.component_key,
            component_order=[pref_label, ".+"],
            levels=level_list,
            level_pass={},
        )
        self._token = getattr(settings, "grafana_mcp_token", None)
        self._seen_lines: set[tuple[str, str]] = set()
        self._seen_eventsender: set[tuple[str, str]] = set()
        self._range_keys: set[tuple[str, str]] = set()
        self._kept_lines: list[str] = []
        self._distribution_lines: list[str] = []
        self._other_lines: list[str] = []
        self._debug_dropped = 0
        self._panel_exprs: list[str] = []

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

    def _raise_query_budget(self, n_panel: int) -> None:
        n_time = max(1, len(self.packs))
        n_combined = n_time * 2
        n_per_urn = len(self.urns) if len(self.urns) > 1 else 0
        n_passes = 3 if self.include_debug else 2
        n_cluster = 1
        # In request_id mode reserve budget for the two-hop notification probe
        # (URN query + one query per resolved requestId, each across time packs)
        # plus a Tempo trace call.
        n_probe = (
            n_time * (1 + MAX_NOTIFICATION_REQUEST_IDS) + 2 if self.request_id else 0
        )
        required = max(
            LOGQL_TEMPLATES_PER_PACK * n_time * n_passes,
            (n_combined + n_per_urn) * n_passes + n_cluster + n_panel * n_time,
        ) + n_probe
        if self.max_queries < required:
            self.result.notes.append(
                f"raised Loki query budget {self.max_queries} → {required} "
                f"(stall×distribution+all, {n_time} time packs)"
            )
            self.max_queries = required

    def ingest_lines(
        self,
        payload: Any,
        *,
        component_hint: str | None = None,
        level_pass: str = "warn",
    ) -> list[str]:
        kept: list[str] = []
        if payload is None:
            return kept
        preferred = self.preferred_component
        for ts, line in _log_entries(payload):
            persisted, truncated = truncate_line(line, self.line_chars)
            redacted, n = redact_text(persisted, self._token)
            self.result.redactions += n
            if truncated:
                self.result.lines_truncated += 1
            level = classify_level(redacted)
            if level == "debug" and level_pass != "debug":
                self.result.lines_discarded += 1
                self._debug_dropped += 1
                continue
            if level_pass == "warn" and level == "info":
                self.result.lines_discarded += 1
                continue
            key = (ts, redacted)
            if key in self._seen_lines:
                self.result.lines_discarded += 1
                continue
            pair_key = eventsender_dedup_key(ts, redacted)
            if pair_key is not None and pair_key in self._seen_eventsender:
                self.result.lines_discarded += 1
                continue
            self._seen_lines.add(key)
            if pair_key is not None:
                self._seen_eventsender.add(pair_key)
            if len(kept) >= self.line_limit:
                self.result.lines_discarded += 1
                continue
            kept.append(redacted)
            self._kept_lines.append(redacted)
            self.result.lines_kept += 1
            if line_has_stale_urn(redacted, self.urns):
                self.result.lines_with_stale_urn += 1
            level_key = level or level_pass
            self.result.level_counts[level_key] = (
                self.result.level_counts.get(level_key, 0) + 1
            )
            component = classify_component(redacted) or component_hint or "unknown"
            self.result.component_counts[component] = (
                self.result.component_counts.get(component, 0) + 1
            )
            is_preferred = (
                preferred not in {".+", "all", "*"} and component == preferred
            )
            if is_preferred:
                self._distribution_lines.append(redacted)
            else:
                self._other_lines.append(redacted)
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
        panel_vars = {
            "env": self.env,
            self.component_key: self.preferred_component,
            "level": self.level,
        }
        panel_args: dict[str, Any] = {"uid": uid, "variables": panel_vars}
        panel_payload = self.call("get_dashboard_panel_queries", panel_args)
        self._panel_exprs = _panel_logql(panel_payload)

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
        raw_names = _label_names(names_payload)
        lower_map = {n.lower(): n for n in raw_names}
        self.component_key = "component"
        self.result.component_key = "component"
        pref_label = self.preferred_component
        self.result.component_order = [pref_label, ".+"]
        names = set(lower_map)
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
        self._raise_query_budget(len(self._panel_exprs))

        # Notification success probe first (request_id mode) so it always has
        # query budget and success is never missed.
        if self.request_id:
            self._notification_probe(ds)

        stall_pack = next((p for p in self.packs if p.label == "stall"), self.packs[0])
        for pack in self.packs:
            self._query_priority_logs(pack, ds)
            self._query_panels(pack, ds)
        self._query_stall_stats(stall_pack, ds)

        # Trace id extraction + Tempo cascade (graceful when unavailable).
        self._extract_trace_id()
        self._query_tempo()

        if self._debug_dropped:
            self.result.notes.append(
                f"dropped {self._debug_dropped} LEVEL=debug lines"
            )
        passes = self.result.level_pass
        if any(v == "info" for v in passes.values()):
            self.result.notes.append(
                "warn/error/alert LEVEL pass returned 0 lines; packed LEVEL=INFO fallback"
            )
        if not self._kept_lines:
            self.result.notes.append(
                "No LEVEL=alert|error|warn or LEVEL=INFO Loki lines for stale URNs "
                f"after querying component={self.preferred_component} then All "
                "over stall and recent windows; LEVEL=debug not queried."
            )
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
        self.result.distribution_lines = list(self._distribution_lines)
        self.result.other_lines = list(self._other_lines)
        snippets = [
            highlight_snippet(line)
            for line in (self._distribution_lines + self._other_lines)
        ]
        snippets.sort(key=lambda text: 0 if "DeliveryRequest:" in text else 1)
        self.result.highlights = snippets[:8]
        self.result.lines_kept = len(self._kept_lines)
        return self.result

    def _time_args(self, pack: TimePack, ds: str) -> dict[str, Any]:
        return {
            "datasourceUid": ds,
            "startRfc3339": pack.start,
            "endRfc3339": pack.end,
        }

    def _query_panels(self, pack: TimePack, ds: str) -> None:
        if not self._panel_exprs:
            return
        time_args = self._time_args(pack, ds)
        level_pass = self.result.level_pass.get(pack.label) or "warn"
        variables = {
            "component": self.preferred_component,
            "env": self.env,
            "level": self.level,
        }
        for expr in self._panel_exprs:
            rewritten = rewrite_panel_logql(
                expr,
                env=self.env,
                level=self.level,
                urns=self.urns,
                variables=variables,
                level_pass=level_pass if level_pass != "none" else "warn",
            )
            if rewritten is None:
                self.result.notes.append(
                    "skipped panel LogQL still containing ${ after substitution"
                )
                continue
            rewritten = ensure_environment_label(rewritten, self.environment_value)
            payload = self.call(
                "query_loki_logs",
                {**time_args, "logql": rewritten, "limit": self.line_limit},
            )
            if payload is not None:
                self.ingest_lines(
                    payload,
                    component_hint=self.preferred_component,
                    level_pass=level_pass if level_pass != "none" else "warn",
                )

    def _run_logql_list(
        self,
        pack: TimePack,
        ds: str,
        queries: list[str],
        *,
        level_pass: str,
    ) -> int:
        time_args = self._time_args(pack, ds)
        raw_lines = 0
        for query in queries:
            hint = (
                self.preferred_component
                if 'component="distribution"' in query
                or f'component="{self.preferred_component}"' in query
                else None
            )
            payload = self.call(
                "query_loki_logs",
                {**time_args, "logql": query, "limit": self.line_limit},
            )
            raw_lines += len(_log_entries(payload)) if payload is not None else 0
            if payload is not None:
                self.ingest_lines(
                    payload, component_hint=hint, level_pass=level_pass
                )
        return raw_lines

    def _query_priority_logs(self, pack: TimePack, ds: str) -> None:
        per_urn = pack.label == "stall" or pack.label.startswith("urn:")
        warn_queries = build_priority_logql(
            self.urns,
            preferred_component=self.preferred_component,
            include_per_urn=per_urn,
            level_pass="warn",
            environment=self.environment_value,
        )
        warn_raw = self._run_logql_list(pack, ds, warn_queries, level_pass="warn")
        if warn_raw > 0:
            self.result.level_pass[pack.label] = "warn"
            return
        info_queries = build_priority_logql(
            self.urns,
            preferred_component=self.preferred_component,
            include_per_urn=per_urn,
            level_pass="info",
            environment=self.environment_value,
        )
        info_raw = self._run_logql_list(pack, ds, info_queries, level_pass="info")
        if info_raw > 0:
            self.result.level_pass[pack.label] = "info"
            return
        if self.include_debug:
            debug_queries = build_priority_logql(
                self.urns,
                preferred_component=self.preferred_component,
                include_per_urn=per_urn,
                level_pass="debug",
                environment=self.environment_value,
            )
            debug_raw = self._run_logql_list(
                pack, ds, debug_queries, level_pass="debug"
            )
            self.result.level_pass[pack.label] = "debug" if debug_raw else "none"
            return
        self.result.level_pass[pack.label] = "none"

    def _query_stall_stats(self, pack: TimePack, ds: str) -> None:
        time_args = self._time_args(pack, ds)
        selector = component_selector(
            self.preferred_component, environment=self.environment_value
        )
        self.call("query_loki_stats", {**time_args, "logql": selector})

    def _scan_notification(self, query: str, ds: str) -> list[str]:
        """Run a notification LogQL query across all packs; return redacted lines."""
        out: list[str] = []
        for pack in self.packs:
            payload = self.call(
                "query_loki_logs",
                {**self._time_args(pack, ds), "logql": query, "limit": self.line_limit},
            )
            if payload is None:
                continue
            for _, line in _log_entries(payload):
                persisted, truncated = truncate_line(line, self.line_chars)
                redacted, n = redact_text(persisted, self._token)
                self.result.redactions += n
                if truncated:
                    self.result.lines_truncated += 1
                if redacted not in out:
                    out.append(redacted)
        return out

    def _notification_probe(self, ds: str) -> None:
        """Detect Notification-component completion for the request, incl. DEBUG.

        Runs only in request_id (single-DR) mode. The success markers
        ("successfully processed" / "mail sent to") are logged keyed by the
        correlation ``requestId`` — NOT by the DeliveryRequest URN — so a single
        URN filter misses them. Two-hop:

          1. query the Notification component for the URN (payload/context lines);
          2. extract the ``requestId`` from those lines and query the Notification
             component for that id, then scan for the markers.

        All queries omit the level filter so the DEBUG "successfully processed"
        marker is never dropped, regardless of INCLUDE_DEBUG_LOGS.
        """
        self.result.notification_checked = True
        self.result.notification_component = self.notification_component
        self.result.notification_lookback_hours = self.lookback_hours
        target = (
            self.urns[0]
            if self.urns
            else f"strn:distribution:DeliveryRequest:{self.request_id}"
        )
        selector = component_selector(
            self.notification_component, environment=self.environment_value
        )

        # Hop 1: notification lines that mention the URN (payload/context).
        context = self._scan_notification(
            f'{selector} |= "{_escape_label(target)}"', ds
        )

        # Resolve the correlation requestId(s) that tie the URN to the markers.
        request_ids = extract_request_ids(
            context, field=self.notification_request_id_field
        )
        self.result.notification_request_id = request_ids[0] if request_ids else None

        scanned = list(context)
        # Hop 2: notification lines for each requestId (where the markers live).
        for rid in request_ids[:MAX_NOTIFICATION_REQUEST_IDS]:
            for line in self._scan_notification(
                f'{selector} |= "{_escape_label(rid)}"', ds
            ):
                if line not in scanned:
                    scanned.append(line)

        self.result.notification_lines_scanned = len(scanned)
        matched_lines, matched_markers = match_success_markers(
            scanned, self.notification_markers
        )
        self.result.notification_success_lines = matched_lines[:NOTIFICATION_SUCCESS_CAP]
        self.result.notification_markers_matched = matched_markers
        self.result.notification_success = bool(matched_lines)
        self._notification_lines = scanned
        if matched_lines:
            self.result.notes.append(
                "Notification success detected ("
                + ", ".join(matched_markers)
                + f"; requestId={self.result.notification_request_id}); "
                "STGPT will be skipped"
            )
        else:
            rid_note = (
                f"requestId={self.result.notification_request_id}"
                if self.result.notification_request_id
                else "no requestId resolved from URN lines"
            )
            self.result.notes.append(
                "No Notification success marker found for the request "
                f"(component={self.notification_component}, "
                f"lines_scanned={self.result.notification_lines_scanned}, "
                f"{rid_note}, window=last {int(self.lookback_hours)}h)"
            )

    def _extract_trace_id(self) -> None:
        sources = (
            list(self.result.notification_success_lines)
            + list(getattr(self, "_notification_lines", []))
            + self._distribution_lines
            + self._kept_lines
        )
        trace_id = extract_trace_id(sources, field=self.trace_id_field)
        if trace_id:
            self.result.trace_id = trace_id

    def _find_tempo_tool(self) -> str | None:
        if self.available is None:
            return None
        for name in sorted(self.available):
            if name in WRITE_TOOLS or name.startswith(WRITE_TOOL_PREFIXES):
                continue
            low = name.lower()
            if any(hint in low for hint in TEMPO_TOOL_HINTS):
                return name
        return None

    def _query_tempo(self) -> None:
        tempo_uid = getattr(self.settings, "tempo_datasource_uid", None)
        self.result.tempo_datasource_uid = tempo_uid
        if not tempo_uid:
            self.result.notes.append("Tempo trace skipped: TEMPO_ID not set")
            return
        trace_id = self.result.trace_id
        if not trace_id:
            self.result.notes.append(
                "Tempo trace skipped: no trace id found in logs"
            )
            return
        tool = self._find_tempo_tool()
        if not tool:
            self.result.notes.append(
                "Tempo trace skipped: no read-only Tempo tool available"
            )
            return
        payload = self.call(
            tool, {"datasourceUid": tempo_uid, "traceId": trace_id}
        )
        spans = parse_trace_spans(payload)
        self.result.trace_spans = spans
        link = _deeplink_url(payload)
        if link:
            self.result.trace_deeplink = link
        if not spans:
            self.result.notes.append("Tempo trace: no spans returned")


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
