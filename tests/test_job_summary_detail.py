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
from src.delivery_analysis.stgpt_client import ChatResult
from tests.test_notification_tempo import FakeMcp

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)
URN43 = "strn:distribution:DeliveryRequest:43"

PAYLOAD = [
    {"state": "SUBMITTED", "_urn": URN43, "_updated": {"on": "8/27/2026 9:30:43 AM"}},
]

ROOT_CAUSE = "DeliveryRequest 43 has been SUBMITTED since 8/27/2026 9:30:43 AM"


def _ok_payload(quote: str) -> str:
    return json.dumps(
        {
            "root_cause": ROOT_CAUSE,
            "suggested_fix": "Replay or unlock the stuck GRANT path after Loki review",
            "confidence": "medium",
            "citations": [{"quote": quote, "source": "srm_submitted", "line": None}],
            "cannot_determine": False,
        }
    )


def _chat_fn(calls):
    def chat_fn(persona, messages):
        calls.append(persona)
        payload = _ok_payload(URN43)
        return ChatResult(200, {"completion": payload}, payload, "rid")

    return chat_fn


def _run(env):
    calls: list[str] = []
    envvars = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    with patch.dict(os.environ, envvars, clear=False):
        with patch(
            "src.delivery_analysis.collect.fetch_delivery_requests",
            return_value=PAYLOAD,
        ):
            with TemporaryDirectory() as tmp:
                run_collect_environments(
                    env=env,
                    as_of=AS_OF,
                    out_dir=tmp,
                    settings=load_settings(env=env),
                    mcp_client=FakeMcp(),
                    analyze=True,
                    chat_fn=_chat_fn(calls),
                )
                root = Path(tmp)
                top = (root / "summary.md").read_text(encoding="utf-8")
                per_env = {
                    e: (root / e / "summary.md").read_text(encoding="utf-8")
                    for e in os.listdir(root)
                    if (root / e).is_dir()
                }
                return top, per_env, calls


class TopLevelEmbedTests(unittest.TestCase):
    def test_all_run_embeds_full_reports(self):
        top, per_env, calls = _run("ALL")
        # Index table still present.
        self.assertIn("| env | verdict | reason | SRM URL | details |", top)
        for env in SRM_ENVIRONMENTS:
            self.assertIn(f"[{env}/summary.md]({env}/summary.md)", top)
            self.assertIn(f"## Environment: {env}", top)
        # STALE env detail embedded: Grafana inventory + AI analysis + citation.
        self.assertIn("## Grafana / Loki inventory", top)
        self.assertIn("## AI analysis", top)
        self.assertIn(ROOT_CAUSE, top)
        self.assertIn(f"`{URN43}` (srm_submitted)", top)
        self.assertTrue(calls)  # STGPT was invoked for the STALE envs

    def test_per_env_files_still_full_report(self):
        top, per_env, calls = _run("ALL")
        for env in SRM_ENVIRONMENTS:
            report = per_env[env]
            self.assertIn("## Grafana / Loki inventory", report)
            self.assertIn("## AI analysis", report)
            self.assertIn(ROOT_CAUSE, report)

    def test_single_env_top_level_has_full_ai(self):
        top, per_env, calls = _run("qa")
        self.assertIn("| env | verdict | reason | SRM URL | details |", top)
        self.assertIn("## Environment: qa", top)
        self.assertIn("## AI analysis", top)
        self.assertIn(ROOT_CAUSE, top)
        self.assertIn(f"`{URN43}` (srm_submitted)", top)
        # Only the qa env folder exists for a single-env run.
        self.assertEqual(set(per_env), {"qa"})


if __name__ == "__main__":
    unittest.main()
