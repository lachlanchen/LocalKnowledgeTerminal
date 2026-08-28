from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from lkt.llm import LlamaCppClient, WORD_ORIGIN_PROMPT, _extract_json
from lkt.models import Evidence


class LlmParsingTests(unittest.TestCase):
    def test_origin_prompt_defines_compound_siblings(self) -> None:
        self.assertIn("component-a parent earlier-compound", WORD_ORIGIN_PROMPT)
        self.assertIn("Components are siblings", WORD_ORIGIN_PROMPT)
        self.assertIn("ruby token text must concatenate exactly", WORD_ORIGIN_PROMPT)

    def test_extracts_fenced_json_after_thinking(self) -> None:
        result = _extract_json(
            '<think>private reasoning</think>\n```json\n{"title":"語源","key_points":[]}\n```'
        )
        self.assertEqual(result["title"], "語源")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(ValueError):
            _extract_json("not structured")

    def test_raw_chat_strips_thinking_and_reports_runtime_metrics(self) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": "<think>hidden</think>Visible answer"}}
                    ],
                    "usage": {"prompt_tokens": 21, "completion_tokens": 8},
                    "timings": {"predicted_per_second": 3.25},
                }
            ).encode()
        )
        client = LlamaCppClient("http://localhost/v1/chat/completions", "test")
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = client.chat(
                [{"role": "user", "content": "Hello"}], "Title: Current card"
            )
        self.assertEqual(result["message"], "Visible answer")
        self.assertFalse(result["grounded"])
        self.assertTrue(result["contextual"])
        self.assertEqual(result["metrics"]["prompt_tokens"], 21)
        self.assertEqual(result["metrics"]["completion_tokens"], 8)
        self.assertEqual(result["metrics"]["tokens_per_second"], 3.25)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertIn("CURRENT CARD", sent["messages"][0]["content"])

    def test_card_generation_constrains_json_and_repairs_once(self) -> None:
        client = LlamaCppClient("http://localhost/v1/chat/completions", "test")
        invalid = {"choices": [{"message": {"content": "not json"}}]}
        valid = {"choices": [{"message": {"content": '{"title":"Abacus"}'}}]}
        evidence = [Evidence("entry-1", "abacus", "Greek", "", (1,), "source")]
        with patch.object(
            client,
            "_request",
            side_effect=[(invalid, 1.0), (valid, 1.0)],
        ) as request:
            result = client.generate("abacus", "word", evidence)
        self.assertEqual(result["title"], "Abacus")
        self.assertEqual(request.call_count, 2)
        first_payload = request.call_args_list[0].args[0]
        repair_payload = request.call_args_list[1].args[0]
        self.assertEqual(first_payload["response_format"], {"type": "json_object"})
        self.assertEqual(repair_payload["temperature"], 0.0)
        self.assertIn("Repair the previous response", repair_payload["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
