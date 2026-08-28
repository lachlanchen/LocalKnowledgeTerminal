from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .corpus import CorpusIndex
from .llm import CardModel
from .models import Card
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


def _language(value: Any, fields: tuple[str, ...]) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {field: _short_text(source.get(field), limit=1000) for field in fields}


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
    ):
        self.corpus = corpus
        self.model = model
        self.store = store
        self.max_evidence = max_evidence

    def create(self, query: str, mode: str = "word") -> Card:
        query = _short_text(query, limit=240)
        if len(query) < 1:
            raise ValueError("enter a word or question")
        if mode not in {"word", "knowledge"}:
            raise ValueError("mode must be 'word' or 'knowledge'")
        evidence = self.corpus.search(query, self.max_evidence)
        if not evidence:
            raise NoEvidence(f"no Word Origins evidence found for '{query}'")
        generated = self.model.generate(query, mode, evidence)
        title = _short_text(generated.get("title"), evidence[0].headword, 200)
        card = Card(
            card_id=str(uuid.uuid4()),
            mode=mode,
            query=query,
            title=title,
            subtitle=_short_text(generated.get("subtitle"), limit=400),
            summary_en=_short_text(generated.get("summary_en"), limit=2500),
            origin_story=_short_text(generated.get("origin_story"), limit=5000),
            key_points=_string_list(generated.get("key_points")),
            english=_language(
                generated.get("english"), ("term", "pronunciation", "meaning")
            ),
            japanese=_language(
                generated.get("japanese"), ("term", "reading", "meaning")
            ),
            chinese=_language(
                generated.get("chinese"),
                ("simplified", "traditional", "pinyin", "meaning"),
            ),
            memory_hook=_short_text(generated.get("memory_hook"), limit=1200),
            related_terms=_related(generated.get("related_terms")),
            evidence=evidence,
            model=self.model.model_name,
            created_at=datetime.now(UTC).isoformat(),
            extensions={"outputs": ["web"], "future_outputs": ["eink", "audio"]},
        )
        self.store.save(card)
        return card
