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
from src.delivery_analysis.stgpt_client import ChatResult
from tests.test_notification_tempo import FakeMcp, NOTIF_DEBUG_SUCCESS, NOTIF_NO_SUCCESS

UTC = timezone.utc
# request 43 is within 24h of this clock -> verdict FRESH (present, not stale).
FRESH_AS_OF = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
STALE_AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)
URN43 = "strn:distribution:DeliveryRequest:43"

PAYLOAD = [
    {"state": "SUBMITTED", "_urn": URN43, "_updated": {"on": "8/27/2026 9:30:43 AM"}},
]

OK_PAYLOAD = json.dumps(
    {
        "root_cause": "DeliveryRequest 43 still SUBMITTED; forced early RCA",
        "suggested_fix": "Check the grant worker before the 24h SLA elapses",
        "confidence": "medium",
        "citations": [{"quote": URN43, "source": "srm_submitted", "line": None}],
        "cannot_determine": False,
    }
)


def _run(*, request_id, as_of, force_ai, notif_line=NOTIF_NO_SUCCESS, chat_calls=None):
    chat_calls = chat_calls if chat_calls is not None else []

    def chat_fn(persona, messages):
        chat_calls.append(persona)
        return ChatResult(200, {"completion": OK_PAYLOAD}, OK_PAYLOAD, "rid")

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
                    as_of=as_of,
                    out_dir=tmp,
                    settings=load_settings(request_id=request_id, force_ai=force_ai),
                    mcp_client=FakeMcp(notif_line=notif_line),
                    analyze=True,
                    chat_fn=chat_fn,
                    request_id=request_id,
                )
                analysis = json.loads(
                    (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                )
                summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                return result, analysis, summary, chat_calls


class ConfigTests(unittest.TestCase):
    def test_force_ai_env(self):
        with patch.dict(os.environ, {"FORCE_AI": "true"}, clear=False):
            self.assertTrue(load_settings().force_ai)
        with patch.dict(os.environ, {"FORCE_AI": "false"}, clear=False):
            self.assertFalse(load_settings().force_ai)

    def test_explicit_arg_overrides_env(self):
        with patch.dict(os.environ, {"FORCE_AI": "true"}, clear=False):
            self.assertFalse(load_settings(force_ai=False).force_ai)


class ForceAiFreshTests(unittest.TestCase):
    def test_fresh_force_runs_stgpt(self):
        calls: list[str] = []
        result, analysis, summary, calls = _run(
            request_id="43", as_of=FRESH_AS_OF, force_ai=True, chat_calls=calls
        )
        self.assertEqual(result.verdict, "FRESH")
        self.assertTrue(calls)  # STGPT invoked despite not stale
        self.assertEqual(analysis["status"], "ok")
        self.assertEqual(result.request_outcome, "FRESH (forced AI analysis)")
        self.assertIn("DeliveryRequest 43 still SUBMITTED", summary)

    def test_fresh_without_force_is_investigate_no_ai(self):
        calls: list[str] = []
        result, analysis, summary, calls = _run(
            request_id="43", as_of=FRESH_AS_OF, force_ai=False, chat_calls=calls
        )
        self.assertEqual(result.verdict, "FRESH")
        self.assertEqual(calls, [])  # no STGPT
        self.assertEqual(analysis["status"], "investigate")
        self.assertEqual(result.request_outcome, "INVESTIGATE (no completion evidence)")

    def test_fresh_force_but_notification_success_still_wins(self):
        calls: list[str] = []
        result, analysis, summary, calls = _run(
            request_id="43",
            as_of=FRESH_AS_OF,
            force_ai=True,
            notif_line=NOTIF_DEBUG_SUCCESS,
            chat_calls=calls,
        )
        # Infra success is definitive -> no AI even when forced.
        self.assertEqual(calls, [])
        self.assertEqual(analysis["status"], "infra_success")
        self.assertEqual(result.request_outcome, "SUCCESS")

    def test_stale_still_runs_ai_regardless_of_force(self):
        calls: list[str] = []
        result, analysis, summary, calls = _run(
            request_id="43", as_of=STALE_AS_OF, force_ai=False, chat_calls=calls
        )
        self.assertEqual(result.verdict, "STALE")
        self.assertTrue(calls)
        self.assertEqual(result.request_outcome, "STALE (AI analysis)")


class ForceAiScopeTests(unittest.TestCase):
    def test_force_requires_request_id(self):
        # No request_id: force_ai must NOT trigger AI on a FRESH all-records run.
        calls: list[str] = []

        def chat_fn(persona, messages):
            calls.append(persona)
            return ChatResult(200, {"completion": OK_PAYLOAD}, OK_PAYLOAD, "rid")

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
                        as_of=FRESH_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(force_ai=True),
                        mcp_client=FakeMcp(notif_line=NOTIF_NO_SUCCESS),
                        analyze=True,
                        chat_fn=chat_fn,
                    )
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
        self.assertEqual(result.verdict, "FRESH")
        self.assertEqual(calls, [])  # no request_id -> force does not apply
        self.assertEqual(analysis["status"], "gated")

    def test_force_does_not_apply_to_no_records(self):
        # id absent from SUBMITTED/GRANTED -> NO_RECORDS; force is SUBMITTED/GRANTED only.
        calls: list[str] = []

        def chat_fn(persona, messages):
            calls.append(persona)
            return ChatResult(200, {"completion": OK_PAYLOAD}, OK_PAYLOAD, "rid")

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
                        as_of=FRESH_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(request_id="99999", force_ai=True),
                        mcp_client=FakeMcp(notif_line=NOTIF_NO_SUCCESS),
                        analyze=True,
                        chat_fn=chat_fn,
                        request_id="99999",
                    )
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(calls, [])  # not present in SUBMITTED/GRANTED
        self.assertEqual(analysis["status"], "investigate")


if __name__ == "__main__":
    unittest.main()
