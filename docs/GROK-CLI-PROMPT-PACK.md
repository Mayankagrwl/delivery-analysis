# Grok CLI — how to feed this project (Windows)

Work from **`D:\delivery-analysis`**.  
Grok Build (`grok`) uses the **current directory** as the project. Always `cd` there first.

Official CLI: install with PowerShell `irm https://x.ai/cli/install.ps1 | iex`, then `grok`.  
Attach files in a prompt with `@path`. Grok also reads `AGENTS.md` automatically.

---

## 0. One-time machine setup

In **PowerShell**:

```powershell
# Install Grok Build if needed
irm https://x.ai/cli/install.ps1 | iex
grok --version

# First login (browser)
grok login

# Project folder
cd D:\delivery-analysis
```

Copy these files into that folder **before** the first `grok` session:

| File | Put it here |
|---|---|
| The PRD | `D:\delivery-analysis\docs\PRD-daily-delivery-request-staleness-rca.md` |
| This pack | `D:\delivery-analysis\docs\GROK-CLI-PROMPT-PACK.md` |
| Project rules | `D:\delivery-analysis\AGENTS.md` (create with Prompt 0) |

Optional check:

```powershell
cd D:\delivery-analysis
grok inspect
```

You should see `AGENTS.md` listed once it exists.

---

## How to feed a prompt (do this every session)

**Preferred: interactive TUI (best for a multi-file project)**

```powershell
cd D:\delivery-analysis
grok
```

Then paste **one prompt at a time** from the list below. Wait until Grok finishes writing files and you have reviewed the diff. Do not paste Prompt 3 while Prompt 2 is still running.

Attach the PRD when the prompt needs it:

```
@docs/PRD-daily-delivery-request-staleness-rca.md
```

**Headless one-shot (optional):**

```powershell
cd D:\delivery-analysis
grok -p "your prompt here" --cwd D:\delivery-analysis
```

**Resume yesterday’s session:**

```powershell
cd D:\delivery-analysis
grok -c
```

Rules while feeding:

1. One slice per prompt (S0 → S5). Do not ask it to “build the whole PRD” in one shot.
2. After each slice: `git status`, skim the diff, then `git add` / commit yourself (or tell Grok to commit but **not push** until you say so).
3. Never paste SRM passwords, STGPT keys, or Grafana tokens into a prompt. Point Grok at env var *names* only.
4. On github.com, cron stays **off**. Production cron is github.st.com later.

---

## Prompt 0 — bootstrap AGENTS.md + repo skeleton

Paste this as the **first** message after `cd D:\delivery-analysis` and `grok`:

```
Create the project scaffolding only. Do not implement SRM, Grafana, or STGPT clients yet.

1. Write AGENTS.md at the repo root with these non-negotiables:
   - This is delivery-analysis: daily SRM DeliveryRequest staleness + Grafana Loki RCA + STGPT.
   - Spec: @docs/PRD-daily-delivery-request-staleness-rca.md (follow it; do not invent extra product scope).
   - New project. Do not fold this into ci-rca-collector. Vendor STGPT client later.
   - Develop on github.com with Grok CLI. Production workflow will move to github.st.com.
   - Never hardcode github.com / api.github.com as the only host. Use GITHUB_SERVER_URL / GITHUB_API_URL. GHE API is {server}/api/v3.
   - Never log or write secrets (SRM basic auth, STGPT_API / API_KEY, GRAFANA_MCP_TOKEN).
   - Daily job must exit 0 unless STRICT=true. Partial summary on failure.
   - On github.com: workflow_dispatch only. Do not enable cron here.
   - Python 3.11+, deps: httpx, pydantic>=2, python-dateutil, plus a small MCP SSE client later.
   - STGPT call contract is PRD §5 / §5.1.1. Personas trinity_for_api then alfred_for_api.

2. Create:
   - README.md (short: what it is, Phase A github.com / Phase B github.st.com)
   - .gitignore (venv, __pycache__, .env, rca-srm/, .rca-cache/)
   - requirements.txt (pinned loosely as in PRD)
   - src/delivery_analysis/__init__.py (empty package)
   - .github/workflows/delivery-analysis.yml with workflow_dispatch only (no schedule), a placeholder job that prints "not implemented", runs-on ubuntu-latest
   - docs/ folder if missing

3. Do not call external networks. Do not invent Grafana UIDs other than d68f5a4d-72e6-4b16-b166-a70f41f3cd49.
4. Stop and show me the file tree when done.
```

If the PRD is not in `docs/` yet, first drop it there, or attach it:

```
Also read @docs/PRD-daily-delivery-request-staleness-rca.md and keep AGENTS.md consistent with v1.2.
```

---

## Prompt 1 — S1: SRM client + 24h verdict (no Grafana, no AI)

```
Implement S1 only from @docs/PRD-daily-delivery-request-staleness-rca.md.

Scope:
- HTTP GET DeliveryRequest with basic auth from env SRM_BASIC_USER / SRM_BASIC_PASSWORD
- Default URL env SRM_BASE_URL = https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest
- Auto-discover JSON keys (urn / _urn / updated.on / _updated.on / state) as PRD §13.2
- Filter SUBMITTED and GRANTED
- Parse timestamps like "8/27/2026 9:30:43 AM" as UTC
- STALE_MODE=any, STALE_HOURS=24
- Verdicts: FRESH | STALE | NO_RECORDS | SRM_ERROR
- NO_RECORDS is not an incident: no further calls
- Write rca-srm/srm.json and rca-srm/summary.md (verdict + tables only)
- CLI: python -m src.delivery_analysis.cli collect
- Unit tests with the screenshot fixture:
    SUBMITTED 43 @ 8/27/2026 9:30:43 AM
    GRANTED 38 @ 8/26/2026 3:19:56 PM
    GRANTED 39 @ 8/27/2026 7:41:26 AM
    GRANTED 40 @ 8/27/2026 8:21:34 AM
  Frozen as_of=2026-09-18T08:00:00Z must yield STALE.

Do not implement Grafana MCP or STGPT in this prompt.
Never put real passwords in files. Use env vars.
Run the tests. Fix failures. Stop when tests pass.
```

---

## Prompt 2 — S2: Grafana MCP SSE + Service Logs + URN LogQL

```
Implement S2 only. Keep S1 working.

Read @docs/PRD-daily-delivery-request-staleness-rca.md §6.

Add a Grafana MCP SSE client that:
- Reads GRAFANA_MCP_URL (required on STALE)
- Transport SSE only (GRAFANA_MCP_TRANSPORT=sse)
- Optional GRAFANA_MCP_TOKEN bearer
- On verdict STALE only (not FRESH, not NO_RECORDS)
- Calls read-only tools in PRD order
- Dashboard UID d68f5a4d-72e6-4b16-b166-a70f41f3cd49 title "Service Logs" folder Distribution
- Honour filters env, component, level when the tool allows variable overrides
- Log join key is the full URN string, e.g. "urn":"strn:distribution:DeliveryRequest:300"
- Try label selector {urn="..."} first, else line filter on the URN
- Limits: 200 lines/query, max 4 queries, 500 chars/line, token budget later
- Write rca-srm/grafana.json + collection inventory section in summary.md
- Timeouts 30s per tool. On MCP failure: collection_notes + continue, exit 0

CLI: python -m src.delivery_analysis.cli collect
(collect now does SRM + conditional Grafana)

Mock the MCP layer in tests. Do not require a live Grafana from this machine.
Do not implement STGPT yet.
```

---

## Prompt 3 — S3: STGPT analyze (same contract as ci-rca-collector)

```
Implement S3 only. Wire AI analysis after Grafana collection when verdict is STALE.

Copy the STGPT contract from @docs/PRD-daily-delivery-request-staleness-rca.md §5 and §5.1.1 exactly.

Preferred: vendor a local module src/delivery_analysis/stgpt_client.py that matches
https://github.com/Mayankagrwl/ci-rca-collector/blob/master/tools/rca/stgpt_client.py
Auth: SHA1(f"{clientAppName}_{service}_{api_key}_{timestamp}_{nonce}")
Headers: stchatgpt-auth-token / stchatgpt-auth-nonce / stchatgpt-auth-timestamp
POST STGPT_API_URL default https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps
clientAppName default gtrd_srmtdpplm
Key resolution: STGPT_API then API_KEY (same secret name as ci-rca-collector)
Personas: trinity_for_api, one repair, then alfred_for_api
responseFormat json_object, retry text if rejected
Expected JSON: root_cause, suggested_fix, confidence, citations[], cannot_determine
Citation sources include the new SRM/Loki/grafana_* literals from the PRD
Pack evidence in <EVIDENCE>, cap TOKEN_BUDGET=6000
Write rca-srm/analysis.json (AnalysisRecord) and append AI section to summary.md
Never log the API key or token.

CLI:
  python -m src.delivery_analysis.cli collect
  python -m src.delivery_analysis.cli analyze   # or collect --analyze

Tests: mock post_chat; do not call the real bridge from unit tests.
On bridge failure: status bridge_error, still write summary, exit 0.
```

If Grok can reach GitHub, you can add:

```
Fetch and mirror the request/response behaviour from
https://github.com/Mayankagrwl/ci-rca-collector/blob/master/tools/rca/stgpt_client.py
and analyze.py. Do not copy Drain3, GitHub run collection, or kubectl.
```

---

## Prompt 4 — S4: GitHub.com Actions (dispatch only)

```
Implement S4 for github.com bring-up.

Update .github/workflows/delivery-analysis.yml:
- on.workflow_dispatch with optional input as_of
- NO on.schedule on this host
- setup-python 3.11, pip install -r requirements.txt
- run: python -m src.delivery_analysis.cli collect --analyze
- env from secrets/vars using the PRD names only (SRM_*, GRAFANA_MCP_URL, STGPT_API or API_KEY, etc.)
- upload-artifact rca-srm/
- always write Job Summary from rca-srm/summary.md
- timeout-minutes: 15
- exit 0 behaviour already in CLI
- runs-on: ubuntu-latest (override later on GHE if needed)
- Do not hardcode github.com URLs

Add a short README section: how to set repo secrets on github.com, how to click Run workflow.

Do not add the cron yet.
```

Then, in a **new** empty github.com repo you create (example name `delivery-analysis`):

```powershell
cd D:\delivery-analysis
git init
git add .
git commit -m "S4: dispatch-only delivery-analysis workflow"
git branch -M main
git remote add origin https://github.com/<you>/delivery-analysis.git
git push -u origin main
```

Grok CLI can draft the commit message; **you** create the empty repo and push.

---

## Prompt 5 — S5 notes only (do this after the move to github.st.com)

Do **not** run this while still on github.com. After the tree lives on github.st.com:

```
We are now on github.st.com. Enable production schedule without rewriting Python.

1. In .github/workflows/delivery-analysis.yml add:
   on.schedule: cron "30 2 * * *"
   keep workflow_dispatch
2. Document secret mapping on GHE: same names, same STGPT_API / API_KEY as ci-rca-collector.
3. Honour SSL_CERT_FILE / RCA_SSL_CERT_FILE.
4. Confirm no hardcoded github.com API host remains (grep the repo).
5. README: Phase B production host is github.st.com.

Do not change SRM / Grafana / STGPT logic.
```

---

## Daily inner-loop cheatsheet

| You want | Type this |
|---|---|
| Start | `cd D:\delivery-analysis` then `grok` |
| Point at PRD | `@docs/PRD-daily-delivery-request-staleness-rca.md` |
| Point at one module | `@src/delivery_analysis/cli.py` |
| Continue last chat | `grok -c` |
| See what Grok loaded | `grok inspect` |
| Tests only | In TUI: `Run pytest and fix failures. Do not add features.` |
| Stop the agent expanding scope | `Stop. S2 only. Revert anything outside S2.` |

---

## What not to paste into Grok

- `udevopsdm` password from the screenshot (`xxx`)
- `STGPT_API` / `API_KEY` value
- `GRAFANA_MCP_TOKEN`
- Full `.env` files

Safe to paste: env *names*, dashboard UID, URN examples, the PRD, public `ci-rca-collector` paths.
}
