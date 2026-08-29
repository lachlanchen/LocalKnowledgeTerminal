from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.deck import AutonomousDeckSeeder
from lkt.knowledge import KnowledgeStore
from lkt.models import Evidence
from lkt.service import CardService
from lkt.store import CardStore

from test_card_books import make_card_book, record
from test_corpus import make_index


class _LocalModel:
    model_name = "local-qwen-test"

    def generate(
        self, query: str, mode: str, evidence: list[Evidence]
    ) -> dict[str, Any]:
        return {
            "title": f"Local {mode.title()}",
            "origin_story": f"A restrained local reflection on {evidence[0].headword}.",
        }


class AutonomousDeckTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
