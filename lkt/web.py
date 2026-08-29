from __future__ import annotations

import json
import logging
import mimetypes
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .card_books import CardBookIndex
from .config import Settings
from .corpus import CorpusIndex
from .deck import AutonomousDeckSeeder
from .freedict import FreeDictRag
from .intent import route_intent
from .llm import LlamaCppClient, ModelUnavailable
from .knowledge import KnowledgeStore
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
    "split-morphemes": "Separating the word into fixed parts",
    "expand-origin-branches": "Tracing cited root histories",
    "prepare-translation": "Preparing one language",
    "prepare-pronunciation": "Aligning pronunciation",
    "prepare-grammar-properties": "Separating grammar properties",
    "compose-word-card": "Publishing the Word Card",
    "compose-origin-card": "Publishing the origin graph",
}

_ATOMIC_WORD_MODES = {"knowledge", "word", "root", "affix"}


def correction_source_status(settings: Settings) -> dict[str, dict[str, Any]]:
    """Report retrieval sources that constrain model-generated card atoms."""

    try:
        status = FreeDictRag(settings.freedict_db).status()
    except (OSError, ValueError, sqlite3.Error):
        status = {"ready": False, "database": str(settings.freedict_db)}
    return {"freedict_eng_ara": status}


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


def renderable_card(card: dict[str, Any]) -> dict[str, Any]:
    """Add deterministic presentation aids to old cards without rewriting history."""

    chinese = card.get("chinese")
    if isinstance(chinese, dict) and not chinese.get("ruby_tokens"):
        chinese["ruby_tokens"] = chinese_ruby_tokens(str(chinese.get("simplified", "")))
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
    for label, key in (("English", "english"), ("Japanese", "japanese"), ("Chinese", "chinese")):
        value = card.get(key)
        if isinstance(value, dict):
            terms = [str(item).strip() for item in value.values() if isinstance(item, str) and item.strip()]
            if terms:
                language_lines.append(f"{label}: {' | '.join(terms[:4])}")
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
    }
    if failed:
        payload["error"] = str(failed[0].get("error", ""))[:500] or (
            f"atomic {mode} preparation failed validation"
        )
    return payload


def plan_interactive_word(
    knowledge: KnowledgeStore, query: str, mode: str, model: str
) -> PreparationPlan:
    """Route every lexical view through the same durable atomic planner."""

    if mode not in _ATOMIC_WORD_MODES:
        raise ValueError(f"{mode!r} is not an atomic word mode")
    prompt_version = (
        "interactive-word-card-v1"
        if mode == "knowledge"
        else "interactive-origin-graph-v3"
    )
    planner = PreparationPlanner(
        knowledge,
        model=model,
        prompt_version=prompt_version,
    )
    return (
        planner.plan_word_card(query)
        if mode == "knowledge"
        else planner.plan_word(query)
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
    knowledge = KnowledgeStore(settings.knowledge_db)

    def acquired_card(card: dict[str, Any]) -> dict[str, Any]:
        rendered = renderable_card(card)
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

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'",
            )
            self.end_headers()

        def _json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _asset(self, name: str) -> None:
            allowed = {
                "index.html": STATIC_DIR / "index.html",
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
            self._headers(HTTPStatus.OK, content_type, len(body))
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._asset("index.html")
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
            if parsed.path == "/api/health":
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
                sources_ready = (
                    corpus_ready
                    and all(item.get("ready") for item in card_books.values())
                    and all(item.get("ready") for item in morphology.values())
                    and all(item.get("ready") for item in lexicons.values())
                    and deck.get("ready") is True
                )
                self._json(
                    {
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
                        "knowledge": knowledge.status(),
                        "model": {
                            "ready": model_ready,
                            "name": model.model_name,
                            "local": True,
                        },
                        "outputs": {
                            "web": "ready",
                            "eink": "reserved",
                            "audio": "reserved",
                        },
                    }
                )
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
                try:
                    parsed_limit = int(limit)
                except ValueError:
                    parsed_limit = 12
                self._json(
                    [
                        acquired_card(card)
                        for card in service.store.recent(parsed_limit, mode)
                    ]
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
            self._json(acquired_card(revised), HTTPStatus.CREATED)

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
                    plan = plan_interactive_word(
                        knowledge, query, requested_mode, settings.llm_model
                    )
                    preparation = word_card_preparation_state(
                        plan, knowledge, requested_mode
                    )
                    preparation["query"] = query.strip()
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
            self._json(
                record_card_investigation(card.to_dict(), linked_context),
                HTTPStatus.CREATED,
            )

    return Handler


def run(settings: Settings | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    service, model = build_service(settings)
    server = ThreadingHTTPServer(
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
