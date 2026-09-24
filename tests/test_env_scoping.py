from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.collect import run_collect_environments
from src.delivery_analysis.config import SRM_ENVIRONMENTS, load_settings
from src.delivery_analysis.grafana import (
    WRITE_TOOLS,
    build_priority_logql,
    collect_grafana,
    component_selector,
    loki_environment_value,
)
from src.delivery_analysis.stgpt_client import ChatResult
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 15, 0, 0, tzinfo=UTC)
REQ_ID = "302"
URN302 = "strn:distribution:DeliveryRequest:302"
CORR_ID = "aq0AFUBWyw14I0YBKesDugAAAU"

CONTEXT_LINE = (
    f'[DEBUG] [chrispin] [{CORR_ID}] [notification] [Template.render] '
    f'{{"urn":"{URN302}","meta":{{"requestId":"{CORR_ID}"}}}}'
)
SUCCESS_LINE = f'[DEBUG] [chrispin] [{CORR_ID}] [notification] successfully processed'

# The request's logs live ONLY in the qa environment.
OWNING_ENV_LABEL = "qa"


class EnvScopedFakeMcp:
    """One Loki holding all envs; request 302's logs exist only where the query
    carries environment="qa". Models the real deployment.
    """

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
            return ["distribution", "notification"]
        if name == "query_loki_stats":
            return {"streams": 1}
        if name == "generate_deeplink":
            return {"url": "https://grafana.example.st.com/x"}
        if name == "query_loki_logs":
            q = str(arguments.get("logql", ""))
            if 'component="notification"' not in q:
                return {"data": []}
            # Logs only exist for the owning environment.
            if f'environment="{OWNING_ENV_LABEL}"' not in q:
                return {"data": []}
            if URN302 in q:
                return {"data": [{"line": CONTEXT_LINE}]}
            if CORR_ID in q:
                return {"data": [{"line": SUCCESS_LINE}]}
            return {"data": []}
        return {}

    def close(self):
        return None


class EnvMappingTests(unittest.TestCase):
    def test_prod_maps_to_production(self):
        self.assertEqual(loki_environment_value("prod"), "production")
        self.assertEqual(loki_environment_value("production"), "production")

    def test_other_envs_unchanged(self):
        for env in ("test", "int", "qa", "demo"):
            self.assertEqual(loki_environment_value(env), env)

    def test_empty_is_none(self):
        self.assertIsNone(loki_environment_value(None))
        self.assertIsNone(loki_environment_value(""))


class SelectorEnvTests(unittest.TestCase):
    def test_component_selector_adds_environment(self):
        self.assertEqual(
            component_selector("notification", environment="qa"),
            '{component="notification", environment="qa"}',
        )

    def test_component_selector_without_env_unchanged(self):
        self.assertEqual(component_selector("distribution"), '{component="distribution"}')

    def test_priority_logql_scopes_by_environment(self):
        queries = build_priority_logql(
            [URN302], include_per_urn=False, environment="production"
        )
        self.assertTrue(all('environment="production"' in q for q in queries))

    def test_priority_logql_no_env_when_absent(self):
        queries = build_priority_logql([URN302], include_per_urn=False)
        self.assertFalse(any("environment=" in q for q in queries))


class PerEnvQueryScopingTests(unittest.TestCase):
    def _collect(self, env):
        settings = _settings()
        srm = evaluate_payload([], as_of=AS_OF, environment=env, request_id=REQ_ID)
        fake = EnvScopedFakeMcp()
        result = collect_grafana(srm, settings, client=fake)
        return result, fake

    def test_owning_env_finds_success(self):
        result, fake = self._collect("qa")
        self.assertTrue(result.notification_success)
        notif_qs = [
            a.get("logql")
            for n, a in fake.calls
            if n == "query_loki_logs" and "notification" in str(a.get("logql"))
        ]
        self.assertTrue(all('environment="qa"' in str(q) for q in notif_qs))

    def test_prod_scopes_to_production_and_finds_nothing(self):
        result, fake = self._collect("prod")
        self.assertFalse(result.notification_success)
        notif_qs = [
            a.get("logql")
            for n, a in fake.calls
            if n == "query_loki_logs" and "notification" in str(a.get("logql"))
        ]
        self.assertTrue(notif_qs)
        self.assertTrue(all('environment="production"' in str(q) for q in notif_qs))


class AllRunDistinctResultsTests(unittest.TestCase):
    def test_all_run_only_owning_env_succeeds(self):
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
                    multi = run_collect_environments(
                        env="ALL",
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=load_settings(env="ALL", request_id=REQ_ID),
                        mcp_client=EnvScopedFakeMcp(),
                        analyze=True,
                        chat_fn=chat_fn,
                        request_id=REQ_ID,
                    )
                    outcomes = {
                        e: json.loads(
                            (Path(tmp) / e / "srm.json").read_text(encoding="utf-8")
                        )["request_outcome"]
                        for e in SRM_ENVIRONMENTS
                    }
        # Only qa owns the request's logs -> SUCCESS; the rest are INVESTIGATE.
        self.assertEqual(outcomes["qa"], "SUCCESS")
        for other in ("test", "int", "demo", "prod"):
            self.assertEqual(outcomes[other], "INVESTIGATE (no completion evidence)")
        self.assertEqual(chat_calls, [])  # no STGPT for success or investigate


def _settings(**env):
    base = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=False):
        return load_settings(request_id=REQ_ID)


if __name__ == "__main__":
    unittest.main()
