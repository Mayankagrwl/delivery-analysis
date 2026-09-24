from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import SrmRecord, SrmResult
from .redact import redact_text

_RAW_CHARS = 2000


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "n/a"
    return f"{age_hours:.2f}"


def _fmt_stale(record: SrmRecord) -> str:
    if record.stale is None:
        return "n/a"
    return "yes" if record.stale else "no"


def _table(records: list[SrmRecord]) -> str:
    lines = [
        "| urn | updated.on | age_hours | stale? |",
        "|---|---|---|---|",
    ]
    if not records:
        lines.append("| (none) |  |  |  |")
        return "\n".join(lines)
    for rec in records:
        urn = rec.urn or ""
        updated = rec.updated_on or rec.parse_error or ""
        lines.append(
            f"| {urn} | {updated} | {_fmt_age(rec.age_hours)} | {_fmt_stale(rec)} |"
        )
    return "\n".join(lines)


def _sorted(records: list[SrmRecord]) -> list[SrmRecord]:
    def key(rec: SrmRecord) -> tuple:
        urn = rec.urn or ""
        digits = "".join(ch for ch in urn if ch.isdigit())
        number = int(digits) if digits else 0
        return (number, urn)

    return sorted(records, key=key)


def _grafana_inventory(grafana: Any | None, verdict: str) -> list[str]:
    parts = ["## Grafana / Loki inventory", ""]
    if grafana is None or getattr(grafana, "skipped", False):
        reason = None
        if grafana is not None:
            reason = getattr(grafana, "skip_reason", None)
        if verdict != "STALE":
            parts.append("skipped (verdict not STALE)")
        else:
            parts.append(reason or "skipped (Grafana collection not run)")
        parts.append("")
        return parts

    host = grafana.mcp_url_host or "(unknown host)"
    parts.append(f"- MCP host: {host}")
    parts.append(f"- Transport: {grafana.transport}")
    if grafana.latency_ms is not None:
        parts.append(f"- Latency: {grafana.latency_ms} ms")
    parts.append(
        f"- Dashboard: {grafana.dashboard_title} "
        f"(uid `{grafana.dashboard_uid}`, folder {grafana.dashboard_folder})"
    )
    if grafana.dashboard_url:
        parts.append(f"- Dashboard URL: {grafana.dashboard_url}")
    ds = grafana.loki_datasource_uid or "(not resolved)"
    ds_name = f" ({grafana.loki_datasource_name})" if grafana.loki_datasource_name else ""
    parts.append(f"- Loki datasource: `{ds}`{ds_name}")
    env = getattr(grafana, "env", None) or (grafana.filters or {}).get("env")
    if env:
        parts.append(f"- Dashboard env (not a Loki label): `{env}`")
    order = list(getattr(grafana, "component_order", None) or [])
    if order:
        parts.append("- Component order: " + ", then ".join(f"`{item}`" for item in order))
    parts.append(
        "- Level line filter: `LEVEL=(alert|error|warn|...)` then "
        "`LEVEL=(INFO|info)` fallback; debug only if INCLUDE_DEBUG_LOGS"
    )
    passes = getattr(grafana, "level_pass", None) or {}
    if passes:
        parts.append(
            "- Level pass: "
            + ", ".join(f"{key}={value}" for key, value in passes.items())
        )
    ranges = list(getattr(grafana, "time_ranges", None) or [])
    if not ranges and grafana.time_range:
        ranges = [grafana.time_range]
    if ranges:
        parts.append("- Time ranges:")
        for rng in ranges:
            label = rng.get("label") or ""
            prefix = f"{label}: " if label else ""
            parts.append(
                f"  - {prefix}{rng.get('start')} … {rng.get('end')}"
            )
    if grafana.urn_is_label is True:
        parts.append("- URN join: Loki label `urn`")
    elif grafana.urn_is_label is False:
        parts.append("- URN join: line filter on full URN string")
    if grafana.logql:
        parts.append("- LogQL:")
        for query in grafana.logql:
            parts.append(f"  - `{query}`")
    counts_level = getattr(grafana, "level_counts", None) or {}
    if counts_level:
        parts.append(
            "- Counts by level: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts_level.items()))
        )
    counts_comp = getattr(grafana, "component_counts", None) or {}
    if counts_comp:
        parts.append(
            "- Counts by component: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts_comp.items()))
        )
    parts.append(f"- Log lines kept: {grafana.lines_kept}")
    stale_urn_n = getattr(grafana, "lines_with_stale_urn", None)
    if stale_urn_n is not None:
        parts.append(f"- Lines containing a stale URN: {stale_urn_n}")
    truncated_n = getattr(grafana, "lines_truncated", None)
    if truncated_n is not None:
        parts.append(f"- Lines truncated: {truncated_n}")
    parts.append(f"- Log lines discarded (limit/dedup): {grafana.lines_discarded}")
    parts.append(f"- Redactions: {grafana.redactions}")
    if grafana.deeplinks:
        parts.append("- Deep links:")
        for link in grafana.deeplinks:
            parts.append(f"  - {link}")
    if grafana.tools:
        parts.append("- Tools:")
        parts.append("| tool | status | duration_ms | rows |")
        parts.append("|---|---|---|---|")
        for call in grafana.tools:
            rows = "" if call.row_count is None else str(call.row_count)
            parts.append(
                f"| {call.name} | {call.status} | {call.duration_ms:.0f} | {rows} |"
            )
    parts.append("")
    return parts


def _evidence_highlights(grafana: Any | None) -> list[str]:
    parts = ["## Evidence highlights", ""]
    if grafana is None or getattr(grafana, "skipped", False):
        reason = getattr(grafana, "skip_reason", None) if grafana is not None else None
        parts.append(reason or "skipped (Grafana collection not run)")
        parts.append("")
        return parts
    highlights = list(getattr(grafana, "highlights", None) or [])
    if not highlights:
        parts.append("(none)")
        parts.append("")
        return parts
    for line in highlights[:8]:
        parts.append(f"- `{line}`")
    parts.append("")
    return parts


def _ai_section(analysis: Any | None, verdict: str) -> list[str]:
    parts = ["## AI analysis", ""]
    if analysis is None:
        if verdict != "STALE":
            parts.append("skipped (verdict not STALE)")
        else:
            parts.append("skipped (analysis not run)")
        parts.append("")
        return parts
    parts.append(f"- status: `{analysis.status}`")
    if analysis.status == "investigate":
        parts.append("")
        parts.append(
            "_No AI analysis: the request is not stale and no Notification "
            "completion evidence was found — investigate._"
        )
        for note in list(getattr(analysis, "notes", None) or [])[:3]:
            parts.append(f"- {note}")
        parts.append("")
        return parts
    if analysis.status == "infra_success":
        parts.append("")
        parts.append(
            "> ✅ **SUCCESS** — request marked successful from infra side; "
            "no AI analysis needed."
        )
        parts.append("")
        parts.append("- confidence: not applicable")
        result = analysis.result
        if result is not None:
            parts.append(f"**Root cause:** {result.root_cause}")
            parts.append("")
            parts.append(f"**Suggested fix:** {result.suggested_fix}")
            if result.citations:
                parts.extend(["", "**Citations:**"])
                for cite in result.citations:
                    loc = cite.source + (f":{cite.line}" if cite.line else "")
                    parts.append(f"- `{cite.quote}` ({loc})")
        parts.append("")
        return parts
    if analysis.persona:
        parts.append(f"- persona: `{analysis.persona}`")
    if analysis.cache_hit:
        parts.append("- cache: hit")
    result = analysis.result
    if result is None:
        parts.append("- (no AnalysisResult)")
    else:
        parts.append(f"- confidence: {result.confidence}")
        parts.append(f"- cannot_determine: {str(result.cannot_determine).lower()}")
        parts.append("")
        parts.append(f"**Root cause:** {result.root_cause}")
        parts.append("")
        parts.append(f"**Suggested fix:** {result.suggested_fix}")
        if result.citations:
            parts.extend(["", "**Citations:**"])
            for cite in result.citations:
                loc = cite.source + (f":{cite.line}" if cite.line else "")
                parts.append(f"- `{cite.quote}` ({loc})")
    if analysis.tokens_used is not None:
        budget = analysis.token_budget if analysis.token_budget is not None else ""
        parts.extend(["", f"- token budget used: {analysis.tokens_used} / {budget}"])
    if analysis.prompt_version:
        parts.append(f"- prompt version: {analysis.prompt_version}")
    if analysis.status != "ok":
        parts.extend(_ai_raw_section(analysis))
    parts.append("")
    return parts


def _ai_raw_section(analysis: Any) -> list[str]:
    parts = ["", "### AI raw", ""]
    if analysis.persona:
        parts.append(f"- persona: `{analysis.persona}`")
    notes = list(getattr(analysis, "notes", None) or [])
    if notes:
        parts.append("- parse notes:")
        for note in notes:
            parts.append(f"  - {note}")
    raw = getattr(analysis, "raw_completion", None)
    if isinstance(raw, str) and raw.strip():
        redacted, _ = redact_text(raw)
        parts.append("- raw_completion (truncated):")
        parts.append("")
        parts.append("```")
        parts.append(redacted[:_RAW_CHARS])
        parts.append("```")
    return parts


def _notification_section(result: SrmResult, grafana: Any | None) -> list[str]:
    parts = ["## Infra success check (Notification)", ""]
    if not result.request_id:
        parts.append(
            "not applicable (no request_id; multi-record staleness run)"
        )
        parts.append("")
        return parts
    checked = bool(getattr(grafana, "notification_checked", False)) if grafana else False
    if not checked:
        parts.append(
            "not run (Grafana collection skipped for this verdict)"
        )
        parts.append("")
        return parts
    component = getattr(grafana, "notification_component", None) or "notification"
    success = getattr(grafana, "notification_success", None)
    if success:
        markers = getattr(grafana, "notification_markers_matched", None) or []
        parts.append(
            "**SUCCESS** — request marked successful from infra side — "
            "no AI analysis needed."
        )
        parts.append("")
        parts.append(f"- Notification component: `{component}`")
        parts.append(f"- Matched marker(s): {', '.join(markers) or '(unknown)'}")
        rid = getattr(grafana, "notification_request_id", None)
        if rid:
            parts.append(f"- Correlation requestId: `{rid}`")
        lines = getattr(grafana, "notification_success_lines", None) or []
        if lines:
            parts.append("- Notification log lines:")
            parts.append("")
            parts.append("```")
            parts.extend(lines)
            parts.append("```")
    else:
        lookback = getattr(grafana, "notification_lookback_hours", None)
        window = f" (window: last {int(lookback)}h)" if lookback else ""
        scanned = getattr(grafana, "notification_lines_scanned", 0)
        rid = getattr(grafana, "notification_request_id", None)
        if result.verdict == "STALE":
            parts.append(
                f"NOT FOUND — no `{component}` success marker for the request"
                f"{window}; genuine stale incident, STGPT analysis proceeded."
            )
        else:
            parts.append(
                f"No completion evidence found for id {result.request_id}"
                f"{window} — **investigate**. Request is not stale; no AI "
                "analysis was performed."
            )
        parts.append("")
        parts.append(f"- Notification component: `{component}`")
        parts.append(f"- Notification lines scanned: {scanned}")
        parts.append(
            "- Correlation requestId: "
            + (f"`{rid}`" if rid else "not resolved from URN lines")
        )
    parts.append("")
    return parts


def _trace_section(grafana: Any | None) -> list[str]:
    parts = ["## Request trace (Tempo)", ""]
    if grafana is None:
        parts.append("skipped (Grafana collection not run)")
        parts.append("")
        return parts
    trace_id = getattr(grafana, "trace_id", None)
    spans = list(getattr(grafana, "trace_spans", None) or [])
    if not spans:
        note = None
        for candidate in getattr(grafana, "notes", None) or []:
            if candidate.startswith("Tempo trace"):
                note = candidate
                break
        if trace_id:
            parts.append(f"- trace id: `{trace_id}`")
        parts.append(note or "no Tempo cascade available")
        parts.append("")
        return parts
    parts.append(f"- trace id: `{trace_id}`")
    tempo_uid = getattr(grafana, "tempo_datasource_uid", None)
    if tempo_uid:
        parts.append(f"- Tempo datasource: `{tempo_uid}`")
    parts.append("")
    parts.append("| component/service | span | status | duration_ms |")
    parts.append("|---|---|---|---|")
    for span in spans:
        duration = span.get("duration_ms")
        duration_text = "" if duration is None else str(duration)
        parts.append(
            f"| {span.get('service', '')} | {span.get('name', '')} | "
            f"{span.get('status', '')} | {duration_text} |"
        )
    deeplink = getattr(grafana, "trace_deeplink", None)
    if deeplink:
        parts.append("")
        parts.append(f"- Tempo deeplink: {deeplink}")
    parts.append("")
    return parts


def render_summary_md(
    result: SrmResult,
    grafana: Any | None = None,
    analysis: Any | None = None,
) -> str:
    submitted = _sorted([r for r in result.records if r.state == "SUBMITTED"])
    granted = _sorted([r for r in result.records if r.state == "GRANTED"])
    hours = (
        int(result.stale_hours)
        if result.stale_hours == int(result.stale_hours)
        else result.stale_hours
    )
    title = "# DeliveryRequest staleness"
    if result.environment:
        title += f" — {result.environment}"
    verdict_lines = [
        title,
        "",
        "## Verdict",
        "",
        f"**{result.verdict}** — {result.reason}",
    ]
    if result.request_outcome:
        verdict_lines.append("")
        verdict_lines.append(f"**Outcome:** {result.request_outcome}")
    parts = [
        *verdict_lines,
        "",
        "## Window",
        "",
        f"- Environment: {result.environment or '(default)'}",
        f"- T_now: {_iso(result.as_of)}",
        f"- Timezone: {result.timezone}",
        f"- Stale cutoff: {_iso(result.cutoff)} ({hours}h)",
        f"- STALE_MODE: {result.stale_mode}",
        f"- request_id scope: {result.request_id or '(all records)'}",
        "",
        "## SRM SUBMITTED",
        "",
        _table(submitted),
        "",
        "## SRM GRANTED",
        "",
        _table(granted),
        "",
    ]
    parts.extend(_grafana_inventory(grafana, result.verdict))
    parts.extend(_evidence_highlights(grafana))
    parts.extend(_notification_section(result, grafana))
    parts.extend(_trace_section(grafana))
    parts.extend(_ai_section(analysis, result.verdict))
    if result.notes:
        parts.extend(["## Collection notes", ""])
        parts.extend(f"- {note}" for note in result.notes)
        parts.append("")
    return "\n".join(parts)


def write_artifacts(
    result: SrmResult,
    out_dir: Path,
    grafana: Any | None = None,
    analysis: Any | None = None,
) -> None:
    from .analyze import write_analysis
    from .grafana import skipped_grafana

    out_dir.mkdir(parents=True, exist_ok=True)
    srm_path = out_dir / "srm.json"
    grafana_path = out_dir / "grafana.json"
    summary_path = out_dir / "summary.md"
    grafana_doc = grafana if grafana is not None else skipped_grafana(
        reason="Grafana collection not run"
    )
    srm_path.write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    grafana_path.write_text(
        grafana_doc.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    if analysis is not None:
        write_analysis(analysis, out_dir)
    summary_path.write_text(
        render_summary_md(result, grafana=grafana_doc, analysis=analysis),
        encoding="utf-8",
    )


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def render_index_md(
    results: dict[str, SrmResult],
    *,
    env_selection: str,
    request_id: str | None = None,
    env_reports: dict[str, str] | None = None,
) -> str:
    """Aggregated top-level index: one row per env, linking to its summary."""
    envs = list(results.keys())
    parts = [
        "# DeliveryRequest staleness — multi-environment",
        "",
        "## Run",
        "",
        f"- Environments: {env_selection}"
        + (f" ({', '.join(envs)})" if len(envs) > 1 else ""),
        f"- request_id scope: {request_id or '(all records)'}",
    ]
    as_of = next((r.as_of for r in results.values()), None)
    if as_of is not None:
        parts.append(f"- T_now: {_iso(as_of)} (UTC)")
    parts.extend(
        [
            "",
            "## Environments",
            "",
            "| env | verdict | reason | details |",
            "|---|---|---|---|",
        ]
    )
    for env in envs:
        result = results[env]
        link = f"[{env}/summary.md]({env}/summary.md)"
        reason = result.request_outcome or _one_line(result.reason)
        parts.append(
            f"| {env} | {result.verdict} | {_one_line(reason)} | {link} |"
        )
    parts.append("")

    # Embed each env's full per-env report so the Job Summary (which cats this
    # file) shows the Grafana/Loki evidence and AI analysis, not just the table.
    if env_reports:
        for env in envs:
            report = env_reports.get(env)
            if not report:
                continue
            parts.append(f"## Environment: {env}")
            parts.append("")
            parts.append(report.rstrip("\n"))
            parts.append("")
    return "\n".join(parts)


def write_index(
    out_dir: Path,
    results: dict[str, SrmResult],
    *,
    env_selection: str,
    request_id: str | None = None,
    env_reports: dict[str, str] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(
        render_index_md(
            results,
            env_selection=env_selection,
            request_id=request_id,
            env_reports=env_reports,
        ),
        encoding="utf-8",
    )
