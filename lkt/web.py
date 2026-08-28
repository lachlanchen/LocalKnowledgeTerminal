from __future__ import annotations

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .corpus import CorpusIndex
from .llm import LlamaCppClient, ModelUnavailable
from .service import CardService, NoEvidence
from .store import CardStore


LOG = logging.getLogger("lkt.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_service(settings: Settings) -> tuple[CardService, LlamaCppClient]:
    model = LlamaCppClient(
        settings.llm_url, settings.llm_model, settings.request_timeout
    )
    service = CardService(
        CorpusIndex(settings.corpus_db),
        model,
        CardStore(settings.cards_db),
        settings.max_evidence,
    )
    return service, model


def handler_factory(
    settings: Settings, service: CardService, model: LlamaCppClient
) -> type[BaseHTTPRequestHandler]:
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
            allowed = {"index.html", "app.css", "app.js"}
            if name not in allowed:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            path = STATIC_DIR / name
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
            if parsed.path == "/api/health":
                try:
                    count = service.corpus.count()
                    metadata = service.corpus.metadata()
                    corpus_ready = True
                except (FileNotFoundError, OSError):
                    count, metadata, corpus_ready = 0, {}, False
                self._json(
                    {
                        "status": "ready" if corpus_ready and model.health() else "starting",
                        "corpus": {
                            "ready": corpus_ready,
                            "entries": count,
                            "sha256": metadata.get("source_sha256", ""),
                        },
                        "model": {
                            "ready": model.health(),
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
                query = parse_qs(parsed.query).get("q", [""])[0]
                try:
                    results = service.corpus.search(query, settings.max_evidence)
                    self._json([item.to_dict() for item in results])
                except FileNotFoundError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/api/cards":
                limit = parse_qs(parsed.query).get("limit", ["12"])[0]
                try:
                    parsed_limit = int(limit)
                except ValueError:
                    parsed_limit = 12
                self._json(service.store.recent(parsed_limit))
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/cards":
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 16_384:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be a JSON object")
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
