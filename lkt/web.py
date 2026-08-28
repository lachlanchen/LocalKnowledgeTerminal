from __future__ import annotations

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .card_books import CardBookIndex
from .config import Settings
from .corpus import CorpusIndex
from .intent import route_intent
from .llm import LlamaCppClient, ModelUnavailable
from .knowledge import KnowledgeStore
from .morphology import MorphologyIndex
from .pronunciation import chinese_ruby_tokens
from .service import CardService, NoEvidence
from .store import CardStore


LOG = logging.getLogger("lkt.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"


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
                sources_ready = (
                    corpus_ready
                    and all(item.get("ready") for item in card_books.values())
                    and all(item.get("ready") for item in morphology.values())
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
                limit = parse_qs(parsed.query).get("limit", ["12"])[0]
                try:
                    parsed_limit = int(limit)
                except ValueError:
                    parsed_limit = 12
                self._json(
                    [renderable_card(card) for card in service.store.recent(parsed_limit)]
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
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except KeyError:
                self._json({"error": "card not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(renderable_card(revised), HTTPStatus.CREATED)

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
                    result["observation_id"] = observation["observation_id"]
                    result["created_at"] = observation["created_at"]
                    result["context_card_id"] = context_card_id
                    result["context_title"] = (
                        str(context_card.get("title", "")) if context_card else ""
                    )
                    self._json(result, HTTPStatus.CREATED)
                    return
                card = service.create(str(payload.get("query", "")), str(payload.get("mode", "word")))
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
            self._json(card.to_dict(), HTTPStatus.CREATED)

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
