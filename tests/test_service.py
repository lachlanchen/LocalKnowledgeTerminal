from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.models import Evidence
from lkt.service import (
    CardService,
    _morphology_graph,
    _origin_graph,
    _ruby_tokens_for_term,
)
from lkt.store import CardStore

from test_card_books import make_card_book, record
from test_corpus import make_index


class FakeModel:
    model_name = "test-qwen"

    def generate(self, query: str, mode: str, evidence: list[Evidence]) -> dict[str, Any]:
        return {
            "title": query.title(),
            "subtitle": "A counting journey",
            "summary_en": "A calculating frame.",
            "origin_story": evidence[0].excerpt,
            "key_points": ["Greek to Latin", "Used for counting"],
            "english": {"term": query, "pronunciation": "/ˈæbəkəs/", "meaning": "counting frame"},
            "japanese": {"term": "算盤", "reading": "そろばん", "meaning": "計算の道具"},
            "chinese": {"simplified": "算盘", "traditional": "算盤", "pinyin": "suànpán", "meaning": "计算工具"},
            "memory_hook": "Count beads across languages.",
            "related_terms": [{"term": "calculate", "note": "a related action"}],
            "origin_graph": [
                {"stage": "Greek", "form": "abax", "meaning": "counting board", "basis": "book"},
                {"stage": "Latin", "form": "abacus", "meaning": "calculation board", "basis": "model"},
                {"stage": "English", "form": "abacus", "meaning": "bead frame", "basis": "book"},
            ],
            "french": {"term": "boulier", "pronunciation": "/bu.lje/", "meaning": "cadre de calcul"},
            "arabic": {"term": "مِعْداد", "reading": "miʿdād", "meaning": "أداة حساب"},
            "pages": [999],
        }


class ServiceTests(unittest.TestCase):
    def test_morphology_stages_survive_a_later_language_failure(self) -> None:
        class StagedModel:
            model_name = "test-staged-qwen"

            def __init__(self) -> None:
                self.graph_calls = 0
                self.language_calls = 0

            def generate_morphology_graph(
                self, query: str, _mode: str, _evidence: list[Evidence]
            ) -> dict[str, Any]:
                self.graph_calls += 1
                nodes = [
                    {
                        "id": "spect",
                        "type": "root",
                        "form": query,
                        "meaning": "look",
                        "basis": "book",
                        "evidence_ids": ["root-spect"],
                    },
                    *(
                        {
                            "id": word,
                            "type": "word",
                            "form": word,
                            "meaning": meaning,
                            "basis": "model",
                            "evidence_ids": [],
                        }
                        for word, meaning in (
                            ("inspect", "look into"),
                            ("respect", "look back"),
                            ("prospect", "look forward"),
                            ("spectator", "one who watches"),
                        )
                    ),
                ]
                return {
                    "value": {
                        "title": query,
                        "summary_en": "look or see",
                        "morphology_graph": {
                            "center_id": "spect",
                            "nodes": nodes,
                            "edges": [
                                {
                                    "source": "spect",
                                    "target": word,
                                    "relationship": "root-of",
                                }
                                for word in (
                                    "inspect", "respect", "prospect", "spectator"
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
                    },
                    "attempts": 1,
                }

            def generate_morphology_languages(
                self,
                query: str,
                _mode: str,
                _evidence: list[Evidence],
                _graph: dict[str, Any],
            ) -> dict[str, Any]:
                self.language_calls += 1
                if self.language_calls == 1:
                    raise RuntimeError("temporary language-stage failure")
                return {
                    "value": {
                        "english": {"term": query, "meaning": "look or see"},
                        "japanese": {
                            "term": "見る",
                            "reading": "みる",
                            "meaning": "見る",
                        },
                        "chinese": {
                            "simplified": "看",
                            "pinyin": "kàn",
                            "meaning": "看",
                        },
                        "french": {"term": "voir", "meaning": "regarder"},
                        "arabic": {"term": "نظر", "meaning": "رؤية"},
                    },
                    "attempts": 1,
                }

            def generate(
                self, _query: str, _mode: str, _evidence: list[Evidence]
            ) -> dict[str, Any]:
                raise AssertionError("staged morphology must not use monolithic generation")

        with tempfile.TemporaryDirectory() as temp:
            store = CardStore(Path(temp) / "cards.sqlite3")
            model = StagedModel()
            service = CardService(make_index(Path(temp)), model, store)
            evidence = [
                Evidence(
                    "root-spect",
                    "SPECT",
                    "Root Dictionary",
                    "S",
                    (58,),
                    "Latin spect: look or see.",
                    corpus_id="test-root-dictionary",
                    kind="morphology-root",
                )
            ]

            with self.assertRaisesRegex(RuntimeError, "language-stage"):
                service.create_from_evidence("SPECT", "root", evidence)
            card = service.create_from_evidence("SPECT", "root", evidence)

            self.assertEqual(model.graph_calls, 1)
            self.assertEqual(model.language_calls, 2)
            artifacts = store.preparation_artifacts(
                card.extensions["preparation_run_id"]
            )
            graph_stage = next(
                item for item in artifacts
                if item["stage"] == "model-morphology-graph"
            )
            self.assertIn("reused_from_artifact_id", graph_stage["payload"])
            self.assertEqual(card.extensions["morphology_graph"]["center_id"], "spect")

    def test_morphology_graph_keeps_cited_nodes_and_downgrades_fake_book_ids(self) -> None:
        evidence = [
            Evidence(
                "root-spect",
                "SPECT",
                "Root dictionary",
                "",
                (58,),
                "spect means look",
            )
        ]
        graph = _morphology_graph(
            {
                "center_id": "inspect",
                "nodes": [
                    {"id": "inspect", "type": "word", "form": "inspect", "meaning": "examine", "basis": "model"},
                    {"id": "spect", "type": "root", "form": "spect", "meaning": "look", "basis": "book", "evidence_ids": ["root-spect"]},
                    {"id": "in", "type": "prefix", "form": "in-", "meaning": "into", "basis": "book", "evidence_ids": ["invented-record"]},
                ],
                "edges": [
                    {"source": "spect", "target": "inspect", "relationship": "root-of"},
                    {"source": "in", "target": "inspect", "relationship": "prefix-of"},
                ],
                "focus_areas": [
                    {"id": "spect-focus", "label": "SPECT", "kind": "root", "node_ids": ["spect", "inspect"], "headline": "Look within", "explanation": "The root carries seeing."}
                ],
            },
            evidence,
            "inspect",
        )
        by_id = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(by_id["spect"]["basis"], "book")
        self.assertEqual(by_id["in"]["basis"], "model")
        self.assertEqual(graph["focus_areas"][0]["kind"], "overview")
        self.assertEqual(
            set(graph["focus_areas"][0]["node_ids"]), {"inspect", "spect", "in"}
        )

    def test_generated_ruby_must_cover_the_exact_term(self) -> None:
        tokens = [
            {"t": "\u4e00\u6642", "r": "\u3044\u3061\u3058"},
            {"t": "\u7684", "r": "\u3066\u304d"},
        ]
        self.assertEqual(tokens, _ruby_tokens_for_term(tokens, "\u4e00\u6642\u7684"))
        self.assertEqual([], _ruby_tokens_for_term(tokens, "\u77ed\u6682"))

    def test_origin_graph_preserves_branching_parent_links(self) -> None:
        evidence = [
            Evidence("entry-1", "sycophant", "Greek", "", (1,), "Greek roots")
        ]
        graph = _origin_graph(
            [
                {"id": "modern", "parent": "", "stage": "English", "form": "sycophant", "basis": "book"},
                {"id": "fig", "parent": "modern", "stage": "Greek", "form": "sŷkon", "basis": "book"},
                {"id": "show", "parent": "modern", "stage": "Greek", "form": "phaínein", "basis": "model"},
            ],
            evidence,
            "sycophant",
        )
        self.assertEqual([node["parent"] for node in graph], ["", "modern", "modern"])

    def test_origin_graph_repairs_a_disconnected_cycle(self) -> None:
        evidence = [Evidence("entry-1", "word", "", "", (1,), "source")]
        graph = _origin_graph(
            [
                {"id": "modern", "parent": "", "form": "word"},
                {"id": "part-a", "parent": "part-b", "form": "a"},
                {"id": "part-b", "parent": "part-a", "form": "b"},
            ],
            evidence,
            "word",
        )
        by_id = {node["id"]: node for node in graph}
        for node in graph[1:]:
            visited = set()
            current = node
            while current["parent"]:
                self.assertNotIn(current["id"], visited)
                visited.add(current["id"])
                current = by_id[current["parent"]]
            self.assertEqual(current["id"], "modern")

    def test_builds_multilingual_grounded_card_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CardStore(root / "cards.sqlite3")
            service = CardService(make_index(root), FakeModel(), store)
            card = service.create("abacus", "word")
            self.assertEqual(card.japanese["reading"], "そろばん")
            self.assertEqual(card.chinese["pinyin"], "suàn pán")
            self.assertEqual(card.chinese["ruby_tokens"][0], {"t": "算", "r": "suàn"})
            self.assertEqual(card.evidence[0].pages, (12,))
            self.assertEqual(card.origin_graph[0]["form"], "abax")
            self.assertEqual(card.origin_graph[1]["basis"], "model")
            self.assertTrue(card.grounded)
            self.assertEqual(store.recent()[0]["card_id"], card.card_id)
            self.assertEqual(store.get(card.card_id)["title"], "Abacus")

    def test_word_card_stores_rotating_language_equivalents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CardStore(root / "cards.sqlite3")
            service = CardService(make_index(root), FakeModel(), store)
            card = service.create("abacus", "knowledge")
            self.assertEqual(card.origin_graph, [])
            self.assertEqual(
                card.extensions["knowledge_policy"],
                "book-anchored-model-enriched",
            )
            self.assertEqual(card.extra_languages["french"]["term"], "boulier")
            self.assertEqual(card.extra_languages["arabic"]["term"], "مِعْداد")
            self.assertEqual(
                store.get(card.card_id)["extra_languages"]["arabic"]["reading"],
                "miʿdād",
            )

    def test_book_answer_keeps_reviewed_text_and_token_level_furigana(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            answer_book = make_card_book(
                root,
                "answer",
                [
                    record(
                        "answer-001",
                        "answer",
                        1,
                        "Don't worry",
                        "心配しないで",
                        "不必担心",
                        page=7,
                    )
                ],
            )
            service = CardService(
                make_index(root),
                FakeModel(),
                CardStore(root / "cards.sqlite3"),
                card_books={"answer": answer_book},
            )
            card = service.create("Will this work?", "answer")
            self.assertEqual(card.english["term"], "Don't worry")
            self.assertEqual(card.japanese["term"], "心配しないで")
            self.assertEqual(card.chinese["simplified"], "不必担心")
            self.assertEqual(card.japanese["ruby_tokens"][0]["r"], "しんぱい")
            self.assertEqual(card.extensions["corpus_id"], "test-answer-book")
            self.assertEqual(card.evidence[0].pages, (7,))


if __name__ == "__main__":
    unittest.main()
