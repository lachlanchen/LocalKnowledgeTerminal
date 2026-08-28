from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .card_books import CardBookIndex, build_card_book_index
from .config import Settings
from .corpus import CorpusIndex, build_index
from .llm import LlamaCppClient
from .service import CardService
from .store import CardStore


def _settings() -> Settings:
    settings = Settings.from_env()
    settings.ensure_runtime_dirs()
    return settings


def _service(settings: Settings) -> CardService:
    return CardService(
        corpus=CorpusIndex(settings.corpus_db),
        model=LlamaCppClient(
            settings.llm_url, settings.llm_model, settings.request_timeout
        ),
        store=CardStore(settings.cards_db),
        max_evidence=settings.max_evidence,
        card_books={
            "answer": CardBookIndex(settings.answers_db),
            "question": CardBookIndex(settings.questions_db),
        },
    )


def command_ingest(args: argparse.Namespace) -> int:
    settings = _settings()
    destination = Path(args.database).resolve() if args.database else settings.corpus_db

    def progress(count: int) -> None:
        print(f"indexed {count} entries", flush=True)

    result = build_index(Path(args.source), destination, progress)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_search(args: argparse.Namespace) -> int:
    settings = _settings()
    indexes = {
        "word-origins": CorpusIndex(settings.corpus_db),
        "answer": CardBookIndex(settings.answers_db),
        "question": CardBookIndex(settings.questions_db),
    }
    evidence = indexes[args.corpus].search(args.query, args.limit)
    print(
        json.dumps([item.to_dict() for item in evidence], ensure_ascii=False, indent=2)
    )
    return 0


def command_ingest_card_book(args: argparse.Namespace) -> int:
    settings = _settings()
    defaults = {
        "answer": (
            settings.answers_db,
            "book-of-answers-paul-card-book",
            "The Book of Answers — Paul edition",
        ),
        "question": (
            settings.questions_db,
            "book-of-questions-stock-card-book",
            "The Book of Questions",
        ),
    }
    default_database, default_corpus_id, default_title = defaults[args.kind]
    destination = Path(args.database).resolve() if args.database else default_database

    def progress(count: int) -> None:
        print(f"indexed {count} {args.kind} cards", flush=True)

    result = build_card_book_index(
        Path(args.source),
        destination,
        args.corpus_id or default_corpus_id,
        args.title or default_title,
        args.kind,
        progress,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_generate(args: argparse.Namespace) -> int:
    settings = _settings()
    card = _service(settings).create(args.query, args.mode)
    print(json.dumps(card.to_dict(), ensure_ascii=False, indent=2))
    return 0


def command_serve(_args: argparse.Namespace) -> int:
    from .web import run

    run(_settings())
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="lkt", description="Local Knowledge Terminal"
    )
    commands = root.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="build the Word Origins FTS index")
    ingest.add_argument("source", help="path to entries.jsonl")
    ingest.add_argument("--database", help="override destination database")
    ingest.set_defaults(handler=command_ingest)

    card_book = commands.add_parser(
        "ingest-card-book", help="build a multilingual Answer or Question index"
    )
    card_book.add_argument("kind", choices=("answer", "question"))
    card_book.add_argument("source", help="path to multilingual-items.jsonl")
    card_book.add_argument("--database", help="override destination database")
    card_book.add_argument("--corpus-id", help="stable source identifier")
    card_book.add_argument("--title", help="human-readable evidence title")
    card_book.set_defaults(handler=command_ingest_card_book)

    search = commands.add_parser("search", help="inspect retrieved book evidence")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=4)
    search.add_argument(
        "--corpus",
        choices=("word-origins", "answer", "question"),
        default="word-origins",
    )
    search.set_defaults(handler=command_search)

    generate = commands.add_parser("generate", help="generate a grounded card")
    generate.add_argument("query")
    generate.add_argument(
        "--mode", choices=("word", "knowledge", "answer", "question"), default="word"
    )
    generate.set_defaults(handler=command_generate)

    serve = commands.add_parser("serve", help="run the GUI and JSON API")
    serve.set_defaults(handler=command_serve)
    return root


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
