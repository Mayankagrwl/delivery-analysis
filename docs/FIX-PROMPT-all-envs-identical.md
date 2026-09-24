# Fix prompt — ALL run returns identical results for every environment

Paste the short execute prompt (bottom) into Claude Code from the repo root.

---

You are working in the **delivery-analysis** repo. Read `AGENTS.md` first and keep
every non-negotiable intact (read-only Grafana/Tempo tools, never log secrets, exit 0
unless `STRICT=true`, no hardcoded UIDs). Work directly on `main` (no feature branch).
Do **not** push.

## Symptom

An `--env ALL` run produces **identical results for all five environments** — same
request id, same time window, same Loki logs, same analysis — even though the five SRM
URLs return different data when hit manually with curl.

The SRM URLs are:
- `https://trd.st.com/distribution/test/resources/strn:distribution:DeliveryRequest`
- `https://trd.st.com/distribution/int/resources/strn:distribution:DeliveryRequest`
- `https://trd.st.com/distribution/qa/resources/strn:distribution:DeliveryRequest`
- `https://trd.st.com/distribution/demo/resources/strn:distribution:DeliveryRequest`
- `https://trd.st.com/resources/strn:distribution:DeliveryRequest`  (production — **no**
  `/distribution/<env>/` segment)

## Root cause #1 (primary) — the SRM URL override collapses all envs to one URL

In `src/delivery_analysis/collect.py`, `run_collect_environments` builds each env's URL as:

```python
env_url = cfg.srm_base_url_override or srm_url_for_env(target, host=cfg.srm_base_host)
```

`srm_base_url_override` is set from `--url` **or the `SRM_BASE_URL` env var**, and the
workflow passes `SRM_BASE_URL: ${{ vars.SRM_BASE_URL }}`. If that variable is set to
anything (which it is in the user's setup), **every** environment in an ALL run uses that
single override URL → identical SRM payload → identical stale records, request id, and
downstream Loki/analysis for all five envs. The per-env `srm_url_for_env(target)` is
never reached.

### Fix #1

The full-URL escape hatch must apply to **at most one** environment, never to an ALL /
multi-env run:

- In `run_collect_environments`, only honor `cfg.srm_base_url_override` (and the `--url`
  argument) when exactly one environment is being processed **and** that env was
  explicitly selected (not ALL). When `len(targets) > 1` (ALL), **ignore** the override
  and always use `srm_url_for_env(target, host=cfg.srm_base_host)` for each env, so every
  env hits its own URL. If an override is present and ignored because of ALL, add a
  one-line note/log ("SRM_BASE_URL override ignored in ALL mode; using per-env URLs").
- Verify `srm_url_for_env` still yields the exact URLs above (prod = root path, others
  nested under `/distribution/<env>/`).
- Print each env's resolved SRM URL (host+path only, via `safe_url`) in the per-env
  output/log and in the per-env `summary.md`, so it is provable that the five envs used
  five different URLs. The top-level index should also show each env's URL.
- Consider whether the workflow should even pass `SRM_BASE_URL` when the intent is
  per-env: keep the variable for a genuine single-env escape hatch, but the code change
  above makes ALL correct regardless of whether the variable is set.

## Root cause #2 (verify + harden) — Loki `environment` label scoping

Commit `8e34f26` added per-env Loki scoping (`loki_environment_value`,
`component_selector(..., environment=...)`, `build_priority_logql(..., environment=...)`,
and `_Collector` reading `srm.environment`). Confirm this is actually threaded through
**every** query path and matches the real Loki labels:

- The Loki label **key is `environment`** (not `env`; `env` is only a dashboard
  variable). The value for production is **`production`** (not `prod`); for the others it
  is the env name (`test`, `int`, `qa`, `demo`). `loki_environment_value` already maps
  prod→`production` and passes the rest through — keep it, and make the mapping
  overridable via config if needed (e.g. `GRAFANA_ENV_LABEL_VALUES` like
  `test=test,int=int,qa=qa,demo=demo,prod=production`), defaulting to that.
- Ensure the `environment="<value>"` matcher is present in **all** Loki queries for the
  env: the priority queries, the per-URN queries, the **Notification success probe**, the
  panel-derived queries, and the stats query. Any query that omits it will pull another
  environment's logs. Fix any path that still lacks it.
- Do **not** put `environment` in the SRM URL — it only belongs in the Loki label.
- Record the applied `environment` label value in each env's `summary.md` and in the
  LogQL shown, so the scoping is visible.

## Constraints

- Reporting/query-scoping and URL-selection only. Do **not** change staleness/verdict
  logic, STGPT logic, or the request_id success/Tempo gates.
- Keep single-env runs and the legacy single-host behavior working (when only one env is
  selected, the `--url`/`SRM_BASE_URL` override still applies as before).

## Tests (extend `tests/`, keep the suite green)

- `--env ALL` with `SRM_BASE_URL` set → each env still resolves to its own
  `srm_url_for_env(env)` URL (assert all five differ and prod has no `/distribution/`),
  and the override is ignored; a single explicit `--env qa --url X` still uses `X`.
- Mock SRM returning different payloads per URL → the five envs produce different
  records/verdicts (not identical).
- Every Loki query built for an env (priority, per-URN, notification probe, panels,
  stats) contains `environment="<mapped value>"`; prod maps to `production`.
- `loki_environment_value`: test/int/qa/demo pass through, prod/production/empty →
  `production`; optional `GRAFANA_ENV_LABEL_VALUES` override respected.
- The per-env `summary.md` shows the resolved SRM URL and the environment label value.

Run `python -m pytest -q` until green.

## Docs

Update `README.md`/`AGENTS.md`: `SRM_BASE_URL`/`--url` is a single-env escape hatch and is
ignored in ALL runs (each env uses its own URL); the Loki label key is `environment` with
prod→`production`; and any `GRAFANA_ENV_LABEL_VALUES` override.

## Acceptance criteria

1. `--env ALL` produces **different** SRM data and Loki logs per environment; results are
   no longer identical across envs.
2. Each env's resolved SRM URL is shown and matches the five URLs above (prod = root
   path).
3. Every Loki query for an env carries `environment="<value>"` (prod → `production`).
4. Single-env `--url`/`SRM_BASE_URL` override still works; no verdict/STGPT/gate logic
   changed; secrets never logged; exit 0 unless `STRICT=true`; all tests pass.

Commit to `main` with a clear message. Do not push.
