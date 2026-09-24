from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.delivery_analysis.analyze import analyze_staleness
from src.delivery_analysis.cli import main
from src.delivery_analysis.collect import run_collect
from src.delivery_analysis.config import (
    STGPT_CLIENT_APP_NAME,
    STGPT_SERVICE,
    load_settings,
    resolve_stgpt_api_key,
)
from src.delivery_analysis.grafana import GrafanaResult
from src.delivery_analysis.prompt import build_evidence
from src.delivery_analysis.report import write_artifacts
from src.delivery_analysis.stgpt_client import (
    ChatResult,
    StgptError,
    extract_completion,
    flatten_user_content,
    generate_auth_token,
    post_chat,
)
from src.delivery_analysis.verdict import evaluate_payload
from tests.test_s1 import SCREENSHOT_AS_OF, SCREENSHOT_PAYLOAD
from tests.test_s2 import FakeMcp, _default_responses


def _ok_payload(quote: str) -> dict:
    return {
        "root_cause": "DeliveryRequest 43 has been SUBMITTED since 8/27/2026 9:30:43 AM",
        "suggested_fix": "Replay or unlock the stuck GRANT path after checking Loki errors",
        "confidence": "medium",
        "citations": [
            {"quote": quote, "source": "srm_submitted", "line": None},
        ],
        "cannot_determine": False,
    }


class AuthContractTests(unittest.TestCase):
    def test_sha1_token_matches_prd(self) -> None:
        token = generate_auth_token(
            "gtrd_srmtdpplm", "chat", "secret-key", "1710000000", "abc"
        )
        raw = "gtrd_srmtdpplm_chat_secret-key_1710000000_abc"
        self.assertEqual(token, hashlib.sha1(raw.encode("utf-8")).hexdigest())

    def test_key_resolution_order(self) -> None:
        env = {"STGPT_API": "from-stgpt", "API_KEY": "from-api-key"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(resolve_stgpt_api_key(), "from-stgpt")
        with patch.dict(os.environ, {"API_KEY": "from-api-key", "STGPT_API": ""}, clear=False):
            os.environ.pop("STGPT_API", None)
            env2 = os.environ.copy()
            env2.pop("STGPT_API", None)
            env2["API_KEY"] = "from-api-key"
            with patch.dict(os.environ, env2, clear=True):
                self.assertEqual(resolve_stgpt_api_key(), "from-api-key")
        self.assertEqual(resolve_stgpt_api_key("explicit"), "explicit")

    def test_flatten_prefixes_assistant(self) -> None:
        text = flatten_user_content(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "prior"},
            ]
        )
        self.assertIn("Previous assistant reply:\nprior", text)

    def test_extract_completion_order(self) -> None:
        self.assertEqual(extract_completion({"completion": "a"}), "a")
        self.assertEqual(
            extract_completion({"root_cause": "x", "suggested_fix": "y"})
            and "root_cause" in extract_completion({"root_cause": "x", "suggested_fix": "y"}),
            True,
        )


class StgptClientHttpTests(unittest.TestCase):
    def test_post_chat_headers_and_body(self) -> None:
        captured: dict = {}

        def handler(request):
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return __import__("httpx").Response(
                200,
                json={
                    "completion": json.dumps(_ok_payload("quote")),
                    "responseId": "rid-1",
                },
            )

        transport = __import__("httpx").MockTransport(handler)
        result = post_chat(
            "https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps",
            "super-secret-key",
            STGPT_CLIENT_APP_NAME,
            "trinity_for_api",
            [{"role": "user", "content": "<EVIDENCE>\nhi\n</EVIDENCE>"}],
            transport=transport,
            timestamp="1710000000",
            nonce="deadbeef",
            verify=False,
        )
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        expected = generate_auth_token(
            STGPT_CLIENT_APP_NAME, STGPT_SERVICE, "super-secret-key", "1710000000", "deadbeef"
        )
        self.assertEqual(headers["stchatgpt-auth-token"], expected)
        self.assertEqual(headers["stchatgpt-auth-nonce"], "deadbeef")
        self.assertEqual(headers["stchatgpt-auth-timestamp"], "1710000000")
        body = captured["body"]
        self.assertEqual(body["version"], "1.0")
        self.assertEqual(body["clientAppName"], STGPT_CLIENT_APP_NAME)
        self.assertEqual(body["service"], "chat")
        self.assertEqual(body["persona"], "trinity_for_api")
        self.assertEqual(body["responseFormat"], "json_object")
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertNotIn("super-secret-key", json.dumps(body))
        self.assertEqual(result.response_id, "rid-1")

    def test_empty_prompt_raises(self) -> None:
        with self.assertRaises(StgptError) as ctx:
            post_chat(
                "https://example.invalid/chatgpt",
                "k",
                STGPT_CLIENT_APP_NAME,
                "trinity_for_api",
                [{"role": "user", "content": "   "}],
            )
        self.assertEqual(str(ctx.exception), "prompt_empty")


class AnalyzeLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        self.evidence, _, _ = build_evidence(self.srm, None)
        self.quote = "strn:distribution:DeliveryRequest:43"
        self.assertIn(self.quote, self.evidence)

    def _chat(self, completions: list[str | Exception | ChatResult]):
        queue = list(completions)

        def _fn(persona: str, messages):
            _fn.personas.append(persona)  # type: ignore[attr-defined]
            _fn.messages.append(messages)  # type: ignore[attr-defined]
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, ChatResult):
                return item
            return ChatResult(200, {"completion": item, "responseId": "x"}, item, "x")

        _fn.personas = []  # type: ignore[attr-defined]
        _fn.messages = []  # type: ignore[attr-defined]
        return _fn

    def test_ok_on_trinity(self) -> None:
        payload = json.dumps(_ok_payload(self.quote))
        fn = self._chat([payload])
        record = analyze_staleness(self.srm, None, chat_fn=fn, cache_dir=None)
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.persona, "trinity_for_api")
        self.assertFalse(record.fallback_used)
        self.assertEqual(record.result.citations[0].source, "srm_submitted")
        self.assertFalse(record.cannot_determine if False else record.result.cannot_determine)

    def test_alias_keys(self) -> None:
        payload = json.dumps(
            {
                "rootCause": "stuck GRANT",
                "suggestedFix": "replay",
                "confidence": "high",
                "citations": [{"quote": self.quote, "source": "srm_record"}],
            }
        )
        record = analyze_staleness(
            self.srm, None, chat_fn=self._chat([payload]), cache_dir=None
        )
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.result.root_cause, "stuck GRANT")

    def test_repair_then_ok(self) -> None:
        bad = "not json"
        good = json.dumps(_ok_payload(self.quote))
        fn = self._chat([bad, good])
        record = analyze_staleness(self.srm, None, chat_fn=fn, cache_dir=None)
        self.assertEqual(record.status, "ok")
        self.assertEqual(len(fn.messages), 2)
        self.assertEqual(fn.personas, ["trinity_for_api", "trinity_for_api"])

    def test_alfred_after_trinity_fails(self) -> None:
        bad = "not json"
        good = json.dumps(_ok_payload(self.quote))
        fn = self._chat([bad, bad, good])
        record = analyze_staleness(self.srm, None, chat_fn=fn, cache_dir=None)
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.persona, "alfred_for_api")
        self.assertTrue(record.fallback_used)
        self.assertIn("alfred_for_api", fn.personas)

    def test_citation_must_be_verbatim(self) -> None:
        payload = json.dumps(_ok_payload("this quote is not in evidence at all"))
        fn = self._chat([payload, payload, payload, payload])
        record = analyze_staleness(self.srm, None, chat_fn=fn, cache_dir=None)
        self.assertEqual(record.status, "citation_invalid")

    def test_new_citation_sources_accepted(self) -> None:
        payload = json.dumps(
            {
                "root_cause": "loki shows timeout",
                "suggested_fix": "inspect grant worker",
                "confidence": "low",
                "citations": [{"quote": self.quote, "source": "loki_logs"}],
                "cannot_determine": False,
            }
        )
        record = analyze_staleness(
            self.srm, None, chat_fn=self._chat([payload]), cache_dir=None
        )
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.result.citations[0].source, "loki_logs")

    def test_bridge_error(self) -> None:
        fn = self._chat(
            [
                StgptError("connection failed"),
                StgptError("connection failed"),
            ]
        )
        record = analyze_staleness(self.srm, None, chat_fn=fn, cache_dir=None)
        self.assertEqual(record.status, "bridge_error")

    def test_missing_key_is_gated(self) -> None:
        env = os.environ.copy()
        env.pop("STGPT_API", None)
        env.pop("API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            record = analyze_staleness(self.srm, None, cache_dir=None)
        self.assertEqual(record.status, "gated")

    def test_fresh_is_gated(self) -> None:
        fresh = evaluate_payload(
            SCREENSHOT_PAYLOAD,
            as_of=SCREENSHOT_AS_OF.replace(year=2026, month=8, day=27, hour=10),
        )
        record = analyze_staleness(fresh, None, chat_fn=self._chat([]), cache_dir=None)
        self.assertEqual(record.status, "gated")
        self.assertIn("not STALE", record.notes[0])

    def test_garbage_stgpt_with_loki_and_stale_urns_yields_result(self) -> None:
        grafana = GrafanaResult(
            skipped=False,
            highlights=[
                "gateway timeout waiting for grant strn:distribution:DeliveryRequest:43",
                "gateway 502 from grant-service DeliveryRequest:38",
                "gateway retry exhausted for DeliveryRequest:40",
            ],
            lines_kept=3,
        )
        garbage = "%%% not json at all %%% STGPT unusable output"
        fn = self._chat([garbage, garbage, garbage, garbage])
        record = analyze_staleness(self.srm, grafana, chat_fn=fn, cache_dir=None)
        self.assertIsNotNone(record.result)
        self.assertEqual(len(fn.personas), 4)
        self.assertTrue(record.result.cannot_determine)
        sources = {cite.source for cite in record.result.citations}
        self.assertTrue(sources <= {"srm_record", "loki_logs"})
        self.assertTrue(any(cite.source == "srm_record" for cite in record.result.citations))
        self.assertIn("strn:distribution:DeliveryRequest:43", record.result.root_cause)
        with TemporaryDirectory() as tmp:
            write_artifacts(self.srm, Path(tmp), grafana=grafana, analysis=record)
            analysis = json.loads((Path(tmp) / "analysis.json").read_text(encoding="utf-8"))
            summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
            self.assertIn("raw_completion", analysis)
            self.assertIn("%%% not json", analysis["raw_completion"])
            self.assertEqual(analysis["persona"], "alfred_for_api")
            self.assertTrue(analysis["notes"])
            self.assertIsNotNone(analysis["result"])
            self.assertIn("### AI raw", summary)
            self.assertIn("parse notes", summary)
            self.assertIn("%%% not json", summary)


class CollectAnalyzeTests(unittest.TestCase):
    def test_collect_analyze_writes_analysis_json(self) -> None:
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        evidence, _, _ = build_evidence(srm, None)
        quote = "strn:distribution:DeliveryRequest:43"
        payload = json.dumps(_ok_payload(quote))

        def chat_fn(persona, messages):
            return ChatResult(200, {"completion": payload, "id": "rid-9"}, payload, "rid-9")

        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "srm-secret",
            "STGPT_API": "stgpt-secret-key",
            "GRAFANA_MCP_URL": "",
        }
        fake = FakeMcp(responses=_default_responses())
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
                        analyze=True,
                        chat_fn=chat_fn,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    self.assertEqual(analysis["status"], "ok")
                    self.assertEqual(analysis["response_id"], "rid-9")
                    self.assertNotIn("raw_completion", analysis)
                    self.assertIn("## AI analysis", summary)
                    self.assertIn("**Root cause:**", summary)
                    self.assertNotIn("stgpt-secret-key", summary)
                    self.assertNotIn("stgpt-secret-key", json.dumps(analysis))
                    self.assertNotIn("srm-secret", json.dumps(analysis))

    def test_bridge_failure_exits_0(self) -> None:
        def chat_fn(persona, messages):
            raise StgptError("down")

        env = {
            "SRM_BASIC_USER": "user",
            "SRM_BASIC_PASSWORD": "secret",
            "STGPT_API": "stgpt-secret-key",
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
                        analyze=True,
                        chat_fn=chat_fn,
                    )
                    self.assertEqual(result.verdict, "STALE")
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(analysis["status"], "bridge_error")
                    self.assertTrue((Path(tmp) / "summary.md").exists())
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
                                "--analyze",
                            ]
                        )
                    self.assertEqual(rc, 0)

    def test_no_key_in_logs_from_auth_helper(self) -> None:
        secret = "super-secret-key-do-not-log"
        token = generate_auth_token(STGPT_CLIENT_APP_NAME, "chat", secret, "1", "n")
        self.assertNotEqual(token, secret)
        self.assertNotIn(secret, token)

    def test_cli_analyze_mocks_post_chat(self) -> None:
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)
        quote = "strn:distribution:DeliveryRequest:43"
        payload = json.dumps(_ok_payload(quote))

        def fake_post_chat(*args, **kwargs):
            return ChatResult(200, {"completion": payload, "id": "rid-cli"}, payload, "rid-cli")

        env = os.environ.copy()
        env["STGPT_API"] = "stgpt-secret-key"
        env.pop("API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("src.delivery_analysis.analyze.post_chat", side_effect=fake_post_chat):
                with TemporaryDirectory() as tmp:
                    write_artifacts(srm, Path(tmp))
                    rc = main(["analyze", "--out-dir", tmp])
                    self.assertEqual(rc, 0)
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    self.assertEqual(analysis["status"], "ok")
                    self.assertEqual(analysis["response_id"], "rid-cli")
                    self.assertIn("## AI analysis", summary)
                    self.assertNotIn("stgpt-secret-key", summary)
                    self.assertNotIn("stgpt-secret-key", json.dumps(analysis))

    def test_cli_analyze_bridge_error_writes_summary_exit_0(self) -> None:
        srm = evaluate_payload(SCREENSHOT_PAYLOAD, as_of=SCREENSHOT_AS_OF)

        def fake_post_chat(*args, **kwargs):
            raise StgptError("bridge down")

        env = os.environ.copy()
        env["STGPT_API"] = "stgpt-secret-key"
        env.pop("API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("src.delivery_analysis.analyze.post_chat", side_effect=fake_post_chat):
                with TemporaryDirectory() as tmp:
                    write_artifacts(srm, Path(tmp))
                    rc = main(["analyze", "--out-dir", tmp])
                    self.assertEqual(rc, 0)
                    analysis = json.loads(
                        (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(analysis["status"], "bridge_error")
                    summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                    self.assertIn("## AI analysis", summary)
                    self.assertIn("bridge_error", summary)
                    self.assertNotIn("stgpt-secret-key", summary)


if __name__ == "__main__":
    unittest.main()

