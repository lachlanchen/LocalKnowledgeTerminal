from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .card_books import CardBookIndex
from .corpus import CorpusIndex
from .models import Evidence
from .morphology import MorphologyIndex


class RagEngine(Protocol):
    """Independent retrieval boundary for one product experience."""

    corpus_id: str

    def retrieve(self, query: str) -> list[Evidence]:
        ...


@dataclass(frozen=True)
class WordOriginRag:
    corpus: CorpusIndex
    morphology: tuple[MorphologyIndex, ...] = ()
    corpus_id: str = "word-origins"

    def retrieve(self, query: str) -> list[Evidence]:
        evidence = self.corpus.search(query, 1)
        for index in self.morphology:
            evidence.extend(index.exact(query, 1))
        return evidence


@dataclass(frozen=True)
class WordCardRag:
    corpus: CorpusIndex
    limit: int = 4
    morphology: tuple[MorphologyIndex, ...] = ()
    corpus_id: str = "word-origins"

    def retrieve(self, query: str) -> list[Evidence]:
        morphology_limit = min(len(self.morphology), max(0, self.limit - 1))
        word_limit = max(1, self.limit - morphology_limit)
        evidence = self.corpus.search(query, word_limit)
        for index in self.morphology[:morphology_limit]:
            evidence.extend(index.exact(query, 1))
        return evidence


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


@dataclass(frozen=True)
class MorphologyRag:
    """Retrieve one complete word decomposition with a mode-specific priority."""

    primary: MorphologyIndex
    secondary: MorphologyIndex
    corpus_id: str
    primary_limit: int = 1
    secondary_limit: int = 1

    def retrieve(self, query: str) -> list[Evidence]:
        evidence = self.primary.search(query, self.primary_limit)
        evidence.extend(self.secondary.exact(query, self.secondary_limit))
        result: list[Evidence] = []
        seen: set[tuple[str, str]] = set()
        for item in evidence:
            key = (item.corpus_id, item.entry_id)
            if key in seen:
                continue
            result.append(item)
            seen.add(key)
        return result


class RootRag(MorphologyRag):
    def __init__(self, roots: MorphologyIndex, affixes: MorphologyIndex):
        super().__init__(roots, affixes, "english-root-dictionary")


class AffixRag(MorphologyRag):
    def __init__(self, affixes: MorphologyIndex, roots: MorphologyIndex):
        super().__init__(affixes, roots, "english-affix-dictionary")


def build_rag_engines(
    corpus: CorpusIndex,
    card_books: dict[str, CardBookIndex],
    word_card_limit: int,
    morphology: dict[str, MorphologyIndex] | None = None,
) -> dict[str, RagEngine]:
    morphology = morphology or {}
    morphology_references = tuple(
        morphology[key] for key in ("root", "affix") if key in morphology
    )
    engines: dict[str, RagEngine] = {
        "word": WordOriginRag(corpus, morphology_references),
        "knowledge": WordCardRag(corpus, word_card_limit, morphology_references),
    }
    if "answer" in card_books:
        engines["answer"] = AnswerRag(card_books["answer"])
    if "question" in card_books:
        engines["question"] = QuestionRag(card_books["question"])
    if "root" in morphology and "affix" in morphology:
        engines["root"] = RootRag(morphology["root"], morphology["affix"])
        engines["affix"] = AffixRag(morphology["affix"], morphology["root"])
    return engines
