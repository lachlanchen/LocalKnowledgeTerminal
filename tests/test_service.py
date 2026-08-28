from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.models import Evidence
from lkt.service import CardService
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
            "pages": [999],
        }


class ServiceTests(unittest.TestCase):
    def test_builds_multilingual_grounded_card_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CardStore(root / "cards.sqlite3")
            service = CardService(make_index(root), FakeModel(), store)
            card = service.create("abacus", "word")
            self.assertEqual(card.japanese["reading"], "そろばん")
            self.assertEqual(card.chinese["pinyin"], "suànpán")
            self.assertEqual(card.evidence[0].pages, (12,))
            self.assertTrue(card.grounded)
            self.assertEqual(store.recent()[0]["card_id"], card.card_id)
            self.assertEqual(store.get(card.card_id)["title"], "Abacus")

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
