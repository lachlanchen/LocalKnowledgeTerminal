from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .card_books import CardBookIndex
from .corpus import CorpusIndex
from .llm import CardModel
from .models import Card, Evidence
from .store import CardStore


class NoEvidence(LookupError):
    pass


def _short_text(value: Any, fallback: str = "", limit: int = 4000) -> str:
    if not isinstance(value, str):
        return fallback
    return re.sub(r"\s+", " ", value).strip()[:limit] or fallback


def _string_list(value: Any, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_short_text(item, limit=500) for item in value[:limit] if _short_text(item)]


def _language(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {field: _short_text(source.get(field), limit=1000) for field in fields}


def _ruby_tokens(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = _short_text(item.get("t"), limit=80)
        reading = _short_text(item.get("r"), limit=80)
        if text:
            result.append({"t": text, **({"r": reading} if reading else {})})
    return result


def _related(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value[:8]:
        if isinstance(item, dict):
            term = _short_text(item.get("term"), limit=120)
            note = _short_text(item.get("note"), limit=500)
            if term:
                result.append({"term": term, "note": note})
    return result


class CardService:
    def __init__(
        self,
        corpus: CorpusIndex,
        model: CardModel,
        store: CardStore,
        max_evidence: int = 4,
        card_books: dict[str, CardBookIndex] | None = None,
    ):
        self.corpus = corpus
        self.model = model
        self.store = store
        self.max_evidence = max_evidence
        self.card_books = card_books or {}

    def _retrieve(self, query: str, mode: str) -> list[Evidence]:
        if mode in {"word", "knowledge"}:
            return self.corpus.search(query, self.max_evidence)
        book = self.card_books.get(mode)
        if book is None:
            raise FileNotFoundError(f"{mode} corpus is not configured")
        if mode == "answer":
            return [book.draw(query)]
        results = book.search(query, 1)
        return results or [book.draw(query)]

    def create(self, query: str, mode: str = "word") -> Card:
        query = _short_text(query, limit=240)
        if len(query) < 1:
            raise ValueError("enter a word or question")
        if mode not in {"word", "knowledge", "answer", "question"}:
            raise ValueError("mode must be word, knowledge, answer, or question")
        evidence = self._retrieve(query, mode)
        if not evidence:
            raise NoEvidence(f"no book evidence found for '{query}'")
        generated = self.model.generate(query, mode, evidence)
        title = _short_text(generated.get("title"), evidence[0].headword, 200)
        english = _language(
            generated.get("english"), ("term", "pronunciation", "meaning")
        )
        japanese = _language(
            generated.get("japanese"), ("term", "reading", "meaning")
        )
        chinese = _language(
            generated.get("chinese"),
            ("simplified", "traditional", "pinyin", "meaning"),
        )
        if mode in {"answer", "question"}:
            translations = evidence[0].translations
            en = translations.get("en") if isinstance(translations.get("en"), dict) else {}
            ja = translations.get("ja") if isinstance(translations.get("ja"), dict) else {}
            zh = translations.get("zh") if isinstance(translations.get("zh"), dict) else {}
            english["term"] = _short_text(en.get("primary"), english["term"], 3000)
            japanese["term"] = _short_text(ja.get("primary"), japanese["term"], 3000)
            japanese["ruby_tokens"] = _ruby_tokens(ja.get("ruby_tokens"))
            chinese["simplified"] = _short_text(
                zh.get("primary"), chinese["simplified"], 3000
            )
        card = Card(
            card_id=str(uuid.uuid4()),
            mode=mode,
            query=query,
            title=title,
            subtitle=_short_text(generated.get("subtitle"), limit=400),
            summary_en=_short_text(generated.get("summary_en"), limit=2500),
            origin_story=_short_text(generated.get("origin_story"), limit=5000),
            key_points=_string_list(generated.get("key_points")),
            english=english,
            japanese=japanese,
            chinese=chinese,
            memory_hook=_short_text(generated.get("memory_hook"), limit=1200),
            related_terms=_related(generated.get("related_terms")),
            evidence=evidence,
            model=self.model.model_name,
            created_at=datetime.now(UTC).isoformat(),
            extensions={
                "corpus_id": evidence[0].corpus_id,
                "source_title": evidence[0].source_title,
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
            },
        )
        self.store.save(card)
        return card
