from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.cli import main
from src.delivery_analysis.collect import run_collect
from src.delivery_analysis.config import load_settings
from src.delivery_analysis.grafana import (
    DEFAULT_DASHBOARD_UID,
    LOGQL_TOOLS,
    WRITE_TOOLS,
    collect_grafana,
    error_logql,
    stream_selector,
    urn_json_filter,
    urn_line_filter,
)
from src.delivery_analysis.mcp_sse import iter_sse_events, unwrap_tool_result
from src.delivery_analysis.verdict import evaluate_payload
from tests.test_s1 import SCREENSHOT_AS_OF, SCREENSHOT_PAYLOAD


class FakeMcp:
    def __init__(self, responses=None, tools=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.tools = tools or [
            "get_dashboard_summary",
            "get_dashboard_panel_queries",
            "get_dashboard_by_uid",
            "list_datasources",
            "list_loki_label_names",
            "list_loki_label_values",
            "query_loki_stats",
            "query_loki_logs",
            "query_loki_patterns",
            "check_datasources_health",
            "generate_deeplink",
            "update_dashboard",
            "create_incident",
        ]

    def list_tools(self) -> list[str]:
        return list(self.tools)

    def call_tool(self, name: str, arguments: dict) -> object:
        self.calls.append((name, arguments))
        if name in WRITE_TOOLS or name.startswith("update_") or name.startswith("create_"):
            raise AssertionError(f"write tool {name} must not be called")
        handler = self.responses.get(name)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(arguments)
        if handler is not None:
            return handler
        return {}

    def close(self) -> None:
        return None


def _default_responses(*, urn_label: bool = False, line: str | None = None):
    log_line = line or (
        '{"urn":"strn:distribution:DeliveryRequest:43"} timeout waiting for grant'
    )

    def logs(_args: dict) -> dict:
        return {"data": [{"line": log_line, "timestamp": "2026-09-18T07:00:00Z"}]}

    names = ["component", "env", "level"]
    values: list[str] = ["distribution"]
    if urn_label:
        names.append("urn")
        values = [
            "strn:distribution:DeliveryRequest:43",
            "strn:distribution:DeliveryRequest:38",
            "strn:distribution:DeliveryRequest:39",
            "strn:distribution:DeliveryRequest:40",
        ]
    return {
        "get_dashboard_summary": {
            "title": "Service Logs",
            "folderTitle": "Distribution",
        },
        "get_dashboard_panel_queries": [{"title": "logs"}],
        "list_datasources": [{"uid": "loki-uid", "name": "Loki", "type": "loki"}],
        "list_loki_label_names": names,
        "list_loki_label_values": values,
        "query_loki_stats": {"streams": 2, "chunks": 4, "entries": 20, "bytes": 1000},
        "query_loki_logs": logs,
        "query_loki_patterns": [{"pattern": "timeout <*>", "totalCount": 3}],
        "generate_deeplink": lambda args: {
            "url": "https://grafana.example.st.com/" + str(args.get("resourceType"))
        },
    }


class SseParseTests(unittest.TestCase):
    def test_iter_sse_events(self) -> None:
        events = list(
            iter_sse_events(
                [
                    ": ping",
                    "event: endpoint",
                    "data: /message?session=1",
                    "",
                    "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}",
                    "",
                ]
            )
        )
        self.assertEqual(events[0], ("endpoint", "/message?session=1"))
        self.assertEqual(events[1][0], "message")
        self.assertIn("ok", events[1][1])

    def test_unwrap_tool_json_text(self) -> None:
        payload = {
            "content": [{"type": "text", "text": '{"streams": 1}'}],
            "isError": False,
        }
        self.assertEqual(unwrap_tool_result(payload), {"streams": 1})


class LogqlTests(unittest.TestCase):
    def test_label_selector_preferred(self) -> None:
        query = stream_selector(
            {},
            urns=["strn:distribution:DeliveryRequest:300"],
            urn_is_label=True,
        )
        self.assertEqual(query, '{urn="strn:distribution:DeliveryRequest:300"}')

    def test_line_filter_uses_full_urn(self) -> None:
        selector = stream_selector({})
        query = urn_line_filter(selector, ["strn:distribution:DeliveryRequest:300"])
        self.assertIn('|= "strn:distribution:DeliveryRequest:300"', query)
        json_query = urn_json_filter(selector, ["strn:distribution:DeliveryRequest:300"])
        self.assertIn("strn:distribution:DeliveryRequest:300", json_query)
        self.assertIn("urn", json_query)

    def test_filters_in_selector(self) -> None:
        query = stream_selector(
            {"env": "prod", "component": "distribution", "level": "error"}
        )
        self.assertEqual(
            query, '{env="prod", component="distribution", level="error"}'
        )

    def test_error_query(self) -> None:
        query = error_logql(stream_selector({}))
        self.assertIn("DeliveryRequest", query)
        self.assertIn("error", query)


class GrafanaCollectTests(unittest.TestCase):
    def test_stale_runs_readonly_pack_and_writes_inventory(self) -> None:
        fake = FakeMcp(responses=_default_responses(urn_label=False))
        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "srm-secret",
            "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
            "GRAFANA_MCP_TOKEN": "mcp-secret-token",
            "GRAFANA_DASHBOARD_FILTERS": "env=prod,component=distribution,level=error",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=SCREENSHOT_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(),
                        mcp_client=fake,
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    names = [name for name, _ in fake.calls]
                    self.assertIn("get_dashboard_summary", names)
                    self.assertIn("get_dashboard_panel_queries", names)
                    self.assertNotIn("get_dashboard_by_uid", names)
                    self.assertNotIn("update_dashboard", names)
                    self.assertNotIn("create_incident", names)
                    logql_calls = [c for c in fake.calls if c[0] in LOGQL_TOOLS]
                    self.assertLessEqual(len(logql_calls), 4)
                    logql = [args.get("logql") for _, args in logql_calls]
                    self.assertTrue(any("strn:distribution:DeliveryRequest" in str(q) for q in logql))
                    self.assertTrue(
                        any(
                            "env=\"prod\"" in str(q) and "component=\"distribution\"" in str(q)
                            for q in logql
                        )
                    )
                    dash_args = [args for name, args in fake.calls if name.startswith("get_dashboard")]
                    self.assertTrue(all(args.get("uid") == DEFAULT_DASHBOARD_UID for args in dash_args))
                    grafana = json.loads((Path(tmp) / "grafana.json").read_text(encoding="utf-8"))
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    self.assertFalse(grafana["skipped"])
                    self.assertEqual(grafana["dashboard_uid"], DEFAULT_DASHBOARD_UID)
                    self.assertEqual(grafana["transport"], "sse")
                    self.assertEqual(grafana["mcp_url_host"], "grafana-mcp.example.st.com")
                    self.assertIn("## Grafana / Loki inventory", summary)
                    self.assertIn("line filter", summary)
                    self.assertNotIn("mcp-secret-token", summary)
                    self.assertNotIn("mcp-secret-token", json.dumps(grafana))
                    self.assertNotIn("srm-secret", json.dumps(grafana))

    def test_urn_label_selector_preferred(self) -> None:
        fake = FakeMcp(responses=_default_responses(urn_label=True))
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        self.assertTrue(grafana.urn_is_label)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        self.assertTrue(any(str(q).startswith("{urn=") or "urn=~" in str(q) for q in logql))

    def test_fresh_does_not_call_mcp(self) -> None:
        fake = FakeMcp()
        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "secret",
            "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
        }
        fresh_as_of = SCREENSHOT_AS_OF.replace(year=2026, month=8, day=27, hour=10)
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=fresh_as_of,
                        out_dir=tmp,
                        settings=load_settings(),
                        mcp_client=fake,
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "FRESH")
                    self.assertEqual(fake.calls, [])
                    grafana = json.loads((Path(tmp) / "grafana.json").read_text(encoding="utf-8"))
                    self.assertTrue(grafana["skipped"])
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    self.assertIn("skipped (verdict not STALE)", summary)

    def test_no_records_does_not_call_mcp(self) -> None:
        fake = FakeMcp()
        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "secret",
            "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=[],
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=SCREENSHOT_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(),
                        mcp_client=fake,
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "NO_RECORDS")
                    self.assertEqual(fake.calls, [])

    def test_mcp_failure_continues_exit_0(self) -> None:
        class Boom(FakeMcp):
            def list_tools(self) -> list[str]:
                raise RuntimeError("mcp down")

            def call_tool(self, name: str, arguments: dict) -> object:
                raise RuntimeError("mcp down")

        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "secret",
            "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
            "GRAFANA_MCP_TOKEN": "mcp-secret-token",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=SCREENSHOT_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(),
                        mcp_client=Boom(),
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    grafana = json.loads(
                        (Path(tmp) / "grafana.json").read_text(encoding="utf-8")
                    )
                    self.assertTrue(grafana["notes"] or grafana["tools"])
                    self.assertNotIn("mcp-secret-token", json.dumps(grafana))
                    with patch(
                        "src.delivery_analysis.cli.run_collect",
                        return_value=result,
                    ):
                        rc = main(
                            [
                                "collect",
                                "--as-of",
                                "2026-09-18T08:00:00Z",
                                "--out-dir",
                                tmp,
                                "--no-analyze",
                            ]
                        )
                    self.assertEqual(rc, 0)

    def test_missing_mcp_url_on_stale_skips(self) -> None:
        env = os.environ.copy()
        env["SRM_BASIC_USER"] = "user"
        env["SRM_BASIC_PASSWORD"] = "secret"
        env.pop("GRAFANA_MCP_URL", None)
        env.pop("GRAFANA_MCP_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=SCREENSHOT_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(),
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    grafana = json.loads((Path(tmp) / "grafana.json").read_text(encoding="utf-8"))
                    self.assertTrue(grafana["skipped"])
                    self.assertIn("GRAFANA_MCP_URL", grafana["skip_reason"])

    def test_truncates_lines_and_respects_query_budget(self) -> None:
        long_line = "x" * 800

        def logs(_args: dict) -> dict:
            return {"data": [{"line": long_line}]}

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        self.assertTrue(grafana.highlights)
        self.assertLessEqual(len(grafana.highlights[0]), 500)
        self.assertGreater(grafana.lines_discarded, 0)
        logql_calls = [c for c in fake.calls if c[0] in LOGQL_TOOLS]
        self.assertLessEqual(len(logql_calls), 4)
        self.assertTrue(all(args.get("limit", 200) <= 200 for name, args in fake.calls if name == "query_loki_logs"))

    def test_panel_queries_receive_filters(self) -> None:
        fake = FakeMcp(responses=_default_responses())
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        settings.grafana_dashboard_filters = {
            "env": "prod",
            "component": "distribution",
            "level": "error",
        }
        collect_grafana(srm, settings, client=fake)
        panel = [args for name, args in fake.calls if name == "get_dashboard_panel_queries"]
        self.assertEqual(panel[0]["variables"]["env"], "prod")
        self.assertEqual(panel[0]["uid"], DEFAULT_DASHBOARD_UID)


if __name__ == "__main__":
    unittest.main()
