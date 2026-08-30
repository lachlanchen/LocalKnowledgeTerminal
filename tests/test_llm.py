from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from lkt.llm import (
    InvalidModelOutput,
    LlamaCppClient,
    MORPHOLOGY_PROMPT,
    WORD_ORIGIN_PROMPT,
    _extract_json,
    _validate_card_draft,
    _validate_morphology_language_draft,
)
from lkt.models import Evidence


class LlmParsingTests(unittest.TestCase):
    def test_morphology_languages_reject_non_arabic_model_leakage(self) -> None:
        draft = {
            "english": {"term": "SPECT", "meaning": "look"},
            "japanese": {"term": "見る", "reading": "みる", "meaning": "見る"},
            "chinese": {"simplified": "看", "pinyin": "kàn", "meaning": "看"},
            "french": {"term": "voir", "meaning": "regarder"},
            "arabic": {"term": "morg", "meaning": "root meaning"},
        }
        with self.assertRaisesRegex(ValueError, "arabic.valid_script"):
            _validate_morphology_language_draft(draft)

    def test_morphology_prompt_and_validator_require_connected_focus_graph(self) -> None:
        self.assertIn("evidence_ids", MORPHOLOGY_PROMPT)
        self.assertIn("Components point into words", MORPHOLOGY_PROMPT)
        draft = {
            "title": "inspection",
            "summary_en": "careful examination",
            "english": {"term": "inspection", "meaning": "careful examination"},
            "japanese": {"term": "検査", "reading": "けんさ"},
            "chinese": {"simplified": "检查", "pinyin": "jiǎn chá"},
            "morphology_graph": {
                "center_id": "word",
                "nodes": [
                    {"id": "word", "type": "word", "form": "inspection", "meaning": "examination", "basis": "book"},
                    {"id": "in", "type": "prefix", "form": "in-", "meaning": "into", "basis": "book"},
                    {"id": "spect", "type": "root", "form": "spect", "meaning": "look", "basis": "book"},
                    {"id": "ion", "type": "suffix", "form": "-ion", "meaning": "action", "basis": "book"},
                    {"id": "latin", "type": "historical", "form": "specere", "meaning": "to look", "basis": "model"},
                    {"id": "inspect", "type": "related", "form": "inspect", "meaning": "examine", "basis": "model"},
                    {"id": "spectator", "type": "related", "form": "spectator", "meaning": "observer", "basis": "model"},
                ],
                "edges": [
                    {"source": "in", "target": "word"},
                    {"source": "spect", "target": "word"},
                    {"source": "ion", "target": "word"},
                    {"source": "latin", "target": "spect"},
                    {"source": "spect", "target": "inspect"},
                    {"source": "spect", "target": "spectator"},
                ],
                "focus_areas": [
                    {"kind": "overview", "node_ids": ["word", "in", "spect", "ion", "latin", "inspect", "spectator"]},
                    {"kind": "root", "node_ids": ["spect", "latin"]},
                ],
            },
        }
        _validate_card_draft(draft, "root")
        draft["morphology_graph"]["edges"].pop()
        with self.assertRaisesRegex(ValueError, "connected_edges"):
            _validate_card_draft(draft, "root")

    def test_origin_prompt_defines_compound_siblings(self) -> None:
        self.assertIn("Sibling components never parent one another", WORD_ORIGIN_PROMPT)
        self.assertIn("Root Dictionary, and Affix Dictionary", WORD_ORIGIN_PROMPT)
        self.assertIn("focus_areas", WORD_ORIGIN_PROMPT)
        self.assertIn("ruby token text must concatenate exactly", WORD_ORIGIN_PROMPT)

    def test_extracts_fenced_json_after_thinking(self) -> None:
        result = _extract_json(
            '<think>private reasoning</think>\n```json\n{"title":"語源","key_points":[]}\n```'
        )
        self.assertEqual(result["title"], "語源")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(ValueError):
            _extract_json("not structured")

    def test_terminal_invalid_stage_retains_two_bounded_attempts(self) -> None:
        client = LlamaCppClient("http://localhost/v1/chat/completions", "test")
        body = {
            "choices": [{"message": {"content": "x" * 5_000}}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 1200},
            "timings": {"predicted_per_second": 2.75},
        }
        payload = {
            "messages": [{"role": "user", "content": "evidence"}],
            "max_tokens": 1200,
        }
        with patch.object(client, "_request", side_effect=[(body, 2.0), (body, 3.0)]):
            with self.assertRaises(InvalidModelOutput) as caught:
                client._complete_card_stage(
                    payload,
                    lambda _value: None,
                    repair_tokens=1600,
                    repair_instruction="Try again.",
                )

        failures = caught.exception.failures
        self.assertEqual([item["attempt"] for item in failures], [1, 2])
        self.assertTrue(all(len(item["raw"]) == 4_000 for item in failures))
        self.assertEqual(failures[0]["metrics"]["completion_tokens"], 1200)
        self.assertEqual(failures[1]["metrics"]["elapsed_seconds"], 3.0)

    def test_rejects_a_blank_structured_card(self) -> None:
        with self.assertRaisesRegex(ValueError, "summary_en"):
            _validate_card_draft(
                {"title": "sycophant", "summary_en": "", "origin_graph": []},
                "word",
            )

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

    def test_card_generation_repairs_invalid_json_once(self) -> None:
        client = LlamaCppClient("http://localhost/v1/chat/completions", "test")
        invalid = {"choices": [{"message": {"content": "not json"}}]}
        valid_content = {
            "title": "Abacus",
            "summary_en": "A counting frame.",
            "origin_graph": [
                {"id": "modern", "parent": "", "stage": "English", "form": "abacus", "meaning": "counting frame", "basis": "book"},
                {"id": "latin", "parent": "modern", "stage": "Latin", "form": "abacus", "meaning": "counting board", "basis": "book"},
                {"id": "greek", "parent": "latin", "stage": "Greek", "form": "abax", "meaning": "board", "basis": "book"},
            ],
            "morphology_graph": {
                "center_id": "modern",
                "nodes": [
                    {"id": "modern", "type": "word", "form": "abacus", "meaning": "counting frame", "basis": "book"},
                    {"id": "latin", "type": "historical", "form": "abacus", "meaning": "counting board", "basis": "book"},
                    {"id": "greek", "type": "historical", "form": "abax", "meaning": "board", "basis": "book"},
                    {"id": "plural", "type": "related", "form": "abaci", "meaning": "plural form", "basis": "model"},
                    {"id": "calculate", "type": "related", "form": "calculate", "meaning": "find a number", "basis": "model"},
                    {"id": "abaci", "type": "related", "form": "abaci", "meaning": "plural form", "basis": "model"},
                    {"id": "count", "type": "related", "form": "count", "meaning": "enumerate", "basis": "model"},
                ],
                "edges": [
                    {"source": "greek", "target": "latin", "relationship": "developed-into"},
                    {"source": "latin", "target": "modern", "relationship": "developed-into"},
                    {"source": "modern", "target": "plural", "relationship": "related-form"},
                    {"source": "modern", "target": "calculate", "relationship": "related-form"},
                    {"source": "modern", "target": "abaci", "relationship": "related-form"},
                    {"source": "modern", "target": "count", "relationship": "related-form"},
                ],
                "focus_areas": [
                    {"id": "overview", "kind": "overview", "node_ids": ["modern", "latin", "greek", "plural", "calculate", "abaci", "count"]},
                    {"id": "history", "kind": "history", "node_ids": ["modern", "latin", "greek"]},
                ],
            },
            "english": {"term": "abacus", "pronunciation": "", "meaning": "counting frame"},
            "japanese": {"term": "soroban", "reading": "soroban", "meaning": "counting tool", "ruby_tokens": [{"t": "soroban", "r": ""}]},
            "chinese": {"simplified": "suanpan", "traditional": "", "pinyin": "suan pan", "meaning": "counting tool"},
        }
        valid = {
            "choices": [
                {"message": {"content": json.dumps(valid_content)}}
            ]
        }
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
        self.assertEqual(first_payload["max_tokens"], 1200)
        self.assertNotIn("response_format", first_payload)
        self.assertEqual(repair_payload["temperature"], 0.0)
        self.assertEqual(repair_payload["max_tokens"], 1600)
        self.assertFalse(
            any(message["role"] == "assistant" for message in repair_payload["messages"])
        )
        self.assertIn("Start again from the supplied evidence", repair_payload["messages"][-1]["content"])

    def test_morphology_is_divided_and_repairs_without_recursive_raw_json(self) -> None:
        client = LlamaCppClient("http://localhost/v1/chat/completions", "test")
        nodes = [
            {
                "id": "spect",
                "type": "root",
                "form": "SPECT",
                "meaning": "look",
                "basis": "book",
                "evidence_ids": ["root-spect"],
            },
            *(
                {
                    "id": word,
                    "type": "word",
                    "form": word,
                    "meaning": "related word",
                    "basis": "model",
                    "evidence_ids": [],
                }
                for word in (
                    "inspect", "respect", "prospect", "spectator", "retrospect", "introspection"
                )
            ),
        ]
        graph = {
            "title": "SPECT",
            "summary_en": "look or see",
            "morphology_graph": {
                "center_id": "spect",
                "nodes": nodes,
                "edges": [
                    {"source": "spect", "target": word, "relationship": "root-of"}
                    for word in (
                        "inspect", "respect", "prospect", "spectator", "retrospect", "introspection"
                    )
                ],
                "focus_areas": [
                    {
                        "id": "overview",
                        "kind": "overview",
                        "node_ids": [item["id"] for item in nodes],
                    },
                    {
                        "id": "root",
                        "kind": "root",
                        "node_ids": ["spect", "inspect"],
                    },
                ],
            },
        }
        languages = {
            "english": {"term": "SPECT", "meaning": "look or see"},
            "japanese": {"term": "見る", "reading": "みる", "meaning": "見る"},
            "chinese": {"simplified": "看", "pinyin": "kàn", "meaning": "看"},
            "french": {"term": "voir", "meaning": "regarder"},
            "arabic": {"term": "نظر", "meaning": "رؤية"},
        }
        invalid = {"choices": [{"message": {"content": '{"title":"SPECT"'}}]}
        valid_graph = {
            "choices": [{"message": {"content": json.dumps(graph)}}]
        }
        valid_languages = {
            "choices": [{"message": {"content": json.dumps(languages)}}]
        }
        evidence = [
            Evidence(
                "root-spect",
                "SPECT",
                "S",
                "Latin",
                (58,),
                "Latin spect means look or see.",
            )
        ]

        with patch.object(
            client,
            "_request",
            side_effect=[
                (invalid, 1.0),
                (valid_graph, 2.0),
                (valid_languages, 1.0),
            ],
        ) as request:
            result = client.generate("SPECT", "root", evidence)

        self.assertEqual(result["title"], "SPECT")
        self.assertEqual(result["japanese"]["reading"], "みる")
        self.assertEqual(request.call_count, 3)
        first_graph = request.call_args_list[0].args[0]
        repaired_graph = request.call_args_list[1].args[0]
        language_call = request.call_args_list[2].args[0]
        self.assertEqual(first_graph["max_tokens"], 1200)
        self.assertEqual(repaired_graph["max_tokens"], 1400)
        self.assertFalse(
            any(
                message["role"] == "assistant"
                for message in repaired_graph["messages"]
            )
        )
        self.assertEqual(language_call["max_tokens"], 512)
        self.assertIn(
            "Arabic-script letters only",
            language_call["messages"][0]["content"],
        )
        self.assertEqual(
            set(result["_preparation_stages"]),
            {"model-morphology-graph", "model-morphology-languages"},
        )


if __name__ == "__main__":
    unittest.main()
