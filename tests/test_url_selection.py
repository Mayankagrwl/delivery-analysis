from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.collect import run_collect_environments
from src.delivery_analysis.config import (
    SRM_ENVIRONMENTS,
    load_settings,
    srm_url_for_env,
)
from src.delivery_analysis.grafana import loki_environment_value
from src.delivery_analysis.report import render_summary_md
from src.delivery_analysis.verdict import evaluate_payload

UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)

STALE_PAYLOAD = [
    {
        "state": "SUBMITTED",
        "_urn": "strn:distribution:DeliveryRequest:43",
        "_updated": {"on": "8/27/2026 9:30:43 AM"},
    }
]


class _UrlRecorder:
    """Records the SRM URL each env is fetched with; returns per-URL payloads."""

    def __init__(self, per_url=None, default=None):
        self.urls: list[str] = []
        self.per_url = per_url or {}
        self.default = default if default is not None else []

    def __call__(self, url, user, password, **kwargs):
        self.urls.append(url)
        return self.per_url.get(url, self.default)


class AllRunIgnoresOverrideTests(unittest.TestCase):
    def test_all_run_uses_per_env_urls_even_with_srm_base_url(self):
        recorder = _UrlRecorder(default=[])
        env = {
            "SRM_BASIC_USER": "u",
            "SRM_BASIC_PASSWORD": "pw",
            "SRM_BASE_URL": "https://override.example/all-collapsed",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings(env="ALL")
            self.assertEqual(
                settings.srm_base_url_override, "https://override.example/all-collapsed"
            )
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=recorder,
            ):
                with TemporaryDirectory() as tmp:
                    run_collect_environments(
                        env="ALL",
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=settings,
                        analyze=False,
                    )
        # Override ignored; each env hit its own computed URL.
        self.assertNotIn("https://override.example/all-collapsed", recorder.urls)
        expected = {srm_url_for_env(e) for e in SRM_ENVIRONMENTS}
        self.assertEqual(set(recorder.urls), expected)
        self.assertEqual(len(set(recorder.urls)), 5)
        # prod uses the root path, no /distribution/ segment.
        prod_url = srm_url_for_env("prod")
        self.assertIn(prod_url, recorder.urls)
        self.assertNotIn("/distribution/", prod_url)

    def test_single_env_url_override_still_applies(self):
        recorder = _UrlRecorder(default=[])
        with patch.dict(
            os.environ, {"SRM_BASIC_USER": "u", "SRM_BASIC_PASSWORD": "pw"}, clear=False
        ):
            settings = load_settings(env="qa", url="https://explicit.example/x")
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=recorder,
            ):
                with TemporaryDirectory() as tmp:
                    run_collect_environments(
                        env="qa",
                        as_of=AS_OF,
                        out_dir=tmp,
                        url="https://explicit.example/x",
                        settings=settings,
                        analyze=False,
                    )
        self.assertEqual(recorder.urls, ["https://explicit.example/x"])


class PerEnvDataDiffersTests(unittest.TestCase):
    def test_different_payloads_per_url_give_different_verdicts(self):
        # Only qa returns a stale record; the rest are empty.
        per_url = {srm_url_for_env("qa"): STALE_PAYLOAD}
        recorder = _UrlRecorder(per_url=per_url, default=[])
        with patch.dict(
            os.environ, {"SRM_BASIC_USER": "u", "SRM_BASIC_PASSWORD": "pw"}, clear=False
        ):
            settings = load_settings(env="ALL")
            with patch(
                "src.delivery_analysis.collect.fetch_delivery_requests",
                side_effect=recorder,
            ):
                with TemporaryDirectory() as tmp:
                    multi = run_collect_environments(
                        env="ALL",
                        as_of=AS_OF,
                        out_dir=tmp,
                        settings=settings,
                        analyze=False,
                    )
                    verdicts = {e: multi.results[e].verdict for e in SRM_ENVIRONMENTS}
                    # per-env summary records the resolved URL
                    qa_summary = (Path(tmp) / "qa" / "summary.md").read_text(
                        encoding="utf-8"
                    )
        self.assertEqual(verdicts["qa"], "STALE")
        for other in ("test", "int", "demo", "prod"):
            self.assertEqual(verdicts[other], "NO_RECORDS")
        self.assertIn(f"SRM URL: {srm_url_for_env('qa')}", qa_summary)


class EnvLabelValueOverrideTests(unittest.TestCase):
    def test_default_mapping(self):
        self.assertEqual(loki_environment_value("prod"), "production")
        self.assertEqual(loki_environment_value("qa"), "qa")

    def test_config_override_respected(self):
        with patch.dict(
            os.environ,
            {"GRAFANA_ENV_LABEL_VALUES": "prod=prod-cluster,qa=qa-eu"},
            clear=False,
        ):
            settings = load_settings()
        self.assertEqual(settings.grafana_env_label_values["prod"], "prod-cluster")
        self.assertEqual(
            loki_environment_value("prod", settings.grafana_env_label_values),
            "prod-cluster",
        )
        self.assertEqual(
            loki_environment_value("qa", settings.grafana_env_label_values), "qa-eu"
        )


class SummaryShowsUrlAndEnvLabelTests(unittest.TestCase):
    def test_window_shows_url(self):
        srm = evaluate_payload(
            STALE_PAYLOAD,
            as_of=AS_OF,
            environment="qa",
            url=srm_url_for_env("qa"),
        )
        summary = render_summary_md(srm)
        self.assertIn(f"- SRM URL: {srm_url_for_env('qa')}", summary)


if __name__ == "__main__":
    unittest.main()
