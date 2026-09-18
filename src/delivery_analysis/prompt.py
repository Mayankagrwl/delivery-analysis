"""Evidence wrapper + JSON-only STGPT contract for DeliveryRequest RCA."""

from __future__ import annotations

from typing import Any

from .budget import token_count, trim_middle
from .config import PROMPT_VERSION, TOKEN_BUDGET_TOTAL
from .models import ALLOWED_CITATION_SOURCES, SrmResult

SYSTEM_PROMPT = (
    f"You are a distribution / SRM DeliveryRequest RCA assistant (prompt {PROMPT_VERSION}). "
    "Use only the text inside <EVIDENCE>. "
    "Do not invent URNs, timestamps, LogQL, or dashboard names. "
    "Explain why DeliveryRequest records may be stuck in SUBMITTED or GRANTED past 24h. "
    "Solutions must be operational (replay, unlock, downstream dependency, auth, quota, "
    "Loki-confirmed error class) and tied to citations. "
    "Reply with ONLY a single JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause (string), suggested_fix (string), "
    "confidence (high|medium|low), "
    "citations (array of {quote, source, line}), cannot_determine (boolean). "
    "Each citations[].quote MUST be a verbatim substring of <EVIDENCE>. "
    "source must be one of: " + ", ".join(sorted(ALLOWED_CITATION_SOURCES)) + "."
)

_REPAIR = (
    "Your previous reply was not valid JSON: {reason}. "
    "Reply with ONLY the JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause, suggested_fix, confidence, citations, cannot_determine. "
    "citations[].quote must be copied verbatim from <EVIDENCE>."
)


def build_evidence(
    srm: SrmResult,
    grafana: Any | None = None,
    *,
    cap_tokens: int | None = None,
) -> tuple[str, int, bool]:
    cap = TOKEN_BUDGET_TOTAL if cap_tokens is None else cap_tokens
    body = "\n".join(_evidence_lines(srm, grafana))
    trimmed, did_trim, _before, after = trim_middle(body, cap)
    return f"<EVIDENCE>\n{trimmed}\n</EVIDENCE>", after, did_trim


def build_messages(
    evidence: str,
    *,
    prior_completion: str | None = None,
    repair_reason: str | None = None,
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{evidence}\n\n"
                "Diagnose why these DeliveryRequests are stale. "
                "JSON only, citations from <EVIDENCE>."
            ),
        },
    ]
    if prior_completion:
        messages.append({"role": "assistant", "content": prior_completion})
        messages.append(
            {
                "role": "user",
                "content": _REPAIR.format(reason=repair_reason or "validation failed"),
            }
        )
    return messages


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _evidence_lines(srm: SrmResult, grafana: Any | None) -> list[str]:
    lines = [
        f"verdict: {srm.verdict}",
        f"reason: {srm.reason}",
        f"T_now: {_iso(srm.as_of)}",
        f"cutoff: {_iso(srm.cutoff)}",
        f"timezone: {srm.timezone}",
        f"stale_hours: {srm.stale_hours}",
        f"stale_mode: {srm.stale_mode}",
        "### srm_submitted",
    ]
    submitted = [r for r in srm.records if r.state == "SUBMITTED"]
    granted = [r for r in srm.records if r.state == "GRANTED"]
    if not submitted:
        lines.append("(none)")
    for rec in submitted:
        lines.append(_record_line(rec))
    lines.append("### srm_granted")
    if not granted:
        lines.append("(none)")
    for rec in granted:
        lines.append(_record_line(rec))
    lines.append("### srm_record")
    for rec in srm.records:
        lines.append(_record_line(rec))

    if grafana is None or getattr(grafana, "skipped", False):
        reason = getattr(grafana, "skip_reason", None) if grafana is not None else None
        lines.append("### collection_notes")
        lines.append(reason or "Grafana collection not run")
        return lines

    lines.append("### grafana_dashboard")
    lines.append(
        f"title={grafana.dashboard_title} uid={grafana.dashboard_uid} "
        f"folder={grafana.dashboard_folder}"
    )
    if grafana.dashboard_url:
        lines.append(f"url={grafana.dashboard_url}")
    lines.append("### grafana_datasource")
    lines.append(
        f"uid={grafana.loki_datasource_uid or ''} "
        f"name={grafana.loki_datasource_name or ''}"
    )
    lines.append("### loki_labels")
    lines.append(f"urn_is_label={grafana.urn_is_label}")
    if grafana.filters:
        lines.append("filters=" + ",".join(f"{k}={v}" for k, v in grafana.filters.items()))
    lines.append("### loki_stats")
    if grafana.time_range:
        lines.append(
            f"time_range {grafana.time_range.get('start')} … {grafana.time_range.get('end')}"
        )
    lines.append(f"lines_kept={grafana.lines_kept} lines_discarded={grafana.lines_discarded}")
    if grafana.logql:
        for query in grafana.logql:
            lines.append(f"logql: {query}")
    lines.append("### loki_logs")
    highlights = list(getattr(grafana, "highlights", None) or [])
    if not highlights:
        lines.append("(none)")
    for line in highlights:
        lines.append(line)
    lines.append("### loki_patterns")
    pattern_notes = [
        call.error or call.name
        for call in getattr(grafana, "tools", []) or []
        if getattr(call, "name", "") == "query_loki_patterns"
    ]
    if pattern_notes:
        lines.extend(str(item) for item in pattern_notes)
    else:
        lines.append("(none)")
    lines.append("### collection_notes")
    notes = list(srm.notes) + list(getattr(grafana, "notes", None) or [])
    if not notes:
        lines.append("(none)")
    else:
        lines.extend(notes)
    return [line for line in lines if line is not None]


def _record_line(rec: Any) -> str:
    stale = "stale" if rec.stale else "fresh"
    age = rec.age_hours if rec.age_hours is not None else "n/a"
    return (
        f"{rec.urn or ''} state={rec.state or ''} "
        f"updated.on={rec.updated_on or ''} age_hours={age} {stale}"
    )


def evidence_token_count(evidence: str) -> int:
    return token_count(evidence)
