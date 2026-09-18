from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["FRESH", "STALE", "NO_RECORDS", "SRM_ERROR"]


class KeyMap(BaseModel):
    state: str | None = None
    urn: str | None = None
    updated_on: str | None = None

    def complete(self) -> bool:
        return bool(self.state and self.urn and self.updated_on)


class SrmRecord(BaseModel):
    urn: str | None = None
    state: str | None = None
    updated_on: str | None = None
    updated_on_utc: datetime | None = None
    age_hours: float | None = None
    stale: bool | None = None
    parse_error: str | None = None


class SrmResult(BaseModel):
    verdict: Verdict
    reason: str
    as_of: datetime
    cutoff: datetime
    timezone: str = "UTC"
    stale_hours: float = 24
    stale_mode: str = "any"
    states: list[str] = Field(default_factory=lambda: ["SUBMITTED", "GRANTED"])
    url: str | None = None
    key_map: KeyMap = Field(default_factory=KeyMap)
    records: list[SrmRecord] = Field(default_factory=list)
    key_inventory: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


ALLOWED_CITATION_SOURCES = {
    "first_error_window",
    "tail_window",
    "stack_traces",
    "log_templates",
    "junit",
    "change_context",
    "annotations",
    "history",
    "step_table",
    "srm_submitted",
    "srm_granted",
    "srm_record",
    "loki_logs",
    "loki_stats",
    "loki_patterns",
    "loki_labels",
    "grafana_dashboard",
    "grafana_datasource",
    "collection_notes",
}

AnalysisStatus = Literal[
    "ok",
    "cached",
    "gated",
    "parse_error",
    "citation_invalid",
    "bridge_error",
    "unusable",
]


class AnalysisCitation(BaseModel):
    quote: str
    source: str
    line: int | None = None


class AnalysisResult(BaseModel):
    root_cause: str
    suggested_fix: str
    confidence: Literal["high", "medium", "low"]
    citations: list[AnalysisCitation] = Field(default_factory=list)
    cannot_determine: bool = False


class AnalysisRecord(BaseModel):
    status: AnalysisStatus
    prompt_version: str = "srm.s3.1"
    persona: str | None = None
    fingerprint: str | None = None
    schema_version: str | None = "1.0"
    result: AnalysisResult | None = None
    cache_hit: bool = False
    fallback_used: bool = False
    response_id: str | None = None
    notes: list[str] = Field(default_factory=list)
    analyzed_at: datetime | None = None
    raw_completion: str | None = None
    stgpt_responses: list[dict[str, Any]] = Field(default_factory=list)
    tokens_used: int | None = None
    token_budget: int | None = None
    evidence_trimmed: bool = False
