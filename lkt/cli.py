from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, Callable

from .atomic import build_worker
from .card_books import CardBookIndex, build_card_book_index
from .config import Settings
from .corpus import CorpusIndex, build_index
from .deck import AutonomousDeckSeeder, DeckSeedResult
from .device import background_preparation_blocker
from .graph import rebuild_ladybug
from .freedict import build_freedict_index
from .llm import LlamaCppClient
from .knowledge import KnowledgeStore
from .lexicon import LocalLexiconRag
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


def command_ingest_freedict(args: argparse.Namespace) -> int:
    settings = _settings()
    destination = (
        Path(args.database).resolve() if args.database else settings.freedict_db
    )

    def progress(count: int) -> None:
        print(f"indexed {count} FreeDict pairs", flush=True)

    result = build_freedict_index(Path(args.source), destination, progress)
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


def command_sync_card_knowledge(_args: argparse.Namespace) -> int:
    """Backfill reviewed cards and queue their independent local enrichment."""

    settings = _settings()
    knowledge = KnowledgeStore(settings.knowledge_db)
    cards = CardStore(settings.cards_db).accepted_for_modes(("answer", "question"))
    planner = PreparationPlanner(
        knowledge,
        model=settings.llm_model,
        prompt_version="autonomous-content-enrichment-v2",
    )
    acquired = []
    jobs: set[str] = set()
    for card in reversed(cards):
        acquired.append(knowledge.acquire_card_book_card(card))
        jobs.update(
            planner.plan_card_enrichment(
                str(card["card_id"]), include_investigation=False
            ).jobs.values()
        )
    print(
        json.dumps(
            {
                "cards": len(acquired),
                "language_atoms": sum(
                    len(item.get("language_entity_ids", {})) for item in acquired
                ),
                "enrichment_jobs": len(jobs),
                "knowledge": knowledge.status(),
            },
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
    results = LocalLexiconRag(
        settings.data_dir / "lexicons" / "wn", settings.freedict_db
    ).search(
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


def command_plan_translation(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_translation(
        args.query,
        args.target_language,
        source_language=args.source_language,
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


def command_plan_language(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_language(
        args.query,
        args.target_language,
        source_language=args.source_language,
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


def command_plan_word_card_view(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_word_card_view(
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


def command_plan_evidence(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_evidence(args.query, language=args.language)
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


def command_plan_morphemes(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_morphemes(args.query, language=args.language)
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


def command_plan_origin(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_origin(args.query, language=args.language)
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


def command_plan_origin_card(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_origin_card(args.query, language=args.language)
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


def command_retire_morphemes(args: argparse.Namespace) -> int:
    settings = _settings()
    store = KnowledgeStore(settings.knowledge_db)
    term_id = store.upsert_term(args.language, args.query, status="draft")
    print(
        json.dumps(
            store.retire_morpheme_analysis(term_id, args.reason),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_retire_language(args: argparse.Namespace) -> int:
    settings = _settings()
    store = KnowledgeStore(settings.knowledge_db)
    term_id = store.upsert_term(args.source_language, args.query, status="draft")
    print(
        json.dumps(
            store.retire_language_analysis(term_id, args.target_language, args.reason),
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


def command_plan_card_investigations(args: argparse.Namespace) -> int:
    settings = _settings()
    planner = _planner(settings, args)
    plan = planner.plan_card_enrichment(args.card_id)
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


def _deck_seeder(
    settings: Settings, modes: list[str] | tuple[str, ...]
) -> AutonomousDeckSeeder:
    return AutonomousDeckSeeder(
        _service(settings),
        CardStore(settings.cards_db),
        KnowledgeStore(settings.knowledge_db),
        modes=modes,
    )


def command_seed_deck(args: argparse.Namespace) -> int:
    """Prepare one unseen reviewed Answer/Question record with local Qwen."""

    settings = _settings()
    result = _deck_seeder(settings, args.modes).run_once(args.seed)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def guarded_deck_seed(seeder: AutonomousDeckSeeder) -> DeckSeedResult:
    """Run autonomous work only while the device has safe power and thermals."""

    blocker = background_preparation_blocker()
    if blocker:
        return DeckSeedResult(status="paused", message=blocker)
    return seeder.run_once()


def run_atomic_watch(
    worker: Any,
    stop_event: Any,
    *,
    idle_seconds: float,
    job_delay: float,
    emit: Callable[[Any], None],
    idle_action: Callable[[], Any] | None = None,
    idle_action_interval: float = 120.0,
) -> int:
    """Run one persisted task at a time, then do bounded idle work."""

    next_idle_action = monotonic()
    while not stop_event.is_set():
        result = worker.run_once()
        if result is None:
            if idle_action is not None and monotonic() >= next_idle_action:
                emit(idle_action())
                next_idle_action = monotonic() + max(1.0, idle_action_interval)
                stop_event.wait(job_delay)
                continue
            stop_event.wait(idle_seconds)
            continue
        emit(result)
        stop_event.wait(job_delay)
    return 0


def command_work_atomic(args: argparse.Namespace) -> int:
    settings = _settings()
    knowledge = KnowledgeStore(settings.knowledge_db)
    if args.recover_running:
        recovered = knowledge.recover_running_jobs()
        if recovered:
            print(
                json.dumps(
                    {"event": "recovered-interrupted-jobs", "count": recovered}
                ),
                flush=True,
            )
    worker = build_worker(
        knowledge,
        CorpusIndex(settings.corpus_db),
        MorphologyIndex(settings.roots_db),
        MorphologyIndex(settings.affixes_db),
        LocalLexiconRag(
            settings.data_dir / "lexicons" / "wn", settings.freedict_db
        ),
        LlamaCppClient(
            settings.llm_url, settings.llm_model, settings.request_timeout
        ),
        CardStore(settings.cards_db),
    )
    if args.watch:
        stop_event = Event()
        seeder = (
            _deck_seeder(settings, args.autoprepare_modes)
            if args.autoprepare_book_deck
            else None
        )

        def stop_worker(_signum: int, _frame: Any) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
        return run_atomic_watch(
            worker,
            stop_event,
            idle_seconds=max(0.25, min(float(args.idle_seconds), 60.0)),
            job_delay=max(0.0, min(float(args.job_delay), 60.0)),
            emit=lambda result: print(
                json.dumps(result.__dict__, ensure_ascii=False), flush=True
            ),
            idle_action=(lambda: guarded_deck_seed(seeder)) if seeder is not None else None,
            idle_action_interval=max(
                10.0, min(float(args.autoprepare_interval_seconds), 86_400.0)
            ),
        )
    print(
        json.dumps(
            [result.__dict__ for result in worker.run(args.limit)],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_clean_cards(args: argparse.Namespace) -> int:
    settings = _settings()
    result = CardStore(settings.cards_db).purge_unvalidated(Path(args.backup))
    print(json.dumps(result, ensure_ascii=False, indent=2))
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

    freedict = commands.add_parser(
        "ingest-freedict",
        help="build the compact exact English-Arabic correction index",
    )
    freedict.add_argument("source", help="path to the FreeDict eng-ara TEI file")
    freedict.add_argument("--database", help="override destination database")
    freedict.set_defaults(handler=command_ingest_freedict)

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

    sync_card_knowledge = commands.add_parser(
        "sync-card-knowledge",
        help="backfill accepted Answer/Question text into normalized knowledge",
    )
    sync_card_knowledge.set_defaults(handler=command_sync_card_knowledge)

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

    plan_translation = commands.add_parser(
        "plan-translation",
        help="revisit one language atom without rebuilding the complete word plan",
    )
    plan_translation.add_argument("query")
    plan_translation.add_argument(
        "target_language", choices=("ja", "zh", "fr", "ar")
    )
    plan_translation.add_argument("--source-language", default="en")
    plan_translation.add_argument("--prompt-version", default="atomic-v2")
    plan_translation.add_argument("--source-fingerprint", default="")
    plan_translation.set_defaults(handler=command_plan_translation)

    plan_language = commands.add_parser(
        "plan-language",
        help="rebuild one translation and its dependent pronunciation",
    )
    plan_language.add_argument("query")
    plan_language.add_argument(
        "target_language", choices=("ja", "zh", "fr", "ar")
    )
    plan_language.add_argument("--source-language", default="en")
    plan_language.add_argument("--prompt-version", default="language-v2")
    plan_language.add_argument("--source-fingerprint", default="")
    plan_language.set_defaults(handler=command_plan_language)

    plan_word_card_view = commands.add_parser(
        "plan-word-card-view",
        help="recompose a Word Card from the newest accepted language atoms",
    )
    plan_word_card_view.add_argument("query")
    plan_word_card_view.add_argument("--language", default="en")
    plan_word_card_view.add_argument(
        "--display-languages", nargs="+", default=DISPLAY_LANGUAGES
    )
    plan_word_card_view.add_argument("--prompt-version", default="word-card-view-v2")
    plan_word_card_view.add_argument("--source-fingerprint", default="")
    plan_word_card_view.set_defaults(handler=command_plan_word_card_view)

    plan_evidence = commands.add_parser(
        "plan-evidence",
        help="refresh retrieval after a source or lexical-filter revision",
    )
    plan_evidence.add_argument("query")
    plan_evidence.add_argument("--language", default="en")
    plan_evidence.add_argument("--prompt-version", default="retrieval-v2")
    plan_evidence.add_argument("--source-fingerprint", default="")
    plan_evidence.set_defaults(handler=command_plan_evidence)

    plan_morphemes = commands.add_parser(
        "plan-morphemes",
        help="revisit only the bounded surface decomposition",
    )
    plan_morphemes.add_argument("query")
    plan_morphemes.add_argument("--language", default="en")
    plan_morphemes.add_argument("--prompt-version", default="morphology-v2")
    plan_morphemes.add_argument("--source-fingerprint", default="")
    plan_morphemes.set_defaults(handler=command_plan_morphemes)

    plan_origin = commands.add_parser(
        "plan-origin",
        help="revisit only cited historical branches from accepted components",
    )
    plan_origin.add_argument("query")
    plan_origin.add_argument("--language", default="en")
    plan_origin.add_argument("--prompt-version", default="origin-v2")
    plan_origin.add_argument("--source-fingerprint", default="")
    plan_origin.set_defaults(handler=command_plan_origin)

    plan_origin_card = commands.add_parser(
        "plan-origin-card",
        help="compose a visible origin card from accepted atomic knowledge",
    )
    plan_origin_card.add_argument("query")
    plan_origin_card.add_argument("--language", default="en")
    plan_origin_card.add_argument("--prompt-version", default="origin-card-v1")
    plan_origin_card.add_argument("--source-fingerprint", default="")
    plan_origin_card.set_defaults(handler=command_plan_origin_card)

    retire_morphemes = commands.add_parser(
        "retire-morphemes",
        help="quarantine a rejected decomposition while preserving provenance",
    )
    retire_morphemes.add_argument("query")
    retire_morphemes.add_argument("--language", default="en")
    retire_morphemes.add_argument("--reason", required=True)
    retire_morphemes.set_defaults(handler=command_retire_morphemes)

    retire_language = commands.add_parser(
        "retire-language",
        help="quarantine one rejected translation and pronunciation pair",
    )
    retire_language.add_argument("query")
    retire_language.add_argument(
        "target_language", choices=("ja", "zh", "fr", "ar")
    )
    retire_language.add_argument("--source-language", default="en")
    retire_language.add_argument("--reason", required=True)
    retire_language.set_defaults(handler=command_retire_language)

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

    plan_card_investigations = commands.add_parser(
        "plan-card-investigations",
        help="enqueue vocabulary and EN/JA/ZH grammar for an accepted book card",
    )
    plan_card_investigations.add_argument("card_id")
    plan_card_investigations.add_argument(
        "--prompt-version", default="autonomous-content-enrichment-v2"
    )
    plan_card_investigations.add_argument("--source-fingerprint", default="")
    plan_card_investigations.set_defaults(handler=command_plan_card_investigations)

    seed_deck = commands.add_parser(
        "seed-deck",
        help="prepare one unseen reviewed book record with the local model",
    )
    seed_deck.add_argument(
        "--modes",
        nargs="+",
        choices=("answer", "question"),
        default=("answer", "question"),
    )
    seed_deck.add_argument("--seed", default="")
    seed_deck.set_defaults(handler=command_seed_deck)

    work_atomic = commands.add_parser(
        "work-atomic",
        help="run bounded evidence/meaning jobs and checkpoint accepted results",
    )
    work_atomic.add_argument("--limit", type=int, default=1)
    work_atomic.add_argument(
        "--watch",
        action="store_true",
        help="keep polling for queued work until interrupted",
    )
    work_atomic.add_argument("--idle-seconds", type=float, default=2.0)
    work_atomic.add_argument("--job-delay", type=float, default=1.0)
    work_atomic.add_argument(
        "--autoprepare-book-deck",
        action="store_true",
        help="prepare one unseen Answer/Question record whenever the queue is idle",
    )
    work_atomic.add_argument(
        "--autoprepare-modes",
        nargs="+",
        choices=("answer", "question"),
        default=("answer", "question"),
    )
    work_atomic.add_argument(
        "--autoprepare-interval-seconds", type=float, default=120.0
    )
    work_atomic.add_argument(
        "--recover-running",
        action="store_true",
        help="requeue an interrupted worker lease before starting",
    )
    work_atomic.set_defaults(handler=command_work_atomic)

    clean_cards = commands.add_parser(
        "clean-cards",
        help="back up the card ledger and purge unvalidated legacy material",
    )
    clean_cards.add_argument("--backup", required=True)
    clean_cards.set_defaults(handler=command_clean_cards)

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
