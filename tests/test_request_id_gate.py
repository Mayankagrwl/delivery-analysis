from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.collect import run_collect
from src.delivery_analysis.config import load_settings
from src.delivery_analysis.grafana import build_time_packs, collect_grafana
from src.delivery_analysis.stgpt_client import ChatResult
from src.delivery_analysis.verdict import evaluate_payload

# Reuse the shared fakes / fixtures from the notification-tempo suite.
from tests.test_notification_tempo import (
    FakeMcp,
    NOTIF_DEBUG_SUCCESS,
    NOTIF_NO_SUCCESS,
    PAYLOAD,
    URN43,
)

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)
# An id absent from PAYLOAD -> verdict NO_RECORDS.
MISSING_ID = "99999"


def _spy_chat(calls):
    def chat_fn(persona, messages):
        calls.append(persona)
        return ChatResult(200, {"completion": "{}"}, "{}", "rid")

    return chat_fn


def _run(*, request_id, as_of, notif_line, tempo_id=None, chat_calls=None,
         payload=PAYLOAD):
    env = {
        "SRM_BASIC_USER": "u",
        "SRM_BASIC_PASSWORD": "pw",
        "GRAFANA_MCP_URL": "https://grafana-mcp.example.st.com/sse",
    }
    if tempo_id:
        env["TEMPO_ID"] = tempo_id
    chat_calls = chat_calls if chat_calls is not None else []
    with patch.dict(os.environ, env, clear=False):
        with patch(
            "src.delivery_analysis.collect.fetch_delivery_requests",
            return_value=payload,
        ):
            with TemporaryDirectory() as tmp:
                result = run_collect(
                    as_of=as_of,
                    out_dir=tmp,
                    settings=load_settings(request_id=request_id),
                    mcp_client=FakeMcp(notif_line=notif_line),
                    analyze=True,
                    chat_fn=_spy_chat(chat_calls),
                    request_id=request_id,
                )
                srm = json.loads((Path(tmp) / "srm.json").read_text(encoding="utf-8"))
                analysis = json.loads(
                    (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                )
                summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                return result, srm, analysis, summary, chat_calls


class NoRecordsSuccessTests(unittest.TestCase):
    def test_success_found_for_missing_id_skips_stgpt(self):
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id=MISSING_ID,
            as_of=AS_OF,
            notif_line=NOTIF_DEBUG_SUCCESS,
            tempo_id="tempo-uid",
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(calls, [])  # STGPT never called
        self.assertEqual(analysis["status"], "infra_success")
        self.assertEqual(analysis["tokens_used"], 0)
        self.assertTrue(analysis["result"]["citations"])
        self.assertEqual(srm["request_outcome"], "SUCCESS")
        self.assertIn("SUCCESS", summary)
        self.assertIn("successfully processed", summary)
        # Tempo cascade rendered.
        self.assertIn("## Request trace (Tempo)", summary)
        self.assertIn("send-mail", summary)


class NoRecordsInvestigateTests(unittest.TestCase):
    def test_no_success_not_stale_does_not_call_stgpt(self):
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id=MISSING_ID,
            as_of=AS_OF,
            notif_line=NOTIF_NO_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(calls, [])  # STGPT NOT called
        self.assertEqual(analysis["status"], "investigate")
        self.assertEqual(analysis["tokens_used"], 0)
        self.assertEqual(srm["request_outcome"], "INVESTIGATE (no completion evidence)")
        self.assertIn("investigate", summary.lower())
        self.assertNotIn("Not an incident", summary)
        # Grafana probe ran and surfaced the outcome in the summary.
        self.assertIn("No completion evidence found", summary)

    def test_fresh_single_id_investigates_without_ai(self):
        # request 43 evaluated close to its updated.on -> FRESH.
        fresh_as_of = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id="43",
            as_of=fresh_as_of,
            notif_line=NOTIF_NO_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "FRESH")
        self.assertEqual(calls, [])
        self.assertEqual(analysis["status"], "investigate")


class StaleRequestIdTests(unittest.TestCase):
    def test_stale_no_success_runs_stgpt(self):
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id="43",
            as_of=AS_OF,  # 43 is stale here
            notif_line=NOTIF_NO_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "STALE")
        self.assertTrue(calls)  # genuine stale incident -> STGPT invoked
        self.assertNotEqual(analysis["status"], "infra_success")
        self.assertNotEqual(analysis["status"], "investigate")

    def test_stale_with_success_wins_over_analysis(self):
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id="43",
            as_of=AS_OF,
            notif_line=NOTIF_DEBUG_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "STALE")
        self.assertEqual(calls, [])  # success wins, no AI
        self.assertEqual(analysis["status"], "infra_success")
        self.assertEqual(srm["request_outcome"], "SUCCESS")


class AllRecordsRegressionTests(unittest.TestCase):
    def test_no_request_id_fresh_skips_grafana_and_stgpt(self):
        fresh_as_of = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id=None,
            as_of=fresh_as_of,
            notif_line=NOTIF_DEBUG_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "FRESH")
        self.assertEqual(calls, [])
        # No request_id: analysis is the plain STALE-gated skip, not investigate.
        self.assertEqual(analysis["status"], "gated")
        self.assertIsNone(srm["request_outcome"])
        # Notification gate is not applicable without request_id.
        self.assertIn("not applicable", summary)

    def test_no_request_id_stale_runs_stgpt(self):
        calls: list[str] = []
        result, srm, analysis, summary, calls = _run(
            request_id=None,
            as_of=AS_OF,
            notif_line=NOTIF_DEBUG_SUCCESS,
            chat_calls=calls,
        )
        self.assertEqual(result.verdict, "STALE")
        self.assertTrue(calls)  # all-records STALE still runs STGPT
        # notification gate is not applicable without request_id
        self.assertIn("not applicable", summary)


class LookbackWindowTests(unittest.TestCase):
    def test_build_time_packs_prepends_lookback(self):
        srm = evaluate_payload(PAYLOAD, as_of=AS_OF, request_id="43")
        packs = build_time_packs(srm, request_id="43", lookback_hours=336)
        self.assertEqual(packs[0].label, "lookback")
        expected_start = (AS_OF - timedelta(hours=336)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(packs[0].start, expected_start)
        self.assertFalse(any(p.label == "recent" for p in packs))

    def test_no_request_id_has_no_lookback_pack(self):
        srm = evaluate_payload(PAYLOAD, as_of=AS_OF)
        packs = build_time_packs(srm)
        self.assertFalse(any(p.label == "lookback" for p in packs))

    def test_lookback_hours_propagates_via_collector(self):
        env = {
            "SRM_BASIC_USER": "u",
            "SRM_BASIC_PASSWORD": "pw",
            "GRAFANA_MCP_URL": "https://g/sse",
            "REQUEST_ID_LOOKBACK_HOURS": "336",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings(request_id=MISSING_ID)
        srm = evaluate_payload(PAYLOAD, as_of=AS_OF, request_id=MISSING_ID)
        result = collect_grafana(srm, settings, client=FakeMcp())
        self.assertTrue(result.notification_checked)
        self.assertEqual(result.notification_lookback_hours, 336.0)
        # The probe queried a window starting ~336h before as_of.
        starts = [rng.get("start") for rng in result.time_ranges]
        expected_start = (AS_OF - timedelta(hours=336)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn(expected_start, starts)


if __name__ == "__main__":
    unittest.main()
