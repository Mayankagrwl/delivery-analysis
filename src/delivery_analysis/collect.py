from __future__ import annotations

from datetime import datetime
from pathlib import Path
from .analyze import ChatFn, analyze_staleness, skipped_analysis
from .config import Settings, load_settings
from .grafana import GrafanaResult, collect_grafana, should_query_grafana, skipped_grafana
from .mcp_sse import McpClient
from .models import AnalysisRecord, SrmResult
from .report import write_artifacts
from .srm import SrmError, fetch_delivery_requests, require_credentials, safe_url
from .timestamps import UTC
from .verdict import error_result, evaluate_payload


def run_collect(
    *,
    as_of: datetime | None = None,
    out_dir: Path | str = "rca-srm",
    url: str | None = None,
    strict: bool | None = None,
    settings: Settings | None = None,
    mcp_client: McpClient | None = None,
    analyze: bool = True,
    chat_fn: ChatFn | None = None,
    cache_dir: Path | str | None = None,
) -> SrmResult:
    cfg = settings or load_settings(url=url, strict=strict)
    when = as_of or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    else:
        when = when.astimezone(UTC)
    target_url = url or cfg.srm_base_url
    safe = safe_url(target_url)
    out_path = Path(out_dir)

    try:
        user, password = require_credentials(cfg)
        payload = fetch_delivery_requests(
            target_url,
            user,
            password,
            verify=cfg.tls_verify,
            timeout=cfg.timeout_seconds,
        )
        result = evaluate_payload(
            payload,
            as_of=when,
            stale_hours=cfg.stale_hours,
            stale_mode=cfg.stale_mode,
            states=cfg.srm_states,
            url=safe,
        )
    except SrmError as exc:
        result = error_result(
            as_of=when,
            reason=str(exc),
            stale_hours=cfg.stale_hours,
            stale_mode=cfg.stale_mode,
            states=cfg.srm_states,
            url=safe,
            notes=[str(exc)],
        )
    except Exception as exc:
        result = error_result(
            as_of=when,
            reason="SRM collect failed",
            stale_hours=cfg.stale_hours,
            stale_mode=cfg.stale_mode,
            states=cfg.srm_states,
            url=safe,
            notes=[exc.__class__.__name__],
        )

    grafana = _maybe_grafana(result, cfg, mcp_client)
    if grafana.notes and not grafana.skipped:
        result.notes.extend(grafana.notes)
    if not grafana.skipped:
        host = grafana.mcp_url_host or "unknown"
        print(f"grafana_host={host} transport={grafana.transport}")
        print(f"grafana_tools={len(grafana.tools)} lines_kept={grafana.lines_kept}")

    analysis = _maybe_analyze(
        result,
        grafana,
        cfg,
        analyze=analyze,
        chat_fn=chat_fn,
        cache_dir=cache_dir,
    )
    if analysis is not None:
        print(f"analysis_status={analysis.status}")

    write_artifacts(result, out_path, grafana=grafana, analysis=analysis)
    return result


def _maybe_grafana(
    result: SrmResult,
    cfg: Settings,
    mcp_client: McpClient | None,
) -> GrafanaResult:
    if result.verdict in {"FRESH", "NO_RECORDS"}:
        return skipped_grafana(reason="skipped (verdict not STALE)")
    if result.verdict == "SRM_ERROR" and not cfg.query_grafana_on_srm_error:
        return skipped_grafana(reason="skipped (verdict not STALE)")
    if not should_query_grafana(
        result.verdict, on_srm_error=cfg.query_grafana_on_srm_error
    ):
        return skipped_grafana(reason="skipped (verdict not STALE)")
    try:
        return collect_grafana(result, cfg, client=mcp_client)
    except Exception as exc:
        return GrafanaResult(
            mcp_url_host=None,
            transport="sse",
            notes=[f"Grafana collection failed: {exc.__class__.__name__}"],
        )


def _maybe_analyze(
    result: SrmResult,
    grafana: GrafanaResult,
    cfg: Settings,
    *,
    analyze: bool,
    chat_fn: ChatFn | None,
    cache_dir: Path | str | None,
) -> AnalysisRecord | None:
    if not analyze:
        return None
    if result.verdict != "STALE":
        return skipped_analysis(reason="skipped (verdict not STALE)")
    try:
        return analyze_staleness(
            result,
            grafana,
            url=cfg.stgpt_api_url,
            client_app_name=cfg.stgpt_client_app_name,
            chat_fn=chat_fn,
            cache_dir=cache_dir,
            token_budget=cfg.token_budget,
        )
    except Exception as exc:
        return AnalysisRecord(
            status="unusable",
            notes=[exc.__class__.__name__],
        )
