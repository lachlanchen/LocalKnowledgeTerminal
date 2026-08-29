from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lkt.deck import (
    AutonomousDeckSeeder,
    AutonomousLexicalSeeder,
    AutonomousMorphologySeeder,
    AutonomousSeedCoordinator,
    BalancedProductSeeder,
    DeckSeedResult,
)
from lkt.knowledge import KnowledgeStore
from lkt.models import Evidence
from lkt.service import CardService
from lkt.store import CardStore

from test_card_books import make_card_book, record
from test_corpus import make_index
from test_morphology import make_morphology_index


class _LocalModel:
    model_name = "local-qwen-test"

    def generate(
        self, query: str, mode: str, evidence: list[Evidence]
    ) -> dict[str, Any]:
        return {
            "title": f"Local {mode.title()}",
            "origin_story": f"A restrained local reflection on {evidence[0].headword}.",
        }


class _MorphologyModel:
    model_name = "local-qwen-test"

    def generate(
        self, query: str, mode: str, evidence: list[Evidence]
    ) -> dict[str, Any]:
        primary_id = evidence[0].entry_id
        center = query.casefold()
        return {
            "title": query,
            "summary_en": "A book-grounded morphology lesson.",
            "english": {"term": query, "pronunciation": "test", "meaning": "look"},
            "japanese": {
                "term": "見る",
                "reading": "みる",
                "meaning": "見ること",
                "ruby_tokens": [{"t": "見", "r": "み"}, {"t": "る", "r": ""}],
            },
            "chinese": {
                "simplified": "看",
                "traditional": "看",
                "pinyin": "kàn",
                "meaning": "观看",
            },
            "morphology_graph": {
                "center_id": center,
                "nodes": [
                    {"id": center, "type": "word", "form": query, "meaning": "look", "basis": "book", "evidence_ids": [primary_id]},
                    {"id": f"{center}-root", "type": "root", "form": "spect", "meaning": "look", "basis": "book", "evidence_ids": [primary_id]},
                    {"id": f"{center}-latin", "type": "historical", "form": "specere", "meaning": "to look", "basis": "model", "evidence_ids": []},
                    {"id": f"{center}-related", "type": "related", "form": "inspect", "meaning": "look into", "basis": "model", "evidence_ids": []},
                    {"id": f"{center}-prefix", "type": "prefix", "form": "in-", "meaning": "into", "basis": "model", "evidence_ids": []},
                ],
                "edges": [
                    {"source": f"{center}-latin", "target": f"{center}-root", "relationship": "developed-into"},
                    {"source": f"{center}-root", "target": center, "relationship": "root-of"},
                    {"source": f"{center}-root", "target": f"{center}-related", "relationship": "root-of"},
                    {"source": f"{center}-prefix", "target": f"{center}-related", "relationship": "prefix-of"},
                ],
                "focus_areas": [
                    {"id": "overview", "label": "Overview", "kind": "overview", "node_ids": [center, f"{center}-root", f"{center}-latin", f"{center}-related", f"{center}-prefix"], "headline": query, "explanation": "A concise graph."}
                ],
            },
        }


class AutonomousDeckTests(unittest.TestCase):
    def test_lexical_seeder_does_not_treat_exhausted_jobs_as_new_work(self) -> None:
        class _Knowledge:
            def jobs_for_subject(self, _subject_key: str) -> list[dict[str, str]]:
                return [
                    {"job_id": "failed-origin", "status": "failed"},
                    {"job_id": "complete-card", "status": "complete"},
                ]

        seeder = object.__new__(AutonomousLexicalSeeder)
        seeder.knowledge = _Knowledge()
        exhausted = SimpleNamespace(
            subject_key="term:failed",
            jobs={
                "expand-origin-branches": "failed-origin",
                "compose-origin-card": "complete-card",
            },
        )
        self.assertFalse(seeder._plan_has_pending_work(exhausted))

        queued = SimpleNamespace(
            subject_key="term:queued",
            jobs={"expand-origin-branches": "new-origin"},
        )
        seeder.knowledge.jobs_for_subject = lambda _key: [
            {"job_id": "new-origin", "status": "queued"}
        ]
        self.assertTrue(seeder._plan_has_pending_work(queued))

    def test_morphology_seeder_grows_each_polished_book_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roots = make_morphology_index(root, "root")
            affixes = make_morphology_index(root, "affix")
            cards = CardStore(root / "cards.sqlite3")
            service = CardService(
                make_index(root),
                _MorphologyModel(),
                cards,
                morphology={"root": roots, "affix": affixes},
            )
            seeder = AutonomousMorphologySeeder(service, cards)

            first = seeder.run_mode("root", "stable-cycle")
            second = seeder.run_mode("root", "stable-cycle")
            affix = seeder.run_mode("affix", "stable-cycle")

            self.assertEqual((first.status, second.status, affix.status), ("prepared", "prepared", "prepared"))
            self.assertNotEqual(first.source_entry_id, second.source_entry_id)
            self.assertEqual(len(cards.accepted_for_modes(("root",))), 2)
            self.assertEqual(len(cards.accepted_for_modes(("affix",))), 1)
            first_card = cards.get(first.card_id)
            self.assertIsNotNone(first_card)
            run_id = first_card["extensions"]["preparation_run_id"]
            self.assertEqual(
                [item["stage"] for item in cards.preparation_artifacts(run_id)],
                [
                    "retrieved-evidence",
                    "cleaned-model-draft",
                    "normalized-card",
                    "published-card",
                ],
            )

    def test_balances_modes_and_never_repeats_a_source_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            answers = make_card_book(
                root,
                "answer",
                [
                    record("answer-001", "answer", 1, "Begin", "始める", "开始"),
                    record("answer-002", "answer", 2, "Wait", "待つ", "等待"),
                ],
            )
            questions = make_card_book(
                root,
                "question",
                [
                    record(
                        "question-001",
                        "question",
                        1,
                        "What matters?",
                        "何が大切ですか？",
                        "什么最重要？",
                    )
                ],
            )
            cards = CardStore(root / "cards.sqlite3")
            knowledge = KnowledgeStore(root / "knowledge.sqlite3")
            service = CardService(
                make_index(root),
                _LocalModel(),
                cards,
                card_books={"answer": answers, "question": questions},
            )
            seeder = AutonomousDeckSeeder(service, cards, knowledge)

            results = [seeder.run_once(f"seed-{index}") for index in range(4)]

            self.assertEqual(
                [result.mode for result in results[:3]],
                ["answer", "question", "answer"],
            )
            self.assertEqual(results[3].status, "complete")
            source_ids = [result.source_entry_id for result in results[:3]]
            self.assertEqual(len(source_ids), len(set(source_ids)))
            self.assertEqual(len(cards.accepted_for_modes(("answer", "question"))), 3)
            self.assertEqual(knowledge.status()["counts"]["content_items"], 9)
            self.assertEqual(knowledge.status()["queued_jobs"], 12)
            self.assertEqual(
                seeder.progress(),
                {
                    "ready": True,
                    "accepted": 3,
                    "total": 3,
                    "remaining": 0,
                    "complete": True,
                    "modes": {
                        "answer": {
                            "accepted": 2,
                            "total": 2,
                            "remaining": 0,
                            "complete": True,
                        },
                        "question": {
                            "accepted": 1,
                            "total": 1,
                            "remaining": 0,
                            "complete": True,
                        },
                    },
                },
            )

    def test_existing_accepted_source_is_skipped_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            answers = make_card_book(
                root,
                "answer",
                [
                    record("answer-001", "answer", 1, "Begin", "始める", "开始"),
                    record("answer-002", "answer", 2, "Wait", "待つ", "等待"),
                ],
            )
            cards = CardStore(root / "cards.sqlite3")
            knowledge = KnowledgeStore(root / "knowledge.sqlite3")
            service = CardService(
                make_index(root),
                _LocalModel(),
                cards,
                card_books={"answer": answers},
            )

            first = AutonomousDeckSeeder(
                service, cards, knowledge, modes=("answer",)
            ).run_once("same-cycle")
            second = AutonomousDeckSeeder(
                service, cards, knowledge, modes=("answer",)
            ).run_once("same-cycle")

            self.assertNotEqual(first.source_entry_id, second.source_entry_id)

    def test_lexical_seeder_queues_each_corpus_word_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = make_index(root)
            cards = CardStore(root / "cards.sqlite3")
            knowledge = KnowledgeStore(root / "knowledge.sqlite3")
            seeder = AutonomousLexicalSeeder(
                corpus,
                cards,
                knowledge,
                model="local-qwen-test",
            )

            first = seeder.run_once("lexical-cycle")
            second = seeder.run_once("lexical-cycle")
            complete = seeder.run_once("lexical-cycle")

            self.assertEqual(first.status, "queued")
            self.assertEqual(second.status, "queued")
            self.assertNotEqual(first.source_entry_id, second.source_entry_id)
            self.assertEqual(complete.status, "complete")
            self.assertGreater(knowledge.status()["queued_jobs"], 20)
            self.assertEqual(
                seeder.progress(),
                {
                    "ready": True,
                    "planned": 2,
                    "accepted": 0,
                    "total": 2,
                    "remaining": 0,
                    "complete": True,
                    "modes": ["knowledge", "word", "root", "affix"],
                },
            )

    def test_seed_coordinator_alternates_independent_sources(self) -> None:
        class _Seeder:
            def __init__(self, mode: str):
                self.mode = mode

            def run_once(self) -> DeckSeedResult:
                return DeckSeedResult(status="queued", mode=self.mode)

        coordinator = AutonomousSeedCoordinator(
            (_Seeder("lexical"), _Seeder("answer"))
        )
        self.assertEqual(coordinator.run_once().mode, "lexical")
        self.assertEqual(coordinator.run_once().mode, "answer")
        self.assertEqual(coordinator.run_once().mode, "lexical")

    def test_product_seeder_catches_up_lexical_modes_before_growing_books(self) -> None:
        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [
                    *({"mode": "question"} for _ in range(28)),
                    *({"mode": "answer"} for _ in range(31)),
                    *({"mode": "knowledge"} for _ in range(3)),
                    *({"mode": "word"} for _ in range(2)),
                    *({"mode": "root"} for _ in range(2)),
                    *({"mode": "affix"} for _ in range(2)),
                ]

        class _Book:
            modes = ("question", "answer")

            def run_mode(self, mode: str) -> DeckSeedResult:
                self.fail_mode = mode
                return DeckSeedResult(status="prepared", mode=mode)

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                return DeckSeedResult(status="queued", mode="lexical")

        book = _Book()
        result = BalancedProductSeeder(book, _Lexical(), _Store()).run_once()

        self.assertEqual(result.mode, "lexical")
        self.assertFalse(hasattr(book, "fail_mode"))

    def test_product_seeder_starts_each_balanced_round_with_question(self) -> None:
        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [
                    {"mode": mode}
                    for mode in BalancedProductSeeder.MODES
                    for _ in range(4)
                ]

        class _Book:
            modes = ("question", "answer")

            def run_mode(self, mode: str) -> DeckSeedResult:
                return DeckSeedResult(status="prepared", mode=mode)

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                raise AssertionError("question must be the first balanced mode")

        result = BalancedProductSeeder(_Book(), _Lexical(), _Store()).run_once()
        self.assertEqual(result.mode, "question")

    def test_product_seeder_routes_root_and_affix_to_their_own_books(self) -> None:
        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [
                    {"mode": mode}
                    for mode in BalancedProductSeeder.MODES
                    if mode != "root"
                ]

        class _Book:
            modes = ("question", "answer")

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                raise AssertionError("root must not be routed through a word plan")

        class _Morphology:
            def run_mode(self, mode: str) -> DeckSeedResult:
                return DeckSeedResult(status="prepared", mode=mode)

        result = BalancedProductSeeder(
            _Book(), _Lexical(), _Store(), morphology=_Morphology()
        ).run_once()
        self.assertEqual(result.mode, "root")

    def test_product_seeder_does_not_let_repairs_starve_morphology(self) -> None:
        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [
                    *({"mode": "question"} for _ in range(8)),
                    *({"mode": "answer"} for _ in range(8)),
                    *({"mode": "knowledge"} for _ in range(2)),
                    *({"mode": "word"} for _ in range(2)),
                    *({"mode": "root"} for _ in range(2)),
                    *({"mode": "affix"} for _ in range(2)),
                ]

        class _Book:
            modes = ("question", "answer")

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                return DeckSeedResult(status="repair-queued", mode="lexical")

        class _Morphology:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_mode(self, mode: str) -> DeckSeedResult:
                self.calls.append(mode)
                return DeckSeedResult(status="prepared", mode=mode)

        morphology = _Morphology()
        result = BalancedProductSeeder(
            _Book(), _Lexical(), _Store(), morphology=morphology
        ).run_once()

        self.assertEqual(result.mode, "root")
        self.assertEqual(morphology.calls, ["root"])


if __name__ == "__main__":
    unittest.main()
