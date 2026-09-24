# delivery-analysis

Daily check of ST SRM `DeliveryRequest` records in states `SUBMITTED` and `GRANTED`. If any row is older than 24 hours (UTC), the job pulls Grafana Loki evidence via MCP SSE and asks ST ChatGPT for RCA. Empty queue (`NO_RECORDS`) is not an incident.

Spec: [`docs/PRD-daily-delivery-request-staleness-rca.md`](docs/PRD-daily-delivery-request-staleness-rca.md) (v1.2). New project — not a mode of `ci-rca-collector`.

## Hosting

Develop on github.com with Grok CLI; production workflow runs on github.st.com.

| Phase | Host | Workflow |
|---|---|---|
| **A — develop** | github.com + Grok CLI | `workflow_dispatch` only. No cron. |
| **B — produce** | github.st.com | Same tree. Enable `30 2 * * *` UTC after secrets are mapped. |

Do not hardcode `github.com` / `api.github.com` as the only API host. Use `GITHUB_SERVER_URL` / `GITHUB_API_URL`. On GitHub Enterprise the API base is `{server}/api/v3`.

## Run on github.com (Phase A)

No schedule on this host. Bring-up is **Actions → delivery-analysis → Run workflow**.

1. Open the repo on github.com.
2. **Settings → Secrets and variables → Actions**.
3. Add **secrets** (never commit these):
   - `SRM_BASIC_PASSWORD`
   - `STGPT_API` (preferred) or `API_KEY` — same key as `ci-rca-collector`, do not mint a second one
   - `GRAFANA_MCP_TOKEN` (optional)
4. Add **variables** as needed (PRD §10 names only), typically:
   - `SRM_BASIC_USER`, `SRM_BASE_HOST`, `SRM_ENV`
   - `GRAFANA_MCP_URL`, `GRAFANA_LOKI_DATASOURCE_UID`
   - `STGPT_API_URL`, `STGPT_CLIENT_APP_NAME`
5. **Actions → delivery-analysis → Run workflow**.
6. Dispatch inputs:
   - `environment`: dropdown `ALL, test, int, qa, demo, prod`, default **`ALL`**.
   - `request_id`: optional; scope the whole run to one DeliveryRequest (bare number `123456` or full URN).
   - `as_of`: optional frozen evaluation time UTC, e.g. `2026-09-18T08:00:00Z`.
7. After the run: Job Summary is the aggregated `rca-srm/summary.md` roll-up; the `rca-srm/` artifact (with per-env subfolders) is always uploaded.

The job uses `runs-on: ubuntu-latest` (override later with variable `RUNS_ON` on github.st.com). CLI already exits 0 unless `STRICT=true` (under `STRICT`, an ALL run exits non-zero if any env errored).

Cron (`30 2 * * *` UTC) is **not** enabled here. Turn it on only after the tree moves to github.st.com.

## Environments and SRM URLs

SRM URLs are derived per environment from a configurable host — nothing is hardcoded to `trd-srm.st.com` anymore.

| env | URL |
|---|---|
| `test` | `{host}/distribution/test/resources/strn:distribution:DeliveryRequest` |
| `int` | `{host}/distribution/int/resources/strn:distribution:DeliveryRequest` |
| `qa` | `{host}/distribution/qa/resources/strn:distribution:DeliveryRequest` |
| `demo` | `{host}/distribution/demo/resources/strn:distribution:DeliveryRequest` |
| `prod` | `{host}/resources/strn:distribution:DeliveryRequest` (**root path — no `/distribution/<env>/` segment**) |

`{host}` defaults to `https://trd.st.com` and is overridable via the `SRM_BASE_HOST` variable.

**URL precedence:** `--url` (full-URL escape hatch) > `SRM_BASE_URL` env > per-env computed URL.

### Variables

| Variable | Default | Meaning |
|---|---|---|
| `SRM_BASE_HOST` | `https://trd.st.com` | Host used to build per-env SRM URLs |
| `SRM_ENV` | `prod` | Default env when no `--env` / dropdown value is given (`ALL` runs every env) |
| `SRM_BASE_URL` | — | Optional full-URL escape hatch; overrides the computed per-env URL |

### CLI

```bash
python -m src.delivery_analysis.cli collect --analyze --env ALL
python -m src.delivery_analysis.cli collect --analyze --env qa
python -m src.delivery_analysis.cli collect --analyze --env prod --request-id 123456
```

- `--env` accepts a single env or `ALL` (case-insensitive); defaults to `SRM_ENV`, else `ALL`.
- `--request-id` accepts a bare number or a full URN; defaults to the `REQUEST_ID` env var, else all records.

### Artifact layout

Each env writes its own artifacts under `rca-srm/<env>/` (`summary.md`, `srm.json`, `grafana.json`, `analysis.json`). The top-level `rca-srm/summary.md` is an aggregated index: one row per env with its verdict, reason, and a link to that env's `summary.md`. An ALL run isolates failures — one env's error never aborts the others.

## Single-request infra success gate + Tempo trace

When a run is scoped to one DeliveryRequest (`--request-id` / the `request_id` dispatch input), two extra steps run on the `STALE` path:

**Notification success gate.** The pipeline probes the **Notification** component logs in Loki for the request URN and looks for either success marker (case-insensitive): `"successfully processed"` (normally a **DEBUG** line) or `"mail sent to"` (normally **INFO**). The probe uses a dedicated `{component="<notification>"} |= "<urn>"` query with **no level filter**, so it finds DEBUG success lines even when `INCLUDE_DEBUG_LOGS` is off.

- **Found → SUCCESS:** the request is treated as complete from the infra side. **STGPT is not called.** `analysis.json` records `status="infra_success"`, `fallback_used=false`, `tokens_used=0`, and the matched notification lines as citations. `summary.md` shows a **SUCCESS** banner under `## Infra success check (Notification)`.
- **Not found:** something went wrong — the existing Grafana + STGPT analysis runs as before, and the section says the success log was not found.

This gate only runs in single-request mode; normal all-records staleness runs record "not applicable".

**Tempo trace cascade.** After collecting logs, the run extracts a trace id from the log lines (field variants `trace_id` / `traceId` / `traceID` / `trace-id`, 16- or 32-char hex). If `TEMPO_ID` is set and a read-only Tempo/trace query tool is available over MCP, it queries Tempo for that trace and renders a cascade table (component/service → span → status → duration) under `## Request trace (Tempo)`. If `TEMPO_ID` is unset, no trace id is found, or no Tempo tool is available, it skips with a clear note and never fails the job. Tempo/trace tools are called read-only; write tools are refused.

### More variables

| Variable | Default | Meaning |
|---|---|---|
| `TEMPO_ID` | — | Tempo datasource UID for the trace cascade (a variable — never hardcoded). Unset ⇒ trace skipped. |
| `NOTIFICATION_COMPONENT` | `notification` | Loki `component` value for the notification success probe |
| `NOTIFICATION_SUCCESS_MARKERS` | `successfully processed\|mail sent to` | Pipe-separated success markers (case-insensitive) |
| `TRACE_ID_FIELD` | `trace_id` | Preferred trace-id field key when extracting from log lines |

## Status

S4: dispatch-only GitHub Actions workflow on github.com, now with environment-aware SRM URLs (`environment` dropdown, default `ALL`), an optional `request_id` scope, and per-env `rca-srm/<env>/` artifacts plus an aggregated `rca-srm/summary.md`. In single-request mode it adds a Notification success gate (skips STGPT on infra success) and a Tempo trace cascade. SRM, Grafana MCP, and STGPT run from `python -m src.delivery_analysis.cli collect --analyze --env ...`.
