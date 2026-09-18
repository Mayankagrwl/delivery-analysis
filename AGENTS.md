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
