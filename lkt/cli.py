from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .card_books import CardBookIndex, build_card_book_index
from .config import Settings
from .corpus import CorpusIndex, build_index
from .graph import rebuild_ladybug
from .llm import LlamaCppClient
from .knowledge import KnowledgeStore
from .lexicon import WordnetRag
from .morphology import MorphologyIndex, build_morphology_index
from .preparation import DISPLAY_LANGUAGES, PreparationPlanner
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
        morphology={
            "root": MorphologyIndex(settings.roots_db),
            "affix": MorphologyIndex(settings.affixes_db),
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
        "root": MorphologyIndex(settings.roots_db),
        "affix": MorphologyIndex(settings.affixes_db),
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


def command_ingest_morphology(args: argparse.Namespace) -> int:
    settings = _settings()
    defaults = {
        "root": (
            settings.roots_db,
            "english-root-dictionary-jin-pdf2tex",
            "New Oriental English Root Dictionary",
        ),
        "affix": (
            settings.affixes_db,
            "english-affix-dictionary-jin-pdf2tex",
            "English Affix Dictionary",
        ),
    }
    default_database, default_corpus_id, default_title = defaults[args.kind]
    destination = Path(args.database).resolve() if args.database else default_database

    def progress(count: int) -> None:
        print(f"indexed {count} {args.kind} records", flush=True)

    result = build_morphology_index(
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


def command_knowledge_status(_args: argparse.Namespace) -> int:
    settings = _settings()
    print(
        json.dumps(
            KnowledgeStore(settings.knowledge_db).status(),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_rebuild_graph(args: argparse.Namespace) -> int:
    settings = _settings()
    destination = Path(args.database).resolve() if args.database else settings.graph_db
    result = rebuild_ladybug(
        KnowledgeStore(settings.knowledge_db), destination, replace=args.replace
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_lexicon_search(args: argparse.Namespace) -> int:
    settings = _settings()
    results = WordnetRag(settings.data_dir / "lexicons" / "wn").search(
        args.query,
        source_language=args.source,
        target_languages=args.targets,
        limit=args.limit,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def _planner(settings: Settings, args: argparse.Namespace) -> PreparationPlanner:
    return PreparationPlanner(
        KnowledgeStore(settings.knowledge_db),
        model=settings.llm_model,
        prompt_version=args.prompt_version,
        source_fingerprint=args.source_fingerprint,
    )


def command_plan_word(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_word(
        args.query,
        language=args.language,
        display_languages=args.display_languages,
    )
    print(
        json.dumps(
            {
                "subject_entity_id": plan.subject_entity_id,
                "subject_key": plan.subject_key,
                "jobs": planner.store.jobs_for_subject(plan.subject_key),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_plan_content(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_content(
        args.kind,
        args.text,
        language=args.language,
        source_key=args.source_key,
        display_languages=args.display_languages,
    )
    print(
        json.dumps(
            {
                "subject_entity_id": plan.subject_entity_id,
                "subject_key": plan.subject_key,
                "jobs": planner.store.jobs_for_subject(plan.subject_key),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
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

    morphology = commands.add_parser(
        "ingest-morphology", help="build a reviewed Root or Affix FTS index"
    )
    morphology.add_argument("kind", choices=("root", "affix"))
    morphology.add_argument("source", help="path to entries-polished.jsonl")
    morphology.add_argument("--database", help="override destination database")
    morphology.add_argument("--corpus-id", help="stable source identifier")
    morphology.add_argument("--title", help="human-readable evidence title")
    morphology.set_defaults(handler=command_ingest_morphology)

    search = commands.add_parser("search", help="inspect retrieved book evidence")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=4)
    search.add_argument(
        "--corpus",
        choices=("word-origins", "answer", "question", "root", "affix"),
        default="word-origins",
    )
    search.set_defaults(handler=command_search)

    generate = commands.add_parser("generate", help="generate a grounded card")
    generate.add_argument("query")
    generate.add_argument(
        "--mode",
        choices=("word", "knowledge", "answer", "question", "root", "affix"),
        default="word",
    )
    generate.set_defaults(handler=command_generate)

    knowledge_status = commands.add_parser(
        "knowledge-status",
        help="initialise and report the established-knowledge database",
    )
    knowledge_status.set_defaults(handler=command_knowledge_status)

    rebuild_graph = commands.add_parser(
        "rebuild-graph",
        help="rebuild the LadybugDB traversal projection from accepted knowledge",
    )
    rebuild_graph.add_argument("--database", help="override graph destination")
    rebuild_graph.add_argument(
        "--replace", action="store_true", help="replace an existing projection"
    )
    rebuild_graph.set_defaults(handler=command_rebuild_graph)

    lexicon_search = commands.add_parser(
        "lexicon-search",
        help="inspect sense-aligned multilingual WordNet evidence",
    )
    lexicon_search.add_argument("query")
    lexicon_search.add_argument(
        "--source", choices=("en", "ja", "zh", "fr", "ar"), default="en"
    )
    lexicon_search.add_argument(
        "--targets",
        nargs="+",
        choices=("en", "ja", "zh", "fr", "ar"),
        default=("ja", "zh", "fr", "ar"),
    )
    lexicon_search.add_argument("--limit", type=int, default=6)
    lexicon_search.set_defaults(handler=command_lexicon_search)

    plan_word = commands.add_parser(
        "plan-word", help="enqueue small reusable preparation jobs for one word"
    )
    plan_word.add_argument("query")
    plan_word.add_argument("--language", default="en")
    plan_word.add_argument(
        "--display-languages", nargs="+", default=DISPLAY_LANGUAGES
    )
    plan_word.add_argument("--prompt-version", default="atomic-v1")
    plan_word.add_argument("--source-fingerprint", default="")
    plan_word.set_defaults(handler=command_plan_word)

    plan_content = commands.add_parser(
        "plan-content",
        help="enqueue separate language, grammar, and investigation jobs",
    )
    plan_content.add_argument("kind", choices=("answer", "question", "sentence"))
    plan_content.add_argument("text")
    plan_content.add_argument("--language", default="en")
    plan_content.add_argument("--source-key", default="")
    plan_content.add_argument(
        "--display-languages", nargs="+", default=DISPLAY_LANGUAGES
    )
    plan_content.add_argument("--prompt-version", default="atomic-v1")
    plan_content.add_argument("--source-fingerprint", default="")
    plan_content.set_defaults(handler=command_plan_content)

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
