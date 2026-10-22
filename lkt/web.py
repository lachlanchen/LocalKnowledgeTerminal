from __future__ import annotations

import json
import logging
import mimetypes
import sqlite3
from collections import OrderedDict
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock, Thread
from time import monotonic
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .card_books import CardBookIndex
from .config import Settings
from .corpus import CorpusIndex
from .deck import (
    AutonomousDeckSeeder,
    AutonomousLexicalSeeder,
    AutonomousMorphologySeeder,
)
from .evidence_policy import filter_card_evidence
from .freedict import FreeDictRag
from .intent import route_intent
from .jmdict import JapaneseReadingIndex
from .llm import LlamaCppClient, ModelUnavailable
from .knowledge import KnowledgeStore
from .language_projection import project_language_atoms
from .languages import language_profile, normalize_language_code, ordered_language_codes
from .morphology import MorphologyIndex
from .preparation import PreparationPlan, PreparationPlanner
from .pronunciation import chinese_ruby_tokens
from .service import CardService, NoEvidence
from .store import CardStore


LOG = logging.getLogger("lkt.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"

_PREPARATION_LABELS = {
    "retrieve-evidence": "Reading books and dictionaries",
    "prepare-meaning": "Choosing the central meaning",
    "split-morphemes": "Reviewing word structure with book evidence",
    "expand-origin-branches": "Tracing the cited lexical history",
    "prepare-translation": "Preparing one language",
    "prepare-pronunciation": "Aligning pronunciation",
    "prepare-grammar-properties": "Separating grammar properties",
    "compose-word-card": "Publishing the Word Card",
    "compose-origin-card": "Publishing the origin graph",
}

_ATOMIC_WORD_MODES = {"knowledge", "word", "root", "affix"}
_INTERACTIVE_PRIORITY_OFFSET = -1000
_LIVE_LEXICAL_MODES = {"knowledge", "word", "root", "affix"}
_LEXICAL_PROJECTION_VERSION = "live-lexical-projection-v14"
_LEXICAL_PROJECTION_CACHE_SIZE = 256
_CARD_SUMMARY_PAGE_SIZE = 64
_PROJECTION_WORKERS = 4
_HEALTH_CACHE_TTL_SECONDS = 60.0
_HEALTH_CACHE_MAX_TTL_SECONDS = 3600.0


def health_statistics_refresh_interval(
    refresh_seconds: float | None, *, last_refresh_failed: bool = False
) -> float:
    """Back off slow statistics scans while cheap live signals stay fresh."""

    if last_refresh_failed:
        return _HEALTH_CACHE_TTL_SECONDS
    measured = max(0.0, float(refresh_seconds or 0.0))
    return max(
        _HEALTH_CACHE_TTL_SECONDS,
        min(_HEALTH_CACHE_MAX_TTL_SECONDS, measured * 4.0),
    )


def live_health_response(
    snapshot: dict[str, Any], knowledge: KnowledgeStore, cards: CardStore,
) -> dict[str, Any]:
    """Overlay cheap live signals without mutating the slow statistics cache."""

    result = dict(snapshot)
    try:
        worker = knowledge.worker_status()
    except (OSError, ValueError, sqlite3.Error):
        LOG.warning("Live worker status is unavailable", exc_info=True)
        worker = {
            "ready": False, "generation_ready": False, "status": "unavailable",
            "blocker": "Cannot read the current worker heartbeat",
        }
    result["worker"] = worker
    generation = dict(snapshot.get("generation") or {})
    if snapshot.get("status") == "ready":
        generation.update(
            ready=bool(worker.get("generation_ready")),
            blocker=str(worker.get("blocker", "")),
        )
    else:
        generation["ready"] = False
    result["generation"] = generation
    result["live_sampled_at"] = datetime.now(UTC).isoformat()

    # These are visible deck totals, not source-coverage counts. Keep both
    # concepts separate: graph cleanup can remove duplicate cards while the
    # underlying knowledge and source coverage continue advancing.
    try:
        result["live_decks"] = {
            "ready": True,
            "modes": {
                mode: cards.accepted_deck_info(mode)
                for mode in ("answer", "question", "knowledge", "word", "root", "affix")
            },
        }
    except (OSError, ValueError, sqlite3.Error):
        LOG.warning("Live card counts are unavailable", exc_info=True)
        result["live_decks"] = {"ready": False, "modes": {}}

    try:
        with closing(knowledge._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                ("occasional_book_growth_v1",),
            ).fetchone()
        cadence = json.loads(row["value"]) if row else {}
        if not isinstance(cadence, dict):
            raise ValueError("invalid book cadence state")
        result["book_growth"] = {
            **cadence, "available": bool(cadence),
            "status": cadence.get("status", "not-observed"),
        }
    except (OSError, TypeError, ValueError, sqlite3.Error):
        LOG.warning("Live book schedule is unavailable", exc_info=True)
        result["book_growth"] = {"available": False, "status": "unavailable"}
    return result


def _rotation_form_key(value: Any) -> str:
    return "".join(
        character.casefold() for character in str(value) if character.isalnum()
    )


def live_card_rotation_eligible(card: dict[str, Any]) -> bool:
    """Reject sparse or detached live graph overlays from automatic rotation."""

    mode = str(card.get("mode", "")).strip()
    if mode not in {"word", "root", "affix"}:
        return True
    extensions = card.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    projection = extensions.get("lexical_projection")
    if isinstance(projection, dict) and projection.get("status") not in (None, "live"):
        return False
    graph = extensions.get("lexical_view")
    if not isinstance(graph, dict):
        return False
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return False
    if len(nodes) <= 1 and not edges:
        return False
    if mode == "word":
        return True

    branch_id = str(graph.get("branch_focus_id", "")).strip()
    if not branch_id:
        return True
    node_by_id = {
        str(node.get("id", "")): node for node in nodes if isinstance(node, dict)
    }
    branch = node_by_id.get(branch_id)
    if branch is None:
        return False
    subject_id = str(
        graph.get("canonical_subject_entity_id")
        or graph.get("subject_entity_id")
        or ""
    ).strip()
    if subject_id == branch_id:
        return True

    def is_subject_component(relation: Any) -> bool:
        return (
            isinstance(relation, dict)
            and str(relation.get("source", "")) == subject_id
            and str(relation.get("target", "")) == branch_id
            and str(relation.get("relation", "")) == "has-component"
        )

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if is_subject_component(edge):
            return True
        relations = edge.get("relations")
        if isinstance(relations, list) and any(
            is_subject_component(relation) for relation in relations
        ):
            return True

    return _rotation_form_key(card.get("query", "")) == _rotation_form_key(
        branch.get("label", "")
    )


def acquire_random_renderable_card(
    store: CardStore,
    mode: str,
    excluded_card_ids: tuple[str, ...],
    hydrate: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    max_attempts: int = 3,
    deadline_seconds: float = 18.0,
) -> dict[str, Any] | None:
    """Hydrate a bounded candidate set without piling up abandoned requests."""

    rejected: list[str] = []
    seen: set[str] = set()
    deadline = monotonic() + max(1.0, float(deadline_seconds))
    for _ in range(max(1, min(int(max_attempts), 24))):
        if seen and monotonic() >= deadline:
            break
        omitted = tuple(dict.fromkeys((*excluded_card_ids, *rejected)))[-24:]
        candidate = store.random_accepted(mode, omitted)
        if candidate is None:
            return None
        card_id = str(candidate.get("card_id", "")).strip()
        if not card_id or card_id in seen:
            return None
        seen.add(card_id)
        rendered = hydrate(candidate)
        if live_card_rotation_eligible(rendered):
            return rendered
        rejected.append(card_id)
    return None


def correction_source_status(settings: Settings) -> dict[str, dict[str, Any]]:
    """Report retrieval sources that constrain model-generated card atoms."""

    try:
        status = FreeDictRag(settings.freedict_db).status()
    except (OSError, ValueError, sqlite3.Error):
        status = {"ready": False, "database": str(settings.freedict_db)}
    jmdict_db = Path(
        getattr(settings, "jmdict_db", Path("missing-jmdict.sqlite3"))
    ).resolve()
    try:
        japanese = JapaneseReadingIndex(jmdict_db).status()
    except (OSError, ValueError, sqlite3.Error):
        japanese = {"ready": False, "database": str(jmdict_db)}
    return {"freedict_eng_ara": status, "jmdict": japanese}


def autonomous_deck_status(
    service: CardService, knowledge: KnowledgeStore
) -> dict[str, Any]:
    """Expose autonomous book coverage without starting an inference job."""

    try:
        return AutonomousDeckSeeder(service, service.store, knowledge).progress()
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error):
        return {
            "ready": False,
            "accepted": 0,
            "total": 0,
            "remaining": 0,
            "complete": False,
            "modes": {},
        }


def autonomous_lexical_status(
    service: CardService, knowledge: KnowledgeStore
) -> dict[str, Any]:
    """Expose missing-only lexical planning progress without queuing work."""

    try:
        progress = AutonomousLexicalSeeder(
            service.corpus,
            service.store,
            knowledge,
            model=service.model.model_name,
        ).progress()
        from .growth import growth_status

        progress["history_growth"] = growth_status(knowledge)
        return progress
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error):
        return {
            "ready": False,
            "planned": 0,
            "accepted": 0,
            "total": 0,
            "remaining": 0,
            "complete": False,
            "modes": ["knowledge", "word", "root", "affix"],
        }


def autonomous_morphology_status(
    service: CardService, knowledge: KnowledgeStore
) -> dict[str, Any]:
    """Expose graph-backed Root/Affix source coverage without queuing work."""

    try:
        progress = AutonomousMorphologySeeder(
            service.morphology,
            service.store,
            knowledge,
            model=service.model.model_name,
        ).progress()
        v3 = dict(progress.get("v3", {}))
        legacy = dict(progress.get("legacy_cards", {}))
        projection_modes: dict[str, dict[str, Any]] = {}
        for mode in ("root", "affix"):
            info = service.store.accepted_deck_info(mode)
            revision = str(info.get("revision", ""))
            _count, separator, updated_at = revision.partition(":")
            projection_modes[mode] = {
                "active_cards": int(info.get("total", 0)),
                "revision": revision,
                "updated_at": updated_at if separator else "",
            }
        active_cards = sum(
            int(item["active_cards"]) for item in projection_modes.values()
        )
        updated_at = max(
            (str(item["updated_at"]) for item in projection_modes.values()),
            default="",
        )
        return {
            "ready": bool(progress.get("ready", False)),
            "accepted": active_cards,
            "active_cards": active_cards,
            "modes": projection_modes,
            "updated_at": updated_at,
            "graph_revision": int(v3.get("graph_revision", 0)),
            "legacy_cards": legacy,
            "v3": v3,
            "observed": int(v3.get("observed", 0)),
            "materialized": int(v3.get("materialized", 0)),
            "ambiguous": int(v3.get("ambiguous", 0)),
            "qwen_ready": int(v3.get("qwen_ready", 0)),
            "enrichment": dict(v3.get("enrichment", {})),
        }
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error):
        return {
            "ready": False,
            "accepted": 0,
            "active_cards": 0,
            "modes": {
                mode: {"active_cards": 0, "revision": "", "updated_at": ""}
                for mode in ("root", "affix")
            },
            "updated_at": "",
            "graph_revision": 0,
            "legacy_cards": {},
            "v3": {},
        }


def _legacy_card_language_atoms(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    atoms = card.get("languages")
    result = {
        normalize_language_code(code): dict(value)
        for code, value in atoms.items()
        if isinstance(atoms, dict) and isinstance(value, dict)
    } if isinstance(atoms, dict) else {}
    legacy_specs = (
        ("en", "english", "term", "pronunciation"),
        ("ja", "japanese", "term", "reading"),
        ("zh", "chinese", "simplified", "pinyin"),
    )
    for code, key, term_key, reading_key in legacy_specs:
        value = card.get(key)
        if not isinstance(value, dict) or not str(value.get(term_key, "")).strip():
            continue
        profile = language_profile(code)
        result.setdefault(
            code,
            {
                "language": code,
                "label": profile.label,
                "direction": profile.direction,
                "term": str(value.get(term_key, "")),
                "reading": str(value.get(reading_key, "")),
                "meaning": str(value.get("meaning", "")),
                "ruby_tokens": list(value.get("ruby_tokens", [])),
                "state": "legacy",
            },
        )
    extras = card.get("extra_languages")
    if isinstance(extras, dict):
        for key, value in extras.items():
            if not isinstance(value, dict):
                continue
            raw_code = value.get("language") or {"french": "fr", "arabic": "ar"}.get(key, key)
            try:
                code = normalize_language_code(raw_code)
            except ValueError:
                continue
            profile = language_profile(code)
            result.setdefault(
                code,
                {
                    "language": code,
                    "label": str(value.get("label", profile.label)),
                    "direction": str(value.get("direction", profile.direction)),
                    "term": str(value.get("term", "")),
                    "reading": str(value.get("reading", value.get("pronunciation", ""))),
                    "meaning": str(value.get("meaning", "")),
                    "state": "legacy",
                },
            )
    return result


def _apply_legacy_language_fields(
    card: dict[str, Any], atoms: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    source_code = next(
        (code for code, atom in atoms.items() if atom.get("source") is True),
        "en",
    )
    ordered = {
        code: atoms[code]
        for code in ordered_language_codes(source_code, atoms)
        if code in atoms
    }
    card["languages"] = ordered
    if "en" in ordered:
        atom = ordered["en"]
        card["english"] = {
            "term": str(atom.get("term", "")),
            "pronunciation": str(atom.get("reading", "")),
            "meaning": str(atom.get("meaning", "")),
        }
    if "ja" in ordered:
        atom = ordered["ja"]
        card["japanese"] = {
            "term": str(atom.get("term", "")),
            "reading": str(atom.get("reading", "")),
            "meaning": str(atom.get("meaning", "")),
            "ruby_tokens": list(atom.get("ruby_tokens", [])),
        }
    if "zh" in ordered:
        atom = ordered["zh"]
        card["chinese"] = {
            "simplified": str(atom.get("term", "")),
            "traditional": "",
            "pinyin": str(atom.get("reading", "")),
            "meaning": str(atom.get("meaning", "")),
            "ruby_tokens": list(atom.get("ruby_tokens", [])),
        }
    extras: dict[str, dict[str, str]] = {}
    for code, atom in ordered.items():
        if code in {"en", "ja", "zh"}:
            continue
        profile = language_profile(code)
        extras[profile.card_key] = {
            "language": code,
            "label": profile.label,
            "direction": profile.direction,
            "term": str(atom.get("term", "")),
            "reading": str(atom.get("reading", "")),
            "pronunciation": str(atom.get("reading", "")),
            "meaning": str(atom.get("meaning", "")),
        }
    card["extra_languages"] = extras
    return card


def graph_language_fields(
    graph: dict[str, Any], subject_entity_id: str
) -> dict[str, dict[str, Any]]:
    """Extract language atoms from one bounded live knowledge projection."""

    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    by_id = {str(node.get("id", "")): node for node in nodes}
    term_ids = {subject_entity_id}
    translation_ids: set[str] = set()
    for edge in edges:
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        relation = str(edge.get("relation", ""))
        if relation == "has-translation" and source == subject_entity_id:
            if str(by_id.get(target, {}).get("type", "")) == "term":
                term_ids.add(target)
            else:
                translation_ids.add(target)
        if relation == "renders-as" and source in translation_ids:
            term_ids.add(target)
    result: dict[str, dict[str, Any]] = {}
    for term_id in sorted(term_ids):
        term = by_id.get(term_id)
        if not term or str(term.get("type", "")) != "term":
            continue
        payload = term.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        try:
            code = normalize_language_code(payload.get("language", ""))
        except ValueError:
            continue
        profile = language_profile(code)
        meanings = []
        pronunciations = []
        for edge in edges:
            if str(edge.get("source", "")) != term_id:
                continue
            target = by_id.get(str(edge.get("target", "")), {})
            if str(edge.get("relation", "")) == "has-meaning" and target.get("type") == "meaning":
                meanings.append(target)
            if str(edge.get("relation", "")) == "has-pronunciation" and target.get("type") == "pronunciation":
                pronunciations.append(target)
        meaning = max(meanings, key=lambda item: float(item.get("quality") or 0.0), default={})
        pronunciation = max(
            pronunciations,
            key=lambda item: float(item.get("quality") or 0.0),
            default={},
        )
        metadata = payload.get("translation_metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        provenance = metadata.get("provenance", {})
        provenance = provenance if isinstance(provenance, dict) else {}
        result[code] = {
            "language": code,
            "label": profile.label,
            "direction": profile.direction,
            "term": str(term.get("label", "")),
            "reading": str(pronunciation.get("label", metadata.get("transliteration", ""))),
            "meaning": str(meaning.get("label", "")),
            "term_id": term_id,
            "meaning_id": str(meaning.get("id", "")),
            "pronunciation_id": str(pronunciation.get("id", "")),
            "source": term_id == subject_entity_id,
            "confidence": term.get("quality") or provenance.get("confidence"),
            "state": "complete" if meaning else "partial",
            "projection": "live-knowledge-graph",
        }
    return result


def _merge_language_atoms(
    stored: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    result = {code: dict(value) for code, value in stored.items()}
    for code, value in live.items():
        merged = dict(result.get(code, {}))
        merged.update(
            {
                key: item
                for key, item in value.items()
                if item not in (None, "", [], {})
            }
        )
        result[code] = merged
    return result


def renderable_card(card: dict[str, Any]) -> dict[str, Any]:
    """Add deterministic presentation aids to old cards without rewriting history."""

    atoms = _legacy_card_language_atoms(card)
    if atoms:
        _apply_legacy_language_fields(card, atoms)
    chinese = card.get("chinese")
    if isinstance(chinese, dict) and not chinese.get("ruby_tokens"):
        chinese["ruby_tokens"] = chinese_ruby_tokens(str(chinese.get("simplified", "")))
        if "zh" in card.get("languages", {}):
            card["languages"]["zh"]["ruby_tokens"] = list(chinese["ruby_tokens"])
    return card


def chat_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    message = str(payload.get("message", "")).strip()
    if not message:
        raise ValueError("enter a chat message")
    if len(message) > 2000:
        raise ValueError("chat message is too long")
    history = payload.get("history", [])
    if not isinstance(history, list):
        raise ValueError("chat history must be a list")
    messages: list[dict[str, str]] = []
    for item in history[-10:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = str(item.get("content", "")).strip()[:4000]
        if content:
            messages.append({"role": str(item["role"]), "content": content})
    if sum(len(item["content"]) for item in messages) > 16_000:
        raise ValueError("chat history is too long; clear the conversation")
    messages.append({"role": "user", "content": message})
    return messages


def card_chat_context(card: dict[str, Any]) -> str:
    language_lines = []
    for code, value in _legacy_card_language_atoms(card).items():
        terms = [
            str(value.get(key, "")).strip()
            for key in ("term", "reading", "meaning")
            if str(value.get(key, "")).strip()
        ]
        if terms:
            language_lines.append(
                f"{language_profile(code).label}: {' | '.join(terms[:3])}"
            )
    evidence_lines = []
    evidence = card.get("evidence")
    if isinstance(evidence, list):
        for item in evidence[:2]:
            if not isinstance(item, dict):
                continue
            pages = ", ".join(str(page) for page in item.get("pages", []))
            evidence_lines.append(
                f"Source {item.get('entry_id', '')}"
                f"{f' page {pages}' if pages else ''}: {str(item.get('excerpt', ''))[:1200]}"
            )
    context = "\n".join(
        [
            f"Title: {str(card.get('title', ''))[:300]}",
            f"Summary: {str(card.get('summary_en', ''))[:1200]}",
            f"Explanation: {str(card.get('origin_story', ''))[:1800]}",
            *language_lines,
            *evidence_lines,
        ]
    )
    return context[:8000]


def word_card_preparation_state(
    plan: PreparationPlan, knowledge: KnowledgeStore, mode: str = "knowledge"
) -> dict[str, Any]:
    """Return a small polling contract for one independently prepared view."""

    planned_ids = list(dict.fromkeys(plan.jobs.values()))
    by_id = {
        str(job["job_id"]): job
        for job in knowledge.jobs_for_subject(plan.subject_key)
        if str(job["job_id"]) in planned_ids
    }
    jobs = [by_id[job_id] for job_id in planned_ids if job_id in by_id]
    failed = [job for job in jobs if job["status"] == "failed"]
    completed = sum(job["status"] == "complete" for job in jobs)
    current = next((job for job in jobs if job["status"] == "running"), None)
    if current is None:
        current = next((job for job in jobs if job["status"] == "queued"), None)
    final_job = "compose-word-card" if mode == "knowledge" else "compose-origin-card"
    worker = knowledge.worker_status()
    payload: dict[str, Any] = {
        "status": "failed" if failed else "preparing",
        "mode": mode,
        "subject_entity_id": plan.subject_entity_id,
        "subject_key": plan.subject_key,
        "completed_jobs": completed,
        "total_jobs": len(jobs),
        "current_job": str(current["job_type"]) if current else final_job,
        "current_label": _PREPARATION_LABELS.get(
            str(current["job_type"]) if current else final_job,
            "Preparing accepted knowledge",
        ),
        "poll_after_ms": 3000,
        "worker": worker,
        "generation_ready": worker["generation_ready"],
        "generation_blocker": worker["blocker"],
    }
    if failed:
        payload["error"] = str(failed[0].get("error", ""))[:500] or (
            f"atomic {mode} preparation failed validation"
        )
    return payload


def plan_interactive_word(
    knowledge: KnowledgeStore,
    query: str,
    mode: str,
    model: str,
    *,
    source_language: str = "en",
) -> PreparationPlan:
    """Route every lexical view through the same durable atomic planner."""

    if mode not in _ATOMIC_WORD_MODES:
        raise ValueError(f"{mode!r} is not an atomic word mode")
    prompt_version = (
        "interactive-word-card-v1"
        if mode == "knowledge"
        else "interactive-origin-graph-v4"
    )
    planner = PreparationPlanner(
        knowledge,
        model=model,
        prompt_version=prompt_version,
        priority_offset=_INTERACTIVE_PRIORITY_OFFSET,
    )
    return (
        planner.plan_word_card(query, language=source_language)
        if mode == "knowledge"
        else planner.plan_word(query, language=source_language)
    )


def build_service(settings: Settings) -> tuple[CardService, LlamaCppClient]:
    model = LlamaCppClient(
        settings.llm_url, settings.llm_model, settings.request_timeout
    )
    service = CardService(
        CorpusIndex(settings.corpus_db),
        model,
        CardStore(settings.cards_db),
        settings.max_evidence,
        {
            "answer": CardBookIndex(settings.answers_db),
            "question": CardBookIndex(settings.questions_db),
        },
        {
            "root": MorphologyIndex(settings.roots_db),
            "affix": MorphologyIndex(settings.affixes_db),
        },
    )
    return service, model


def handler_factory(
    settings: Settings, service: CardService, model: LlamaCppClient
) -> type[BaseHTTPRequestHandler]:
    knowledge = KnowledgeStore(settings.knowledge_db, serving=True)
    projection_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
    projection_inflight: dict[tuple[Any, ...], Event] = {}
    projection_cache_lock = RLock()
    projection_work_slots = BoundedSemaphore(_PROJECTION_WORKERS)

    class ProjectionBusy(RuntimeError):
        pass

    def knowledge_projection_generation() -> str:
        """Invalidate projections only when renderable knowledge advances."""

        return knowledge.projection_generation()

    def cached_lexical_subgraph(
        subject_id: str,
        mode: str,
        limits: dict[str, int | str],
        generation: int,
    ) -> dict[str, Any]:
        """Share one live projection across concurrent requests for the same view."""

        cache_key = (
            _LEXICAL_PROJECTION_VERSION,
            generation,
            subject_id,
            mode,
            tuple(sorted(limits.items())),
        )
        owner = False
        with projection_cache_lock:
            graph = projection_cache.get(cache_key)
            if graph is not None:
                projection_cache.move_to_end(cache_key)
                return graph
            ready = projection_inflight.get(cache_key)
            if ready is None:
                ready = Event()
                projection_inflight[cache_key] = ready
                owner = True
        if not owner:
            if not ready.wait(timeout=30):
                raise TimeoutError("live lexical projection timed out")
            with projection_cache_lock:
                graph = projection_cache.get(cache_key)
                if graph is None:
                    raise OSError("live lexical projection failed")
                projection_cache.move_to_end(cache_key)
                return graph

        try:
            graph = dict(knowledge.lexical_subgraph(subject_id, mode, limits))
            graph["projection_version"] = _LEXICAL_PROJECTION_VERSION
            with projection_cache_lock:
                projection_cache[cache_key] = graph
                projection_cache.move_to_end(cache_key)
                while len(projection_cache) > _LEXICAL_PROJECTION_CACHE_SIZE:
                    projection_cache.popitem(last=False)
            return graph
        finally:
            with projection_cache_lock:
                projection_inflight.pop(cache_key, None)
                ready.set()

    def lexical_subject_id(
        extensions: dict[str, Any], previous: dict[str, Any]
    ) -> str:
        subject = extensions.get("knowledge_subject", "")
        if isinstance(subject, dict):
            subject = subject.get("entity_id", subject.get("subject_entity_id", ""))
        subject_id = str(subject).strip()
        if ":" in subject_id:
            prefix, candidate = subject_id.split(":", 1)
            if prefix and candidate:
                subject_id = candidate.strip()
        return (
            subject_id
            or str(previous.get("subject_entity_id", "")).strip()
        )[:200]

    def lexical_branch_entity_id(
        mode: str,
        extensions: dict[str, Any],
        previous: dict[str, Any],
    ) -> str:
        """Resolve a presentation focus id to its canonical morpheme entity."""

        requested = str(
            extensions.get("branch_focus_id")
            or previous.get("branch_focus_id")
            or ""
        ).strip()
        morphology_graph = extensions.get("morphology_graph")
        morphology_graph = (
            morphology_graph if isinstance(morphology_graph, dict) else {}
        )
        graph_nodes = morphology_graph.get("nodes", [])
        node_by_id = {
            str(node.get("id", "")): node
            for node in graph_nodes
            if isinstance(node, dict) and str(node.get("id", "")).strip()
        }
        expected_kinds = {"root"} if mode == "root" else {"prefix", "suffix"}

        focus_areas = morphology_graph.get("focus_areas", [])
        if isinstance(focus_areas, list):
            for focus in focus_areas:
                if not isinstance(focus, dict):
                    continue
                if requested and str(focus.get("id", "")) != requested:
                    continue
                for node_id in focus.get("node_ids", []):
                    candidate = str(node_id).strip()
                    if str(node_by_id.get(candidate, {}).get("type", "")) in expected_kinds:
                        return candidate[:200]

        focus_entity_ids = previous.get("focus_entity_ids", [])
        if isinstance(focus_entity_ids, list):
            for entity_id in focus_entity_ids:
                candidate = str(entity_id).strip()
                if candidate.startswith("morpheme-"):
                    return candidate[:200]
                if str(node_by_id.get(candidate, {}).get("type", "")) in expected_kinds:
                    return candidate[:200]
        if requested.startswith("morpheme-"):
            return requested[:200]
        return ""

    def lexical_projection_limits(
        mode: str, previous: dict[str, Any], branch_entity_id: str
    ) -> dict[str, int | str]:
        stored = previous.get("limits", {})
        stored = stored if isinstance(stored, dict) else {}

        def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(stored.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        limits: dict[str, int | str] = {
            "nodes": bounded("nodes", 32, 1, 64),
            "edges": bounded("edges", 48, 0, 96),
            "depth": bounded("depth", 4 if mode == "word" else 3, 0, 6),
        }
        if branch_entity_id:
            limits["branch_focus_id"] = branch_entity_id
        return limits

    def live_lexical_card(
        card: dict[str, Any], generation: str | None = None
    ) -> dict[str, Any]:
        """Overlay the current bounded graph without mutating stored card history."""

        mode = str(card.get("mode", "")).strip()
        if mode not in _LIVE_LEXICAL_MODES:
            return card
        extensions = card.get("extensions")
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        previous = extensions.get("lexical_view")
        previous = dict(previous) if isinstance(previous, dict) else {}
        subject_id = lexical_subject_id(extensions, previous)
        branch_entity_id = lexical_branch_entity_id(mode, extensions, previous)
        limits = lexical_projection_limits(mode, previous, branch_entity_id)
        marker: dict[str, Any] = {
            "version": _LEXICAL_PROJECTION_VERSION,
            "status": "fallback",
        }
        if not subject_id:
            marker["reason"] = "missing-knowledge-subject"
            extensions["lexical_projection"] = marker
            card["extensions"] = extensions
            return card

        generation = (
            knowledge_projection_generation() if generation is None else generation
        )
        try:
            graph = cached_lexical_subgraph(subject_id, mode, limits, generation)
            extensions["lexical_view"] = graph
            extensions["lexical_projection"] = {
                "version": _LEXICAL_PROJECTION_VERSION,
                "status": "live",
                "graph_revision": graph.get("graph_revision", 0),
                "projection_hash": graph.get("projection_hash", ""),
            }
            if mode in {"knowledge", "word"}:
                stored_atoms = _legacy_card_language_atoms(card)
                live_atoms = project_language_atoms(knowledge, subject_id)
                if live_atoms:
                    _apply_legacy_language_fields(
                        card, _merge_language_atoms(stored_atoms, live_atoms)
                    )
                    extensions["language_projection"] = {
                        "status": "live",
                        "graph_revision": graph.get("graph_revision", 0),
                        "languages": list(live_atoms),
                    }
            support_forms = tuple(
                str(node.get("label", "")).strip()
                for node in graph.get("nodes", [])
                if isinstance(node, dict)
                and str(node.get("type", ""))
                not in {"language", "period", "history-event"}
                and str(node.get("label", "")).strip()
            )
            filtered = filter_card_evidence(card, support_forms=support_forms)
            card["evidence"] = filtered.get("evidence", card.get("evidence", []))
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
            LOG.warning(
                "Live lexical projection unavailable for card %s: %s",
                str(card.get("card_id", "")),
                exc,
            )
            marker["reason"] = "knowledge-unavailable"
            extensions["lexical_projection"] = marker
        card["extensions"] = extensions
        return card

    def _acquired_card_unbounded(
        card: dict[str, Any], generation: str | None = None
    ) -> dict[str, Any]:
        rendered = live_lexical_card(renderable_card(card), generation)
        source = knowledge.content_for_card(str(rendered.get("card_id", "")), "en")
        if source is None:
            return rendered
        extensions = rendered.get("extensions")
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        extensions["source_content_entity_id"] = source["entity_id"]
        extensions["investigation_terms"] = knowledge.investigation_terms(
            source["entity_id"]
        )
        grammar_analyses: dict[str, dict[str, Any]] = {}
        for language in ("en", "ja", "zh"):
            content = knowledge.content_for_card(
                str(rendered.get("card_id", "")), language
            )
            if content is None:
                continue
            analysis = knowledge.grammar_for_content(str(content["entity_id"]))
            if analysis is None:
                continue
            grammar_analyses[language] = {
                **analysis,
                "source_entity_id": content["entity_id"],
                "source_text": content["text"],
            }
        if grammar_analyses:
            extensions["grammar_analyses"] = grammar_analyses
        rendered["extensions"] = extensions
        return rendered

    def acquired_card(
        card: dict[str, Any], generation: str | None = None
    ) -> dict[str, Any]:
        if not projection_work_slots.acquire(timeout=1.0):
            raise ProjectionBusy("live card hydration is busy")
        try:
            return _acquired_card_unbounded(card, generation)
        finally:
            projection_work_slots.release()

    def requested_lexical_subgraph(
        entity_id: str, mode: str, limits: dict[str, int]
    ) -> dict[str, Any]:
        if not projection_work_slots.acquire(timeout=1.0):
            raise ProjectionBusy("live graph projection is busy")
        try:
            return knowledge.lexical_subgraph(entity_id, mode, limits)
        finally:
            projection_work_slots.release()

    def deck_headers(
        mode: str, *, limit: int = 0, offset: int = 0, returned: int = 0,
        generation: str | None = None,
    ) -> dict[str, str]:
        info = service.store.accepted_deck_info(mode)
        headers = {
            "X-Total-Count": str(info["total"]),
            "X-Deck-Revision": str(info["revision"]),
            "X-Knowledge-Generation": str(
                knowledge_projection_generation() if generation is None else generation
            ),
        }
        if limit:
            headers.update(
                {
                    "X-Page-Limit": str(limit),
                    "X-Page-Offset": str(offset),
                    "X-Has-More": (
                        "true" if offset + returned < int(info["total"]) else "false"
                    ),
                }
            )
        return headers

    health_cache_lock = RLock()
    health_refreshing = False
    health_last_attempt = 0.0
    health_collected_at: float | None = None
    health_refresh_seconds: float | None = None
    health_last_refresh_failed = False
    health_snapshot: dict[str, Any] = {
        "status": "starting",
        "corpus": {"ready": False, "entries": 0, "sha256": ""},
        "card_books": {},
        "morphology": {},
        "lexicons": {},
        "autonomous_deck": {
            "ready": False,
            "accepted": 0,
            "total": 0,
            "remaining": 0,
            "complete": False,
            "modes": {},
        },
        "autonomous_lexical": {
            "ready": False,
            "planned": 0,
            "complete": False,
        },
        "autonomous_morphology": {
            "ready": False,
            "planned": 0,
            "complete": False,
        },
        "knowledge": {},
        "worker": {
            "generation_ready": False,
            "blocker": "Refreshing local source status",
        },
        "generation": {
            "ready": False,
            "blocker": "Refreshing local source status",
        },
        "model": {"ready": False, "name": model.model_name, "local": True},
        "outputs": {"web": "ready", "eink": "reserved", "audio": "reserved"},
    }

    def collect_health_snapshot() -> dict[str, Any]:
        """Build the detailed health report in the sole background refresher."""

        model_ready = model.health()
        try:
            count = service.corpus.count()
            metadata = service.corpus.metadata()
            corpus_ready = True
        except (FileNotFoundError, OSError):
            count, metadata, corpus_ready = 0, {}, False
        card_books: dict[str, Any] = {}
        for mode, index in service.card_books.items():
            try:
                book_metadata = index.metadata()
                card_books[mode] = {
                    "ready": True,
                    "items": index.count(),
                    "title": book_metadata.get("source_title", ""),
                    "sha256": book_metadata.get("source_sha256", ""),
                }
            except (FileNotFoundError, OSError):
                card_books[mode] = {"ready": False, "items": 0}
        morphology: dict[str, Any] = {}
        for kind, index in service.morphology.items():
            try:
                morphology_metadata = index.metadata()
                morphology[kind] = {
                    "ready": True,
                    "items": index.count(),
                    "title": morphology_metadata.get("source_title", ""),
                    "sha256": morphology_metadata.get("source_sha256", ""),
                }
            except (FileNotFoundError, OSError):
                morphology[kind] = {"ready": False, "items": 0}
        lexicons = correction_source_status(settings)
        deck = autonomous_deck_status(service, knowledge)
        lexical = autonomous_lexical_status(service, knowledge)
        morphology_growth = autonomous_morphology_status(service, knowledge)
        worker = knowledge.worker_status()
        sources_ready = (
            corpus_ready
            and all(item.get("ready") for item in card_books.values())
            and all(item.get("ready") for item in morphology.values())
            and all(item.get("ready") for item in lexicons.values())
            and deck.get("ready") is True
            and lexical.get("ready") is True
            and morphology_growth.get("ready") is True
        )
        if not sources_ready:
            generation_blocker = "Local retrieval sources are starting"
        elif not model_ready:
            generation_blocker = "Local Qwen is starting"
        else:
            generation_blocker = str(worker["blocker"])
        return {
            "status": "ready" if sources_ready and model_ready else "starting",
            "corpus": {
                "ready": corpus_ready,
                "entries": count,
                "sha256": metadata.get("source_sha256", ""),
            },
            "card_books": card_books,
            "morphology": morphology,
            "lexicons": lexicons,
            "autonomous_deck": deck,
            "autonomous_lexical": lexical,
            "autonomous_morphology": morphology_growth,
            "knowledge": knowledge.status(),
            "worker": worker,
            "generation": {
                "ready": bool(
                    sources_ready and model_ready and worker["generation_ready"]
                ),
                "blocker": generation_blocker,
            },
            "model": {"ready": model_ready, "name": model.model_name, "local": True},
            "outputs": {"web": "ready", "eink": "reserved", "audio": "reserved"},
        }

    def refresh_health_snapshot() -> None:
        nonlocal health_last_attempt, health_refreshing, health_snapshot
        nonlocal health_collected_at, health_refresh_seconds, health_last_refresh_failed
        started = monotonic()
        try:
            refreshed = collect_health_snapshot()
        except Exception:  # Background boundary must preserve the last good response.
            LOG.exception("Health snapshot refresh failed")
            with health_cache_lock:
                health_last_refresh_failed = True
        else:
            with health_cache_lock:
                health_snapshot = refreshed
                health_refresh_seconds = monotonic() - started
                health_collected_at = monotonic()
                health_last_refresh_failed = False
        finally:
            with health_cache_lock:
                health_last_attempt = monotonic()
                health_refreshing = False

    def current_health_snapshot() -> dict[str, Any]:
        nonlocal health_refreshing
        with health_cache_lock:
            refresh_interval = health_statistics_refresh_interval(
                health_refresh_seconds,
                last_refresh_failed=health_last_refresh_failed,
            )
            stale = monotonic() - health_last_attempt >= refresh_interval
            if stale and not health_refreshing:
                health_refreshing = True
                Thread(
                    target=refresh_health_snapshot,
                    name="lkt-health-refresh",
                    daemon=True,
                ).start()
            snapshot = dict(health_snapshot)
            age = (
                max(0.0, monotonic() - health_collected_at)
                if health_collected_at is not None else None
            )
            snapshot["statistics_cache"] = {
                "age_seconds": round(age, 3) if age is not None else None,
                "ttl_seconds": refresh_interval,
                "stale": bool(
                    age is None or age >= refresh_interval
                    or health_last_refresh_failed
                ),
                "refreshing": health_refreshing,
                "refresh_seconds": health_refresh_seconds,
                "last_refresh_failed": health_last_refresh_failed,
            }
        # Never hold the shared cache lock during database reads, and never
        # freeze heartbeat age or a stopped/running transition inside it.
        return live_health_response(snapshot, knowledge, service.store)

    def investigation_context(
        payload: dict[str, Any], query: str, requested_mode: str
    ) -> dict[str, Any] | None:
        source_card_id = str(payload.get("source_card_id", "")).strip()[:100]
        if not source_card_id:
            return None
        if requested_mode != "knowledge":
            raise ValueError("a linked card investigation must open Word Card mode")
        source = knowledge.content_for_card(source_card_id, "en")
        if source is None:
            raise ValueError("the source card has no acquired English content")
        selected = next(
            (
                item
                for item in knowledge.investigation_terms(source["entity_id"])
                if str(item.get("term", "")).casefold() == query.strip().casefold()
            ),
            None,
        )
        if selected is None:
            raise ValueError("the selected word is not an accepted investigation term")
        return {
            "source_card_id": source_card_id,
            "source_entity_id": source["entity_id"],
            "result_entity_id": selected["entity_id"],
            "selected_text": selected.get("surface", selected["term"]),
        }

    def record_card_investigation(
        card: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        rendered = acquired_card(card)
        if context is None:
            return rendered
        selected_text = str(context["selected_text"])
        thread_id = knowledge.create_inquiry_thread(f"Investigate: {selected_text}")
        event_id = knowledge.save_inquiry_event(
            thread_id,
            f"Open Word Card for {selected_text}",
            response=f"Opened Word Card {rendered.get('card_id', '')}",
            source_entity_id=str(context["source_entity_id"]),
            result_entity_id=str(context["result_entity_id"]),
            card_id=str(context["source_card_id"]),
            selected_text=selected_text,
            model=str(rendered.get("model", "")),
        )
        extensions = dict(rendered.get("extensions") or {})
        extensions["inquiry"] = {
            "thread_id": thread_id,
            "event_id": event_id,
            "source_card_id": context["source_card_id"],
            "source_entity_id": context["source_entity_id"],
            "result_entity_id": context["result_entity_id"],
        }
        rendered["extensions"] = extensions
        return rendered

    class Handler(BaseHTTPRequestHandler):
        server_version = "LKT/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOG.info("%s - %s", self.address_string(), format % args)

        def _headers(
            self,
            status: int,
            content_type: str,
            length: int,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'",
            )
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _body(
            self,
            body: bytes,
            status: int,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.close_connection = True
            try:
                self._headers(status, content_type, len(body), extra_headers)
                self.wfile.write(body)
            except OSError:
                LOG.debug("Client disconnected before response completed")

        def _json(
            self,
            value: Any,
            status: int = 200,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._body(
                body,
                status,
                "application/json; charset=utf-8",
                extra_headers,
            )

        def _asset(self, name: str) -> None:
            allowed = {
                "index.html": STATIC_DIR / "index.html",
                "favicon.svg": STATIC_DIR / "favicon.svg",
                "app.css": STATIC_DIR / "app.css",
                "app.js": STATIC_DIR / "app.js",
                "cytoscape-3.34.0.min.js": (
                    STATIC_DIR / "vendor" / "cytoscape-3.34.0.min.js"
                ),
            }
            path = allowed.get(name)
            if path is None:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if not path.is_file():
                self._json({"error": "asset missing"}, HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or "javascript" in content_type:
                content_type += "; charset=utf-8"
            self._body(body, HTTPStatus.OK, content_type)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._asset("index.html")
                return
            if parsed.path == "/assets/favicon.svg":
                self._asset("favicon.svg")
                return
            if parsed.path == "/assets/app.css":
                self._asset("app.css")
                return
            if parsed.path == "/assets/app.js":
                self._asset("app.js")
                return
            if parsed.path == "/assets/vendor/cytoscape-3.34.0.min.js":
                self._asset("cytoscape-3.34.0.min.js")
                return
            if parsed.path == "/api/graph/search":
                parameters = parse_qs(parsed.query)
                query = str(parameters.get("q", [""])[0]).strip()
                mode = str(parameters.get("mode", ["word"])[0]).strip().lower()
                try:
                    limit = int(parameters.get("limit", ["20"])[0])
                    entities = knowledge.search_lexical_entities(
                        query, limit=limit, mode=mode
                    )
                except (TypeError, ValueError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "query": query,
                        "mode": mode,
                        "preferred_entity_id": (
                            entities[0]["entity_id"] if entities else ""
                        ),
                        "entities": entities,
                        "knowledge": knowledge.status(),
                    }
                )
                return
            if parsed.path == "/api/graph":
                parameters = parse_qs(parsed.query)
                entity_id = str(parameters.get("entity_id", [""])[0]).strip()
                mode = str(parameters.get("mode", ["word"])[0]).strip()
                if not entity_id:
                    self._json(
                        {"error": "entity_id is required"}, HTTPStatus.BAD_REQUEST
                    )
                    return
                if mode not in {"knowledge", "word", "root", "affix"}:
                    self._json({"error": "invalid graph mode"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    limits = {
                        "nodes": max(
                            1, min(int(parameters.get("nodes", ["64"])[0]), 128)
                        ),
                        "edges": max(
                            1, min(int(parameters.get("edges", ["128"])[0]), 256)
                        ),
                        "depth": max(
                            1, min(int(parameters.get("depth", ["4"])[0]), 6)
                        ),
                    }
                    graph = requested_lexical_subgraph(entity_id, mode, limits)
                except (KeyError, TypeError, ValueError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                except ProjectionBusy as exc:
                    self._json(
                        {"error": str(exc)},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"Retry-After": "1"},
                    )
                    return
                self._json(graph)
                return
            if parsed.path == "/api/health":
                self._json(current_health_snapshot())
                return
            if parsed.path == "/api/search":
                parameters = parse_qs(parsed.query)
                query = parameters.get("q", [""])[0]
                corpus = parameters.get("corpus", ["word-origins"])[0]
                try:
                    if corpus == "word-origins":
                        results = service.corpus.search(query, settings.max_evidence)
                    elif corpus in {"answer", "question"}:
                        results = service.card_books[corpus].search(
                            query, settings.max_evidence
                        )
                    elif corpus in {"root", "affix"}:
                        results = service.morphology[corpus].search(
                            query, settings.max_evidence
                        )
                    else:
                        raise ValueError("unknown corpus")
                    self._json([item.to_dict() for item in results])
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except FileNotFoundError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/api/cards":
                parameters = parse_qs(parsed.query)
                limit = parameters.get("limit", ["12"])[0]
                mode = parameters.get("mode", [""])[0]
                offset = parameters.get("offset", ["0"])[0]
                try:
                    parsed_limit = max(
                        1, min(int(limit), _CARD_SUMMARY_PAGE_SIZE)
                    )
                    parsed_offset = max(0, int(offset))
                except (TypeError, ValueError):
                    parsed_limit, parsed_offset = 12, 0
                cards = service.store.recent_summaries(
                    parsed_limit, mode, parsed_offset
                )
                self._json(
                    cards,
                    extra_headers=deck_headers(
                        mode,
                        limit=parsed_limit,
                        offset=parsed_offset,
                        returned=len(cards),
                    ),
                )
                return
            if parsed.path == "/api/cards/random":
                parameters = parse_qs(parsed.query)
                mode = str(parameters.get("mode", [""])[0]).strip()
                excluded = tuple(
                    item.strip()[:100]
                    for item in str(
                        parameters.get("exclude", [""])[0]
                    ).split(",")[:24]
                    if item.strip()
                )
                try:
                    generation = knowledge_projection_generation()
                    rendered = acquire_random_renderable_card(
                        service.store,
                        mode,
                        excluded,
                        lambda card: acquired_card(card, generation),
                    )
                except ProjectionBusy as exc:
                    self._json(
                        {"error": str(exc)},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"Retry-After": "1"},
                    )
                    return
                if rendered is None:
                    self._json(
                        {"error": "accepted renderable card not found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json(
                    rendered,
                    extra_headers=deck_headers(mode, generation=generation),
                )
                return
            if parsed.path.startswith("/api/cards/"):
                card_id = parsed.path[len("/api/cards/"):].strip()[:100]
                card_parameters = parse_qs(parsed.query)
                if card_parameters.get("revision", [""])[0] == "1":
                    card = service.store.get_published_revision(card_id)
                else:
                    card = service.store.get_accepted(card_id)
                if card is None:
                    self._json({"error": "card not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    generation = knowledge_projection_generation()
                    rendered = acquired_card(card, generation)
                except ProjectionBusy as exc:
                    self._json(
                        {"error": str(exc)},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"Retry-After": "1"},
                    )
                    return
                if (
                    card_parameters.get("rotation", [""])[0] == "1"
                    and not live_card_rotation_eligible(rendered)
                ):
                    self._json(
                        {"error": "card is not currently rotation eligible"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                self._json(
                    rendered,
                    extra_headers=deck_headers(
                        str(card.get("mode", "")), generation=generation
                    ),
                )
                return
            if parsed.path == "/api/observations":
                limit = parse_qs(parsed.query).get("limit", ["12"])[0]
                try:
                    parsed_limit = int(limit)
                except ValueError:
                    parsed_limit = 12
                self._json(service.store.recent_observations(parsed_limit))
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            prefix = "/api/cards/"
            if not path.startswith(prefix):
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            card_id = path[len(prefix):].strip()[:100]
            if not card_id or not service.store.archive(card_id):
                self._json({"error": "card not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"card_id": card_id, "status": "archived"})

        def do_PATCH(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            prefix = "/api/cards/"
            if not path.startswith(prefix):
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            card_id = path[len(prefix):].strip()[:100]
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 131_072:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict) or not isinstance(body.get("patch"), dict):
                    raise ValueError("revision requires a JSON object in patch")
                revised = service.store.revise(
                    card_id,
                    body["patch"],
                    str(body.get("review_note", "")),
                    body.get("quality_score"),
                )
                knowledge.acquire_card_book_card(revised)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except KeyError:
                self._json({"error": "card not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                rendered = acquired_card(revised)
            except ProjectionBusy as exc:
                self._json(
                    {"error": str(exc)},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"Retry-After": "1"},
                )
                return
            self._json(rendered, HTTPStatus.CREATED)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in {"/api/cards", "/api/chat", "/api/intent"}:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 32_768:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be a JSON object")
                if path == "/api/intent":
                    self._json(route_intent(payload.get("query")).to_dict())
                    return
                if path == "/api/chat":
                    messages = chat_messages(payload)
                    context_card_id = str(payload.get("card_id", "")).strip()[:100]
                    context_card = (
                        service.store.get(context_card_id) if context_card_id else None
                    )
                    if context_card_id and context_card is None:
                        raise ValueError("current card was not found")
                    result = model.chat(
                        messages,
                        card_chat_context(context_card) if context_card else "",
                    )
                    observation = service.store.save_observation(
                        messages[-1]["content"],
                        result["message"],
                        result["model"],
                        result["metrics"],
                        context_card_id=context_card_id,
                    )
                    requested_thread_id = str(
                        payload.get("thread_id", "")
                    ).strip()[:100]
                    thread_id = (
                        requested_thread_id
                        if knowledge.has_inquiry_thread(requested_thread_id)
                        else knowledge.create_inquiry_thread(
                            (
                                f"Discuss: {str(context_card.get('title', ''))[:160]}"
                                if context_card
                                else f"Model Lab: {messages[-1]['content'][:160]}"
                            )
                        )
                    )
                    acquired = (
                        knowledge.acquire_card_book_card(context_card)
                        if context_card
                        else {}
                    )
                    event_id = knowledge.save_inquiry_event(
                        thread_id,
                        messages[-1]["content"],
                        response=result["message"],
                        parent_event_id=(
                            str(payload.get("parent_event_id", "")).strip()[:100]
                            or None
                        ),
                        source_entity_id=acquired.get("source_entity_id"),
                        card_id=context_card_id,
                        model=result["model"],
                    )
                    result["observation_id"] = observation["observation_id"]
                    result["thread_id"] = thread_id
                    result["event_id"] = event_id
                    result["created_at"] = observation["created_at"]
                    result["context_card_id"] = context_card_id
                    result["context_title"] = (
                        str(context_card.get("title", "")) if context_card else ""
                    )
                    self._json(result, HTTPStatus.CREATED)
                    return
                query = str(payload.get("query", ""))
                requested_mode = str(payload.get("mode", "word"))
                linked_context = investigation_context(payload, query, requested_mode)
                if payload.get("refresh") is not True:
                    established = service.store.find_active(requested_mode, query)
                    if established is not None:
                        knowledge.acquire_card_book_card(established)
                        self._json(record_card_investigation(established, linked_context))
                        return
                if requested_mode in _ATOMIC_WORD_MODES:
                    source_language = normalize_language_code(
                        payload.get("source_language", payload.get("language", "en"))
                    )
                    plan = plan_interactive_word(
                        knowledge,
                        query,
                        requested_mode,
                        settings.llm_model,
                        source_language=source_language,
                    )
                    retried_jobs = (
                        knowledge.requeue_failed_jobs(plan.jobs.values())
                        if payload.get("retry_failed") is True
                        else 0
                    )
                    preparation = word_card_preparation_state(
                        plan, knowledge, requested_mode
                    )
                    preparation["retried_jobs"] = retried_jobs
                    preparation["query"] = query.strip()
                    preparation["source_language"] = source_language
                    if linked_context is not None:
                        preparation.update(
                            {
                                "source_card_id": linked_context["source_card_id"],
                                "source_entity_id": linked_context["source_entity_id"],
                                "result_entity_id": linked_context["result_entity_id"],
                            }
                        )
                    if (
                        preparation["completed_jobs"] == preparation["total_jobs"]
                        and preparation["status"] != "failed"
                    ):
                        completed_card = service.store.find_active(
                            requested_mode, query
                        )
                        if completed_card is not None:
                            self._json(
                                record_card_investigation(
                                    completed_card, linked_context
                                )
                            )
                            return
                        preparation["status"] = "failed"
                        preparation["error"] = (
                            f"no accepted {requested_mode} view was produced for "
                            f"{query.strip()!r}"
                        )
                    self._json(
                        preparation,
                        (
                            HTTPStatus.UNPROCESSABLE_ENTITY
                            if preparation["status"] == "failed"
                            else HTTPStatus.ACCEPTED
                        ),
                    )
                    return
                card = service.create(query, requested_mode)
                knowledge.acquire_card_book_card(card.to_dict())
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except NoEvidence as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            except ModelUnavailable as exc:
                self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            except ProjectionBusy as exc:
                self._json(
                    {"error": str(exc)},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"Retry-After": "1"},
                )
                return
            try:
                rendered = record_card_investigation(card.to_dict(), linked_context)
            except ProjectionBusy as exc:
                self._json(
                    {"error": str(exc)},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"Retry-After": "1"},
                )
                return
            self._json(rendered, HTTPStatus.CREATED)

    current_health_snapshot()
    return Handler


class ResponsiveThreadingHTTPServer(ThreadingHTTPServer):
    """Close idle clients promptly while keeping control requests independent."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(20)
        try:
            super().finish_request(request, client_address)
        except OSError:
            LOG.debug("Client connection closed during request")


def run(settings: Settings | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    service, model = build_service(settings)
    server = ResponsiveThreadingHTTPServer(
        (settings.host, settings.port), handler_factory(settings, service, model)
    )
    LOG.info("Local Knowledge Terminal listening on http://%s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
