from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .card_books import CardBookIndex
from .corpus import CorpusIndex
from .llm import CardModel
from .models import Card, Evidence
from .morphology import MorphologyIndex
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


def _identifier(value: Any, fallback: str) -> str:
    identifier = re.sub(
        r"[^a-z0-9-]+", "-", _short_text(value, fallback, 64).casefold()
    ).strip("-")
    return identifier or fallback


def _morphology_graph(
    value: Any, evidence: list[Evidence], title: str
) -> dict[str, Any]:
    """Normalize a rich model graph while enforcing book-node provenance."""

    source = value if isinstance(value, dict) else {}
    valid_evidence_ids = {item.entry_id for item in evidence}
    allowed_types = {"word", "prefix", "root", "suffix", "historical", "related"}
    nodes: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    raw_nodes = source.get("nodes") if isinstance(source.get("nodes"), list) else []
    for index, raw in enumerate(raw_nodes[:18]):
        if not isinstance(raw, dict):
            continue
        form = _short_text(raw.get("form"), limit=100)
        if not form:
            continue
        node_id = _identifier(raw.get("id"), f"node-{index + 1}")
        if node_id in used_ids:
            node_id = f"{node_id}-{index + 1}"
        used_ids.add(node_id)
        node_type = _short_text(raw.get("type"), "related", 20).casefold()
        node_type = node_type if node_type in allowed_types else "related"
        raw_evidence_ids = raw.get("evidence_ids")
        evidence_ids = [
            str(item).strip()
            for item in raw_evidence_ids[:5]
            if str(item).strip() in valid_evidence_ids
        ] if isinstance(raw_evidence_ids, list) else []
        requested_basis = _short_text(raw.get("basis"), "model", 12).casefold()
        basis = "book" if requested_basis == "book" and evidence_ids else "model"
        confidence = _short_text(raw.get("confidence"), "medium", 12).casefold()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "form": form,
                "meaning": _short_text(raw.get("meaning"), limit=180),
                "language": _short_text(raw.get("language"), "English", 60),
                "history": _short_text(raw.get("history"), limit=420),
                "basis": basis,
                "evidence_ids": list(dict.fromkeys(evidence_ids)),
                "confidence": confidence,
            }
        )

    if not nodes:
        return {"center_id": "", "nodes": [], "edges": [], "focus_areas": []}
    requested_center = _identifier(source.get("center_id"), nodes[0]["id"])
    center_id = requested_center if requested_center in used_ids else next(
        (node["id"] for node in nodes if node["type"] == "word"), nodes[0]["id"]
    )
    valid_relationships = {
        "developed-into", "prefix-of", "root-of", "suffix-of", "related-form"
    }
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    raw_edges = source.get("edges") if isinstance(source.get("edges"), list) else []
    for raw in raw_edges[:32]:
        if not isinstance(raw, dict):
            continue
        source_id = _identifier(raw.get("source"), "")
        target_id = _identifier(raw.get("target"), "")
        relationship = _short_text(
            raw.get("relationship"), "related-form", 32
        ).casefold()
        relationship = (
            relationship if relationship in valid_relationships else "related-form"
        )
        key = (source_id, target_id, relationship)
        if (
            source_id in used_ids
            and target_id in used_ids
            and source_id != target_id
            and key not in seen_edges
        ):
            edges.append(
                {"source": source_id, "target": target_id, "relationship": relationship}
            )
            seen_edges.add(key)

    focus_areas: list[dict[str, Any]] = []
    raw_focuses = (
        source.get("focus_areas") if isinstance(source.get("focus_areas"), list) else []
    )
    for index, raw in enumerate(raw_focuses[:12]):
        if not isinstance(raw, dict):
            continue
        node_ids = raw.get("node_ids")
        node_ids = [
            _identifier(item, "") for item in node_ids if _identifier(item, "") in used_ids
        ] if isinstance(node_ids, list) else []
        if not node_ids:
            continue
        kind = _short_text(raw.get("kind"), "history", 20).casefold()
        if kind not in {"overview", "root", "prefix", "suffix", "history"}:
            kind = "history"
        focus_areas.append(
            {
                "id": _identifier(raw.get("id"), f"focus-{index + 1}"),
                "label": _short_text(raw.get("label"), kind.title(), 60),
                "kind": kind,
                "node_ids": list(dict.fromkeys(node_ids)),
                "headline": _short_text(raw.get("headline"), limit=180),
                "explanation": _short_text(raw.get("explanation"), limit=420),
            }
        )
    all_node_ids = [node["id"] for node in nodes]
    overview = next((area for area in focus_areas if area["kind"] == "overview"), None)
    if overview:
        overview["node_ids"] = all_node_ids
        focus_areas.remove(overview)
    else:
        overview = {
            "id": "overview",
            "label": "Whole word",
            "kind": "overview",
            "node_ids": all_node_ids,
            "headline": title,
            "explanation": "Complete evidenced morphology graph.",
        }
    focus_areas.insert(0, overview)
    return {
        "center_id": center_id,
        "nodes": nodes,
        "edges": edges,
        "focus_areas": focus_areas,
    }


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
            by_id = {item["id"]: item for item in result}
            for item in result:
                if item is root:
                    continue
                visited = {item["id"]}
                parent = item["parent"]
                while parent != root["id"]:
                    if not parent or parent not in by_id or parent in visited:
                        item["parent"] = root["id"]
                        break
                    visited.add(parent)
                    parent = by_id[parent]["parent"]
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


def _legacy_morphology_graph(
    nodes: list[dict[str, str]], title: str
) -> dict[str, Any]:
    """Project saved v1 origin nodes into the unified graph contract."""

    ids = {node["id"] for node in nodes}
    center = next(
        (node for node in nodes if not node.get("parent") or node["parent"] not in ids),
        nodes[0],
    )
    graph_nodes = [
        {
            "id": node["id"],
            "type": "word" if node is center else "historical",
            "form": node["form"],
            "meaning": node.get("meaning", ""),
            "language": node.get("stage", ""),
            "history": "",
            "basis": node.get("basis", "model"),
            "evidence_ids": [],
            "confidence": "medium",
        }
        for node in nodes
    ]
    edges = [
        {
            "source": node["id"],
            "target": node["parent"],
            "relationship": "developed-into",
        }
        for node in nodes
        if node.get("parent") in ids
    ]
    node_ids = [node["id"] for node in graph_nodes]
    return {
        "center_id": center["id"],
        "nodes": graph_nodes,
        "edges": edges,
        "focus_areas": [
            {
                "id": "overview",
                "label": "Whole history",
                "kind": "overview",
                "node_ids": node_ids,
                "headline": title,
                "explanation": "Saved legacy origin graph.",
            }
        ],
    }


class CardService:
    def __init__(
        self,
        corpus: CorpusIndex,
        model: CardModel,
        store: CardStore,
        max_evidence: int = 4,
        card_books: dict[str, CardBookIndex] | None = None,
        morphology: dict[str, MorphologyIndex] | None = None,
        rag_engines: dict[str, RagEngine] | None = None,
    ):
        self.corpus = corpus
        self.model = model
        self.store = store
        self.max_evidence = max_evidence
        self.card_books = card_books or {}
        self.morphology = morphology or {}
        self.rag_engines = rag_engines or build_rag_engines(
            corpus, self.card_books, max_evidence, self.morphology
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
        if mode not in {"word", "knowledge", "answer", "question", "root", "affix"}:
            raise ValueError(
                "mode must be word, knowledge, answer, question, root, or affix"
            )
        evidence = self._retrieve(query, mode)
        if not evidence:
            raise NoEvidence(f"no book evidence found for '{query}'")
        preparation_run_id = self.store.start_preparation(
            mode, query, self.model.model_name
        )
        self.store.save_preparation_artifact(
            preparation_run_id,
            "retrieved-evidence",
            [item.to_dict() for item in evidence],
        )
        try:
            generated = self.model.generate(query, mode, evidence)
        except Exception as exc:
            self.store.finish_preparation(
                preparation_run_id, "failed", error=str(exc)
            )
            raise
        self.store.save_preparation_artifact(
            preparation_run_id, "cleaned-model-draft", generated
        )
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
        legacy_origin = (
            _origin_graph(generated.get("origin_graph"), evidence, title)
            if mode == "word" and isinstance(generated.get("origin_graph"), list)
            else []
        )
        morphology_graph = {}
        if mode in {"word", "root", "affix"}:
            if isinstance(generated.get("morphology_graph"), dict):
                morphology_graph = _morphology_graph(
                    generated.get("morphology_graph"), evidence, title
                )
            elif legacy_origin:
                morphology_graph = _legacy_morphology_graph(legacy_origin, title)
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
                "corpus_ids": list(dict.fromkeys(item.corpus_id for item in evidence)),
                "source_titles": list(
                    dict.fromkeys(item.source_title for item in evidence)
                ),
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
                "experience": mode,
                "preparation_run_id": preparation_run_id,
                "knowledge_policy": (
                    "multi-book-recursive-morphology"
                    if mode in {"root", "affix"}
                    else (
                        "book-anchored-model-enriched"
                        if mode in {"word", "knowledge"}
                        else "reviewed-book-text-model-reflection"
                    )
                ),
                **({"morphology_graph": morphology_graph} if morphology_graph else {}),
            },
            origin_graph=legacy_origin,
            extra_languages=extra_languages,
        )
        self.store.save_preparation_artifact(
            preparation_run_id, "normalized-card", card.to_dict()
        )
        self.store.save(card)
        self.store.save_preparation_artifact(
            preparation_run_id, "published-card", card.to_dict()
        )
        self.store.finish_preparation(
            preparation_run_id, "complete", card_id=card.card_id
        )
        return card
