# delivery-analysis — agent rules

This is **delivery-analysis**: daily SRM `DeliveryRequest` staleness + Grafana Loki RCA + STGPT.

This repo is a **new project**. Do not fold it into `ci-rca-collector`. Vendor the STGPT client later (do not copy Drain3, GitHub Actions log collection, or kubectl).

Spec: `docs/PRD-daily-delivery-request-staleness-rca.md` (v1.2). Follow it. Do not invent extra product scope.

## What this product does

Daily check of ST SRM `DeliveryRequest` records in states `SUBMITTED` and `GRANTED`. If any row is older than 24 hours (UTC), pull Grafana Loki evidence via MCP SSE (`Service Logs` UID `d68f5a4d-72e6-4b16-b166-a70f41f3cd49`) and send packed evidence to ST ChatGPT using the same bridge contract as `ci-rca-collector`. Write `rca-srm/summary.md`.

Empty queue (`NO_RECORDS`) is not an incident: no Grafana, no STGPT.

## Hosting

- Phase A: develop on **github.com** with Grok CLI. Workflow is `workflow_dispatch` only. **No cron.**
- Phase B: move the same tree to **github.st.com**. Enable `30 2 * * *` UTC there.
- Never hardcode `github.com` or `https://api.github.com` as the only API. Use `GITHUB_SERVER_URL` / `GITHUB_API_URL`. If the host is not github.com, API base is `{server}/api/v3`.

## Multi-environment SRM + workflow inputs

- Five environments: `test`, `int`, `qa`, `demo`, `prod`. SRM URLs are **derived per env** from `SRM_BASE_HOST` (default `https://trd.st.com`) via `config.srm_url_for_env` — never hardcode `trd-srm.st.com` or any single SRM URL. Non-prod envs nest under `/distribution/<env>/`; **`prod` uses the root path with no `/distribution/<env>/` segment**.
- URL precedence: `--url` (full-URL escape hatch) > `SRM_BASE_URL` env > per-env computed URL.
- `SRM_ENV` (default `prod`) sets the default env; the workflow `environment` dropdown defaults to **`ALL`** and lists `ALL, test, int, qa, demo, prod`. `ALL` loops every env sequentially (single job), isolating failures, and **includes prod**.
- Artifacts are per-env under `rca-srm/<env>/`; the top-level `rca-srm/summary.md` is an aggregated index (one row per env, linking to each env's summary). Exit 0 unless `STRICT=true`; under `STRICT` an ALL run exits non-zero if any env errored.
- Optional `request_id` (workflow string input / `--request-id` / `REQUEST_ID`) scopes the whole pipeline to one DeliveryRequest URN (accepts a bare number or a full URN). Filtering happens in `evaluate_payload` so verdict, Grafana LogQL, and STGPT evidence all scope to that id. A not-found/fresh id is `NO_RECORDS`/`FRESH` (no Grafana/STGPT).

## Single-request infra success gate + Tempo trace

Only active when `request_id` is set (single-DR mode); normal all-records runs record "not applicable" and behave as before.

- **Notification success gate.** On the `STALE` path, `grafana.py`'s `_Collector` probes the Notification component (`NOTIFICATION_COMPONENT`, default `notification`) in Loki for the request URN with a dedicated `{component="<notification>"} |= "<urn>"` query that has **no level filter**, so the DEBUG `"successfully processed"` marker is never dropped regardless of `INCLUDE_DEBUG_LOGS`. Markers come from `NOTIFICATION_SUCCESS_MARKERS` (default `successfully processed|mail sent to`, case-insensitive). On a match, `analyze_staleness` short-circuits: **no STGPT call**, `AnalysisRecord.status="infra_success"`, `fallback_used=False`, `tokens_used=0`, notification lines as citations. No match ⇒ the existing persona analysis runs unchanged.
- **Tempo trace cascade.** `TEMPO_ID` (env var → `Settings.tempo_datasource_uid`) is a **variable — never hardcode a UID**. The collector extracts a trace id from log lines (`TRACE_ID_FIELD` default `trace_id`; also accepts `traceId`/`traceID`/`trace-id`, 16/32-hex), then, if a read-only Tempo/trace tool is present in the MCP tool set, queries it and stores an ordered `trace_spans` cascade. Tempo/trace tools are **read-only** (same `WRITE_TOOLS`/prefix guard as Loki). Missing `TEMPO_ID`, trace id, or tool ⇒ skip with a `"Tempo trace skipped: <reason>"` note; never fail the job.
- Report: `summary.md` gains `## Infra success check (Notification)` and `## Request trace (Tempo)`; the AI section shows `infra_success` as a green result with confidence "not applicable". Secret redaction applies to every new probed/queried line.
- New workflow `vars.*`: `TEMPO_ID`, `NOTIFICATION_COMPONENT`, `NOTIFICATION_SUCCESS_MARKERS`, `TRACE_ID_FIELD`.

## Non-negotiables

1. Implement one PRD slice at a time (S0 scaffolding → S1 SRM → S2 Grafana MCP → S3 STGPT → S4 dispatch workflow → S5 GHE cron).
2. Never log or write secrets: SRM basic auth (`SRM_BASIC_PASSWORD`), `STGPT_API` / `API_KEY`, `GRAFANA_MCP_TOKEN`.
3. Daily job must exit 0 unless `STRICT=true`. Partial summary on failure.
4. STGPT call contract is PRD §5 / §5.1.1. Vendor/reimplement `stgpt_client` only (SHA1 token, personas `trinity_for_api` then `alfred_for_api`, JSON `root_cause` / `suggested_fix` / `citations`). Same secret name as `ci-rca-collector`.
5. Grafana MCP: SSE at `GRAFANA_MCP_URL`. Read-only tools. Join logs on full URN `strn:distribution:DeliveryRequest:<id>`. Do not invent Grafana UIDs other than `d68f5a4d-72e6-4b16-b166-a70f41f3cd49`.
6. Do not copy Drain3, GitHub Actions log collection, or kubectl from `ci-rca-collector`.
7. Python 3.11+. Minimal deps: `httpx`, `pydantic>=2`, `python-dateutil`, plus a small MCP SSE client later.
8. On github.com: `workflow_dispatch` only. Do not enable cron here.
9. Do not push to remotes unless the operator explicitly asks.
