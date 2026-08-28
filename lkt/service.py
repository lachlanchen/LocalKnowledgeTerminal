from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .card_books import CardBookIndex
from .corpus import CorpusIndex
from .llm import CardModel
from .models import Card, Evidence
from .pronunciation import chinese_pinyin, chinese_ruby_tokens
from .retrieval import RagEngine, build_rag_engines
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


def _ruby_tokens_for_term(value: Any, term: str) -> list[dict[str, str]]:
    """Accept generated ruby only when its visible text exactly covers the term."""

    tokens = _ruby_tokens(value)
    visible = re.sub(r"\s+", "", "".join(item["t"] for item in tokens))
    expected = re.sub(r"\s+", "", term)
    return tokens if tokens and visible == expected else []


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


def _origin_graph(
    value: Any, evidence: list[Evidence], title: str
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    used_ids: set[str] = set()
    has_explicit_relationships = False
    if isinstance(value, list):
        for index, item in enumerate(value[:7]):
            if not isinstance(item, dict):
                continue
            form = _short_text(item.get("form"), limit=80)
            if not form:
                continue
            basis = _short_text(item.get("basis"), "model", 12).lower()
            node_id = re.sub(
                r"[^a-z0-9-]+",
                "-",
                _short_text(item.get("id"), f"origin-{index + 1}", 48).lower(),
            ).strip("-") or f"origin-{index + 1}"
            if node_id in used_ids:
                node_id = f"{node_id}-{index + 1}"
            used_ids.add(node_id)
            parent = re.sub(
                r"[^a-z0-9-]+",
                "-",
                _short_text(item.get("parent"), limit=48).lower(),
            ).strip("-")
            has_explicit_relationships = has_explicit_relationships or "parent" in item
            result.append(
                {
                    "id": node_id,
                    "parent": parent,
                    "stage": _short_text(item.get("stage"), "Earlier form", 80),
                    "form": form,
                    "meaning": _short_text(item.get("meaning"), limit=180),
                    "basis": "book" if basis == "book" else "model",
                }
            )
    if len(result) >= 2:
        known_ids = {item["id"] for item in result}
        if not has_explicit_relationships:
            for index, item in enumerate(result):
                item["parent"] = result[index + 1]["id"] if index + 1 < len(result) else ""
        else:
            roots = [item for item in result if not item["parent"] or item["parent"] not in known_ids]
            root = roots[0] if roots else result[0]
            root["parent"] = ""
            for item in roots[1:]:
                item["parent"] = root["id"]
        return result
    anchor = evidence[0]
    return [
        {
            "id": "modern-word",
            "parent": "",
            "stage": "Modern English",
            "form": title,
            "meaning": "Present form",
            "basis": "book",
        },
        {
            "id": "book-origin",
            "parent": "modern-word",
            "stage": anchor.date_label or anchor.section or "Book record",
            "form": anchor.headword,
            "meaning": _short_text(anchor.excerpt, limit=140),
            "basis": "book",
        },
    ]


class CardService:
    def __init__(
        self,
        corpus: CorpusIndex,
        model: CardModel,
        store: CardStore,
        max_evidence: int = 4,
        card_books: dict[str, CardBookIndex] | None = None,
        rag_engines: dict[str, RagEngine] | None = None,
    ):
        self.corpus = corpus
        self.model = model
        self.store = store
        self.max_evidence = max_evidence
        self.card_books = card_books or {}
        self.rag_engines = rag_engines or build_rag_engines(
            corpus, self.card_books, max_evidence
        )

    def _retrieve(self, query: str, mode: str) -> list[Evidence]:
        engine = self.rag_engines.get(mode)
        if engine is None:
            raise FileNotFoundError(f"{mode} corpus is not configured")
        return engine.retrieve(query)

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
        generated_japanese = (
            generated.get("japanese")
            if isinstance(generated.get("japanese"), dict)
            else {}
        )
        japanese["ruby_tokens"] = _ruby_tokens_for_term(
            generated_japanese.get("ruby_tokens"), japanese["term"]
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
        chinese["pinyin"] = chinese_pinyin(
            chinese["simplified"], chinese.get("pinyin", "")
        )
        chinese["ruby_tokens"] = chinese_ruby_tokens(chinese["simplified"])
        extra_languages: dict[str, dict[str, str]] = {}
        if mode == "knowledge":
            extra_languages = {
                "french": _language(
                    generated.get("french"), ("term", "pronunciation", "meaning")
                ),
                "arabic": _language(
                    generated.get("arabic"), ("term", "reading", "meaning")
                ),
            }
            extra_languages = {
                language: value
                for language, value in extra_languages.items()
                if value.get("term")
            }
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
                "experience": mode,
                "knowledge_policy": (
                    "book-anchored-model-enriched"
                    if mode == "word"
                    else "retrieval-grounded"
                ),
            },
            origin_graph=(
                _origin_graph(generated.get("origin_graph"), evidence, title)
                if mode == "word"
                else []
            ),
            extra_languages=extra_languages,
        )
        self.store.save(card)
        return card
