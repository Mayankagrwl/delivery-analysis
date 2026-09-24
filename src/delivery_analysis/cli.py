from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import analyze_staleness
from .collect import run_collect_environments
from .config import load_settings
from .grafana import GrafanaResult
from .models import SrmResult
from .report import write_artifacts
from .timestamps import parse_as_of


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delivery_analysis",
        description=(
            "Daily SRM DeliveryRequest staleness check, Grafana Loki collection, "
            "and STGPT RCA (S1–S3)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser(
        "collect",
        help="Fetch DeliveryRequest records and write rca-srm/ verdict artifacts",
    )
    collect.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Frozen evaluation time UTC (ISO-8601), e.g. 2026-09-18T08:00:00Z",
    )
    collect.add_argument(
        "--out-dir",
        dest="out_dir",
        default="rca-srm",
        help="Artifact directory (default: rca-srm)",
    )
    collect.add_argument(
        "--url",
        default=None,
        help="Full SRM URL escape hatch; wins over SRM_BASE_URL and per-env URLs",
    )
    collect.add_argument(
        "--env",
        default=None,
        help=(
            "Environment to collect: one of test/int/qa/demo/prod, or ALL "
            "(case-insensitive). Default: SRM_ENV env var, else ALL."
        ),
    )
    collect.add_argument(
        "--request-id",
        dest="request_id",
        default=None,
        help=(
            "Scope the run to a single DeliveryRequest (bare number or full URN). "
            "Default: REQUEST_ID env var, else all records."
        ),
    )
    collect.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Exit non-zero on SRM_ERROR (default: STRICT env, otherwise false)",
    )
    collect.add_argument(
        "--analyze",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run STGPT analysis when verdict is STALE (default: true)",
    )
    analyze = sub.add_parser(
        "analyze",
        help="Run STGPT analysis from existing rca-srm/ artifacts",
    )
    analyze.add_argument(
        "--out-dir",
        dest="out_dir",
        default="rca-srm",
        help="Artifact directory (default: rca-srm)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        return _run_analyze_command(Path(args.out_dir))
    if args.command != "collect":
        return 2
    settings = load_settings(
        url=args.url,
        strict=args.strict,
        env=args.env,
        request_id=args.request_id,
    )
    multi = run_collect_environments(
        env=args.env,
        as_of=parse_as_of(args.as_of),
        out_dir=Path(args.out_dir),
        url=args.url,
        settings=settings,
        analyze=args.analyze,
        request_id=args.request_id,
    )
    print(f"environment={multi.env_selection}")
    for env, result in multi.results.items():
        print(f"env={env} verdict={result.verdict} reason={result.reason}")
    if multi.any_error and settings.strict:
        return 1
    return 0


def _run_analyze_command(out_dir: Path) -> int:
    srm_path = out_dir / "srm.json"
    grafana_path = out_dir / "grafana.json"
    if not srm_path.exists():
        print("srm.json is missing; run collect first")
        return 0
    srm = SrmResult.model_validate_json(srm_path.read_text(encoding="utf-8"))
    grafana = None
    if grafana_path.exists():
        grafana = GrafanaResult.model_validate_json(
            grafana_path.read_text(encoding="utf-8")
        )
    settings = load_settings()
    analysis = analyze_staleness(
        srm,
        grafana,
        url=settings.stgpt_api_url,
        client_app_name=settings.stgpt_client_app_name,
        token_budget=settings.token_budget,
    )
    write_artifacts(srm, out_dir, grafana=grafana, analysis=analysis)
    print(f"analysis_status={analysis.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
