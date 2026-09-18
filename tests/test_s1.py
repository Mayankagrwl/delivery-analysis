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
from src.delivery_analysis.report import render_summary_md, write_artifacts
from src.delivery_analysis.srm import discover_key_map
from src.delivery_analysis.timestamps import parse_srm_timestamp
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
SCREENSHOT_AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)

SCREENSHOT_PAYLOAD = [
    {
        "state": "SUBMITTED",
        "_urn": "strn:distribution:DeliveryRequest:43",
        "_updated": {"on": "8/27/2026 9:30:43 AM"},
    },
    {
        "state": "GRANTED",
        "_urn": "strn:distribution:DeliveryRequest:38",
        "_updated": {"on": "8/26/2026 3:19:56 PM"},
    },
    {
        "state": "GRANTED",
        "_urn": "strn:distribution:DeliveryRequest:39",
        "_updated": {"on": "8/27/2026 7:41:26 AM"},
    },
    {
        "state": "GRANTED",
        "_urn": "strn:distribution:DeliveryRequest:40",
        "_updated": {"on": "8/27/2026 8:21:34 AM"},
    },
]


class TimestampTests(unittest.TestCase):
    def test_screenshot_format_is_utc(self) -> None:
        dt = parse_srm_timestamp("8/27/2026 9:30:43 AM")
        self.assertEqual(dt, datetime(2026, 8, 27, 9, 30, 43, tzinfo=UTC))

    def test_pm_timestamp(self) -> None:
        dt = parse_srm_timestamp("8/26/2026 3:19:56 PM")
        self.assertEqual(dt, datetime(2026, 8, 26, 15, 19, 56, tzinfo=UTC))


class DiscoverTests(unittest.TestCase):
    def test_nested_underscore_keys(self) -> None:
        key_map = discover_key_map(SCREENSHOT_PAYLOAD)
        self.assertEqual(key_map.state, "state")
        self.assertEqual(key_map.urn, "_urn")
        self.assertEqual(key_map.updated_on, "_updated.on")

    def test_alternate_flat_keys(self) -> None:
        payload = [
            {
                "state": "SUBMITTED",
                "urn": "strn:distribution:DeliveryRequest:1",
                "updated.on": "8/27/2026 9:30:43 AM",
            }
        ]
        key_map = discover_key_map(payload)
        self.assertEqual(key_map.urn, "urn")
        self.assertEqual(key_map.updated_on, "updated.on")

    def test_camel_case_timestamp(self) -> None:
        payload = [
            {
                "state": "GRANTED",
                "_urn": "strn:distribution:DeliveryRequest:2",
                "updatedOn": "2026-08-27T09:30:43Z",
            }
        ]
        key_map = discover_key_map(payload)
        self.assertEqual(key_map.updated_on, "updatedOn")


class VerdictTests(unittest.TestCase):
    def test_screenshot_fixture_is_stale(self) -> None:
        result = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "STALE")
        self.assertEqual(len(result.records), 4)
        self.assertTrue(all(rec.stale for rec in result.records))
        urns = {rec.urn for rec in result.records}
        self.assertEqual(
            urns,
            {
                "strn:distribution:DeliveryRequest:43",
                "strn:distribution:DeliveryRequest:38",
                "strn:distribution:DeliveryRequest:39",
                "strn:distribution:DeliveryRequest:40",
            },
        )
        self.assertEqual(result.key_map.urn, "_urn")
        self.assertEqual(result.key_map.updated_on, "_updated.on")

    def test_fresh_within_24h(self) -> None:
        as_of = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
        result = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=as_of)
        self.assertEqual(result.verdict, "FRESH")
        self.assertTrue(all(rec.stale is False for rec in result.records))

    def test_mix_of_fresh_and_stale_is_stale(self) -> None:
        as_of = datetime(2026, 8, 27, 16, 0, 0, tzinfo=UTC)
        result = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=as_of)
        self.assertEqual(result.verdict, "STALE")
        by_urn = {rec.urn: rec.stale for rec in result.records}
        self.assertTrue(by_urn["strn:distribution:DeliveryRequest:38"])
        self.assertFalse(by_urn["strn:distribution:DeliveryRequest:43"])

    def test_cutoff_boundary_is_not_stale(self) -> None:
        payload = [
            {
                "state": "SUBMITTED",
                "_urn": "strn:distribution:DeliveryRequest:1",
                "_updated": {"on": "9/17/2026 8:00:00 AM"},
            }
        ]
        result = evaluate_payload(payload, as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "FRESH")
        self.assertFalse(result.records[0].stale)

    def test_empty_payload_is_no_records(self) -> None:
        result = evaluate_payload([], as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertIn("Not an incident", result.reason)

    def test_other_states_only_is_no_records(self) -> None:
        payload = [
            {
                "state": "CLOSED",
                "_urn": "strn:distribution:DeliveryRequest:99",
                "_updated": {"on": "8/01/2026 9:00:00 AM"},
            }
        ]
        result = evaluate_payload(payload, as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(result.records, [])

    def test_missing_keys_is_srm_error(self) -> None:
        payload = [{"foo": "bar", "baz": 1}]
        result = evaluate_payload(payload, as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "SRM_ERROR")
        self.assertTrue(result.key_inventory)
        self.assertNotIn("bar", json.dumps(result.model_dump(mode="json")))

    def test_unparseable_timestamp_is_srm_error(self) -> None:
        payload = [
            {
                "state": "SUBMITTED",
                "_urn": "strn:distribution:DeliveryRequest:1",
                "_updated": {"on": "not-a-date"},
            }
        ]
        result = evaluate_payload(payload, as_of=SCREENSHOT_AS_OF)
        self.assertEqual(result.verdict, "SRM_ERROR")


class ReportTests(unittest.TestCase):
    def test_summary_has_verdict_and_tables(self) -> None:
        result = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        markdown = render_summary_md(result)
        self.assertIn("**STALE**", markdown)
        self.assertIn("## SRM SUBMITTED", markdown)
        self.assertIn("## SRM GRANTED", markdown)
        self.assertIn("strn:distribution:DeliveryRequest:43", markdown)
        self.assertIn("strn:distribution:DeliveryRequest:38", markdown)
        self.assertIn("## Grafana / Loki inventory", markdown)
        self.assertIn("skipped (Grafana collection not run)", markdown)
        self.assertIn("## AI analysis", markdown)
        self.assertNotIn("STGPT", markdown)

    def test_artifacts_do_not_contain_password(self) -> None:
        result = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        secret = "super-secret-password"
        with TemporaryDirectory() as tmp:
            write_artifacts(result, Path(tmp))
            srm = (Path(tmp) / "srm.json").read_text(encoding="utf-8")
            summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
            payload = json.loads(srm)
            self.assertEqual(payload["verdict"], "STALE")
            self.assertEqual(payload["key_map"]["urn"], "_urn")
            self.assertNotIn(secret, srm)
            self.assertNotIn(secret, summary)


class CollectCliTests(unittest.TestCase):
    def test_collect_writes_stale_artifacts(self) -> None:
        env = {
            "SRM_BASIC_USER": "udevopsdm",
            "SRM_BASIC_PASSWORD": "super-secret-password",
            "GRAFANA_MCP_URL": "",
            "GRAFANA_MCP_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ) as fetch:
                with TemporaryDirectory() as tmp:
                    result = run_collect(
                        as_of=SCREENSHOT_AS_OF,
                        out_dir=tmp,
                        settings=load_settings(),
                        analyze=False,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    fetch.assert_called_once()
                    args, kwargs = fetch.call_args
                    self.assertEqual(args[1], "udevopsdm")
                    self.assertEqual(args[2], "super-secret-password")
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    srm = (Path(tmp) / "srm.json").read_text(encoding="utf-8")
                    self.assertIn("**STALE**", summary)
                    self.assertNotIn("super-secret-password", summary)
                    self.assertNotIn("super-secret-password", srm)

    def test_cli_collect_screenshot(self) -> None:
        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "super-secret-password",
            "GRAFANA_MCP_URL": "",
            "GRAFANA_MCP_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=SCREENSHOT_PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
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
                    payload = json.loads(
                        (Path(tmp) / "srm.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(payload["verdict"], "STALE")

    def test_missing_credentials_is_srm_error_exit_0(self) -> None:
        env = os.environ.copy()
        env.pop("SRM_BASIC_USER", None)
        env.pop("SRM_BASIC_PASSWORD", None)
        env.pop("STRICT", None)
        with patch.dict(os.environ, env, clear=True):
            with TemporaryDirectory() as tmp:
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
                payload = json.loads(
                    (Path(tmp) / "srm.json").read_text(encoding="utf-8")
                )
                self.assertEqual(payload["verdict"], "SRM_ERROR")

    def test_strict_exits_nonzero_on_srm_error(self) -> None:
        env = os.environ.copy()
        env.pop("SRM_BASIC_USER", None)
        env.pop("SRM_BASIC_PASSWORD", None)
        env.pop("STRICT", None)
        with patch.dict(os.environ, env, clear=True):
            with TemporaryDirectory() as tmp:
                rc = main(
                    [
                        "collect",
                        "--as-of",
                        "2026-09-18T08:00:00Z",
                        "--out-dir",
                        tmp,
                        "--strict",
                        "--no-analyze",
                    ]
                )
                self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
