# PRD: Daily DeliveryRequest Staleness Monitor + Grafana Loki RCA

| Field | Value |
|---|---|
| **Document type** | Product Requirements Document |
| **Status** | Draft v1.2 — decisions locked 2026-09-18; requirements only, no implementation |
| **Date** | 2026-09-18 |
| **Owner** | TDP / Platform (SRM DeliveryRequest operations) |
| **Related repo (AI contract only)** | [Mayankagrwl/ci-rca-collector](https://github.com/Mayankagrwl/ci-rca-collector) — reuse STGPT client contract and the same API key secret. **Do not add this product into that repo.** |
| **Home for code** | New project. **Phase A:** develop on **github.com** with **Grok CLI**. **Phase B:** move the same tree to **github.st.com** (GitHub Enterprise) and run the daily workflow there. |
| **Trigger cadence** | Once per day — `30 2 * * *` UTC (08:00 IST) plus `workflow_dispatch` |
| **Primary deliverable of this work** | A daily GitHub Actions workflow that detects stale SRM `DeliveryRequest` records, pulls related Grafana/Loki evidence via MCP, and produces an AI RCA summary using the existing ST ChatGPT bridge |

---

## 1. Problem statement

Operators currently check ST SRM `DeliveryRequest` records by hand with `curl` + `jq`:

```bash
curl -s -u "$SRM_BASIC_USER:$SRM_BASIC_PASSWORD" \
  https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest \
  | jq -c '.[] | select(.state=="SUBMITTED") | {urn: ._urn, updated: ._updated.on}'

curl -s -u "$SRM_BASIC_USER:$SRM_BASIC_PASSWORD" \
  https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest \
  | jq -c '.[] | select(.state=="GRANTED") | {urn: ._urn, updated: ._updated.on}'
```

Observed sample (from the attached terminal capture, 2026-09-18):

| State | URN | `updated.on` |
|---|---|---|
| SUBMITTED | `strn:distribution:DeliveryRequest:43` | `8/27/2026 9:30:43 AM` |
| GRANTED | `strn:distribution:DeliveryRequest:38` | `8/26/2026 3:19:56 PM` |
| GRANTED | `strn:distribution:DeliveryRequest:39` | `8/27/2026 7:41:26 AM` |
| GRANTED | `strn:distribution:DeliveryRequest:40` | `8/27/2026 8:21:34 AM` |

Those timestamps are **outside the last 24 hours**. Stale `SUBMITTED` / `GRANTED` requests are an operational signal that distribution may be stuck. Today there is no automated:

1. freshness check against a 24-hour window,
2. pull of related Loki / dashboard evidence,
3. AI root-cause analysis using the same STGPT method already used by `ci-rca-collector`,
4. single daily summary report.

This product automates that loop.

---

## 2. Goals

1. Run **once a day** as a GitHub Actions workflow.
2. Query SRM for `DeliveryRequest` records in states `SUBMITTED` and `GRANTED`.
3. Decide whether any returned record is **outside the last 24 hours**.
4. When the staleness condition is met, connect to the **Grafana MCP server** using GitHub Actions env `GRAFANA_MCP_URL` and fetch:
   - Loki logs relevant to the stale URNs / distribution pipeline,
   - related dashboard context (search + panel queries if a dashboard is configured),
   - any other read-only Grafana details that help RCA (datasource health, label values, log stats, recent error patterns).
5. Print a **collection summary** of what was fetched.
6. Send the packed evidence to the **existing ST ChatGPT bridge** using the same request/response contract as `tools/rca/stgpt_client.py` + `tools/rca/analyze.py`.
7. Print a **workflow summary report** containing:
   - staleness verdict,
   - fetched evidence inventory,
   - AI `root_cause`, `citations`, `suggested_fix` / possible solutions,
   - confidence and analysis status.

Non-goal of this document: implement the workflow, Python client, or prompts. This PRD only specifies them.

---

## 3. Non-goals

- Changing the Phase-1 collector behaviour of `ci-rca-collector` (GitHub Actions log Drain3 / classify pipeline).
- Creating GitHub Issues, PR comments, Slack/email delivery (that is Phase 3 of the existing spec).
- Write operations against Grafana (no dashboard create/update, no incident create, no alert-rule mutation).
- Replacing Grafana MCP with direct Loki HTTP if MCP is available. Direct Loki is a documented fallback only.
- Training Drain3 baselines on SRM logs in v1.
- Multi-environment fan-out beyond the single SRM host in the attached curl (`https://trd-srm.st.com`).
- Putting this product inside `ci-rca-collector`. It is a **new project**. Only the STGPT request/response contract and the existing API-key secret are reused.
- Hardcoding `github.com` as the Actions/API host. Production runs on **github.st.com**.

---

## 4. Users and use cases

| Actor | Need |
|---|---|
| On-call / TDP operator | Daily signal: are DeliveryRequests moving, or stuck older than 24h? |
| RCA engineer | Packaged Loki + SRM context plus grounded AI hypothesis instead of raw curl. |
| Workflow owner | A job that never fails the schedule noisily; always emits a summary, even on partial failure. |

### Primary use case

> Every morning the workflow runs. If `SUBMITTED` or `GRANTED` DeliveryRequests exist and their `updated.on` is older than 24 hours, the job pulls Loki/Grafana evidence, asks STGPT for RCA, and publishes `summary.md` on the workflow run.

### Secondary use cases

- Healthy day: records exist and all `updated.on` values are within 24 hours → **no Grafana / no AI**. Summary says “fresh”.
- Empty day: SRM returns `[]` for both states → verdict `NO_RECORDS`. **Not an incident. Do not call Grafana MCP. Do not call STGPT.** Write a short “queue empty / nothing to analyse” summary only.
- Transport failure: SRM, Grafana MCP, or STGPT is down → partial summary, job exits 0 unless `--strict`.

---

## 5. Current-state constraints (must reuse)

The new workflow **must talk to the AI the same way `ci-rca-collector` already does**. Do not invent a new LLM client.

### 5.1 ST ChatGPT bridge (source of truth)

Reuse / vendor the modules:

- `tools/rca/stgpt_client.py` — transport + auth
- `tools/rca/analyze.py` — gate, cache, persona loop, citation check
- `tools/rca/prompt.py` — evidence wrapper + JSON-only contract
- `tools/rca/config.py` — URL, client app name, personas, token budget
- `tools/rca/redact.py` — never persist secrets into reports

**Auth (do not change):**

```
token = SHA1("{clientAppName}_{service}_{api_key}_{timestamp}_{nonce}")
Headers:
  stchatgpt-auth-token
  stchatgpt-auth-nonce
  stchatgpt-auth-timestamp
```

**Request body (do not change shape):**

```json
{
  "version": "1.0",
  "clientAppName": "gtrd_srmtdpplm",
  "service": "chat",
  "timestamp": "<unix>",
  "persona": "trinity_for_api | alfred_for_api",
  "messages": [
    { "role": "user", "content": "<flattened evidence + diagnose instruction>" }
  ],
  "responseFormat": "json_object"
}
```

Default endpoint: `https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps`  
Override via `STGPT_API_URL`.

Default client app: `gtrd_srmtdpplm`  
Override via `STGPT_CLIENT_APP_NAME` or `CLIENT_APP_NAME`.

API key resolution order (already in collector): explicit arg → `STGPT_API` → `API_KEY`.

**Same secret as `ci-rca-collector`.** In the new repo’s GitHub Actions, wire the existing organization/repo secret (whichever name that workflow already uses: typically `STGPT_API` or `API_KEY`). Do not mint a second key.

Personas, in order: `trinity_for_api` then `alfred_for_api`. One repair retry per persona if JSON/citations are invalid.

### 5.1.1 Full STGPT call contract (copy into the new repo)

Vendor `tools/rca/stgpt_client.py` from `ci-rca-collector` (preferred) or reimplement this exact behaviour.

**HTTP**

| Item | Value |
|---|---|
| Method | `POST` |
| URL | `STGPT_API_URL` or default `https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps` |
| Content-Type | `application/json` |
| Timeout | 60 seconds |
| Redirects | do not follow |
| TLS | verify on unless `RCA_SSL_VERIFY=false`; honour `SSL_CERT_FILE` / `RCA_SSL_CERT_FILE` |

**Auth token (must match collector bit-for-bit)**

```
raw    = f"{clientAppName}_{service}_{api_key}_{timestamp}_{nonce}"
token  = SHA1(raw).hexdigest()
```

- `clientAppName` = `gtrd_srmtdpplm` unless overridden
- `service` = `chat`
- `timestamp` = unix seconds as a decimal string
- `nonce` = `uuid4().hex`
- Never log `api_key` or `token`

**Request headers**

```
stchatgpt-auth-token:     <sha1 hex>
stchatgpt-auth-nonce:     <nonce>
stchatgpt-auth-timestamp: <timestamp>
```

**Request body fields**

| Field | Value |
|---|---|
| `version` | `"1.0"` |
| `clientAppName` | resolved name |
| `service` | `"chat"` |
| `timestamp` | same string as the header |
| `persona` | `trinity_for_api` first, then `alfred_for_api` |
| `messages` | single flattened user message (collector concatenates roles with `\n\n`; assistant turns prefixed `Previous assistant reply:\n`) |
| `responseFormat` | `"json_object"` first; retry `"text"` if the bridge rejects `json_object` |

**Response handling (same extraction order as `stgpt_client.extract_completion`)**

Look for completion text in: `completion`, `message`, `text`, `content`, then nested `data`, then `choices[0].message.content`. If the body already contains `root_cause` / `suggested_fix`, treat the object as the completion.

Record `responseId` / `response_id` / `id` onto `analysis.json`.

HTTP/transport failure → analysis status `bridge_error`. Do not raise out of the daily job.

**Call sequence for this product**

1. Build `<EVIDENCE>` (SRM tables + Loki + Service Logs snippets), cap 6000 tokens.
2. `post_chat(..., persona="trinity_for_api", responseFormat="json_object")`.
3. Parse JSON → `AnalysisResult`. If parse/citation fail, one repair turn (same persona).
4. If still unusable, `post_chat(..., persona="alfred_for_api")` + one repair.
5. Persist `AnalysisRecord` (`ok` / `parse_error` / `citation_invalid` / `bridge_error` / `unusable`).

Empty prompt must not be sent (`StgptError("prompt_empty")` equivalent → `unusable`).

Expected model JSON (same schema as `AnalysisResult`):

```json
{
  "root_cause": "string",
  "suggested_fix": "string",
  "confidence": "high|medium|low",
  "citations": [
    { "quote": "verbatim substring of <EVIDENCE>", "source": "<allowed source>", "line": null }
  ],
  "cannot_determine": false
}
```

Aliases already accepted by `analyze.py` (`rootCause`, `suggestedFix`, `cause`, `fix`, …) must keep working.

### 5.2 Citation sources — extension required

Existing allowed sources are CI-specific:

`first_error_window`, `tail_window`, `stack_traces`, `log_templates`, `junit`, `change_context`, `annotations`, `history`, `step_table`

This product adds SRM / Grafana sources. The analyze layer used by **this workflow** must accept the additional literals below. Do not break the CI collector’s existing enum if the modules are shared — isolate the allowed-source set per product, or extend it additively.

New allowed `citations[].source` values:

| Source | Meaning |
|---|---|
| `srm_submitted` | SUBMITTED query result set |
| `srm_granted` | GRANTED query result set |
| `srm_record` | A single DeliveryRequest URN payload |
| `loki_logs` | Lines returned by `query_loki_logs` |
| `loki_stats` | `query_loki_stats` / stream cardinality |
| `loki_patterns` | `query_loki_patterns` or `find_error_pattern_logs` |
| `loki_labels` | Label name/value discovery |
| `grafana_dashboard` | Dashboard search / summary / panel queries |
| `grafana_datasource` | Datasource list / health |
| `collection_notes` | Workflow notes, HTTP errors, skipped steps |

Rule is unchanged: `citations[].quote` **must be a verbatim substring** of the `<EVIDENCE>` block actually sent to the model.

### 5.3 Failure posture (copy from AGENTS.md)

- Never log tokens, basic-auth passwords, Grafana service-account tokens, or STGPT keys.
- Never write secrets into `summary.json` / `summary.md`.
- Collector/workflow must not fail the schedule: catch exceptions, emit partial summary, exit 0 unless `STRICT=true`.
- Prefer reuse of `httpx` + existing STGPT client. No new LLM SDK.

---

## 6. Grafana MCP connection

### 6.1 How the workflow reaches Grafana

Python scripts **must** read the MCP base URL from the GitHub Actions environment variable:

```
GRAFANA_MCP_URL
```

**Transport is locked: SSE** (Server-Sent Events). Example:

- `https://grafana-mcp.example.st.com/sse`

The client is a **remote MCP SSE client**. Do not spawn `uvx mcp-grafana` on the runner unless a later change explicitly says so.

Optional companion secrets (if the MCP endpoint itself requires auth in addition to whatever the server uses to talk to Grafana):

| Variable | Purpose |
|---|---|
| `GRAFANA_MCP_URL` | Required. MCP SSE URL used by Python. |
| `GRAFANA_MCP_TOKEN` | Optional bearer for the MCP endpoint. |
| `GRAFANA_URL` | Optional. Only if scripts also call Grafana REST directly as fallback. |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` / `GRAFANA_API_KEY` | Optional. Direct Grafana auth for fallback. |
| `GRAFANA_LOKI_DATASOURCE_UID` | Loki datasource UID. Required for reliable LogQL. |
| `GRAFANA_DASHBOARD_UID` | Locked: `d68f5a4d-72e6-4b16-b166-a70f41f3cd49` |
| `GRAFANA_DASHBOARD_TITLE` | `Service Logs` (folder: **Distribution**) |
| `GRAFANA_DASHBOARD_FILTERS` | Dashboard variables: `env`, `component`, `level` (and any others present). Apply them when running panel queries. |

### 6.2 Tools the workflow is allowed to call (read-only)

Call only what is needed for this RCA. Preferred order:

1. `get_dashboard_by_uid` / `get_dashboard_summary` / `get_dashboard_panel_queries` for UID `d68f5a4d-72e6-4b16-b166-a70f41f3cd49` (`Service Logs` under **Distribution**).
2. Honour dashboard variables `env`, `component`, `level` when executing panel queries (pass through as variable overrides if the MCP tool supports them; otherwise encode equivalent label matchers in LogQL).
3. `list_datasources` — confirm Loki is present; capture UID if env not set.
4. `list_loki_label_names` + `list_loki_label_values` — confirm how `urn` is stored (label vs line field).
5. `query_loki_stats` — cheap cardinality check before pulling lines.
6. `query_loki_logs` — actual log lines for each stale URN and for error-class queries.
7. `query_loki_patterns` and/or `find_error_pattern_logs` — error clustering if available.
8. `check_datasources_health` — if Loki looks empty.
9. `generate_deeplink` — include Explore / `Service Logs` dashboard links in the report, with filters applied when possible.

Do **not** call write tools (`update_dashboard`, `create_incident`, `alerting_manage_rules`, …).

### 6.3 Loki query strategy (locked identity, configurable matchers)

**Identity of a request in logs:** the URN string itself, e.g.

```
"urn":"strn:distribution:DeliveryRequest:300"
```

`300` is the request number. Every stale row from SRM already carries a full URN (`strn:distribution:DeliveryRequest:<id>`). Loki queries must search for that exact URN string (and, as a fallback, the numeric id) so logs can be joined 1:1 with the SRM table.

Default investigation window: `now-24h` … `now` (UTC). Also query `updated.on − 2h` … `updated.on + 2h` per stale URN so the original stall moment is visible.

Default LogQL templates (overridable via env / YAML). Prefer dashboard-equivalent streams from `Service Logs` (respect `env`, `component`, `level` when known):

```
# exact URN as it appears in log lines
{component=~".+"} |= "strn:distribution:DeliveryRequest:<id>"

# JSON-style field as provided
{component=~".+"} |= "\"urn\":\"strn:distribution:DeliveryRequest:<id>\""

# errors around distribution / DeliveryRequest
{component=~".+"} |= "DeliveryRequest" |~ "(?i)error|fail|timeout|denied|exception"
```

If `list_loki_label_values` shows a dedicated `urn` label, prefer a label selector:

```
{urn="strn:distribution:DeliveryRequest:<id>"}
```

Implementation must try label-match first, then line-filter on the full URN string.

Limits:

- Max 200 lines per LogQL query.
- Max 4 LogQL queries per run unless config raises it.
- Truncate each line to 500 characters before packing evidence.
- Hard cap packed Loki text at the existing token budget (`TOKEN_BUDGET_TOTAL`, default 6000) using the same middle-trim behaviour as `prompt.py`.

---

## 7. Staleness decision (functional core)

### 7.1 Clock

- Evaluation time `T_now` = workflow start, **UTC (GMT)**.
- Window = `[T_now - 24h, T_now]`.
- Parse `updated.on` values such as `8/27/2026 9:30:43 AM`. Format: `M/D/YYYY h:mm:ss AM/PM`.
- Timezone of SRM timestamps is **UTC / GMT** (`SRM_TIMESTAMP_TZ=UTC`).

A record is **stale** iff its parsed `updated.on` is **strictly before** `T_now - 24h`.

### 7.2 Decision table

Let `S` = SUBMITTED records, `G` = GRANTED records.

| Condition | Verdict | Grafana MCP | STGPT |
|---|---|---|---|
| SRM HTTP/auth/parse failure | `SRM_ERROR` | No (unless `QUERY_GRAFANA_ON_SRM_ERROR=true`) | No |
| `S ∪ G` empty | `NO_RECORDS` (not an incident) | **No** | **No** |
| Every record in `S ∪ G` has `updated.on` within 24h | `FRESH` | No | No |
| At least one record in `S ∪ G` is stale | `STALE` | Yes | Yes, after collection |
| Mix of fresh + stale | `STALE` | Yes | Yes. Report lists both sets. |

“If I get some response and the output does not fall within the last 24 hours” is implemented as the `STALE` row: **non-empty result set AND at least one timestamp outside the window**.

**Locked rule (`STALE_MODE=any`):** every `SUBMITTED` / `GRANTED` row older than 24 hours counts. A single old row is enough to set verdict `STALE`, even if newer rows exist. There is no “newest-only” mode in v1.

`NO_RECORDS` is explicitly **not an incident**: no Grafana MCP, no STGPT.

---

## 8. End-to-end workflow

```
schedule (daily)
    │
    ├─ 1. Collect SRM
    │     GET DeliveryRequest
    │     filter state ∈ {SUBMITTED, GRANTED}
    │     project {urn, updated, state}
    │
    ├─ 2. Evaluate window
    │     parse timestamps → verdict FRESH | STALE | NO_RECORDS | SRM_ERROR
    │
    ├─ 3. If not STALE
    │     write summary.md (verdict only)
    │     publish GitHub Job Summary
    │     exit 0
    │
    ├─ 4. If STALE
    │     connect MCP at GRAFANA_MCP_URL
    │     run read-only tool pack (§6.2–6.3)
    │     redact + budget-trim
    │     write collection inventory
    │
    ├─ 5. Analyze
    │     build <EVIDENCE> from SRM rows + Loki + dashboard snippets
    │     post_chat via stgpt_client (trinity → repair → alfred)
    │     validate JSON + verbatim citations
    │
    └─ 6. Report
          summary.md + summary.json + analysis.json
          GitHub Job Summary
          artifact upload
          exit 0 (unless STRICT)
```

### 8.1 Suggested GitHub Actions skeleton (requirement, not implementation)

- `on.schedule`: `30 2 * * *` UTC (08:00 IST). Locked. **Production schedule is on github.st.com**, not github.com.
- `on.workflow_dispatch`: manual rerun with optional `as_of` timestamp for replay. Used on both hosts during bring-up.
- Permissions: `contents: read`, `actions: read`. No write to foreign repos.
- Secrets / vars: listed in §10. Re-create or map the same secret *names* on github.st.com (values stay in Enterprise secret store).
- Steps stay under a 15-minute job timeout (MCP + two personas + repair).
- Always upload `rca-srm/` even on partial failure.
- Workflow file must be valid on **both** GitHub.com-hosted runners and GHE runners (`runs-on` configurable, default `ubuntu-latest`).

### 8.2 Hosting and promotion (github.com → github.st.com)

Development and production are two hosts of the **same codebase**.

| Phase | Host | How work happens | What the workflow does |
|---|---|---|---|
| **A — develop** | `github.com` | Author and iterate with **Grok CLI** against the public/new repo. Local + `workflow_dispatch` only. | Optional dry-run. **Cron may be disabled** (`if: false` or no `schedule` on this host) so github.com does not hit prod SRM/Grafana/STGPT on a timer. |
| **B — produce** | `github.st.com` | Move / mirror the repo to Enterprise. Wire Enterprise secrets and `GRAFANA_MCP_URL`. Enable the daily cron. | Canonical daily job. Job Summary + artifacts live here. |

Rules that keep the move cheap:

1. **No hardcoded GitHub host.** Do not embed `https://github.com`, `https://api.github.com`, or `github.com` in Python or YAML except as a documented fallback.
2. Resolve GitHub the same way `ci-rca-collector` already does:
   - API base: `--api-url` → `RCA_GITHUB_API_URL` → `GITHUB_API_URL` → derive from `GITHUB_SERVER_URL` / `GH_HOST` → default `https://api.github.com`.
   - Enterprise rule: if server host is **not** `github.com`, API base is `{server}/api/v3` (so `https://github.st.com/api/v3`).
   - Token: `--token` → `RCA_GITHUB_TOKEN` → `COMMON_ACTIONS_PAT` → `GITHUB_TOKEN` → `GH_TOKEN`.
3. Prefer **GitHub Actions context only** (`github.server_url`, `github.api_url`, `GITHUB_EVENT_PATH`). Most of this product does not need the GitHub REST API at all (SRM + Grafana MCP + STGPT). Keep it that way so the move is a git remote change plus secrets.
4. Grok CLI on github.com is a **dev inner loop** (edit, test, `workflow_dispatch`). It is not the production scheduler.
5. Same workflow file in both places. Host-specific values belong in Actions **variables/secrets**, not in source:
   - `SRM_BASE_URL`, `GRAFANA_MCP_URL`, `STGPT_API_URL`, `STGPT_API` / `API_KEY`, basic-auth secrets.
6. After the move: point the new repo’s secrets at the **same STGPT API key** already used by `ci-rca-collector` on Enterprise. Do not create a second key.
7. Runners, custom CA, and `SSL_CERT_FILE` / `RCA_SSL_CERT_FILE` are expected on github.st.com; the client must honour them (already in the STGPT contract).
8. README must document: “Develop on github.com with Grok CLI; production workflow runs on github.st.com.”

---

## 9. Evidence pack and report

### 9.1 Collection summary (always printed when Grafana ran)

Must include:

- MCP URL host (no token), transport, latency.
- Tools called, each with status, duration, row/line count.
- Dashboards resolved (uid, title, URL).
- Loki datasource uid/name.
- LogQL strings actually executed.
- Time ranges used.
- Number of log lines kept vs discarded by budget.
- Redaction notes (how many secrets/tokens masked).
- Deep links to Grafana Explore / dashboard.

### 9.2 Workflow summary report (`summary.md`)

Required sections, in order:

1. **Verdict** — `FRESH` / `STALE` / `NO_RECORDS` / `SRM_ERROR` plus one-line reason.
2. **Window** — `T_now`, timezone, 24h cutoff.
3. **SRM SUBMITTED** — table of `urn | updated.on | age_hours | stale?`
4. **SRM GRANTED** — same table.
5. **Grafana / Loki inventory** — or “skipped (verdict not STALE)”.
6. **Evidence highlights** — short excerpts (not the full dump).
7. **AI analysis**
   - status (`ok` / `cached` / `gated` / `parse_error` / `citation_invalid` / `bridge_error` / `unusable`)
   - persona used
   - confidence
   - root cause
   - citations (quote + source)
   - possible solutions (`suggested_fix`)
   - `cannot_determine` flag
8. **Collection notes / errors**
9. **Footer** — workflow run id, git sha, prompt version, token budget used.

### 9.3 Machine-readable artifacts

| File | Content |
|---|---|
| `rca-srm/srm.json` | Raw filtered records + parse metadata |
| `rca-srm/grafana.json` | Tool-call log + truncated payloads |
| `rca-srm/summary.json` | Verdict + inventories |
| `rca-srm/analysis.json` | `AnalysisRecord` compatible with collector |
| `rca-srm/summary.md` | Human report above |

`summary.json` / `analysis.json` should stay close enough to `ci-rca-collector` models that `analyze.py` can be reused with a thin adapter, not a second LLM stack.

### 9.4 Prompt framing for this product

System instruction stays JSON-only and evidence-grounded, adapted from `prompt.py`:

- Role: distribution / SRM DeliveryRequest RCA assistant (not CI).
- Use only `<EVIDENCE>`.
- Do not invent URNs, timestamps, LogQL, or dashboard names.
- Explain *why* requests may be stuck in SUBMITTED/GRANTED past 24h.
- Solutions must be operational (replay, unlock, downstream dependency, auth, quota, Loki-confirmed error class) and tied to citations.

---

## 10. Configuration and secrets

All of these are GitHub Actions `env` / `vars` / `secrets`. Python reads them; nothing is hardcoded except documented defaults.

### 10.1 SRM

| Name | Required | Default | Notes |
|---|---|---|---|
| `SRM_BASE_URL` | yes | `https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest` | |
| `SRM_BASIC_USER` | yes | — | Screenshot user `udevopsdm` |
| `SRM_BASIC_PASSWORD` | yes | — | Secret. Never log. |
| `SRM_STATES` | no | `SUBMITTED,GRANTED` | Comma-separated |
| `SRM_TIMESTAMP_TZ` | no | `UTC` | Locked: UTC / GMT |
| `STALE_HOURS` | no | `24` | |
| `STALE_MODE` | no | `any` | Locked: every old SUBMITTED/GRANTED row |

### 10.2 Grafana MCP

| Name | Required | Default | Notes |
|---|---|---|---|
| `GRAFANA_MCP_URL` | yes (when STALE) | — | SSE endpoint |
| `GRAFANA_MCP_TOKEN` | no | — | |
| `GRAFANA_MCP_TRANSPORT` | no | `sse` | Locked |
| `GRAFANA_LOKI_DATASOURCE_UID` | recommended | — | |
| `GRAFANA_DASHBOARD_UID` | no | `d68f5a4d-72e6-4b16-b166-a70f41f3cd49` | **Service Logs** in folder **Distribution** |
| `GRAFANA_DASHBOARD_TITLE` | no | `Service Logs` | |
| `LOKI_QUERY_PACK` | no | URN-based templates in §6.3 | YAML/JSON list |
| `LOKI_LINE_LIMIT` | no | `200` | |
| `QUERY_GRAFANA_ON_EMPTY` | no | `false` | Locked false — empty queue is not an incident |
| `QUERY_GRAFANA_ON_SRM_ERROR` | no | `false` | |

### 10.3 STGPT (same as collector)

| Name | Required | Default |
|---|---|---|
| `STGPT_API` or `API_KEY` | yes (when STALE) | — | **Same secret as `ci-rca-collector`** — do not create a second key |
| `STGPT_API_URL` | no | `https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps` |
| `STGPT_CLIENT_APP_NAME` / `CLIENT_APP_NAME` | no | `gtrd_srmtdpplm` |
| `RCA_SSL_VERIFY` | no | `true` |
| `TOKEN_BUDGET` | no | `6000` |
| `STRICT` | no | `false` |

---

## 11. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Daily scheduled workflow plus `workflow_dispatch`. | P0 |
| FR-02 | SRM GET with basic auth; filter `SUBMITTED` and `GRANTED`; extract `urn` + `updated.on`. | P0 |
| FR-03 | Parse screenshot-style timestamps; compute age vs 24h window. | P0 |
| FR-04 | Verdict matrix in §7.2. | P0 |
| FR-05 | On `STALE`, open Grafana MCP at `GRAFANA_MCP_URL` from Python. | P0 |
| FR-06 | Execute the read-only tool pack and honour budget/redaction. | P0 |
| FR-07 | Print collection inventory before the AI call. | P0 |
| FR-08 | Send evidence through existing `stgpt_client.post_chat` contract (auth headers, persona, `responseFormat`). | P0 |
| FR-09 | Accept / validate `AnalysisResult` JSON; repair once; fall back to `alfred_for_api`. | P0 |
| FR-10 | Write `summary.md` + `summary.json` + `analysis.json` and publish Job Summary. | P0 |
| FR-11 | Never fail the cron on downstream errors unless `STRICT=true`. | P0 |
| FR-12 | Never persist credentials. | P0 |
| FR-13 | Manual replay with a frozen `as_of` time for testing. | P1 |
| FR-14 | Configurable LogQL pack and dashboard UID/query. | P1 |
| FR-15 | Grafana Explore deeplinks in the report. | P1 |
| FR-16 | Cache AI result by hash of (verdict + urn set + loki query pack version + prompt version) to avoid duplicate daily cost when nothing changed. | P2 |
| FR-17 | Optional second Loki window around each stale `updated.on`. | P2 |

---

## 12. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-01 | Job timeout ≤ 15 minutes. |
| NFR-02 | STGPT timeout 60s (existing client default). |
| NFR-03 | MCP tool-call timeout 30s each. |
| NFR-04 | Deterministic report structure so diffs across days are readable. |
| NFR-05 | Works on `ubuntu-latest` on **both** github.com and github.st.com runners, Python 3.11+. |
| NFR-05a | Source and workflow YAML are host-agnostic. Promotion to github.st.com is clone/push + secret mapping, not a rewrite. |
| NFR-06 | Dependencies stay minimal: `httpx`, `pydantic>=2`, MCP client library, `python-dateutil`. No PyGithub. |
| NFR-07 | All outbound hosts are configurable (SRM, MCP, STGPT) for Enterprise network / custom CA (`SSL_CERT_FILE` / `RCA_SSL_CERT_FILE` already used by collector). |
| NFR-08 | Idempotent: rerunning the same day with same inputs overwrites artifacts, does not double-page operators beyond one summary. |

---

## 13. Decisions log and remaining questions

### 13.1 Locked on 2026-09-18

| Topic | Decision |
|---|---|
| Dashboard | Folder **Distribution**, dashboard **Service Logs**, UID `d68f5a4d-72e6-4b16-b166-a70f41f3cd49`. Filters: `env`, `component`, `level`. |
| Log join key | Full URN string as in logs: `"urn":"strn:distribution:DeliveryRequest:300"` (`300` = request number). |
| Timestamp TZ | UTC / GMT |
| Stale rule | **Every** SUBMITTED/GRANTED row older than 24h (`STALE_MODE=any`) |
| Empty queue | Not an incident. No Grafana MCP. No STGPT. |
| MCP transport | **SSE**. Client reads `GRAFANA_MCP_URL`. |
| Code home | **New project.** Develop on **github.com + Grok CLI**, then move the tree to **github.st.com** for the production daily workflow. Vendor STGPT client; same API key secret as `ci-rca-collector`. |
| Cron | `30 2 * * *` UTC (08:00 IST) on **github.st.com**. Disabled or dispatch-only on github.com. |
| GitHub host | Never hardcode `github.com`. Use `GITHUB_SERVER_URL` / `GITHUB_API_URL`. GHE API = `https://github.st.com/api/v3`. |

### 13.2 SRM JSON field names — owned by implementation (no user input)

The screenshot `jq` is garbled. The new project **must discover keys itself** on the first successful GET:

1. Request the DeliveryRequest collection.
2. Walk each object for:
   - `state` (exact `SUBMITTED` / `GRANTED`)
   - URN: first hit among `urn`, `_urn`, `id`, or any string matching `strn:distribution:DeliveryRequest:<n>`
   - timestamp: first hit among `updated.on`, `_updated.on`, `updatedOn`, `lastUpdated`, or any date-like string
3. Persist the resolved key path into `rca-srm/srm.json` (`key_map`) so later runs are stable.
4. If nothing matches, verdict `SRM_ERROR` with a redacted key inventory — still no secrets in the report.

No extra sample from the operator is required.

### 13.3 Still optional later

- Loki datasource UID (auto-discover via `list_datasources` if unset).
- Extra SRM fields (owner, target env, error message) if the resource has them.
- Report distribution beyond GitHub Job Summary (email / Teams).
- `STRICT` default for the daily run (remains `false`).
- Exact MCP SSE path suffix and auth header name on `GRAFANA_MCP_URL`.

---

## 14. Acceptance criteria

A reviewer can sign this off when a future implementation (not this PRD) does all of the following on a fixture day equivalent to the attached screenshot (records dated 26–27 Aug 2026, run date 18 Sep 2026):

1. Classifies the run as `STALE`.
2. Does **not** call STGPT if a fixture is constructed with all timestamps inside 24h.
3. Reads `GRAFANA_MCP_URL` from the environment and performs at least one Loki query tool call on `STALE`.
4. Sends STGPT a body that matches §5.1 (clientAppName, persona, auth headers, `responseFormat`).
5. Produces `summary.md` containing verdict tables, collection inventory, root cause, citations, and solutions.
6. Redacts basic-auth and tokens from all artifacts.
7. Exits 0 when Grafana or STGPT is mocked to fail, and still writes a partial summary.

---

## 15. Risks

| Risk | Mitigation |
|---|---|
| Screenshot jq paths are wrong → empty parse | Capture one live JSON sample in fixtures before coding parsers. |
| MCP URL is stdio-oriented, not HTTP | Document transport in config; allow a thin adapter. |
| LogQL too broad → token blow-up / noisy RCA | stats-first, limits, budget trim, configurable pack. |
| Citation source enum change breaks CI collector | Additive sources or a product-specific allow-list. |
| Daily STGPT cost on perpetual stale queue | Cache key on urn-set + evidence hash; skip AI if unchanged. |
| Treating old GRANTED rows as incidents forever | `STALE_MODE=newest` or state-age SLO per state. |

---

## 16. Proposed delivery slices (for a later implementation phase)

Not in scope to build now. Listed so the PRD can be scheduled.

| Slice | Scope |
|---|---|
| S0 | New repo on **github.com**; Grok CLI inner loop; fixtures from the screenshot |
| S1 | SRM client + 24h verdict + `summary.md` without Grafana/AI |
| S2 | Grafana MCP **SSE** client using `GRAFANA_MCP_URL`; `Service Logs` UID + URN LogQL pack |
| S3 | Vendor `stgpt_client` / analyze loop; same API key secret as `ci-rca-collector` |
| S4 | Actions workflow on github.com as `workflow_dispatch` only (prove the job) |
| S5 | Move repo to **github.st.com**; map secrets; enable `30 2 * * *` UTC cron; custom CA if required |

---

## 17. Appendix A — Attached probe (as specified)

Host prompt in the screenshot: `tdpplm@gnbsx22061`.

Submitted probe (reconstructed):

```bash
curl -s -u udevopsdm:"$SRM_BASIC_PASSWORD" \
  https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest \
  | jq -c '.[] | select(.state=="SUBMITTED") | {urn: ._urn, updated: ._updated.on}'
```

Granted probe (reconstructed):

```bash
curl -s -u udevopsdm:"$SRM_BASIC_PASSWORD" \
  https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest \
  | jq -c '.[] | select(.state=="GRANTED") | {urn: ._urn, updated: ._updated.on}'
```

Implementation must treat the live JSON schema as authoritative once a sample is provided. The screenshot’s `jq` filter text is partially garbled.

---

## 18. Appendix B — Mapping to ci-rca-collector

| Collector piece | Reuse in this product |
|---|---|
| `stgpt_client.post_chat` | Identical call path |
| Personas `trinity_for_api` / `alfred_for_api` | Identical order |
| `AnalysisResult` / `AnalysisRecord` | Identical output files |
| `prompt.py` evidence wrapper | Same `<EVIDENCE>` discipline, new sections |
| `redact.py` | Required |
| Drain3 / GitHub run collection | **Not used** |
| `rca.yml` reusable workflow | Pattern only; new workflow file |

This product is a **sibling daily monitor**, not a new mode of GitHub Actions log collection.
