# Fix prompt — run the Notification-success + Tempo gate for request_id runs regardless of verdict

Paste the short execute prompt (bottom) into Claude Code from the repo root.

---

You are working in the **delivery-analysis** repo. Read `AGENTS.md` and the two
existing specs in `docs/` (`IMPLEMENTATION-PROMPT-multi-env.md`,
`IMPLEMENTATION-PROMPT-notification-success-tempo.md`) first. Keep every
non-negotiable intact (read-only Grafana/Tempo tools, never log secrets, exit 0
unless `STRICT=true`, no hardcoded datasource UIDs). Work on `main` (the user does
not want a feature branch). Do **not** push.

## The bug

When a specific `request_id` is analyzed and that id is **not** in
`SUBMITTED`/`GRANTED`, the verdict is `NO_RECORDS` — which is the normal, expected
state for a request that already completed. But the Notification-success gate and
the Tempo trace never run, so the report just says "Requested DeliveryRequest id N
not found in SUBMITTED/GRANTED. Not an incident." with **no completion evidence and
no trace**.

Root cause is the gating in `src/delivery_analysis/collect.py`:

- `_maybe_grafana()` returns early with `skipped (verdict not STALE)` when
  `result.verdict in {"FRESH", "NO_RECORDS"}`, so `collect_grafana()` (which
  contains the working `_notification_probe` + Tempo path) is never called.
- `_maybe_analyze()` returns `skipped (verdict not STALE)` unless verdict is `STALE`.

The probe itself (`grafana.py::_Collector._notification_probe`) already builds the
URN from `request_id` when there are no stale URNs — it just never gets invoked.

## Required behavior

**Whenever `request_id` is set, the Notification-success probe + Tempo trace MUST run
regardless of verdict** (`STALE`, `FRESH`, `NO_RECORDS`; and `SRM_ERROR` should still
respect the existing `query_grafana_on_srm_error` flag). Then:

- **Notification success found** (a Notification-component line matches
  `"successfully processed"` (DEBUG) or `"mail sent to"` (INFO) for the URN):
  report the request as **SUCCESS** — print those log lines as evidence/citations,
  render the Tempo cascade across components, and **skip STGPT** (status
  `infra_success`, `tokens_used=0`).
- **No success found** in request_id mode — split by staleness:
  - **Not stale** (verdict `FRESH` or `NO_RECORDS`): **do NOT call STGPT/AI.**
    Report an investigate/WARN outcome — the summary must clearly state that no
    Notification completion evidence was found for the id in the window (not a bare
    "not an incident"), but no AI analysis is performed and no tokens are spent.
  - **Stale** (verdict `STALE`): this is a genuine stale incident, so run the STGPT
    analysis as usual on the collected evidence.

So: AI runs only when the request is actually **STALE**. A non-stale request either
resolves to SUCCESS (notification evidence found) or to a no-AI investigate outcome.

The existing all-records staleness path (no `request_id`) must be **unchanged**:
Grafana + STGPT still run only on `STALE` as today.

## Changes

### `collect.py`
- In `_maybe_grafana`: when `request_id` is set (check `result.request_id` /
  `cfg.request_id`), always proceed to `collect_grafana(...)` regardless of verdict
  (except keep the `SRM_ERROR` + `query_grafana_on_srm_error` guard). Keep the
  current STALE-only behavior when no `request_id`.
- In `_maybe_analyze`: when `request_id` is set:
  - if `grafana.notification_success` is `True` → return an `AnalysisRecord`
    with status `infra_success`, the notification lines as citations, no STGPT call.
  - else if `verdict == "STALE"` → run `analyze_staleness(...)` on the collected
    evidence (genuine stale incident).
  - else (not stale, no success) → **do NOT call STGPT.** Return a record with a
    non-AI status (e.g. `gated`/`investigate`) and a note that no completion evidence
    was found and analysis was intentionally not run because the request is not stale.
  - When no `request_id`: keep today's `STALE`-only gating exactly.
- Make sure the infra-success short-circuit still happens for a `request_id` run that
  *is* also STALE (success wins over analysis).

### `grafana.py`
- Add a **configurable lookback window for request_id mode** so an older completed
  request is found. Add `REQUEST_ID_LOOKBACK_HOURS` config (default `168` = 7 days).
  In `build_time_packs` (or where packs are built for the collector), when
  `request_id` is set, ensure the probe window is `now - lookback .. now` (in
  addition to any stall/recent packs), rather than only the recent 24h. Cap the
  number of queries sensibly and keep within the existing LogQL budget logic.
- Everything else in the probe/Tempo path already works — do not rewrite it.

### `config.py`
- Add `REQUEST_ID_LOOKBACK_HOURS` (env `REQUEST_ID_LOOKBACK_HOURS`, default 168) to
  `Settings`.

### `report.py`
- In `request_id` mode, the per-env `summary.md` and the top-level index must surface
  the outcome even when verdict is `NO_RECORDS`/`FRESH`:
  - a clear **SUCCESS** banner with the Notification evidence lines and the Tempo
    cascade when success was found;
  - or a **"No completion evidence found for id N (window: last <lookback>h) —
    investigate"** line when not. For a non-stale request this stands alone (no AI
    section); for a stale request the STGPT analysis is appended as usual.
- The index `reason`/`details` for a request_id run should reflect SUCCESS /
  INVESTIGATE, not just "not an incident".

### `verdict.py`
- Keep `NO_RECORDS` as the SRM verdict, but for a request_id run adjust the reason
  wording so it doesn't imply "all good" on its own (e.g. "Requested id N not in
  SUBMITTED/GRANTED — checking Notification/Tempo for completion"). The final
  success/investigate conclusion comes from the Grafana probe, shown in the report.

## Tests (extend `tests/`, keep the suite green)

- request_id + `NO_RECORDS` + a mocked Notification "mail sent to" / "successfully
  processed" line → Grafana probe runs, `notification_success=True`, STGPT skipped,
  summary shows SUCCESS with the evidence lines and Tempo cascade.
- request_id + `NO_RECORDS`/`FRESH` + no matching notification line → probe runs,
  **STGPT is NOT called**, summary shows the investigate outcome (no bare "not an
  incident", no AI section), tokens spent = 0.
- request_id + `STALE` + no notification success → STGPT **is** invoked (genuine
  stale incident).
- request_id + `STALE` + notification success → still short-circuits to
  `infra_success` (success wins, no AI).
- No request_id (all-records) → Grafana/STGPT still run only on `STALE`
  (regression guard).
- `REQUEST_ID_LOOKBACK_HOURS` widens the probe window in request_id mode.

Run `python -m pytest -q` until green.

## Docs

Update `README.md` and `AGENTS.md`: document that a `request_id` run always checks
Notification completion + Tempo regardless of verdict, the `REQUEST_ID_LOOKBACK_HOURS`
variable, and the SUCCESS vs INVESTIGATE reporting.

## Acceptance criteria

1. A `request_id` run against an id not in SUBMITTED/GRANTED still queries the
   Notification component (incl. DEBUG) and Tempo, and reports SUCCESS with evidence
   + cascade when the completion logs exist.
2. When no completion evidence exists and the request is **not stale**, the run
   reports an investigate/WARN outcome **without any AI call** (0 tokens) — never a
   silent "not an incident". AI runs only when the request is actually `STALE`.
3. The all-records staleness path is unchanged.
4. Secrets never logged; exit 0 unless `STRICT=true`; all tests pass.

Commit to `main` with a clear message. Do not push.
