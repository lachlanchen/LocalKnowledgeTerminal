from __future__ import annotations

import unittest
from pathlib import Path

from lkt.web import card_chat_context, chat_messages, renderable_card


class WebInputTests(unittest.TestCase):
    def test_bare_terminal_defaults_to_the_answer_carousel(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('let mode = "answer";', script)
        self.assertIn('initialParameters.get("mode") : "answer"', script)
        self.assertIn('class="mode active" data-mode="answer"', page)
        self.assertIn("shuffledAnswerDeck(carouselCards)", script)
        self.assertIn("carouselCards.length > 1", script)
        self.assertIn("}, 30000);", script)
        self.assertIn('fetch("/api/intent"', script)
        self.assertIn('ambientRouting = !initialParameters.has("mode")', script)

    def test_old_cards_receive_chinese_ruby_without_database_migration(self) -> None:
        card = {"chinese": {"simplified": "中国", "pinyin": "zhōng guó"}}
        rendered = renderable_card(card)
        self.assertEqual(
            rendered["chinese"]["ruby_tokens"][1],
            {"t": "国", "r": "guó"},
        )

    def test_chat_messages_keep_only_bounded_user_and_assistant_history(self) -> None:
        messages = chat_messages(
            {
                "message": "new question",
                "history": [
                    {"role": "system", "content": "discard me"},
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "earlier reply"},
                    {"role": "invalid", "content": "discard me too"},
                ],
            }
        )
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier reply"},
                {"role": "user", "content": "new question"},
            ],
        )

    def test_chat_rejects_an_empty_message(self) -> None:
        with self.assertRaises(ValueError):
            chat_messages({"message": "  "})

    def test_card_chat_context_keeps_retrieved_source(self) -> None:
        context = card_chat_context(
            {
                "title": "Abacus",
                "summary_en": "A counting frame.",
                "origin_story": "It passed through Greek and Latin.",
                "english": {"term": "abacus"},
                "evidence": [
                    {
                        "entry_id": "entry-0003",
                        "pages": [12],
                        "excerpt": "The source book excerpt.",
                    }
                ],
            }
        )
        self.assertIn("Abacus", context)
        self.assertIn("entry-0003 page 12", context)
        self.assertIn("The source book excerpt", context)


if __name__ == "__main__":
    unittest.main()
