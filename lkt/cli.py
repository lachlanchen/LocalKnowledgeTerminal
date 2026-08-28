from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    evidence = CorpusIndex(settings.corpus_db).search(args.query, args.limit)
    print(
        json.dumps([item.to_dict() for item in evidence], ensure_ascii=False, indent=2)
    )
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

    search = commands.add_parser("search", help="inspect retrieved book evidence")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=4)
    search.set_defaults(handler=command_search)

    generate = commands.add_parser("generate", help="generate a grounded card")
    generate.add_argument("query")
    generate.add_argument("--mode", choices=("word", "knowledge"), default="word")
    generate.set_defaults(handler=command_generate)

    serve = commands.add_parser("serve", help="run the GUI and JSON API")
    serve.set_defaults(handler=command_serve)
    return root


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
