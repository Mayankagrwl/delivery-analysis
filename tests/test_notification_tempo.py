from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.collect import run_collect
from src.delivery_analysis.config import load_settings, parse_success_markers
from src.delivery_analysis.grafana import (
    WRITE_TOOLS,
    collect_grafana,
    extract_trace_id,
    match_success_markers,
    parse_trace_spans,
)
from src.delivery_analysis.report import render_summary_md
from src.delivery_analysis.stgpt_client import ChatResult
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)

URN43 = "strn:distribution:DeliveryRequest:43"
PAYLOAD = [
    {
        "state": "SUBMITTED",
        "_urn": URN43,
        "_updated": {"on": "8/27/2026 9:30:43 AM"},
    },
    {
        "state": "GRANTED",
        "_urn": "strn:distribution:DeliveryRequest:38",
        "_updated": {"on": "8/26/2026 3:19:56 PM"},
    },
]

NOTIF_DEBUG_SUCCESS = (
    f'[DEBUG] component=notification urn="{URN43}" successfully processed '
    "trace_id=abcdef0123456789"
)
NOTIF_INFO_SUCCESS = (
    f'[INFO] component=notification urn="{URN43}" mail sent to ops@example.com '
    "traceId=ABCDEF0123456789ABCDEF0123456789"
)
NOTIF_NO_SUCCESS = (
    f'[DEBUG] component=notification urn="{URN43}" started processing request'
)
DIST_ERROR = (
    f'[error] component=distribution urn="{URN43}" timeout waiting for grant'
)

TEMPO_SPANS = {
    "spans": [
        {"service": "distribution", "name": "submit", "status": "ok", "duration_ms": 12},
        {"service": "notification", "name": "send-mail", "status": "ok", "duration_ms": 30},
    ],
    "url": "https://grafana.example.st.com/explore?traceId=abcdef0123456789",
}


class FakeMcp:
    def __init__(self, *, notif_line=NOTIF_DEBUG_SUCCESS, dist_line=DIST_ERROR,
                 tempo=True, tempo_payload=None, extra_tools=None):
        self.calls: list[tuple[str, dict]] = []
        self.notif_line = notif_line
        self.dist_line = dist_line
        self.tempo_payload = tempo_payload if tempo_payload is not None else TEMPO_SPANS
        self.tools = [
            "get_dashboard_summary",
            "get_dashboard_panel_queries",
            "list_datasources",
            "list_loki_label_names",
            "list_loki_label_values",
            "query_loki_stats",
            "query_loki_logs",
            "generate_deeplink",
            "check_datasources_health",
            "update_dashboard",
            "create_incident",
        ]
        if tempo:
            self.tools.append("query_tempo")
        if extra_tools:
            self.tools.extend(extra_tools)

    def list_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in WRITE_TOOLS or name.startswith(("update_", "create_", "delete_")):
            raise AssertionError(f"write tool {name} called")
        if name == "get_dashboard_summary":
            return {"title": "Service Logs", "folderTitle": "Distribution"}
        if name == "get_dashboard_panel_queries":
            return []
        if name == "list_datasources":
            return [{"uid": "loki-uid", "name": "Loki", "type": "loki"}]
        if name == "list_loki_label_names":
            return ["component", "env", "level"]
        if name == "list_loki_label_values":
            return ["distribution", "notification"]
        if name == "query_loki_stats":
            return {"streams": 1, "entries": 5}
        if name == "generate_deeplink":
            return {"url": "https://grafana.example.st.com/x"}
        if name == "query_tempo":
            return self.tempo_payload
        if name == "query_loki_logs":
            q = str(arguments.get("logql", ""))
            if 'component="notification"' in q:
                if self.notif_line is None:
                    return {"data": []}
                return {"data": [{"line": self.notif_line, "timestamp": "2026-08-27T09:00:00Z"}]}
            if self.dist_line is None:
                return {"data": []}
            return {"data": [{"line": self.dist_line, "timestamp": "2026-08-27T09:00:00Z"}]}
        return {}

    def close(self):
        return None


def _srm(request_id="43"):
    return evaluate_payload(PAYLOAD, as_of=AS_OF, request_id=request_id)


def _settings(**env):
    base = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=False):
        return load_settings()


# --- pure helpers -----------------------------------------------------------

class MarkerMatchTests(unittest.TestCase):
    def setUp(self):
        self.markers = parse_success_markers(None)

    def test_debug_successfully_processed(self):
        lines, markers = match_success_markers([NOTIF_DEBUG_SUCCESS], self.markers)
        self.assertEqual(len(lines), 1)
        self.assertIn("successfully processed", markers)

    def test_info_mail_sent(self):
        lines, markers = match_success_markers([NOTIF_INFO_SUCCESS], self.markers)
        self.assertEqual(len(lines), 1)
        self.assertIn("mail sent to", markers)

    def test_no_marker(self):
        lines, markers = match_success_markers([NOTIF_NO_SUCCESS], self.markers)
        self.assertEqual(lines, [])
        self.assertEqual(markers, [])

    def test_case_insensitive(self):
        line = f'component=notification SUCCESSFULLY PROCESSED urn="{URN43}"'
        lines, markers = match_success_markers([line], self.markers)
        self.assertEqual(len(lines), 1)


class TraceIdExtractTests(unittest.TestCase):
    def test_trace_id_snake(self):
        self.assertEqual(
            extract_trace_id(["x trace_id=abcdef0123456789 y"]), "abcdef0123456789"
        )

    def test_trace_id_camel(self):
        self.assertEqual(
            extract_trace_id(['traceId="abcdef0123456789"']), "abcdef0123456789"
        )

    def test_trace_id_upper_and_32hex(self):
        long_id = "ABCDEF0123456789ABCDEF0123456789"
        self.assertEqual(
            extract_trace_id([f"traceID: {long_id}"]).lower(), long_id.lower()
        )

    def test_trace_id_hyphen(self):
        self.assertEqual(
            extract_trace_id(["trace-id=00112233445566778899aabbccddeeff"]),
            "00112233445566778899aabbccddeeff",
        )

    def test_no_trace_id(self):
        self.assertIsNone(extract_trace_id(["nothing here", "id=42"]))


class ParseTraceSpansTests(unittest.TestCase):
    def test_parses_ordered_spans(self):
        spans = parse_trace_spans(TEMPO_SPANS)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["service"], "distribution")
        self.assertEqual(spans[1]["service"], "notification")
        self.assertEqual(spans[1]["duration_ms"], 30)


# --- notification probe -----------------------------------------------------

class NotificationProbeTests(unittest.TestCase):
    def test_debug_success_detected_even_without_include_debug(self):
        env = os.environ.copy()
        env.pop("INCLUDE_DEBUG_LOGS", None)
        with patch.dict(os.environ, env, clear=True):
            settings = _settings()
        self.assertFalse(settings.include_debug_logs)
        fake = FakeMcp(notif_line=NOTIF_DEBUG_SUCCESS)
        result = collect_grafana(_srm("43"), settings, client=fake)
        self.assertTrue(result.notification_checked)
        self.assertTrue(result.notification_success)
        self.assertIn("successfully processed", result.notification_markers_matched)
        self.assertTrue(result.notification_success_lines)
        # The dedicated probe queried the notification component with no level filter.
        notif_qs = [
            a.get("logql")
            for n, a in fake.calls
            if n == "query_loki_logs" and 'component="notification"' in str(a.get("logql"))
        ]
        self.assertTrue(notif_qs)
        self.assertTrue(all("LEVEL=" not in str(q) for q in notif_qs))

    def test_no_success_line_sets_false(self):
        fake = FakeMcp(notif_line=NOTIF_NO_SUCCESS)
        result = collect_grafana(_srm("43"), _settings(), client=fake)
        self.assertTrue(result.notification_checked)
        self.assertFalse(result.notification_success)

    def test_probe_not_run_without_request_id(self):
        # Normal all-records staleness run: gate is not applicable.
        srm = evaluate_payload(PAYLOAD, as_of=AS_OF)
        self.assertIsNone(srm.request_id)
        fake = FakeMcp()
        result = collect_grafana(srm, _settings(), client=fake)
        self.assertFalse(result.notification_checked)
        notif_qs = [
            a for n, a in fake.calls
            if n == "query_loki_logs" and 'component="notification"' in str(a.get("logql"))
        ]
        self.assertEqual(notif_qs, [])


# --- Tempo cascade ----------------------------------------------------------

class TempoCascadeTests(unittest.TestCase):
    def test_cascade_built_from_mocked_tool(self):
        settings = _settings(TEMPO_ID="tempo-uid")
        fake = FakeMcp()
        result = collect_grafana(_srm("43"), settings, client=fake)
        self.assertEqual(result.trace_id, "abcdef0123456789")
        self.assertEqual(len(result.trace_spans), 2)
        self.assertEqual(result.tempo_datasource_uid, "tempo-uid")
        self.assertTrue(any(n == "query_tempo" for n, _ in fake.calls))
        self.assertTrue(result.trace_deeplink)

    def test_skip_when_tempo_id_unset(self):
        env = os.environ.copy()
        env.pop("TEMPO_ID", None)
        with patch.dict(os.environ, env, clear=True):
            settings = _settings()
        result = collect_grafana(_srm("43"), settings, client=FakeMcp())
        self.assertEqual(result.trace_spans, [])
        self.assertTrue(any("TEMPO_ID not set" in n for n in result.notes))

    def test_skip_when_no_trace_id(self):
        settings = _settings(TEMPO_ID="tempo-uid")
        fake = FakeMcp(
            notif_line=f'component=notification urn="{URN43}" successfully processed',
            dist_line=f'component=distribution urn="{URN43}" timeout',
        )
        result = collect_grafana(_srm("43"), settings, client=fake)
        self.assertIsNone(result.trace_id)
        self.assertTrue(any("no trace id" in n for n in result.notes))

    def test_skip_when_no_tempo_tool(self):
        settings = _settings(TEMPO_ID="tempo-uid")
        fake = FakeMcp(tempo=False)
        result = collect_grafana(_srm("43"), settings, client=fake)
        self.assertEqual(result.trace_spans, [])
        self.assertTrue(any("no read-only Tempo tool" in n for n in result.notes))


# --- gate wiring end to end -------------------------------------------------

class GateWiringTests(unittest.TestCase):
    def _run(self, *, notif_line, chat_calls):
        def chat_fn(persona, messages):
            chat_calls.append(persona)
            return ChatResult(200, {"completion": "{}"}, "{}", "rid")

        env = {
            "SRM_BASIC_USER": "u",
            "SRM_BASIC_PASSWORD": "pw",
            "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=load_settings(request_id="43"),
                        mcp_client=FakeMcp(notif_line=notif_line),
                        analyze=True,
                        chat_fn=chat_fn,
                        request_id="43",
                    )
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    return result, analysis, summary

    def test_success_short_circuits_stgpt(self):
        chat_calls: list[str] = []
        result, analysis, summary = self._run(
            notif_line=NOTIF_DEBUG_SUCCESS, chat_calls=chat_calls
        )
        self.assertEqual(result.verdict, "STALE")
        self.assertEqual(chat_calls, [])  # STGPT never called
        self.assertEqual(analysis["status"], "infra_success")
        self.assertEqual(analysis["tokens_used"], 0)
        self.assertFalse(analysis["fallback_used"])
        self.assertTrue(analysis["result"]["citations"])
        self.assertIn("Infra success check (Notification)", summary)
        self.assertIn("SUCCESS", summary)

    def test_no_success_runs_stgpt(self):
        chat_calls: list[str] = []
        result, analysis, summary = self._run(
            notif_line=NOTIF_NO_SUCCESS, chat_calls=chat_calls
        )
        self.assertTrue(chat_calls)  # STGPT path entered
        self.assertNotEqual(analysis["status"], "infra_success")
        self.assertIn("NOT FOUND", summary)


class ReportSectionTests(unittest.TestCase):
    def test_not_applicable_without_request_id(self):
        srm = evaluate_payload(PAYLOAD, as_of=AS_OF)
        summary = render_summary_md(srm)
        self.assertIn("## Infra success check (Notification)", summary)
        self.assertIn("not applicable", summary)
        self.assertIn("## Request trace (Tempo)", summary)


if __name__ == "__main__":
    unittest.main()
