# Implementation prompt — Notification success gate + Tempo trace cascade

Paste the short execute prompt (bottom of this repo's README note) into Claude Code
from the root of the `delivery-analysis` repo. This spec builds on the already-merged
multi-environment + `request_id` change.

---

You are working in the **delivery-analysis** repo. Read `AGENTS.md` and
`docs/PRD-daily-delivery-request-staleness-rca.md` first and keep every
non-negotiable intact: never log secrets; job exits 0 unless `STRICT=true`;
**Grafana/Tempo tools must be read-only**; don't invent datasource UIDs (they come
from variables); partial summary on failure. Work on a feature branch and do **not**
push — I will review.

Assume the `request_id` workflow input and `--request-id` CLI option already exist
and scope the pipeline to a single `strn:distribution:DeliveryRequest:<id>` URN.

## Goal

When we analyze **one specific Delivery Request** (i.e. `request_id` is provided),
add an **infra-side success gate** based on the **Notification** component logs, and
a **Tempo trace cascade** that follows the request across all components via its
trace id.

The success gate must short-circuit the expensive path: if the Notification
component logged success for this request, the request is **SUCCESS** — do **not**
call STGPT/AI — and the matched success log lines are printed as evidence in the
summary / AI-diagnosis report. If no such success log exists, that is **not** the
expected outcome: something went wrong, so continue the existing Grafana + STGPT
analysis path as today.

## Requirement 1 — Notification success criterion

Scope: runs only when `request_id` is set (single-DR mode). For normal all-records
staleness runs, skip this gate (record a one-line note that it's not applicable).

Success is confirmed when a **Notification component** log line for this request
matches **either** marker (case-insensitive):

- `"successfully processed"` — normally a **DEBUG** line.
- `"mail sent to"` — normally an **INFO** line.

Implementation notes:

- Add config (in `config.py`, read via the existing `_env` helper):
  - `NOTIFICATION_COMPONENT` — default `"notification"` (the Loki `component` value).
  - `NOTIFICATION_SUCCESS_MARKERS` — default `"successfully processed|mail sent to"`
    (pipe-separated; parse into a list; keep it overridable).
  - Expose on `Settings` as `notification_component` and `notification_success_markers: list[str]`.
- In `grafana.py`, add a dedicated **notification success probe** in `_Collector`
  that runs when `self.urns` is a single requested URN (request_id mode). It must
  query the Notification component in Loki for the request URN across the collected
  time packs and scan for the markers.
  - **Critical:** the `"successfully processed"` marker is a **DEBUG** line, and the
    existing `ingest_lines`/`_query_priority_logs` path drops DEBUG unless
    `INCLUDE_DEBUG_LOGS`. The notification probe must query **including DEBUG level**
    for the notification component regardless of the global `INCLUDE_DEBUG_LOGS`
    setting, so success is never missed. Build a dedicated LogQL query
    (`{component="<notification>"} |= "<urn>"` with no level filter, or explicitly
    covering warn|info|debug) rather than reusing the level-tiered pass.
  - Match markers on the raw (redacted) line text, case-insensitive; record which
    marker matched and keep the full matched line(s) as evidence.
- Represent the outcome on `GrafanaResult` (models in `grafana.py`):
  - `notification_checked: bool` (was the probe run)
  - `notification_success: bool | None`
  - `notification_success_lines: list[str]` (the matched evidence lines, capped/redacted)
  - `notification_component: str`
- Wire the short-circuit in `collect.py` `_maybe_analyze` (and the standalone
  `analyze` command): if `grafana.notification_success` is `True`, **skip STGPT**
  and return an `AnalysisRecord` that clearly states infra success (see Requirement 3)
  with the notification lines as citations. If `False`/`None`, proceed with the
  existing persona analysis unchanged.

## Requirement 2 — Tempo trace cascade (TEMPO_ID)

Trace the request (and its subsequent/cascading calls) across all components using
the trace id, resolved through **Tempo**.

- Add config `TEMPO_ID` (read from env var `TEMPO_ID`) → `Settings.tempo_datasource_uid`.
  I will set this as a GitHub Actions **variable**; do not hardcode a value or invent
  one. Also add optional `TRACE_ID_FIELD` (default `"trace_id"`) and accept common
  variants when extracting (`trace_id`, `traceId`, `traceID`, `trace-id`).
- After collecting logs for the request, **extract the trace id** from the log lines
  (prefer notification-component lines, else any distribution line for the URN). Use a
  tolerant regex for the field key plus a hex value (16 or 32 hex chars). Store as
  `GrafanaResult.trace_id`.
- If `tempo_datasource_uid` is set **and** a read-only Tempo/trace query tool is
  available, query Tempo for that trace and build a **cascade summary**: the ordered
  list of spans/services (component/service name, span/operation, status, duration).
  - Discover the tool name defensively from the MCP `list_tools()` set the collector
    already has (`self.available`) — match read-only names containing `tempo` or
    `trace` (e.g. a `query_tempo`/`get_trace`-style tool). Never call write tools
    (respect the existing `WRITE_TOOLS`/prefix guard). Log each call in `tools` like
    the existing Loki calls.
  - Store results on `GrafanaResult`: `tempo_datasource_uid`, `trace_id`,
    `trace_spans: list[dict]` (component/service, name, status, duration_ms), and an
    optional `trace_deeplink`.
- **Graceful degradation:** if `TEMPO_ID` is unset, no trace id is found, or no Tempo
  tool is available, skip with a clear note (`"Tempo trace skipped: <reason>"`) and
  continue. Never fail the job for a missing trace.
- Add `TEMPO_ID` (and `NOTIFICATION_COMPONENT` if you want it overridable per host) to
  the workflow `env:` block as `${{ vars.TEMPO_ID }}` etc.

## Requirement 3 — Reporting the success + trace

In `report.py` and the evidence/prompt path:

- Add a **`## Infra success check (Notification)`** section to `summary.md`:
  - When not applicable (no `request_id`): one line saying so.
  - When SUCCESS: a clear **SUCCESS** banner ("Request marked successful from infra
    side — no AI analysis needed"), the matched marker(s), and the notification log
    lines as fenced evidence.
  - When NOT FOUND: state that no notification success log was found for the request
    and that analysis proceeded.
- Add a **`## Request trace (Tempo)`** section: trace id, the cascade table
  (component/service → span → status → duration), and the Tempo deeplink if present;
  or the skip note.
- The **AI analysis** section must reflect the gate: on infra success, show
  status like `infra_success` (add this value to the `AnalysisStatus` Literal in
  `models.py`), confidence not applicable, and the notification lines as citations —
  making it read as a green/successful diagnosis rather than an STGPT result.
- When success short-circuits STGPT, `analysis.json` should record
  `status="infra_success"`, `fallback_used=False`, `tokens_used=0`, and the evidence
  lines; no bridge call is made.

## Cross-cutting

- Keep secret redaction on every new line/query that reaches artifacts or logs.
- No new hard-coded UIDs or hosts; all datasource UIDs come from variables.
- Python 3.11+, existing deps only.

## Tests (extend `tests/`, keep the suite green)

- Notification success detection: a notification-component DEBUG line with
  "successfully processed" → `notification_success=True`; an INFO line with
  "mail sent to" → `True`; neither → `False`; matcher is case-insensitive.
- The notification probe includes DEBUG lines even when `INCLUDE_DEBUG_LOGS` is off.
- On success, STGPT is **not** called and `analysis.status == "infra_success"` with
  the notification lines as citations; on no-success, the STGPT path runs as before.
- Trace id extraction handles `trace_id`/`traceId`/`traceID` and 16/32-hex values.
- Tempo cascade builds from a mocked Tempo tool payload; and skips gracefully with a
  note when `TEMPO_ID` is unset, no trace id is found, or no Tempo tool is available.
- Gate is scoped to `request_id` mode: normal runs record "not applicable" and behave
  exactly as today.

Run `python -m pytest -q` and make everything pass.

## Docs

Update `README.md` and `AGENTS.md`: document `TEMPO_ID`, `NOTIFICATION_COMPONENT`,
`NOTIFICATION_SUCCESS_MARKERS`, `TRACE_ID_FIELD`, the notification success gate
(and that it skips AI), the DEBUG-inclusion detail, and the Tempo cascade section.

## Acceptance criteria

1. With `request_id` set and a notification success log present, the run reports
   **SUCCESS**, prints the notification evidence lines, and does **not** call STGPT.
2. With `request_id` set and no notification success log, the run proceeds with the
   existing Grafana + STGPT analysis.
3. `TEMPO_ID` is a variable (never hardcoded); when set and a trace id is found, the
   summary shows a Tempo cascade across components; otherwise it skips with a note.
4. The notification probe finds DEBUG success lines even with `INCLUDE_DEBUG_LOGS` off.
5. Secrets never logged; job exits 0 unless `STRICT=true`; all tests pass.

Stop before pushing.
