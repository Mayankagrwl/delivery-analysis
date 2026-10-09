from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.delivery_analysis.config import load_settings
from src.delivery_analysis.grafana import (
    WRITE_TOOLS,
    build_priority_logql,
    classify_level,
    classify_service,
    collect_grafana,
    extract_request_ids,
    is_critical_line,
)
from src.delivery_analysis.prompt import build_evidence
from src.delivery_analysis.report import render_summary_md
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 15, 0, 0, tzinfo=UTC)
REQ_ID = "302"
URN302 = "strn:distribution:DeliveryRequest:302"
CID = "aq0AFUBWyw14I0YBKesDugAAAU"

CONTEXT_NOTIF = (
    f'2026-09-18T09:11:02Z LEVEL=DEBUG SERVICE=notification ID={CID} '
    f'TRACE_ID=abcdef0123456789 [Template.render] {{"urn":"{URN302}"}}'
)
DIST_LINE = (
    f'2026-09-18T09:10:00Z LEVEL=INFO SERVICE=distribution ID={CID} '
    "TRACE_ID=1111111111111111 submitted ok"
)
GRANT_CRIT = (
    f'2026-09-18T09:10:30Z LEVEL=CRIT SERVICE=grant ID={CID} '
    "TRACE_ID=2222222222222222 grant worker crashed: NullPointer in replay"
)


class LevelAndServiceTests(unittest.TestCase):
    def test_crit_is_critical(self):
        self.assertTrue(is_critical_line("x LEVEL=CRIT y"))
        self.assertTrue(is_critical_line("x LEVEL=CRITICAL y"))
        self.assertTrue(is_critical_line("x LEVEL=FATAL y"))
        self.assertTrue(is_critical_line("x LEVEL=emerg y"))
        self.assertFalse(is_critical_line("x LEVEL=INFO y"))
        self.assertFalse(is_critical_line("x LEVEL=warn y"))

    def test_classify_crit_normalizes(self):
        self.assertEqual(classify_level("a LEVEL=CRIT b"), "critical")
        self.assertEqual(classify_level("a LEVEL=err b"), "error")
        self.assertEqual(classify_level("a LEVEL=FATAL b"), "fatal")

    def test_classify_service_prefers_service_field(self):
        self.assertEqual(classify_service(GRANT_CRIT), "grant")
        self.assertEqual(
            classify_service('component="notification" foo'), "notification"
        )

    def test_priority_logql_includes_crit(self):
        q = build_priority_logql([URN302], include_per_urn=False)
        self.assertTrue(any("crit" in query.lower() for query in q))
        self.assertTrue(any("fatal" in query.lower() for query in q))


class ExtractIdTests(unittest.TestCase):
    def test_logfmt_id_field(self):
        self.assertEqual(extract_request_ids([f"SERVICE=x ID={CID} msg"]), [CID])

    def test_ignores_trace_id(self):
        # TRACE_ID must NOT be picked up as the correlation ID.
        line = "SERVICE=x TRACE_ID=abcdef0123456789 msg"
        self.assertEqual(extract_request_ids([line]), [])

    def test_still_matches_json_request_id(self):
        self.assertEqual(extract_request_ids([f'"requestId":"{CID}"']), [CID])

    def test_ignores_short_numeric(self):
        self.assertEqual(extract_request_ids(["ID=302"]), [])


class IdTraceFakeMcp:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
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
            return ["component", "environment", "level"]
        if name == "list_loki_label_values":
            return ["distribution", "notification", "grant"]
        if name == "query_loki_stats":
            return {"streams": 1}
        if name == "generate_deeplink":
            return {"url": "https://grafana.example.st.com/x"}
        if name == "query_loki_logs":
            q = str(arguments.get("logql", ""))
            notif = 'component="notification"' in q
            env_only = "component=" not in q  # {environment="qa"} selector
            if URN302 in q:
                if notif:
                    return {"data": [{"line": CONTEXT_NOTIF}]}
                if env_only:
                    return {"data": [{"line": CONTEXT_NOTIF}]}
                return {"data": []}
            if CID in q:
                if notif:
                    return {"data": []}  # no success marker
                if env_only:
                    return {"data": [{"line": DIST_LINE}, {"line": GRANT_CRIT}]}
                return {"data": []}
            return {"data": []}
        return {}

    def close(self):
        return None


def _settings():
    env = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    with patch.dict(os.environ, env, clear=False):
        return load_settings(request_id=REQ_ID)


def _srm():
    return evaluate_payload([], as_of=AS_OF, environment="qa", request_id=REQ_ID)


class IdCrossComponentTraceTests(unittest.TestCase):
    def setUp(self):
        self.result = collect_grafana(_srm(), _settings(), client=IdTraceFakeMcp())

    def test_correlation_id_resolved(self):
        self.assertEqual(self.result.correlation_id, CID)

    def test_crit_line_captured_across_components(self):
        self.assertEqual(self.result.crit_count, 1)
        self.assertTrue(
            any("grant worker crashed" in line for line in self.result.crit_lines)
        )

    def test_components_cascade(self):
        self.assertIn("distribution", self.result.id_trace_components)
        self.assertIn("grant", self.result.id_trace_components)

    def test_id_query_used_env_scope_and_id(self):
        fake = IdTraceFakeMcp()
        collect_grafana(_srm(), _settings(), client=fake)
        id_qs = [
            str(a.get("logql"))
            for n, a in fake.calls
            if n == "query_loki_logs"
            and CID in str(a.get("logql"))
            and "component=" not in str(a.get("logql"))
        ]
        self.assertTrue(id_qs)
        self.assertTrue(all('environment="qa"' in q for q in id_qs))

    def test_summary_shows_id_trace_and_crit(self):
        summary = render_summary_md(_srm(), grafana=self.result)
        self.assertIn("## Request trace (by ID)", summary)
        self.assertIn(CID, summary)
        self.assertIn("CRIT highlights", summary)
        self.assertIn("grant worker crashed", summary)

    def test_evidence_includes_crit(self):
        evidence, _, _ = build_evidence(_srm(), self.result, cap_tokens=6000)
        self.assertIn("### crit_lines", evidence)
        self.assertIn("grant worker crashed", evidence)
        self.assertIn(f"correlation_id={CID}", evidence)


class TempoAliasTests(unittest.TestCase):
    def test_tempo_datasource_id_alias(self):
        with patch.dict(
            os.environ, {"TEMPO_DATASOURCE_ID": "tempo-xyz"}, clear=False
        ):
            os.environ.pop("TEMPO_ID", None)
            self.assertEqual(load_settings().tempo_datasource_uid, "tempo-xyz")


if __name__ == "__main__":
    unittest.main()
