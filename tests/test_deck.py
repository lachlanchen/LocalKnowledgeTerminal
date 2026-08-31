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

    def test_morphology_seeder_queues_each_book_through_atomic_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roots = make_morphology_index(root, "root")
            affixes = make_morphology_index(root, "affix")
            cards = CardStore(root / "cards.sqlite3")
            root_knowledge = KnowledgeStore(root / "root-knowledge.sqlite3")
            root_seeder = AutonomousMorphologySeeder(
                {"root": roots, "affix": affixes},
                cards,
                root_knowledge,
                model="local-qwen-test",
                modes=("root",),
            )
            affix_knowledge = KnowledgeStore(root / "affix-knowledge.sqlite3")
            affix_seeder = AutonomousMorphologySeeder(
                {"root": roots, "affix": affixes},
                cards,
                affix_knowledge,
                model="local-qwen-test",
                modes=("affix",),
            )

            root_result = root_seeder.run_mode("root", "stable-cycle")
            busy_result = root_seeder.run_mode("root", "stable-cycle")
            affix_result = affix_seeder.run_mode("affix", "stable-cycle")

            self.assertEqual(root_result.status, "queued")
            self.assertEqual(busy_result.status, "busy")
            self.assertEqual(affix_result.status, "queued")
            self.assertTrue(root_result.source_entry_id)
            self.assertTrue(affix_result.source_entry_id)
            self.assertEqual(root_result.card_id, "")
            self.assertEqual(affix_result.card_id, "")
            self.assertGreater(root_knowledge.status()["queued_jobs"], 0)
            self.assertGreater(affix_knowledge.status()["queued_jobs"], 0)
            self.assertEqual(cards.accepted_for_modes(("root", "affix")), [])

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
            complete = seeder.run_once("lexical-cycle")

            self.assertEqual(first.status, "queued")
            self.assertEqual(len(first.discoveries), 2)
            self.assertEqual(
                {item["source_kind"] for item in first.discoveries}, {"word-origins"}
            )
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

    def test_lexical_batch_prefers_two_investigations_then_fills_origins(self) -> None:
        class BatchCorpus:
            def __init__(self) -> None:
                self.words = ("alpha", "beta", "gamma", "delta")
                self.exclusions: list[set[str]] = []

            def lexical_headwords(self) -> tuple[str, ...]:
                return self.words

            def metadata(self) -> dict[str, str]:
                return {"source_sha256": "batch-corpus"}

            def draw_unseen_word(
                self, _seed: str, excluded: set[str]
            ) -> SimpleNamespace | None:
                self.exclusions.append(set(excluded))
                word = next((item for item in self.words if item not in excluded), None)
                if word is None:
                    return None
                return SimpleNamespace(headword=word, entry_id=f"origin-{word}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge = KnowledgeStore(root / "knowledge.sqlite3")
            cards = CardStore(root / "cards.sqlite3")
            source_id = knowledge.upsert_content_item(
                "answer",
                "en",
                "Consider patience and perspective.",
                source_key="answer-batch",
                status="accepted",
            )
            accepted_terms = []
            for ordinal, term in enumerate(("patience", "perspective", "consider")):
                term_id = knowledge.upsert_term("en", term, status="accepted")
                knowledge.add_edge(
                    source_id,
                    term_id,
                    "contains-investigation-term",
                    basis="model",
                    properties={"ordinal": ordinal},
                )
                accepted_terms.append(
                    {"term_id": term_id, "term": term, "ordinal": ordinal}
                )
            extraction = knowledge.enqueue_job(
                "extract-investigation-terms",
                f"content:{source_id}",
                subject_entity_id=source_id,
                language="en",
            )
            knowledge.save_job_artifact(
                extraction,
                "accepted-investigation-terms",
                {"terms": accepted_terms},
                language="en",
                validation_state="accepted",
            )
            corpus = BatchCorpus()
            result = AutonomousLexicalSeeder(
                corpus, cards, knowledge, model="local-qwen-test"
            ).run_once("five-word-round")

            self.assertEqual(result.status, "queued")
            self.assertEqual(len(result.discoveries), 5)
            self.assertEqual(
                [item["term"] for item in result.discoveries[:2]],
                ["patience", "perspective"],
            )
            self.assertEqual(
                [item["source_kind"] for item in result.discoveries],
                ["qa-investigation", "qa-investigation"]
                + ["word-origins"] * 3,
            )
            self.assertTrue(
                all({"patience", "perspective"}.issubset(item) for item in corpus.exclusions)
            )
            self.assertEqual(
                [item["term"] for item in knowledge.investigation_suggestion_groups(
                    knowledge.discovered_or_planned_term_keys("en")
                )[0]["terms"]],
                ["consider"],
            )
            self.assertEqual(
                AutonomousLexicalSeeder(
                    corpus, cards, knowledge, model="local-qwen-test"
                ).run_bounded_once("next-round").status,
                "busy",
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

    def test_product_seeder_rotates_after_a_deferred_equal_mode(self) -> None:
        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [
                    *({"mode": "question"} for _ in range(8)),
                    *({"mode": "answer"} for _ in range(8)),
                    *({"mode": "knowledge"} for _ in range(8)),
                    *({"mode": "word"} for _ in range(8)),
                    *({"mode": "root"} for _ in range(2)),
                    *({"mode": "affix"} for _ in range(2)),
                ]

        class _Book:
            modes = ("question", "answer")

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                raise AssertionError("lexical modes are not least-filled")

        class _Morphology:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_mode(self, mode: str) -> DeckSeedResult:
                self.calls.append(mode)
                return DeckSeedResult(status="deferred", mode=mode)

        morphology = _Morphology()
        seeder = BalancedProductSeeder(
            _Book(), _Lexical(), _Store(), morphology=morphology
        )
        self.assertEqual(seeder.run_once().mode, "root")
        self.assertEqual(seeder.run_once().mode, "affix")
        self.assertEqual(morphology.calls, ["root", "affix"])

    def test_deferred_unique_low_root_yields_one_round_to_affix(self) -> None:
        class _Store:
            def accepted_for_modes(self, _modes: tuple[str, ...]) -> list[dict[str, str]]:
                return [
                    *({"mode": "question"} for _ in range(8)),
                    *({"mode": "answer"} for _ in range(8)),
                    *({"mode": "knowledge"} for _ in range(8)),
                    *({"mode": "word"} for _ in range(8)),
                    {"mode": "root"},
                    *({"mode": "affix"} for _ in range(3)),
                ]

        class _Book:
            modes = ("question", "answer")

        class _Lexical:
            def run_bounded_once(self) -> DeckSeedResult:
                raise AssertionError("lexical modes are not least-filled")

        class _Morphology:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_mode(self, mode: str) -> DeckSeedResult:
                self.calls.append(mode)
                return DeckSeedResult(status="deferred", mode=mode)

        morphology = _Morphology()
        seeder = BalancedProductSeeder(
            _Book(), _Lexical(), _Store(), morphology=morphology
        )
        self.assertEqual(seeder.run_once().mode, "root")
        self.assertEqual(seeder.run_once().mode, "affix")
        self.assertEqual(morphology.calls, ["root", "affix"])

    def test_terminal_origin_gap_requeues_after_discovery_is_complete(self) -> None:
        from unittest.mock import patch

        class _Corpus:
            def lexical_headwords(self) -> tuple[str, ...]:
                return ("alpha",)

            def metadata(self) -> dict[str, str]:
                return {}

            def draw_unseen_word(
                self, _seed: str, _planned: set[str]
            ) -> None:
                return None

        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return [{"mode": "knowledge", "query": "alpha"}]

        class _Knowledge:
            def __init__(self) -> None:
                self.jobs: list[dict[str, str]] = []

            def planned_term_keys(self, _language: str) -> set[str]:
                return {"alpha"}

            def jobs_for_subject(self, _subject_key: str) -> list[dict[str, str]]:
                return self.jobs

        versions: list[str] = []

        class _Planner:
            def __init__(
                self,
                knowledge: _Knowledge,
                *,
                model: str,
                prompt_version: str,
                source_fingerprint: str,
            ) -> None:
                self.knowledge = knowledge
                self.prompt_version = prompt_version
                versions.append(prompt_version)

            def plan_lexical_history_repair(self, _query: str) -> SimpleNamespace:
                fresh = "-origin-repair-" in self.prompt_version
                job_id = "fresh-origin" if fresh else "exhausted-origin"
                self.knowledge.jobs = [
                    {"job_id": job_id, "status": "queued" if fresh else "failed"}
                ]
                return SimpleNamespace(
                    subject_key="term:alpha",
                    jobs={"expand-origin-branches": job_id},
                )

        knowledge = _Knowledge()
        seeder = AutonomousLexicalSeeder(
            _Corpus(), _Store(), knowledge, model="local-qwen-test"
        )
        with patch("lkt.deck.PreparationPlanner", _Planner):
            result = seeder.run_once("stable-cycle")

        self.assertEqual(result.status, "repair-queued")
        self.assertEqual(versions[0], "autonomous-lexical-v4")
        self.assertTrue(versions[1].startswith("autonomous-lexical-v4-origin-repair-"))
        self.assertEqual(seeder.progress()["complete"], False)
        self.assertEqual(seeder.progress()["remaining"], 1)

    def test_terminal_word_gap_gets_one_deterministic_full_repair(self) -> None:
        from unittest.mock import patch

        repair_version = "autonomous-lexical-v4-word-repair-v1"

        class _Corpus:
            def lexical_headwords(self) -> tuple[str, ...]:
                return ("alpha",)

            def metadata(self) -> dict[str, str]:
                return {"source_sha256": "corpus-sha"}

            def draw_unseen_word(
                self, _seed: str, _planned: set[str]
            ) -> None:
                return None

        class _Store:
            def accepted_for_modes(
                self, _modes: tuple[str, ...]
            ) -> list[dict[str, str]]:
                return []

        class _Knowledge:
            def __init__(self) -> None:
                self.repair_seen = False
                self.jobs: list[dict[str, str]] = []

            def planned_term_keys(self, _language: str) -> set[str]:
                return {"alpha"}

            def terminal_failed_term_keys(
                self,
                _language: str,
                *,
                exclude_prompt_version: str = "",
                source_fingerprint: str = "",
            ) -> set[str]:
                self.asserted_fingerprint = source_fingerprint
                if exclude_prompt_version == repair_version and self.repair_seen:
                    return set()
                return {"alpha"}

            def jobs_for_subject(self, _subject_key: str) -> list[dict[str, str]]:
                return self.jobs

        versions: list[str] = []

        class _Planner:
            def __init__(
                self,
                knowledge: _Knowledge,
                *,
                model: str,
                prompt_version: str,
                source_fingerprint: str,
            ) -> None:
                self.knowledge = knowledge
                self.prompt_version = prompt_version
                versions.append(prompt_version)

            def plan_word(self, _query: str) -> SimpleNamespace:
                self.knowledge.repair_seen = True
                self.knowledge.jobs = [
                    {"job_id": "repair-word", "status": "queued"}
                ]
                return SimpleNamespace(
                    subject_key="term:alpha",
                    jobs={"compose-word-card": "repair-word"},
                )

        knowledge = _Knowledge()
        seeder = AutonomousLexicalSeeder(
            _Corpus(), _Store(), knowledge, model="local-qwen-test"
        )
        with patch("lkt.deck.PreparationPlanner", _Planner):
            first = seeder.run_once("stable-cycle")
            knowledge.jobs[0]["status"] = "failed"
            second = seeder.run_once("stable-cycle")

        self.assertEqual(first.status, "repair-queued")
        self.assertEqual(second.status, "repair-blocked")
        self.assertEqual(versions, [repair_version])
        self.assertEqual(knowledge.asserted_fingerprint, "corpus-sha")
        self.assertFalse(seeder.progress()["complete"])


if __name__ == "__main__":
    unittest.main()
