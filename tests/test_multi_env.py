from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.cli import main
from src.delivery_analysis.collect import run_collect_environments
from src.delivery_analysis.config import (
    DEFAULT_SRM_HOST,
    SRM_ENVIRONMENTS,
    load_settings,
    srm_url_for_env,
)
from src.delivery_analysis.prompt import build_evidence
from src.delivery_analysis.srm import SrmError, normalize_request_id, urn_request_id
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)

PAYLOAD = [
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
]

_CREDS = {"SRM_BASIC_USER": "udevopsdm", "SRM_BASIC_PASSWORD": "pw", "GRAFANA_MCP_URL": ""}


class SrmUrlForEnvTests(unittest.TestCase):
    def test_non_prod_envs_use_distribution_segment(self) -> None:
        for env in ("test", "int", "qa", "demo"):
            self.assertEqual(
                srm_url_for_env(env),
                f"{DEFAULT_SRM_HOST}/distribution/{env}/"
                "resources/strn:distribution:DeliveryRequest",
            )

    def test_prod_uses_root_path(self) -> None:
        expected = f"{DEFAULT_SRM_HOST}/resources/strn:distribution:DeliveryRequest"
        self.assertEqual(srm_url_for_env("prod"), expected)
        self.assertEqual(srm_url_for_env("production"), expected)
        self.assertEqual(srm_url_for_env(""), expected)
        self.assertNotIn("/distribution/", srm_url_for_env("prod"))

    def test_case_insensitive_and_trimmed(self) -> None:
        self.assertEqual(srm_url_for_env("  QA "), srm_url_for_env("qa"))

    def test_unknown_env_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            srm_url_for_env("staging")
        for env in SRM_ENVIRONMENTS:
            self.assertIn(env, str(ctx.exception))

    def test_host_override_and_trailing_slash(self) -> None:
        url = srm_url_for_env("qa", host="https://trd.example.st.com/")
        self.assertEqual(
            url,
            "https://trd.example.st.com/distribution/qa/"
            "resources/strn:distribution:DeliveryRequest",
        )


class UrlPrecedenceTests(unittest.TestCase):
    def test_explicit_url_wins(self) -> None:
        with patch.dict(
            os.environ, {"SRM_BASE_URL": "https://env.example/x"}, clear=False
        ):
            cfg = load_settings(url="https://explicit.example/y", env="qa")
        self.assertEqual(cfg.srm_base_url, "https://explicit.example/y")

    def test_srm_base_url_env_beats_computed(self) -> None:
        with patch.dict(
            os.environ, {"SRM_BASE_URL": "https://env.example/x"}, clear=False
        ):
            cfg = load_settings(env="qa")
        self.assertEqual(cfg.srm_base_url, "https://env.example/x")

    def test_computed_from_env_when_no_override(self) -> None:
        env = os.environ.copy()
        env.pop("SRM_BASE_URL", None)
        with patch.dict(os.environ, env, clear=True):
            cfg = load_settings(env="qa")
        self.assertEqual(cfg.srm_base_url, srm_url_for_env("qa"))

    def test_srm_base_host_override(self) -> None:
        env = os.environ.copy()
        env.pop("SRM_BASE_URL", None)
        env["SRM_BASE_HOST"] = "https://trd.example.st.com"
        with patch.dict(os.environ, env, clear=True):
            cfg = load_settings(env="prod")
        self.assertEqual(
            cfg.srm_base_url,
            "https://trd.example.st.com/resources/strn:distribution:DeliveryRequest",
        )


class RequestIdHelperTests(unittest.TestCase):
    def test_normalize_bare_and_urn(self) -> None:
        self.assertEqual(normalize_request_id("39"), "39")
        self.assertEqual(
            normalize_request_id("strn:distribution:DeliveryRequest:39"), "39"
        )
        self.assertEqual(normalize_request_id("  123456  "), "123456")
        self.assertIsNone(normalize_request_id(""))
        self.assertIsNone(normalize_request_id(None))

    def test_urn_request_id(self) -> None:
        self.assertEqual(
            urn_request_id("strn:distribution:DeliveryRequest:40"), "40"
        )
        self.assertIsNone(urn_request_id(None))


class RequestIdScopingTests(unittest.TestCase):
    def test_bare_number_filters_to_single_urn(self) -> None:
        result = evaluate_payload(PAYLOAD, as_of=AS_OF, request_id="39")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].urn, "strn:distribution:DeliveryRequest:39")
        self.assertEqual(result.request_id, "39")
        self.assertEqual(result.verdict, "STALE")

    def test_full_urn_input_equivalent(self) -> None:
        result = evaluate_payload(
            PAYLOAD, as_of=AS_OF, request_id="strn:distribution:DeliveryRequest:39"
        )
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].urn, "strn:distribution:DeliveryRequest:39")

    def test_evidence_scopes_to_single_urn(self) -> None:
        result = evaluate_payload(PAYLOAD, as_of=AS_OF, request_id="39")
        evidence, _, _ = build_evidence(result, None, cap_tokens=6000)
        self.assertIn("strn:distribution:DeliveryRequest:39", evidence)
        self.assertNotIn("strn:distribution:DeliveryRequest:43", evidence)
        self.assertNotIn("strn:distribution:DeliveryRequest:38", evidence)

    def test_not_found_id_is_no_records_with_note(self) -> None:
        result = evaluate_payload(PAYLOAD, as_of=AS_OF, request_id="99999")
        self.assertEqual(result.verdict, "NO_RECORDS")
        self.assertEqual(result.records, [])
        joined = " ".join(result.notes)
        self.assertIn("99999", joined)
        self.assertIn("not present", joined)


class MultiEnvOrchestrationTests(unittest.TestCase):
    def test_all_loops_five_envs_and_writes_index(self) -> None:
        with patch.dict(os.environ, _CREDS, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    rc = main(
                        [
                            "collect",
                            "--env",
                            "ALL",
                            "--as-of",
                            "2026-09-18T08:00:00Z",
                            "--out-dir",
                            tmp,
                            "--no-analyze",
                        ]
                    )
                    self.assertEqual(rc, 0)
                    root = Path(tmp)
                    for env in SRM_ENVIRONMENTS:
                        srm = json.loads(
                            (root / env / "srm.json").read_text(encoding="utf-8")
                        )
                        self.assertEqual(srm["environment"], env)
                        self.assertEqual(srm["verdict"], "STALE")
                        self.assertTrue((root / env / "summary.md").exists())
                    index = (root / "summary.md").read_text(encoding="utf-8")
                    self.assertIn("multi-environment", index)
                    for env in SRM_ENVIRONMENTS:
                        self.assertIn(f"{env}/summary.md", index)

    def test_prod_url_has_no_distribution_segment(self) -> None:
        seen: dict[str, str] = {}

        def fake_fetch(url, user, password, **kwargs):
            seen[url] = url
            return PAYLOAD

        with patch.dict(os.environ, _CREDS, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=fake_fetch,
            ):
                with TemporaryDirectory() as tmp:
                    run_collect_environments(
                        env="ALL",
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=load_settings(env="ALL"),
                        analyze=False,
                    )
        prod_urls = [u for u in seen if u.endswith("resources/strn:distribution:DeliveryRequest")
                     and "/distribution/" not in u]
        self.assertTrue(prod_urls, "prod URL should use root path")
        self.assertTrue(any("/distribution/qa/" in u for u in seen))

    def test_single_env_writes_only_that_subfolder(self) -> None:
        with patch.dict(os.environ, _CREDS, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    rc = main(
                        [
                            "collect",
                            "--env",
                            "qa",
                            "--as-of",
                            "2026-09-18T08:00:00Z",
                            "--out-dir",
                            tmp,
                            "--no-analyze",
                        ]
                    )
                    self.assertEqual(rc, 0)
                    root = Path(tmp)
                    self.assertTrue((root / "qa" / "srm.json").exists())
                    for env in ("test", "int", "demo", "prod"):
                        self.assertFalse((root / env).exists())
                    self.assertTrue((root / "summary.md").exists())

    def test_request_id_flows_through_cli(self) -> None:
        with patch.dict(os.environ, _CREDS, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                return_value=PAYLOAD,
            ):
                with TemporaryDirectory() as tmp:
                    rc = main(
                        [
                            "collect",
                            "--env",
                            "qa",
                            "--request-id",
                            "39",
                            "--as-of",
                            "2026-09-18T08:00:00Z",
                            "--out-dir",
                            tmp,
                            "--no-analyze",
                        ]
                    )
                    self.assertEqual(rc, 0)
                    srm = json.loads(
                        (Path(tmp) / "qa" / "srm.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(srm["request_id"], "39")
                    self.assertEqual(len(srm["records"]), 1)
                    self.assertEqual(
                        srm["records"][0]["urn"],
                        "strn:distribution:DeliveryRequest:39",
                    )


class FailureIsolationTests(unittest.TestCase):
    def _fetch_that_fails_int(self):
        def fake_fetch(url, user, password, **kwargs):
            if "/distribution/int/" in url:
                raise SrmError("SRM HTTP 500")
            return PAYLOAD

        return fake_fetch

    def test_all_non_strict_isolates_failure_exit_0(self) -> None:
        env = dict(_CREDS)
        env.pop("STRICT", None)
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=self._fetch_that_fails_int(),
            ):
                with TemporaryDirectory() as tmp:
                    rc = main(
                        [
                            "collect",
                            "--env",
                            "ALL",
                            "--as-of",
                            "2026-09-18T08:00:00Z",
                            "--out-dir",
                            tmp,
                            "--no-analyze",
                            "--no-strict",
                        ]
                    )
                    self.assertEqual(rc, 0)
                    root = Path(tmp)
                    # Failing env still produced artifacts.
                    int_srm = json.loads(
                        (root / "int" / "srm.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(int_srm["verdict"], "SRM_ERROR")
                    # Other envs succeeded.
                    for env_name in ("test", "qa", "demo", "prod"):
                        other = json.loads(
                            (root / env_name / "srm.json").read_text(encoding="utf-8")
                        )
                        self.assertEqual(other["verdict"], "STALE")

    def test_all_strict_exits_nonzero_when_one_env_errors(self) -> None:
        with patch.dict(os.environ, _CREDS, clear=False):
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=self._fetch_that_fails_int(),
            ):
                with TemporaryDirectory() as tmp:
                    rc = main(
                        [
                            "collect",
                            "--env",
                            "ALL",
                            "--as-of",
                            "2026-09-18T08:00:00Z",
                            "--out-dir",
                            tmp,
                            "--no-analyze",
                            "--strict",
                        ]
                    )
                    self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
