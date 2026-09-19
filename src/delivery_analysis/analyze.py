"""STGPT analyze loop: trinity → repair → alfred. Never raises out of the job."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    PERSONAS,
    PROMPT_VERSION,
    TOKEN_BUDGET_TOTAL,
    resolve_stgpt_api_key,
    resolve_stgpt_api_url,
    resolve_stgpt_client_app_name,
)
from .models import (
    ALLOWED_CITATION_SOURCES,
    AnalysisCitation,
    AnalysisRecord,
    AnalysisResult,
    AnalysisStatus,
    SrmResult,
)
from .prompt import _record_line, build_evidence, build_messages
from .redact import redact_text, redact_walk
from .stgpt_client import (
    ChatResult,
    StgptError,
    bridge_error_message,
    flatten_user_content,
    post_chat,
    public_request_url,
)

_LOG = logging.getLogger(__name__)
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_PREVIEW_CHARS = 240
_RAW_CHARS = 2000
_KEY_ALIASES = {
    "rootCause": "root_cause",
    "root_cause": "root_cause",
    "cause": "root_cause",
    "diagnosis": "root_cause",
    "suggestedFix": "suggested_fix",
    "suggested_fix": "suggested_fix",
    "fix": "suggested_fix",
    "cannotDetermine": "cannot_determine",
    "cannot_determine": "cannot_determine",
    "confidence": "confidence",
    "citations": "citations",
}
ChatFn = Callable[[str, Sequence[Mapping[str, str]]], ChatResult]


class _Outcome:
    def __init__(self, status: str, why: str, result: AnalysisResult | None) -> None:
        self.status = status
        self.why = why
        self.result = result


def cache_key(srm: SrmResult, grafana: Any | None) -> str:
    urns = ",".join(sorted(rec.urn or "" for rec in srm.records if rec.stale))
    logql = ""
    if grafana is not None:
        logql = "|".join(getattr(grafana, "logql", None) or [])
    raw = f"{srm.verdict}|{urns}|{logql}|{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def analyze_staleness(
    srm: SrmResult,
    grafana: Any | None = None,
    *,
    api_key: str | None = None,
    url: str | None = None,
    client_app_name: str | None = None,
    chat_fn: ChatFn | None = None,
    cache_dir: Path | str | None = None,
    token_budget: int | None = None,
) -> AnalysisRecord:
    now = datetime.now(timezone.utc)
    cap = TOKEN_BUDGET_TOTAL if token_budget is None else token_budget
    key = cache_key(srm, grafana)
    base = AnalysisRecord(
        status="unusable",
        prompt_version=PROMPT_VERSION,
        fingerprint=key,
        schema_version="1.0",
        analyzed_at=now,
        token_budget=cap,
    )
    try:
        if srm.verdict != "STALE":
            return base.model_copy(
                update={
                    "status": "gated",
                    "notes": [f"skipped: verdict {srm.verdict} is not STALE"],
                }
            )
        cached = _cache_get(cache_dir, key)
        if cached is not None:
            return cached.model_copy(
                update={
                    "status": "cached",
                    "cache_hit": True,
                    "analyzed_at": now,
                    "prompt_version": PROMPT_VERSION,
                    "fingerprint": key,
                }
            )
        if chat_fn is None and not resolve_stgpt_api_key(api_key):
            return base.model_copy(
                update={
                    "status": "gated",
                    "notes": ["skipped: missing STGPT_API"],
                }
            )
        evidence, tokens_used, trimmed = build_evidence(srm, grafana, cap_tokens=cap)
        base = base.model_copy(
            update={"tokens_used": tokens_used, "evidence_trimmed": trimmed}
        )
        caller = chat_fn or _make_chat_fn(
            api_key=api_key, url=url, client_app_name=client_app_name
        )
        record = _run_personas(evidence, caller, base, srm=srm, grafana=grafana)
        redacted = _redact_record(record)
        if redacted.status == "ok":
            _cache_put(cache_dir, key, redacted)
        return redacted
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("analyze error")
        note, _ = redact_text(f"{exc.__class__.__name__}")
        fallback = None
        if _has_grounding(srm, grafana):
            try:
                evidence, _, _ = build_evidence(srm, grafana, cap_tokens=cap)
            except Exception:  # noqa: BLE001
                evidence = ""
            fallback = _fallback_result(srm, grafana, evidence)
        return _redact_record(
            base.model_copy(
                update={"status": "unusable", "notes": [note], "result": fallback}
            )
        )


def write_analysis(record: AnalysisRecord, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = json.loads(record.model_dump_json())
    dump.pop("stgpt_responses", None)
    if record.status == "ok":
        dump.pop("raw_completion", None)
    else:
        raw = dump.get("raw_completion")
        if isinstance(raw, str):
            redacted, _ = redact_text(raw)
            dump["raw_completion"] = redacted[:_RAW_CHARS]
    (out_dir / "analysis.json").write_text(
        json.dumps(dump, indent=2) + "\n", encoding="utf-8"
    )


def _make_chat_fn(
    *,
    api_key: str | None,
    url: str | None,
    client_app_name: str | None,
) -> ChatFn:
    key = resolve_stgpt_api_key(api_key)
    if not key:
        raise StgptError("missing STGPT_API")
    bridge = resolve_stgpt_api_url(url)
    app = resolve_stgpt_client_app_name(client_app_name)

    def _call(persona: str, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        return post_chat(bridge, key, app, persona, messages)

    return _call


def _run_personas(
    evidence: str,
    chat_fn: ChatFn,
    base: AnalysisRecord,
    *,
    srm: SrmResult,
    grafana: Any | None,
) -> AnalysisRecord:
    notes: list[str] = []
    last_id: str | None = None
    last_completion: str | None = None
    last_persona: str | None = None
    last_outcome: _Outcome | None = None
    fallback_used = False
    saw_http_ok = False
    saw_transport = False

    prompt_text = flatten_user_content(build_messages(evidence))
    if not prompt_text:
        if not _has_grounding(srm, grafana):
            return _record(
                base,
                status="unusable",
                persona=None,
                result=None,
                fallback_used=False,
                response_id=None,
                notes=["prompt_empty"],
                raw_completion=None,
            )
        notes.append(
            "prompt looked empty; continuing because SRM/Loki evidence exists"
        )

    for index, persona in enumerate(PERSONAS):
        last_persona = persona
        if index > 0:
            fallback_used = True
            notes.append(f"fallback to {persona}")

        messages = build_messages(evidence)
        chat = _call_chat(chat_fn, persona, messages, notes)
        if chat is None:
            saw_transport = True
            if any("prompt_empty" in note for note in notes) and not _has_grounding(
                srm, grafana
            ):
                return _record(
                    base,
                    status="unusable",
                    persona=persona,
                    result=None,
                    fallback_used=fallback_used,
                    response_id=None,
                    notes=notes,
                    raw_completion=None,
                )
            continue
        if not _is_2xx(chat.status_code):
            notes.append(_http_failure_note(chat, persona))
            continue
        saw_http_ok = True
        last_id = chat.response_id
        notes.append(_response_meta_note(chat, persona, messages))
        api_error = bridge_error_message(chat.body)
        if api_error:
            notes.append(f"{persona}: {api_error}")
            continue
        completion = chat.completion
        if not (isinstance(completion, str) and completion.strip()):
            notes.append(f"{persona}: empty completion")
            continue

        last_completion = completion
        outcome = _interpret_completion(completion, evidence)
        last_outcome = outcome
        if outcome.status == "ok" and outcome.result is not None:
            return _record(
                base,
                status="ok",
                persona=persona,
                result=outcome.result,
                fallback_used=fallback_used,
                response_id=last_id,
                notes=notes,
                raw_completion=completion,
            )

        notes.append(_parse_note(persona, outcome.why, completion))
        repair_messages = build_messages(
            evidence,
            prior_completion=completion,
            repair_reason=outcome.why,
        )
        repaired = _call_chat(chat_fn, persona, repair_messages, notes)
        if repaired is None:
            saw_transport = True
            continue
        if not _is_2xx(repaired.status_code):
            notes.append(_http_failure_note(repaired, persona))
            continue
        saw_http_ok = True
        last_id = repaired.response_id
        notes.append(_response_meta_note(repaired, persona, repair_messages))
        repair_error = bridge_error_message(repaired.body)
        if repair_error:
            notes.append(f"{persona} repair: {repair_error}")
            continue
        if not (isinstance(repaired.completion, str) and repaired.completion.strip()):
            notes.append(f"{persona} repair: empty completion")
            continue
        last_completion = repaired.completion
        outcome = _interpret_completion(repaired.completion, evidence)
        last_outcome = outcome
        if outcome.status == "ok" and outcome.result is not None:
            return _record(
                base,
                status="ok",
                persona=persona,
                result=outcome.result,
                fallback_used=fallback_used,
                response_id=last_id,
                notes=notes,
                raw_completion=last_completion,
            )
        notes.append(_parse_note(f"{persona} repair", outcome.why, last_completion))

    status: AnalysisStatus
    if last_outcome is not None and last_outcome.status in {
        "parse_error",
        "citation_invalid",
    }:
        status = last_outcome.status  # type: ignore[assignment]
    elif not saw_http_ok and saw_transport:
        status = "bridge_error"
    elif not saw_http_ok:
        status = "bridge_error"
    else:
        status = "unusable"
    result = last_outcome.result if last_outcome else None
    if result is None and _has_grounding(srm, grafana):
        result = _fallback_result(srm, grafana, evidence)
        notes.append("deterministic fallback after personas failed")
    return _record(
        base,
        status=status,
        persona=last_persona,
        result=result,
        fallback_used=fallback_used,
        response_id=last_id,
        notes=notes or ["analyze failed"],
        raw_completion=last_completion,
    )


def _has_grounding(srm: SrmResult, grafana: Any | None) -> bool:
    if any(rec.stale for rec in srm.records):
        return True
    if grafana is None:
        return False
    if getattr(grafana, "lines_kept", 0):
        return True
    highlights = getattr(grafana, "highlights", None) or []
    return bool(highlights)


def _fallback_result(
    srm: SrmResult, grafana: Any | None, evidence: str
) -> AnalysisResult:
    stale = [rec for rec in srm.records if rec.stale]
    bits: list[str] = []
    citations: list[AnalysisCitation] = []
    for rec in stale:
        age = rec.age_hours if rec.age_hours is not None else "n/a"
        bits.append(
            f"{rec.urn or '(unknown)'} state={rec.state or ''} "
            f"updated.on={rec.updated_on or ''} age_hours={age}"
        )
        quote = rec.urn or _record_line(rec)
        if quote and quote in evidence:
            citations.append(AnalysisCitation(quote=quote, source="srm_record"))
        else:
            line = _record_line(rec)
            if line in evidence:
                citations.append(AnalysisCitation(quote=line, source="srm_record"))
    loki_lines = list(getattr(grafana, "highlights", None) or []) if grafana else []
    for line in loki_lines:
        if line and line in evidence:
            citations.append(AnalysisCitation(quote=line, source="loki_logs"))
    if bits:
        root = (
            "STGPT did not return a usable analysis. Stale DeliveryRequest records: "
            + "; ".join(bits)
            + "."
        )
    else:
        root = "STGPT did not return a usable analysis."
    if loki_lines:
        root += " Loki lines: " + " | ".join(loki_lines[:5]) + "."
    else:
        root += " No Loki lines were returned in the queried windows."
    if loki_lines:
        suggested = (
            "Inspect the cited Loki lines for the stall, then replay or unlock the "
            "stale URNs and verify the grant/submit path around each updated.on."
        )
    else:
        suggested = (
            "Inspect the stale URNs in SRM; query Loki around each updated.on ±2h "
            "and the last 24h; replay or unlock stuck SUBMITTED/GRANTED requests "
            "and check grant/submit workers and downstream dependencies."
        )
    return AnalysisResult(
        root_cause=root,
        suggested_fix=suggested,
        confidence="low",
        citations=citations,
        cannot_determine=True,
    )


def _call_chat(
    chat_fn: ChatFn,
    persona: str,
    messages: Sequence[Mapping[str, str]],
    notes: list[str],
) -> ChatResult | None:
    try:
        return chat_fn(persona, messages)
    except StgptError as exc:
        if str(exc) == "prompt_empty":
            notes.append(f"{persona}: prompt_empty")
        else:
            note, _ = redact_text(f"{persona}: bridge error: {exc}")
            notes.append(note)
        return None


def _is_2xx(status_code: int) -> bool:
    return 200 <= status_code < 300


def _record(
    base: AnalysisRecord,
    *,
    status: AnalysisStatus,
    persona: str | None,
    result: AnalysisResult | None,
    fallback_used: bool,
    response_id: str | None,
    notes: list[str],
    raw_completion: str | None,
) -> AnalysisRecord:
    return base.model_copy(
        update={
            "status": status,
            "persona": persona,
            "result": result,
            "fallback_used": fallback_used,
            "response_id": response_id,
            "notes": notes,
            "raw_completion": raw_completion,
        }
    )


def _http_failure_note(chat: ChatResult, persona: str) -> str:
    loc = public_request_url(chat.url)
    parts = [f"HTTP {chat.status_code}", f"persona={persona}"]
    if loc:
        parts.append(f"url={loc}")
    note, _ = redact_text(" ".join(parts))
    return note


def _response_meta_note(
    chat: ChatResult,
    persona: str,
    messages: Sequence[Mapping[str, str]],
) -> str:
    completion = chat.completion if isinstance(chat.completion, str) else ""
    uchars = chat.user_message_chars
    if uchars is None:
        uchars = len(flatten_user_content(messages))
    rid = chat.response_id or ""
    note = (
        f"{persona}: completion_len={len(completion)} "
        f"responseId={rid} user_message_chars={uchars}"
    )
    redacted, _ = redact_text(note)
    return redacted


def _parse_note(label: str | None, why: str, completion: str) -> str:
    preview, _ = redact_text(completion)
    preview = " ".join(preview.split())[:_PREVIEW_CHARS]
    note, _ = redact_text(f"{label}: {why}; completion_preview={preview}")
    return note


def _interpret_completion(completion: str, evidence: str) -> _Outcome:
    payload = _first_json_object(_strip_fences(completion))
    if not isinstance(payload, dict):
        return _Outcome("parse_error", "parse_error: no JSON object", None)
    normalized = _normalize_payload(payload)
    try:
        result = AnalysisResult.model_validate(normalized)
    except Exception as exc:
        return _Outcome("parse_error", f"parse_error: {exc}", None)

    if not result.citations:
        return _Outcome("citation_invalid", "missing citations", result)
    bad_source = [
        cite.source for cite in result.citations if cite.source not in ALLOWED_CITATION_SOURCES
    ]
    if bad_source:
        return _Outcome(
            "citation_invalid",
            "unknown citation source: " + ", ".join(bad_source[:3]),
            result,
        )
    missing = [
        cite.quote
        for cite in result.citations
        if cite.quote and cite.quote not in evidence
    ]
    if missing:
        return _Outcome(
            "citation_invalid",
            "ungrounded: " + "; ".join(missing[:3]),
            result,
        )
    return _Outcome("ok", "ok", result)


def _strip_fences(text: str) -> str:
    match = _FENCE_BLOCK.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in payload.items():
        mapped[_KEY_ALIASES.get(str(key), str(key))] = value
    root = mapped.get("root_cause")
    if not isinstance(root, str):
        for key in ("rootCause", "cause", "diagnosis"):
            value = payload.get(key)
            if isinstance(value, str):
                mapped["root_cause"] = value
                break
    fix = mapped.get("suggested_fix")
    if not isinstance(fix, str):
        for key in ("suggestedFix", "fix"):
            value = payload.get(key)
            if isinstance(value, str):
                mapped["suggested_fix"] = value
                break
        else:
            mapped["suggested_fix"] = ""
    mapped["confidence"] = _confidence(mapped.get("confidence"))
    mapped["citations"] = _normalize_citations(mapped.get("citations"))
    flag = mapped.get("cannot_determine")
    if isinstance(flag, str):
        mapped["cannot_determine"] = flag.strip().lower() in {"1", "true", "yes"}
    elif flag is None:
        mapped["cannot_determine"] = False
    else:
        mapped["cannot_determine"] = bool(flag)
    return mapped


def _confidence(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in {"high", "medium", "low"}:
        return value.strip().lower()
    return "low"


def _normalize_citations(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        quote = item.get("quote") or item.get("text") or ""
        source = item.get("source") or item.get("section")
        if not isinstance(quote, str) or not quote:
            continue
        if not isinstance(source, str):
            continue
        line = item.get("line")
        try:
            line_n = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_n = None
        out.append({"quote": quote, "source": source, "line": line_n})
    return out


def _redact_record(record: AnalysisRecord) -> AnalysisRecord:
    payload = record.model_dump(mode="json")
    cleaned, _ = redact_walk(payload)
    return AnalysisRecord.model_validate(cleaned)


def _cache_path(cache_dir: Path | str | None, key: str) -> Path | None:
    if cache_dir is None:
        return None
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{key}.json"


def _cache_get(cache_dir: Path | str | None, key: str) -> AnalysisRecord | None:
    path = _cache_path(cache_dir, key)
    if path is None or not path.exists():
        return None
    try:
        record = AnalysisRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.status not in {"ok", "cached"} or record.result is None:
        return None
    return record


def _cache_put(cache_dir: Path | str | None, key: str, record: AnalysisRecord) -> None:
    path = _cache_path(cache_dir, key)
    if path is None:
        return
    dump = json.loads(record.model_dump_json())
    dump.pop("raw_completion", None)
    dump.pop("stgpt_responses", None)
    path.write_text(json.dumps(dump, indent=2) + "\n", encoding="utf-8")


def skipped_analysis(*, reason: str) -> AnalysisRecord:
    return AnalysisRecord(
        status="gated",
        prompt_version=PROMPT_VERSION,
        notes=[reason],
        analyzed_at=datetime.now(timezone.utc),
    )
