# Fix prompt — restore full per-environment detail (Grafana evidence + AI analysis) in the Job Summary

Paste the short execute prompt (bottom) into Claude Code from the repo root.

---

You are working in the **delivery-analysis** repo. Read `AGENTS.md` first and keep
every non-negotiable intact (read-only Grafana/Tempo tools, never log secrets, exit 0
unless `STRICT=true`, no hardcoded UIDs). Work directly on `main` (no feature branch).
Do **not** push.

## Symptom

After the multi-environment change, a STALE run's GitHub Actions **Job Summary** shows
only the aggregated env/verdict/reason table. The Grafana/Loki evidence and the **AI
analysis with citations** — which used to appear in the Job Summary before
`IMPLEMENTATION-PROMPT-multi-env.md` — are no longer visible there. It looks like
evidence was not collected and STGPT was not called.

## Root cause (already diagnosed — verify, then fix)

This is a **reporting/visibility regression, not a collection/analysis break**:

- The workflow Job Summary step runs `cat rca-srm/summary.md`.
- Before multi-env, `rca-srm/summary.md` was the full single-env report
  (`render_summary_md`) — Grafana inventory, evidence highlights, and the AI analysis
  with citations.
- After multi-env, `rca-srm/summary.md` is the aggregated **index**
  (`report.py::render_index_md` via `write_index`) — just a table with links to
  `rca-srm/<env>/summary.md`. It does not embed the per-env detail, and `write_index`
  only receives the `SrmResult` objects, not the Grafana/AnalysisRecord data.
- The full per-env report is still generated and written to
  `rca-srm/<env>/summary.md` (via `run_collect` → `write_artifacts`) and uploaded in
  the `rca-srm` artifact. The STALE code path is intact: `_maybe_grafana` calls
  `collect_grafana` for STALE, and `_maybe_analyze` calls `analyze_staleness` for
  STALE.

**First, verify** the above before changing anything: run the existing tests /
inspect the code to confirm that for a STALE env, `run_collect` still calls
`collect_grafana` and `analyze_staleness` and that `render_summary_md` (per-env) still
renders the Grafana inventory, evidence highlights, and AI analysis sections. If any of
that is genuinely broken (not just missing from the Job Summary), fix that too and say
so. Otherwise proceed with the visibility fix below.

## Fix — embed each environment's full report in the top-level summary

Make the top-level `rca-srm/summary.md` self-contained so the Job Summary shows the
quick index table **and then the full per-environment report** (Grafana/Loki evidence +
AI analysis with citations) for every env — restoring the pre-multi-env experience,
and for a single-env run making the Job Summary equivalent to the old output.

- In `report.py`, after `write_artifacts` has written each `rca-srm/<env>/summary.md`
  (it runs before `write_index` in `run_collect_environments`, so the files exist),
  change `write_index` so the top-level `summary.md` = the index table **followed by**
  each env's full per-env report content, under a clear `## Environment: <env>`
  separator heading (in the same env order as the table).
  - Preferred: pass the per-env rendered report into the index rather than re-reading
    files — e.g. have `run_collect` / `run_collect_environments` keep the rendered
    per-env markdown (or the `GrafanaResult` + `AnalysisRecord`) and hand it to
    `write_index` / a new `render_index_md(..., env_reports=...)`, so rendering stays
    in-memory and testable. Reading the just-written `<env>/summary.md` from disk is an
    acceptable fallback if simpler.
  - Keep the per-env `rca-srm/<env>/summary.md` files exactly as they are (artifacts).
  - Keep the links in the table.
- Leave the workflow Job Summary step as `cat rca-srm/summary.md` — after this change
  it will show everything again. (If you prefer, you may additionally have the workflow
  cat per-env files, but the self-contained top-level summary is the primary fix.)
- Watch heading levels so the concatenated document renders cleanly (a per-env report
  starting at `#` under a `## Environment: <env>` heading is fine; demote if you like).
- Do not change any collection, staleness, Grafana, Tempo, or STGPT logic. This is
  reporting only.

## Tests (extend `tests/`, keep the suite green)

- For a multi-env run with at least one STALE env (mock Grafana evidence + an AI
  `AnalysisResult` with citations), the top-level `rca-srm/summary.md` contains:
  the index table, a `## Environment: <env>` section per env, and — for the STALE env —
  the Grafana/Loki evidence and the AI analysis section with its citations (assert on
  the root-cause / citation text).
- The per-env `rca-srm/<env>/summary.md` still renders the full report (regression
  guard).
- A single-env STALE run's top-level `summary.md` includes the full AI analysis
  (equivalent to the old behavior).
- Index table + links are still present.

Run `python -m pytest -q` until green.

## Docs

Note in `README.md` that the top-level `rca-srm/summary.md` (and thus the Job Summary)
now contains the full per-environment detail in addition to the index table.

## Acceptance criteria

1. A STALE run's Job Summary again shows the Grafana/Loki evidence and the AI analysis
   with citations, per environment, plus the index table.
2. Per-env artifact files are unchanged; no collection/analysis logic changed.
3. Secrets never logged; exit 0 unless `STRICT=true`; all tests pass.

Commit to `main` with a clear message. Do not push.
