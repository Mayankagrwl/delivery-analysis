from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from .analyze import (
    ChatFn,
    analyze_staleness,
    infra_success_record,
    investigate_record,
    skipped_analysis,
)
from .config import SRM_ENVIRONMENTS, Settings, load_settings, srm_url_for_env
from .grafana import GrafanaResult, collect_grafana, should_query_grafana, skipped_grafana
from .mcp_sse import McpClient
from .models import AnalysisRecord, SrmResult
from .report import write_artifacts, write_index
from .srm import SrmError, fetch_delivery_requests, require_credentials, safe_url
from .timestamps import UTC
from .verdict import error_result, evaluate_payload


@dataclass
class MultiEnvResult:
    """Outcome of a multi-environment (or single-env) collect run."""

    env_selection: str
    results: dict[str, SrmResult] = field(default_factory=dict)
    any_error: bool = False


def _select_environments(env: str | None, cfg: Settings) -> tuple[str, ...]:
    selection = (env or cfg.srm_env or "all").strip().lower()
    if selection in {"all", ""}:
        return SRM_ENVIRONMENTS
    # Validate the single env; srm_url_for_env raises ValueError on unknown.
    srm_url_for_env(selection, host=cfg.srm_base_host)
    return (selection,)


def run_collect_environments(
    *,
    env: str | None = None,
    as_of: datetime | None = None,
    out_dir: Path | str = "rca-srm",
    url: str | None = None,
    strict: bool | None = None,
    settings: Settings | None = None,
    mcp_client: McpClient | None = None,
    analyze: bool = True,
    chat_fn: ChatFn | None = None,
    cache_dir: Path | str | None = None,
    request_id: str | None = None,
) -> MultiEnvResult:
    """Run the collect/analyze pipeline for one env or, with ALL, every env.

    Each env writes its own artifacts under ``<out_dir>/<env>/`` and failures
    are isolated so one env's error never aborts the others. An aggregated
    top-level ``<out_dir>/summary.md`` index is always written.
    """
    cfg = settings or load_settings(url=url, strict=strict, env=env, request_id=request_id)
    scoped_request_id = request_id if request_id is not None else cfg.request_id
    targets = _select_environments(env, cfg)
    selection_label = (
        "ALL" if len(targets) == len(SRM_ENVIRONMENTS) and set(targets) == set(SRM_ENVIRONMENTS)
        else targets[0]
    )
    out_root = Path(out_dir)
    results: dict[str, SrmResult] = {}
    any_error = False
    # The full-URL escape hatch (--url / SRM_BASE_URL) applies to a single
    # explicitly-selected env only. In an ALL / multi-env run it would collapse
    # every env onto one URL (identical SRM data), so ignore it there and always
    # use each env's own computed URL.
    single_env = len(targets) == 1
    if not single_env and cfg.srm_base_url_override:
        print("SRM_BASE_URL override ignored in ALL mode; using per-env URLs")
    for target in targets:
        if single_env and cfg.srm_base_url_override:
            env_url = cfg.srm_base_url_override
        else:
            env_url = srm_url_for_env(target, host=cfg.srm_base_host)
        try:
            result = run_collect(
                as_of=as_of,
                out_dir=out_root / target,
                url=env_url,
                settings=cfg,
                mcp_client=mcp_client,
                analyze=analyze,
                chat_fn=chat_fn,
                cache_dir=cache_dir,
                environment=target,
                request_id=scoped_request_id,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one env's failure
            result = error_result(
                as_of=as_of or datetime.now(UTC),
                reason=f"collect failed for env {target}",
                stale_hours=cfg.stale_hours,
                stale_mode=cfg.stale_mode,
                states=cfg.srm_states,
                environment=target,
                request_id=scoped_request_id,
                notes=[exc.__class__.__name__],
            )
            write_artifacts(result, out_root / target)
        results[target] = result
        if result.verdict == "SRM_ERROR":
            any_error = True
        print(f"env={target} url={safe_url(env_url)} verdict={result.verdict}")

    # Collect each env's full per-env report (already written by write_artifacts)
    # so the top-level summary can embed it after the index table.
    env_reports: dict[str, str] = {}
    for target in targets:
        summary_path = out_root / target / "summary.md"
        if summary_path.exists():
            env_reports[target] = summary_path.read_text(encoding="utf-8")

    write_index(
        out_root,
        results,
        env_selection=selection_label,
        request_id=scoped_request_id,
        env_reports=env_reports,
    )
    return MultiEnvResult(
        env_selection=selection_label, results=results, any_error=any_error
    )


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
    environment: str | None = None,
    request_id: str | None = None,
) -> SrmResult:
    cfg = settings or load_settings(url=url, strict=strict, request_id=request_id)
    scoped_request_id = request_id if request_id is not None else cfg.request_id
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
            environment=environment,
            request_id=scoped_request_id,
        )
    except SrmError as exc:
        result = error_result(
            as_of=when,
            reason=str(exc),
            stale_hours=cfg.stale_hours,
            stale_mode=cfg.stale_mode,
            states=cfg.srm_states,
            url=safe,
            environment=environment,
            request_id=scoped_request_id,
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
            environment=environment,
            request_id=scoped_request_id,
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

    if scoped_request_id:
        if getattr(grafana, "notification_success", None) is True:
            result.request_outcome = "SUCCESS"
        elif result.verdict == "STALE":
            result.request_outcome = "STALE (AI analysis)"
        else:
            result.request_outcome = "INVESTIGATE (no completion evidence)"

    write_artifacts(result, out_path, grafana=grafana, analysis=analysis)
    return result


def _request_scoped(result: SrmResult, cfg: Settings) -> bool:
    return bool(result.request_id or cfg.request_id)


def _maybe_grafana(
    result: SrmResult,
    cfg: Settings,
    mcp_client: McpClient | None,
) -> GrafanaResult:
    if result.verdict == "SRM_ERROR":
        # SRM_ERROR still respects the explicit opt-in flag, request_id or not.
        if not cfg.query_grafana_on_srm_error:
            return skipped_grafana(reason="skipped (verdict SRM_ERROR)")
    elif _request_scoped(result, cfg):
        # request_id mode: always probe Notification + Tempo, any verdict.
        pass
    elif not should_query_grafana(
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
    # Infra success wins in every mode, including a request_id run that is also
    # STALE (success beats AI analysis).
    if getattr(grafana, "notification_success", None) is True:
        return infra_success_record(grafana)
    if result.verdict == "STALE":
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
    if _request_scoped(result, cfg):
        # Not stale and no completion evidence: investigate, never call STGPT.
        return investigate_record(
            result.request_id or cfg.request_id,
            getattr(cfg, "request_id_lookback_hours", None),
        )
    return skipped_analysis(reason="skipped (verdict not STALE)")
