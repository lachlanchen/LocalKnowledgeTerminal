from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .card_books import CardBookIndex
from .corpus import CorpusIndex
from .models import Evidence


class RagEngine(Protocol):
    """Independent retrieval boundary for one product experience."""

    corpus_id: str

    def retrieve(self, query: str) -> list[Evidence]:
        ...


@dataclass(frozen=True)
class WordOriginRag:
    corpus: CorpusIndex
    corpus_id: str = "word-origins"

    def retrieve(self, query: str) -> list[Evidence]:
        return self.corpus.search(query, 1)


@dataclass(frozen=True)
class WordCardRag:
    corpus: CorpusIndex
    limit: int = 4
    corpus_id: str = "word-origins"

    def retrieve(self, query: str) -> list[Evidence]:
        return self.corpus.search(query, self.limit)


@dataclass(frozen=True)
class AnswerRag:
    corpus: CardBookIndex
    corpus_id: str = "book-of-answers"

    def retrieve(self, query: str) -> list[Evidence]:
        return [self.corpus.draw(query)]


@dataclass(frozen=True)
class QuestionRag:
    corpus: CardBookIndex
    corpus_id: str = "book-of-questions"

    def retrieve(self, query: str) -> list[Evidence]:
        return self.corpus.search(query, 1) or [self.corpus.draw(query)]


def build_rag_engines(
    corpus: CorpusIndex,
    card_books: dict[str, CardBookIndex],
    word_card_limit: int,
) -> dict[str, RagEngine]:
    engines: dict[str, RagEngine] = {
        "word": WordOriginRag(corpus),
        "knowledge": WordCardRag(corpus, word_card_limit),
    }
    if "answer" in card_books:
        engines["answer"] = AnswerRag(card_books["answer"])
    if "question" in card_books:
        engines["question"] = QuestionRag(card_books["question"])
    return engines
