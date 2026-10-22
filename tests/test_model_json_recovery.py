from __future__ import annotations

import unittest
from unittest import mock

from lkt.llm import LlamaCppClient
from lkt.model_json import parse_model_json


class ModelJsonRecoveryTests(unittest.TestCase):
    def test_valid_nested_object_is_unchanged(self):
        value, method = parse_model_json('prefix {"parts":[{"form":"fixture"}]} suffix')
        self.assertEqual(value, {"parts": [{"form": "fixture"}]})
        self.assertEqual(method, "none")

    def test_truncated_outer_container_does_not_return_inner_object(self):
        value, method = parse_model_json('{"parts":[{"form":"fixture"}')
        self.assertEqual(value, {"parts": [{"form": "fixture"}]})
        self.assertEqual(method, "container-repair")

    def test_trailing_comma_does_not_modify_string_content(self):
        value, _ = parse_model_json('{"meaning":"literal ,} text", "evidence_ids":["source",],}')
        self.assertEqual(value, {"meaning": "literal ,} text", "evidence_ids": ["source"]})

    def test_single_quotes_recover_without_generated_content(self):
        value, method = parse_model_json("```python\n{'meaning': 'a fixture', 'evidence_ids': ['source']}\n```")
        self.assertEqual(value["evidence_ids"], ["source"])
        self.assertEqual(method, "literal-repair")

    def test_unfinished_value_and_plain_prose_require_model_retry(self):
        for value in ('{"meaning":"unfinished', '{"meaning":', 'It means a fixture.'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_model_json(value)

    def test_reasoning_is_not_mistaken_for_final_output(self):
        with self.assertRaises(ValueError):
            parse_model_json('<think>{"meaning":"not a final answer"}')
        self.assertEqual(parse_model_json('<think>{"wrong":1}</think>{"meaning":"final"}')[0], {"meaning": "final"})

    def test_executable_expression_is_never_evaluated(self):
        with self.assertRaises(ValueError):
            parse_model_json('{"meaning": __import__("os").getcwd()}')

    def client(self, replies):
        client = LlamaCppClient("http://local-only.invalid/v1/chat/completions", "local-qwen")
        client._runtime_context_tokens = mock.Mock(return_value=3072)
        client._request = mock.Mock(side_effect=[({
            "choices": [{"message": {"content": reply}}],
            "usage": {"completion_tokens": 10, "prompt_tokens": 20},
        }, 2.0) for reply in replies])
        return client

    def test_recoverable_syntax_costs_no_extra_model_request(self):
        client = self.client(['{"meaning":"fixture", "evidence_ids":["source"]'])
        result = client.complete_json("Return JSON.", "Evidence ID source: fixture", max_tokens=160)
        self.assertEqual(result["value"]["evidence_ids"], ["source"])
        self.assertEqual(result["metrics"]["requests"], 1)
        self.assertEqual(result["metrics"]["json_recovery"], "container-repair")
        payload = client._request.call_args.args[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])

    def test_one_local_retry_keeps_original_evidence_and_counts_both_requests(self):
        client = self.client(['Not JSON.', '{"meaning":"fixture","evidence_ids":["source"]}'])
        result = client.complete_json("Return JSON.", "Evidence ID source: fixture", max_tokens=160)
        self.assertTrue(result["metrics"]["formatting_retry"])
        self.assertEqual(result["metrics"]["requests"], 2)
        self.assertEqual(result["metrics"]["elapsed_seconds"], 4.0)
        self.assertEqual(result["metrics"]["completion_tokens"], 20)
        payload = client._request.call_args.args[0]
        self.assertIn("Evidence ID source: fixture", payload["messages"][1]["content"])
        self.assertIn("Not JSON.", payload["messages"][1]["content"])
        self.assertEqual(payload["max_tokens"], 256)

    def test_retry_is_bounded(self):
        client = self.client(['Not JSON.', 'Still not JSON.'])
        with self.assertRaises(ValueError):
            client.complete_json("Return JSON.", "Fixture task")
        self.assertEqual(client._request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
