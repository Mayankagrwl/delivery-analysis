# Implementation prompt — multi-environment SRM + workflow inputs

Paste this into Claude Code from the root of the `delivery-analysis` repo.

---

You are working in the **delivery-analysis** repo (daily SRM `DeliveryRequest`
staleness check → Grafana Loki evidence → STGPT RCA). Read `AGENTS.md` and
`docs/PRD-daily-delivery-request-staleness-rca.md` before you start, and keep
every non-negotiable there intact (never log secrets; job exits 0 unless
`STRICT=true`; read-only Grafana tools; don't hardcode `github.com` as the API
host; don't invent Grafana UIDs).

Implement the three requirements below as one coherent change. Do **not** push
to any remote — I will review and push myself.

## Background: current URL handling (what to replace)

- `src/delivery_analysis/config.py` hardcodes
  `DEFAULT_SRM_BASE_URL = "https://trd-srm.st.com/resources/strn:distribution:DeliveryRequest"`
  and `load_settings` resolves the SRM URL as `--url` → `SRM_BASE_URL` env → that default.
- `run_collect` (`collect.py`) takes `url`, calls `fetch_delivery_requests`, then
  `evaluate_payload`. The CLI (`cli.py`) exposes `collect --as-of --out-dir --url --strict --analyze`.
- The workflow (`.github/workflows/delivery-analysis.yml`) is `workflow_dispatch`
  only, with a single optional input `as_of`, and runs
  `python -m src.delivery_analysis.cli collect --analyze [--as-of ...]`, then
  cats `rca-srm/summary.md` into the job summary and uploads `rca-srm/`.

## Requirement 1 — environment-aware SRM URLs

We have five environments. The URL shape differs for production:

| env    | URL |
|--------|-----|
| `test` | `https://trd.st.com/distribution/test/resources/strn:distribution:DeliveryRequest` |
| `int`  | `https://trd.st.com/distribution/int/resources/strn:distribution:DeliveryRequest`  |
| `qa`   | `https://trd.st.com/distribution/qa/resources/strn:distribution:DeliveryRequest`   |
| `demo` | `https://trd.st.com/distribution/demo/resources/strn:distribution:DeliveryRequest` |
| `prod` | `https://trd.st.com/resources/strn:distribution:DeliveryRequest` (no `/distribution/<env>` segment) |

Do this:

- **Remove** the old `https://trd-srm.st.com/...` hardcoding and any other
  hardcoded SRM URL/host scattered in the code or defaults.
- In `config.py` add:
  - `DEFAULT_SRM_HOST = "https://trd.st.com"` (overridable via a new `SRM_BASE_HOST`
    variable so the host is not hardcoded as the only option).
  - `SRM_RESOURCE_PATH = "resources/strn:distribution:DeliveryRequest"`.
  - `SRM_ENVIRONMENTS = ("test", "int", "qa", "demo", "prod")`.
  - A pure function `srm_url_for_env(env: str, *, host: str | None = None) -> str`
    that builds the URL: for `prod`/`production`/empty → `{host}/{SRM_RESOURCE_PATH}`;
    for a known non-prod env → `{host}/distribution/{env}/{SRM_RESOURCE_PATH}`;
    for an unknown env → raise `ValueError` with the allowed list. Normalize env
    to lowercase and strip whitespace. Strip a trailing `/` from host.
- Add `srm_env: str` (and `srm_base_host: str`) to the `Settings` dataclass, read
  from `SRM_ENV` (default `prod`) and `SRM_BASE_HOST`.
- **URL precedence** in `load_settings` and `run_collect`: explicit `--url` >
  `SRM_BASE_URL` env (kept as a full-URL escape hatch) > `srm_url_for_env(srm_env, host)`.
  Keep the existing `--url` override working; it still wins.
- `safe_url()` (in `srm.py`) already strips creds/query — make sure the env URLs
  render cleanly through it in artifacts and logs.

## Requirement 2 — manual workflow with environment selection (default ALL)

- Add a `workflow_dispatch` **choice** input `environment` with options
  `ALL, test, int, qa, demo, prod` and **default `ALL`** (GitHub Actions choice
  options must be listed statically — list them explicitly).
- The CLI must accept a new `--env` option on `collect` that takes any single env
  **or** `ALL` (case-insensitive), defaulting to `SRM_ENV` env var, else `ALL`.
  - When `--env` is a single environment: behave as today but against that env's URL,
    writing artifacts to `rca-srm/<env>/` (subfolder per env).
  - When `--env ALL`: loop over `SRM_ENVIRONMENTS` sequentially, running the full
    collect/analyze pipeline per env into `rca-srm/<env>/`, isolating failures so
    one env's error never aborts the others (still exit 0 unless `STRICT`, matching
    the existing philosophy — under `STRICT`, exit non-zero if any env errored).
  - After the loop (or single run) write an aggregated **`rca-srm/summary.md`**
    index at the top level: one row/section per env with its verdict + reason and a
    link to `rca-srm/<env>/summary.md`. The workflow's job-summary step keeps cat-ing
    `rca-srm/summary.md`, so it now shows the roll-up.
- Update the workflow run step to pass `--env "$ENVIRONMENT"`.
- Keep uploading the whole `rca-srm/` artifact (now containing per-env subfolders).
- Add `SRM_ENV`, `SRM_BASE_HOST` to the workflow `env:` block (as `vars.*`) so they
  can be overridden per host without code changes.

> Decision (adjust if you prefer): I chose a **single-job sequential loop** over a
> dynamic matrix, because it yields one aggregated artifact + one job summary and
> keeps the "partial results, exit 0" contract simple. If you'd rather have parallel
> per-env jobs with separate summaries, say so and switch to a matrix (a setup job
> emitting a JSON env list, `strategy.matrix` over it, one artifact per env).

> Decision: `ALL` **includes `prod`**. If prod should be excluded from ALL runs,
> gate it behind a variable (e.g. `INCLUDE_PROD_IN_ALL`, default false) instead.

## Requirement 3 — optional request-ID filter

- Add an optional `workflow_dispatch` string input `request_id` (the numeric
  DeliveryRequest id, e.g. `123456`), and a matching `--request-id` CLI option
  (read `REQUEST_ID` env as fallback). Empty/absent = analyze all records (today's
  behavior).
- When `request_id` is set, restrict the pipeline to the single URN
  `strn:distribution:DeliveryRequest:<request_id>`:
  - Filter SRM records to only that URN id **before** verdict evaluation (match on
    the numeric id at the end of the URN, robust to prefix variations). Do this in
    `evaluate_payload`/`run_collect` via a new `request_id` param — filtering here
    means the verdict, Grafana time-packs/LogQL (`stale_urns`), and STGPT evidence
    all naturally scope to that one request, no other module needs URN-injection logic.
  - Accept a bare number or a full URN as input; extract the trailing id.
  - If the id isn't present in `SUBMITTED`/`GRANTED`, the verdict is
    `NO_RECORDS`/`FRESH` as usual (record a collection note that the requested id was
    not found / not stale). Do **not** force Grafana/STGPT for a fresh single id.
- Surface the active `request_id` in `summary.md` (window section) and in a
  collection note so it's obvious the run was scoped.

## Cross-cutting

- Add an `environment: str | None` field to `SrmResult` (`models.py`) and show it in
  the per-env `summary.md` title/window so artifacts are self-identifying.
- Keep Python 3.11+, deps unchanged (`httpx`, `pydantic>=2`, `python-dateutil`).
- Keep all secret-redaction behavior; the new URLs contain no secrets but still
  route through `safe_url`/redaction paths.

## Tests (extend `tests/`, keep them green)

Add unit tests covering:
- `srm_url_for_env` for each env incl. `prod` (root path, no `/distribution/`),
  unknown-env `ValueError`, and `SRM_BASE_HOST` override.
- URL precedence: `--url` > `SRM_BASE_URL` > computed-from-env.
- CLI `--env ALL` loops all five envs into `rca-srm/<env>/` and writes an aggregated
  top-level `summary.md`; `--env qa` writes only `rca-srm/qa/`.
- `--request-id`: records filtered to the one URN; verdict/evidence scope to it;
  not-found id yields a NO_RECORDS note; bare-number and full-URN inputs both work.
- One env failing under a non-STRICT ALL run still produces other envs' artifacts and
  exits 0; under STRICT it exits non-zero.

Run the existing suite plus the new tests and make sure everything passes:
`python -m pytest -q`.

## Docs

- Update `README.md` (the "Run on github.com" section and the variables list) and
  `AGENTS.md` to document `environment` (ALL default), `request_id`, `SRM_ENV`,
  `SRM_BASE_HOST`, and the per-env `rca-srm/<env>/` layout. Note that prod uses the
  root path with no `/distribution/<env>/` segment.

## Acceptance criteria

1. No `trd-srm.st.com` or other hardcoded SRM URL remains; URLs are derived per env.
2. Manual dispatch shows an `environment` dropdown (default `ALL`) and an optional
   `request_id` box.
3. `--env ALL` analyzes all five envs with isolated failures and an aggregated
   summary; a specific env analyzes just that one.
4. `request_id` scopes the entire pipeline to that single DeliveryRequest.
5. Secrets never logged; job exits 0 unless `STRICT=true`; all tests pass.

Work in a feature branch, keep commits focused, and stop before pushing.
