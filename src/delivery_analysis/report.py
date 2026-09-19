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
    parts.append(f"- Log lines kept: {grafana.lines_kept}")
    parts.append(f"- Log lines discarded (limit/truncate): {grafana.lines_discarded}")
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
    parts = [
        "# DeliveryRequest staleness",
        "",
        "## Verdict",
        "",
        f"**{result.verdict}** — {result.reason}",
        "",
        "## Window",
        "",
        f"- T_now: {_iso(result.as_of)}",
        f"- Timezone: {result.timezone}",
        f"- Stale cutoff: {_iso(result.cutoff)} ({hours}h)",
        f"- STALE_MODE: {result.stale_mode}",
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
