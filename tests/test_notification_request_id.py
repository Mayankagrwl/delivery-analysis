from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.collect import run_collect
from src.delivery_analysis.config import load_settings
from src.delivery_analysis.grafana import (
    WRITE_TOOLS,
    collect_grafana,
    extract_request_ids,
)
from src.delivery_analysis.stgpt_client import ChatResult
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 15, 0, 0, tzinfo=UTC)
REQ_ID = "302"
URN302 = "strn:distribution:DeliveryRequest:302"
CORR_ID = "aq0AFUBWyw14I0YBKesDugAAAU"

# Modeled on the operator's real Service Logs (component=notification):
# the URN appears only on the verbose payload line, which also carries the
# correlation requestId; the success markers are on separate lines keyed by
# that requestId, NOT by the URN.
CONTEXT_LINE = (
    f'2026-09-18T09:11:02.302Z [DEBUG] [chrispin] [{CORR_ID}] [notification@1.3.4] '
    f'[Template.render] {{"urn":"{URN302}","state":"SUBMITTED",'
    f'"meta":{{"user":"chrispin","requestId":"{CORR_ID}"}}}}'
)
SUCCESS_DEBUG = (
    f'2026-09-18T09:11:02.302Z [DEBUG] [chrispin] [{CORR_ID}] [notification@1.3.4] '
    "[6442457288,6442457668] successfully processed"
)
SUCCESS_INFO = (
    f'2026-09-18T09:11:02.302Z [INFO] [chrispin] [{CORR_ID}] [notification@1.3.4] '
    "[mail.send] mail sent to : emmanuelle.chrispin@st.com"
)


class TwoHopFakeMcp:
    """Notification logs where the URN and the success markers are on different
    lines, joined only by the correlation requestId (mirrors the real dashboard).
    """

    def __init__(self, *, success=True, resolvable=True):
        self.calls: list[tuple[str, dict]] = []
        self.success = success
        self.resolvable = resolvable
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
        ]

    def list_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in WRITE_TOOLS or name.startswith(("update_", "create_", "delete_")):
            raise AssertionError(f"write tool {name}")
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
            return {"streams": 1}
        if name == "generate_deeplink":
            return {"url": "https://grafana.example.st.com/x"}
        if name == "query_loki_logs":
            q = str(arguments.get("logql", ""))
            if 'component="notification"' not in q:
                return {"data": []}
            if URN302 in q:
                # Hop 1: URN -> payload/context line (carries requestId).
                context = CONTEXT_LINE if self.resolvable else (
                    f'[Template.render] {{"urn":"{URN302}"}}'  # no requestId
                )
                return {"data": [{"line": context, "timestamp": "2026-09-18T09:11:02Z"}]}
            if CORR_ID in q:
                # Hop 2: requestId -> the success marker lines.
                if not self.success:
                    return {"data": [{"line": f"[{CORR_ID}] started processing"}]}
                return {
                    "data": [
                        {"line": SUCCESS_DEBUG, "timestamp": "2026-09-18T09:11:02Z"},
                        {"line": SUCCESS_INFO, "timestamp": "2026-09-18T09:11:02Z"},
                    ]
                }
            return {"data": []}
        return {}

    def close(self):
        return None


def _settings(**env):
    base = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=False):
        return load_settings(request_id=REQ_ID)


def _no_records_srm():
    # id 302 absent from SUBMITTED/GRANTED -> NO_RECORDS, still request-scoped.
    return evaluate_payload([], as_of=AS_OF, request_id=REQ_ID)


class ExtractRequestIdTests(unittest.TestCase):
    def test_json_field(self):
        self.assertEqual(extract_request_ids([CONTEXT_LINE]), [CORR_ID])

    def test_bracket_key_value_form(self):
        line = f"foo request-id=abcdef123456 bar"
        self.assertEqual(extract_request_ids([line]), ["abcdef123456"])

    def test_ignores_short_numeric(self):
        self.assertEqual(extract_request_ids(['"requestId":"302"']), [])

    def test_none_when_absent(self):
        self.assertEqual(extract_request_ids(["nothing here"]), [])


class TwoHopProbeTests(unittest.TestCase):
    def test_success_found_via_request_id_hop(self):
        fake = TwoHopFakeMcp(success=True)
        result = collect_grafana(_no_records_srm(), _settings(), client=fake)
        self.assertTrue(result.notification_checked)
        self.assertTrue(result.notification_success)
        self.assertEqual(result.notification_request_id, CORR_ID)
        self.assertEqual(
            set(result.notification_markers_matched),
            {"successfully processed", "mail sent to"},
        )
        # The probe issued a second query filtered by the correlation id.
        rid_queries = [
            a.get("logql")
            for n, a in fake.calls
            if n == "query_loki_logs" and CORR_ID in str(a.get("logql"))
        ]
        self.assertTrue(rid_queries)

    def test_no_success_marks_false_with_diagnostics(self):
        fake = TwoHopFakeMcp(success=False)
        result = collect_grafana(_no_records_srm(), _settings(), client=fake)
        self.assertFalse(result.notification_success)
        self.assertEqual(result.notification_request_id, CORR_ID)
        self.assertGreater(result.notification_lines_scanned, 0)

    def test_unresolvable_request_id_still_safe(self):
        fake = TwoHopFakeMcp(success=True, resolvable=False)
        result = collect_grafana(_no_records_srm(), _settings(), client=fake)
        # No requestId on the URN line -> no hop 2 -> no success (but no crash).
        self.assertIsNone(result.notification_request_id)
        self.assertFalse(result.notification_success)


class EndToEndTests(unittest.TestCase):
    def test_no_records_success_skips_stgpt(self):
        chat_calls: list[str] = []

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
                return_value=[],
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=load_settings(request_id=REQ_ID),
                        mcp_client=TwoHopFakeMcp(success=True),
                        analyze=True,
                        chat_fn=chat_fn,
                        request_id=REQ_ID,
                    )
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(chat_calls, [])  # STGPT skipped on infra success
        self.assertEqual(analysis["status"], "infra_success")
        self.assertEqual(result.request_outcome, "SUCCESS")
        self.assertIn("SUCCESS", summary)
        self.assertIn(CORR_ID, summary)
        self.assertIn("successfully processed", summary)
        self.assertIn("mail sent to", summary)


if __name__ == "__main__":
    unittest.main()
