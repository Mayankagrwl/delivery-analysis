from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
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
    build_priority_logql,
    build_time_packs,
    collect_grafana,
    error_logql,
    highlight_snippet,
    stream_selector,
    urn_json_filter,
    urn_line_filter,
)
from src.delivery_analysis.prompt import build_evidence
from src.delivery_analysis.report import render_summary_md
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
        query = stream_selector({"component": "distribution"})
        self.assertEqual(query, '{component="distribution"}')

    def test_line_filter_uses_full_urn(self) -> None:
        selector = stream_selector({"component": "distribution"})
        query = urn_line_filter(selector, ["strn:distribution:DeliveryRequest:300"])
        self.assertIn('{component="distribution"}', query)
        self.assertIn('|= "strn:distribution:DeliveryRequest:300"', query)
        json_query = urn_json_filter(selector, ["strn:distribution:DeliveryRequest:300"])
        self.assertIn("strn:distribution:DeliveryRequest:300", json_query)
        self.assertIn("urn", json_query)

    def test_filters_in_selector(self) -> None:
        query = stream_selector(
            {"env": "prod", "component": "distribution", "level": "error"}
        )
        self.assertEqual(query, '{component="distribution"}')
        self.assertNotIn("{env=", query)
        self.assertNotIn("{level=", query)

    def test_error_query(self) -> None:
        query = error_logql(stream_selector({"component": "distribution"}))
        self.assertIn("DeliveryRequest", query)
        self.assertIn("LEVEL=", query)
        self.assertIn("error", query)
        self.assertIn("alert", query)
        self.assertIn("warn", query)
        self.assertNotIn("{env=", query)
        self.assertNotIn("{level=", query)

    def test_regex_level_matcher(self) -> None:
        queries = build_priority_logql(
            ["strn:distribution:DeliveryRequest:43"], include_per_urn=False
        )
        self.assertTrue(queries[0].startswith('{component="distribution"}'))
        self.assertIn("LEVEL=(alert|error|warn|ALERT|ERROR|WARN)", queries[0])
        self.assertNotIn("{env=", queries[0])
        self.assertNotIn("{level=", queries[0])


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
                    self.assertGreaterEqual(len(logql_calls), 4)
                    self.assertFalse(
                        any(
                            (call.get("error") or "") == "LogQL query budget reached"
                            for call in json.loads(
                                (Path(tmp) / "grafana.json").read_text(encoding="utf-8")
                            )["tools"]
                        )
                    )
                    logql = [args.get("logql") for _, args in logql_calls]
                    joined = "\n".join(str(q) for q in logql)
                    self.assertTrue(any("strn:distribution:DeliveryRequest" in str(q) for q in logql))
                    self.assertIn('{component="distribution"}', joined)
                    self.assertIn("LEVEL=", joined)
                    self.assertNotIn("{env=", joined)
                    self.assertNotIn("{level=", joined)
                    self.assertNotIn("query_loki_patterns", names)
                    self.assertNotIn("find_error_pattern_logs", names)
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
        joined = "\n".join(str(q) for q in logql)
        self.assertIn('{component="distribution"}', joined)
        self.assertIn('|= "strn:distribution:DeliveryRequest:', joined)
        self.assertNotIn("{urn=", joined)
        self.assertNotIn("{env=", joined)
        self.assertNotIn("{level=", joined)

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
                    from src.delivery_analysis.collect import MultiEnvResult

                    with patch(
                        "src.delivery_analysis.cli.run_collect_environments",
                        return_value=MultiEnvResult(
                            env_selection="prod",
                            results={"prod": result},
                            any_error=False,
                        ),
                    ):
                        rc = main(
                            [
                                "collect",
                                "--env",
                                "prod",
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
        self.assertGreaterEqual(len(logql_calls), 4)
        self.assertFalse(
            any(
                (call.error or "") == "LogQL query budget reached"
                for call in grafana.tools
            )
        )
        self.assertTrue(all(args.get("limit", 200) <= 200 for name, args in fake.calls if name == "query_loki_logs"))
        summary = render_summary_md(srm, grafana=grafana)
        self.assertIn("Lines containing a stale URN:", summary)
        self.assertIn("Lines truncated:", summary)

    def test_highlight_keeps_urn_at_end_of_long_line(self) -> None:
        prefix = (
            "LEVEL=INFO EventSender Started DeliveryAction "
            + ("meta " * 400)
        )
        urn = "strn:distribution:DeliveryRequest:43"
        long_line = prefix + f'"urn":"{urn}"'

        def logs(args: dict) -> dict:
            query = str(args.get("logql") or "")
            if "LEVEL=(INFO|info)" not in query:
                return {"data": []}
            return {
                "data": [
                    {
                        "line": long_line,
                        "timestamp": "2026-08-27T09:30:43Z",
                    }
                ]
            }

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        self.assertTrue(any("DeliveryRequest:43" in h for h in grafana.highlights))
        packed = grafana.distribution_lines + grafana.other_lines
        self.assertTrue(any(urn in line for line in packed))
        self.assertGreaterEqual(grafana.lines_with_stale_urn, 1)
        summary = render_summary_md(srm, grafana=grafana)
        self.assertIn("DeliveryRequest:43", summary)
        snippet = highlight_snippet(long_line)
        self.assertIn("DeliveryRequest:43", snippet)
        self.assertLess(len(snippet), len(long_line))

    def test_eventsender_started_sent_dedup_per_second(self) -> None:
        ts = "2026-08-27T09:30:43Z"
        started = (
            "LEVEL=INFO EventSender Started DeliveryAction "
            "strn:distribution:DeliveryRequest:43"
        )
        sent = (
            "LEVEL=INFO EventSender Sent DeliveryAction "
            "strn:distribution:DeliveryRequest:43"
        )

        def logs(args: dict) -> dict:
            query = str(args.get("logql") or "")
            if "LEVEL=(INFO|info)" not in query:
                return {"data": []}
            return {
                "data": [
                    {"line": started, "timestamp": ts},
                    {"line": sent, "timestamp": ts},
                ]
            }

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        packed = grafana.distribution_lines + grafana.other_lines
        self.assertEqual(sum("EventSender" in line for line in packed), 1)

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

    def test_stall_window_queries_august_2026(self) -> None:
        as_of = datetime(2026, 9, 19, 12, 18, 28, tzinfo=timezone.utc)
        fake = FakeMcp(responses=_default_responses())
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=as_of)
        self.assertEqual(srm.verdict, "STALE")
        updated = {rec.updated_on_utc.date().isoformat() for rec in srm.records if rec.updated_on_utc}
        self.assertIn("2026-08-26", updated)
        self.assertIn("2026-08-27", updated)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        starts = [
            str(args.get("startRfc3339") or "")
            for name, args in fake.calls
            if name in LOGQL_TOOLS
        ]
        self.assertTrue(
            any(item.startswith("2026-08") for item in starts),
            starts,
        )
        self.assertTrue(
            any(item.startswith("2026-09-18") for item in starts),
            starts,
        )
        labels = {rng.get("label") for rng in grafana.time_ranges}
        self.assertIn("recent", labels)
        self.assertIn("stall", labels)
        summary = render_summary_md(srm, grafana=grafana)
        self.assertIn("Time ranges:", summary)
        self.assertIn("2026-08-26T13:19:56Z", summary)
        self.assertIn("2026-08-27T15:30:43Z", summary)
        self.assertIn("2026-09-18T12:18:28Z", summary)
        self.assertIn("2026-09-19T12:18:28Z", summary)
        self.assertIn("Component order:", summary)
        self.assertIn("Level pass:", summary)
        self.assertIn("Level line filter:", summary)
        self.assertFalse(
            any((call.error or "") == "LogQL query budget reached" for call in grafana.tools)
        )

    def test_build_time_packs_combined_and_per_urn(self) -> None:
        as_of = datetime(2026, 9, 19, 12, 18, 28, tzinfo=timezone.utc)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=as_of)
        packs = build_time_packs(srm)
        self.assertEqual([p.label for p in packs], ["stall", "recent"])
        self.assertEqual(packs[0].start, "2026-08-26T13:19:56Z")
        self.assertEqual(packs[0].end, "2026-08-27T15:30:43Z")
        self.assertEqual(packs[1].start, "2026-09-18T12:18:28Z")
        self.assertEqual(packs[1].end, "2026-09-19T12:18:28Z")

        wide = [
            {
                "state": "SUBMITTED",
                "_urn": "strn:distribution:DeliveryRequest:1",
                "_updated": {"on": "8/01/2026 12:00:00 AM"},
            },
            {
                "state": "GRANTED",
                "_urn": "strn:distribution:DeliveryRequest:2",
                "_updated": {"on": "8/10/2026 12:00:00 AM"},
            },
        ]
        wide_srm = evaluate_payload(wide, as_of=as_of)
        wide_packs = build_time_packs(wide_srm)
        labels = [p.label for p in wide_packs]
        self.assertEqual(labels[-1], "recent")
        self.assertNotIn("stall", labels)
        self.assertTrue(any(label.startswith("urn:") for label in labels))

    def test_distribution_queries_before_all_components(self) -> None:
        fake = FakeMcp(responses=_default_responses())
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        collect_grafana(srm, settings, client=fake)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        dist_i = next(
            i for i, query in enumerate(logql) if 'component="distribution"' in query
        )
        all_i = next(i for i, query in enumerate(logql) if 'component=~".+"' in query)
        self.assertLess(dist_i, all_i)
        joined = "\n".join(logql)
        self.assertIn("LEVEL=", joined)
        self.assertNotIn("{env=", joined)
        self.assertNotIn("{level=", joined)
        self.assertNotIn("SERVICE=", joined)
        self.assertTrue(
            any("strn:distribution:DeliveryRequest:(38|39|40|43)" in query for query in logql)
            or any("DeliveryRequest:38" in query for query in logql)
        )

    def test_debug_info_not_packed_when_error_exists(self) -> None:
        def logs(_args: dict) -> dict:
            return {
                "data": [
                    {
                        "line": "level=debug ping strn:distribution:DeliveryRequest:43",
                        "timestamp": "1",
                    },
                    {
                        "line": "level=error timeout strn:distribution:DeliveryRequest:43",
                        "timestamp": "2",
                    },
                    {
                        "line": "level=info started strn:distribution:DeliveryRequest:38",
                        "timestamp": "3",
                    },
                ]
            }

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        packed = grafana.distribution_lines + grafana.other_lines + grafana.highlights
        self.assertTrue(any("level=error" in line for line in packed))
        self.assertFalse(any("level=debug" in line for line in packed))
        self.assertFalse(any("level=info" in line for line in packed))
        evidence, _, _ = build_evidence(srm, grafana)
        self.assertIn("level=error", evidence)
        self.assertNotIn("level=debug", evidence)
        self.assertNotIn("level=info", evidence)

    def test_only_debug_leaves_error_warn_list_empty(self) -> None:
        def logs(_args: dict) -> dict:
            return {
                "data": [
                    {
                        "line": "level=debug hello strn:distribution:DeliveryRequest:43",
                        "timestamp": "1",
                    }
                ]
            }

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        self.assertEqual(grafana.distribution_lines, [])
        self.assertEqual(grafana.other_lines, [])
        evidence, _, _ = build_evidence(srm, grafana)
        self.assertIn("component=distribution LEVEL= line filter", evidence)
        self.assertIn("(none)", evidence)
        self.assertTrue(
            any("LEVEL=alert|error|warn" in note or "LEVEL=INFO" in note for note in grafana.notes),
            grafana.notes,
        )
        self.assertTrue(any("debug" in note.lower() for note in grafana.notes), grafana.notes)

    def test_dashboard_filters_override_defaults(self) -> None:
        fake = FakeMcp(responses=_default_responses())
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        settings.grafana_dashboard_filters = {
            "env": "staging",
            "component": "gateway",
            "level": "error",
        }
        grafana = collect_grafana(srm, settings, client=fake)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        joined = "\n".join(logql)
        self.assertIn('component="gateway"', joined)
        self.assertNotIn("{env=", joined)
        self.assertNotIn("{level=", joined)
        self.assertEqual(grafana.env, "staging")
        panel = [args for name, args in fake.calls if name == "get_dashboard_panel_queries"]
        self.assertEqual(panel[0]["variables"]["env"], "staging")

    def test_default_filters_are_production_distribution(self) -> None:
        env = os.environ.copy()
        env.pop("GRAFANA_DASHBOARD_FILTERS", None)
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.grafana_dashboard_filters.get("env"), "production")
        self.assertEqual(
            settings.grafana_dashboard_filters.get("component"), "distribution"
        )
        self.assertEqual(
            settings.grafana_dashboard_filters.get("level"), "alert|error|warn"
        )

    def test_priority_logql_order_for_screenshot_urns(self) -> None:
        urns = [
            "strn:distribution:DeliveryRequest:38",
            "strn:distribution:DeliveryRequest:39",
            "strn:distribution:DeliveryRequest:40",
            "strn:distribution:DeliveryRequest:43",
        ]
        queries = build_priority_logql(urns)
        self.assertTrue(queries[0].startswith('{component="distribution"}'))
        self.assertIn("LEVEL=(alert|error|warn|ALERT|ERROR|WARN)", queries[0])
        self.assertIn('component=~".+"', queries[-1])
        dist_i = next(i for i, q in enumerate(queries) if 'component="distribution"' in q)
        all_i = next(i for i, q in enumerate(queries) if 'component=~".+"' in q)
        self.assertLess(dist_i, all_i)
        self.assertTrue(any("38|39|40|43" in q for q in queries))
        joined = "\n".join(queries)
        self.assertNotIn("{env=", joined)
        self.assertNotIn("{level=", joined)
        self.assertNotIn("udevopsdm", joined)
        self.assertNotIn("username", joined.lower())
        info = build_priority_logql(urns, level_pass="info")
        self.assertIn("LEVEL=(INFO|info)", info[0])
        self.assertTrue(info[0].startswith('{component="distribution"}'))

    def test_explore_logql_has_component_and_level_line_filter(self) -> None:
        fake = FakeMcp(responses=_default_responses())
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        collect_grafana(srm, settings, client=fake)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        joined = "\n".join(logql)
        self.assertIn('{component="distribution"}', joined)
        self.assertIn("LEVEL=", joined)
        self.assertNotIn("{env=", joined)
        self.assertNotIn("{level=", joined)
        self.assertNotIn("{SERVICE=", joined)
        self.assertFalse(any(name == "query_loki_patterns" for name, _ in fake.calls))
        self.assertFalse(any(name == "find_error_pattern_logs" for name, _ in fake.calls))

    def test_info_fallback_when_warn_pass_empty(self) -> None:
        def logs(args: dict) -> dict:
            query = str(args.get("logql") or "")
            if "LEVEL=(INFO|info)" in query:
                return {
                    "data": [
                        {
                            "line": "LEVEL=INFO grant worker strn:distribution:DeliveryRequest:43",
                            "timestamp": "2026-08-27T09:00:00Z",
                        }
                    ]
                }
            return {"data": []}

        responses = _default_responses()
        responses["query_loki_logs"] = logs
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        joined = "\n".join(logql)
        self.assertIn("LEVEL=(alert|error|warn|ALERT|ERROR|WARN)", joined)
        self.assertIn("LEVEL=(INFO|info)", joined)
        self.assertTrue(any("LEVEL=INFO" in line for line in grafana.highlights))
        self.assertIn("info", grafana.level_pass.values())
        summary = render_summary_md(srm, grafana=grafana)
        self.assertIn("Level pass:", summary)
        self.assertIn("info", summary)

    def test_skips_panel_logql_with_unresolved_vars(self) -> None:
        responses = _default_responses()
        responses["get_dashboard_panel_queries"] = [
            {"title": "logs", "expr": '{component="${missing}"} |= "DeliveryRequest"'}
        ]
        fake = FakeMcp(responses=responses)
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        settings = load_settings()
        settings.grafana_mcp_url = "https://grafana-mcp.example.st.com/sse"
        grafana = collect_grafana(srm, settings, client=fake)
        logql = [args["logql"] for name, args in fake.calls if name == "query_loki_logs"]
        self.assertFalse(any("${" in str(q) for q in logql))
        self.assertTrue(
            any("still containing ${" in note for note in grafana.notes),
            grafana.notes,
        )


if __name__ == "__main__":
    unittest.main()
