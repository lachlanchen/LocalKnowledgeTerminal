from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lkt.retrieval import (
    AffixRag,
    AnswerRag,
    QuestionRag,
    RootRag,
    WordCardRag,
    WordOriginRag,
)

from test_card_books import make_card_book, record
from test_corpus import make_index
from test_morphology import make_morphology_index


class RetrievalTests(unittest.TestCase):
    def test_word_experiences_share_a_corpus_but_keep_separate_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            corpus = make_index(Path(temp))
            origin = WordOriginRag(corpus).retrieve("abacus algorithm")
            card = WordCardRag(corpus, 4).retrieve("abacus algorithm")
            self.assertLessEqual(len(origin), 1)
            self.assertGreaterEqual(len(card), len(origin))

    def test_answer_and_question_use_independent_books(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            answer = make_card_book(
                root,
                "answer",
                [record("answer-001", "answer", 1, "Begin", "始めて", "开始", page=2)],
            )
            question = make_card_book(
                root,
                "question",
                [record("question-001", "question", 1, "Why begin?", "なぜ？", "为什么？", locator="q.xhtml")],
            )
            self.assertEqual(AnswerRag(answer).retrieve("now")[0].kind, "answer")
            self.assertEqual(QuestionRag(question).retrieve("begin")[0].kind, "question")

    def test_morphology_modes_share_sources_but_keep_primary_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roots = make_morphology_index(root, "root")
            affixes = make_morphology_index(root, "affix")
            root_evidence = RootRag(roots, affixes).retrieve("aspect")
            affix_evidence = AffixRag(affixes, roots).retrieve("aspect")
            self.assertEqual(root_evidence[0].kind, "morphology-root")
            self.assertEqual(affix_evidence[0].kind, "morphology-affix")
            self.assertEqual({item.kind for item in root_evidence}, {
                "morphology-root", "morphology-affix"
            })


if __name__ == "__main__":
    unittest.main()
