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
   - `SRM_BASIC_USER`, `SRM_BASE_URL`
   - `GRAFANA_MCP_URL`, `GRAFANA_LOKI_DATASOURCE_UID`
   - `STGPT_API_URL`, `STGPT_CLIENT_APP_NAME`
5. **Actions → delivery-analysis → Run workflow**.
6. Optional input `as_of`: frozen evaluation time UTC, e.g. `2026-09-18T08:00:00Z`.
7. After the run: Job Summary is `rca-srm/summary.md`; the `rca-srm/` artifact is always uploaded.

The job uses `runs-on: ubuntu-latest` (override later with variable `RUNS_ON` on github.st.com). CLI already exits 0 unless `STRICT=true`.

Cron (`30 2 * * *` UTC) is **not** enabled here. Turn it on only after the tree moves to github.st.com.

## Status

S4: dispatch-only GitHub Actions workflow on github.com. SRM, Grafana MCP, and STGPT run from `python -m src.delivery_analysis.cli collect --analyze`.
