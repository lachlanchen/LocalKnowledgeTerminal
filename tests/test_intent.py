from __future__ import annotations

import unittest

from lkt.intent import route_intent


class IntentRoutingTests(unittest.TestCase):
    def test_one_english_word_opens_a_word_card(self) -> None:
        self.assertEqual(
            route_intent("  mother-in-law  ").to_dict(),
            {
                "mode": "knowledge",
                "query": "mother-in-law",
                "reason": "single-english-word",
            },
        )

    def test_a_general_question_uses_local_chat(self) -> None:
        route = route_intent("What should I learn today?")
        self.assertEqual((route.mode, route.reason), ("chat", "general-inquiry"))

    def test_explicit_prefixes_keep_investigations_separate(self) -> None:
        cases = {
            "word: inspection": "knowledge",
            "ORIGIN: inspection": "word",
            "etymology: inspection": "word",
            "root: inspection": "root",
            "prefix: inspection": "affix",
            "suffix: inspection": "affix",
            "question: friendship": "question",
            "answer: what now": "answer",
            "ask: explain recursion": "chat",
        }
        for query, expected_mode in cases.items():
            with self.subTest(query=query):
                self.assertEqual(route_intent(query).mode, expected_mode)

    def test_empty_prefix_and_oversized_input_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "after origin"):
            route_intent("origin:")
        with self.assertRaisesRegex(ValueError, "too long"):
            route_intent("x" * 2001)


if __name__ == "__main__":
    unittest.main()
