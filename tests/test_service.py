from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.llm import InvalidModelOutput
from lkt.models import Evidence
from lkt.service import (
    CardService,
    _morphology_graph,
    _origin_graph,
    _ruby_tokens_for_term,
    _usable_morphology_graph,
    _usable_morphology_languages,
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
                {"stage": "Greek", "form": "abax", "meaning": "counting board", "basis": "book", "evidence_ids": [evidence[0].entry_id]},
                {"stage": "Latin", "form": "abacus", "meaning": "calculation board", "basis": "model"},
                {"stage": "English", "form": "abacus", "meaning": "bead frame", "basis": "book", "evidence_ids": [evidence[0].entry_id]},
            ],
            "french": {"term": "boulier", "pronunciation": "/bu.lje/", "meaning": "cadre de calcul"},
            "arabic": {"term": "مِعْداد", "reading": "miʿdād", "meaning": "أداة حساب"},
            "pages": [999],
        }


class ServiceTests(unittest.TestCase):
    def test_service_caps_model_evidence_and_preserves_primary_record(self) -> None:
        class CapturingModel(FakeModel):
            def __init__(self) -> None:
                self.seen: list[Evidence] = []

            def generate(
                self, query: str, mode: str, evidence: list[Evidence]
            ) -> dict[str, Any]:
                self.seen = list(evidence)
                return super().generate(query, mode, evidence)

        with tempfile.TemporaryDirectory() as temp:
            model = CapturingModel()
            service = CardService(
                make_index(Path(temp)),
                model,
                CardStore(Path(temp) / "cards.sqlite3"),
                max_evidence=4,
            )
            evidence = [
                Evidence(
                    entry_id=f"answer-{index}",
                    headword=f"Answer {index}",
                    section="Answers",
                    date_label="",
                    pages=(index,),
                    excerpt=f"Reviewed answer {index}",
                    corpus_id="test-answers",
                )
                for index in range(7)
            ]
            card = service.create_from_evidence("Answer 0", "answer", evidence)

        self.assertEqual(len(model.seen), 4)
        self.assertEqual(model.seen[0].entry_id, "answer-0")
        self.assertEqual(len(card.evidence), 4)

    def test_staged_morphology_rejects_stale_invalid_arabic(self) -> None:
        self.assertFalse(
            _usable_morphology_languages(
                {
                    "english": {"term": "MORG", "meaning": "death"},
                    "japanese": {"term": "死", "reading": "し", "meaning": "死"},
                    "chinese": {
                        "simplified": "死",
                        "pinyin": "sǐ",
                        "meaning": "死亡",
                    },
                    "french": {"term": "mort", "meaning": "décès"},
                    "arabic": {"term": "morg", "meaning": "death"},
                }
            )
        )

    def test_reusable_morphology_graph_requires_complete_retrieval_provenance(self) -> None:
        evidence = [Evidence("root-spect", "SPECT", "Root", "", (1,), "look")]
        nodes = [
            {"id": "spect", "type": "root", "form": "SPECT", "meaning": "look", "basis": "book", "evidence_ids": ["root-spect"]},
            *(
                {"id": word, "type": "word", "form": word, "meaning": "related", "basis": "model", "evidence_ids": []}
                for word in ("inspect", "respect", "prospect", "spectator", "retrospect", "introspection")
            ),
        ]
        draft = {
            "title": "SPECT",
            "summary_en": "look or see",
            "morphology_graph": {
                "center_id": "spect",
                "nodes": nodes,
                "edges": [
                    {"source": "spect", "target": node["id"], "relationship": "root-of"}
                    for node in nodes[1:]
                ],
                "focus_areas": [
                    {"kind": "overview", "node_ids": [node["id"] for node in nodes]},
                    {"kind": "root", "node_ids": ["spect", "inspect"]},
                ],
            },
        }
        self.assertTrue(_usable_morphology_graph(draft, evidence))
        nodes[0]["evidence_ids"] = ["invented-record"]
        self.assertFalse(_usable_morphology_graph(draft, evidence))

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
                            ("retrospect", "look back"),
                            ("introspection", "look within"),
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
                                    "inspect", "respect", "prospect", "spectator",
                                    "retrospect", "introspection"
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

    def test_rejected_morphology_stages_are_run_linked_and_never_reusable(self) -> None:
        class RejectedStageModel:
            model_name = "local-qwen-test"

            def __init__(self, failed_stage: str) -> None:
                self.failed_stage = failed_stage

            def _failure(self) -> InvalidModelOutput:
                return InvalidModelOutput(
                    "model stage was invalid after one fresh repair attempt",
                    model=self.model_name,
                    failures=[
                        {
                            "attempt": 1,
                            "error": "invalid first response",
                            "raw": "first" * 1_000,
                            "metrics": {"completion_tokens": 1200},
                        },
                        {
                            "attempt": 2,
                            "error": "invalid repaired response",
                            "raw": "second" * 1_000,
                            "metrics": {"elapsed_seconds": 12.5},
                        },
                    ],
                )

            def generate_morphology_graph(
                self, query: str, _mode: str, _evidence: list[Evidence]
            ) -> dict[str, Any]:
                if self.failed_stage == "graph":
                    raise self._failure()
                return {
                    "value": {
                        "title": query,
                        "summary_en": "look or see",
                        "morphology_graph": {},
                    },
                    "attempts": 1,
                }

            def generate_morphology_languages(
                self,
                _query: str,
                _mode: str,
                _evidence: list[Evidence],
                _graph: dict[str, Any],
            ) -> dict[str, Any]:
                raise self._failure()

            def generate(
                self, _query: str, _mode: str, _evidence: list[Evidence]
            ) -> dict[str, Any]:
                raise AssertionError("staged morphology must not use monolithic generation")

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
        for failed_stage in ("graph", "languages"):
            with self.subTest(stage=failed_stage), tempfile.TemporaryDirectory() as temp:
                store = CardStore(Path(temp) / "cards.sqlite3")
                run_ids: list[str] = []
                start_preparation = store.start_preparation

                def capture_run(*args: Any, **kwargs: Any) -> str:
                    run_id = start_preparation(*args, **kwargs)
                    run_ids.append(run_id)
                    return run_id

                store.start_preparation = capture_run  # type: ignore[method-assign]
                service = CardService(
                    make_index(Path(temp)),
                    RejectedStageModel(failed_stage),
                    store,
                )

                with self.assertRaises(InvalidModelOutput):
                    service.create_from_evidence("SPECT", "root", evidence)

                artifacts = store.preparation_artifacts(run_ids[0])
                rejected = next(
                    item for item in artifacts
                    if item["stage"]
                    == f"rejected-model-morphology-{failed_stage}"
                )
                self.assertFalse(rejected["reusable"])
                self.assertEqual(rejected["payload"]["attempts"], 2)
                self.assertEqual(
                    rejected["payload"]["failures"][0]["metrics"][
                        "completion_tokens"
                    ],
                    1200,
                )
                self.assertLessEqual(
                    len(rejected["payload"]["failures"][0]["raw"]), 4_000
                )
                self.assertIsNone(
                    store.reusable_preparation_artifact(
                        "root",
                        "SPECT",
                        "local-qwen-test",
                        rejected["stage"],
                        "",
                    )
                )

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

    def test_legacy_origin_book_basis_requires_retrieved_evidence_id(self) -> None:
        evidence = [Evidence("entry-1", "word", "", "", (1,), "source")]
        graph = _origin_graph(
            [
                {"id": "valid", "parent": "", "form": "word", "basis": "book", "evidence_ids": ["entry-1"]},
                {"id": "invalid", "parent": "valid", "form": "older", "basis": "book", "evidence_ids": ["invented"]},
            ],
            evidence,
            "word",
        )
        self.assertEqual(graph[0]["basis"], "book")
        self.assertEqual(graph[0]["evidence_ids"], ["entry-1"])
        self.assertEqual(graph[1]["basis"], "model")

    def test_normalization_failure_finishes_the_preparation_run(self) -> None:
        class BrokenDraft(dict[str, Any]):
            def get(self, _key: str, _default: Any = None) -> Any:
                raise RuntimeError("normalization failed")

        class BrokenModel(FakeModel):
            def generate(
                self, _query: str, _mode: str, _evidence: list[Evidence]
            ) -> dict[str, Any]:
                return BrokenDraft()

        class TrackingStore(CardStore):
            def __init__(self, path: Path):
                super().__init__(path)
                self.finished: list[str] = []

            def finish_preparation(
                self, preparation_run_id: str, status: str, **kwargs: Any
            ) -> None:
                self.finished.append(status)
                super().finish_preparation(preparation_run_id, status, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TrackingStore(root / "cards.sqlite3")
            service = CardService(make_index(root), BrokenModel(), store)
            with self.assertRaisesRegex(RuntimeError, "normalization failed"):
                service.create("abacus", "word")
            self.assertEqual(store.finished, ["failed"])

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
            self.assertEqual(card.chinese["pinyin"].replace(" ", ""), "suànpán")
            self.assertIn(
                card.chinese["ruby_tokens"],
                [
                    [{"t": "算", "r": "suàn"}, {"t": "盘", "r": "pán"}],
                    [{"t": "算盘", "r": "suànpán"}],
                ],
            )
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
