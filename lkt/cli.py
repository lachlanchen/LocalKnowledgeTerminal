from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Callable

from .atomic import MODEL_FREE_ATOMIC_JOBS, build_worker
from .card_books import CardBookIndex, build_card_book_index
from .config import Settings
from .corpus import CorpusIndex, build_index
from .deck import (
    AutonomousDeckSeeder,
    AutonomousLexicalSeeder,
    AutonomousMorphologySeeder,
    BalancedProductSeeder,
    DeckSeedResult,
    MorphologyV3EnrichmentSeeder,
)
from .device import (
    background_preparation_blocker,
    is_memory_pressure_blocker,
    model_free_preparation_blocker,
)
from .freedict import build_freedict_index
from .llm import LlamaCppClient
from .knowledge import KnowledgeStore
from .language_repair import LanguageRepairSeeder
from .knowledge_export import export_public_lexical_graph
from .jmdict import JapaneseReadingIndex, build_jmdict_index
from .lexicon import LocalLexiconRag
from .morphology import MorphologyIndex, build_morphology_index
from .preparation import (
    DISPLAY_LANGUAGES,
    PreparationPlanner,
    SemanticEnrichmentSeeder,
)
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


def command_reprocess_morphology(args: argparse.Namespace) -> int:
    """Idempotently ingest both reviewed morphology indexes into knowledge."""

    if not bool(args.allow_write):
        print(
            "reprocess-morphology requires --allow-write",
            file=sys.stderr,
        )
        return 2
    settings = _settings()
    knowledge_db = (
        Path(args.knowledge_db).resolve()
        if args.knowledge_db
        else Path(settings.knowledge_db).resolve()
    )
    root_index = (
        Path(args.root_index).resolve()
        if args.root_index
        else Path(settings.roots_db).resolve()
    )
    affix_index = (
        Path(args.affix_index).resolve()
        if args.affix_index
        else Path(settings.affixes_db).resolve()
    )
    from .morphology_ingest_v3 import run as run_current_morphology_ingest

    result = run_current_morphology_ingest(
        knowledge_db,
        (("root", root_index), ("affix", affix_index)),
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_finalize_morphology_v7(args: argparse.Namespace) -> int:
    """Audit or apply the ownership-gated v7 morphology finalization."""

    settings = _settings()
    knowledge_db = Path(args.knowledge_db).resolve() if args.knowledge_db else settings.knowledge_db
    root_index = Path(args.root_index).resolve() if args.root_index else settings.roots_db
    affix_index = Path(args.affix_index).resolve() if args.affix_index else settings.affixes_db
    from contextlib import closing
    import sqlite3

    from .morphology_contract import CURRENT_MORPHOLOGY_PARSER_VERSION
    from .relation_support import (
        finalize_morphology_support_migration,
        morphology_expected_sources,
    )

    expected = morphology_expected_sources(
        (("root", root_index), ("affix", affix_index))
    )
    result = None
    if knowledge_db.exists():
        read_only_uri = f"{knowledge_db.as_uri()}?mode=ro"
        with closing(sqlite3.connect(read_only_uri, uri=True)) as connection:
            result = finalize_morphology_support_migration(
                connection,
                parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                expected_sources=expected,
                expected_source_records=args.expected_source_records,
                allow_write=False,
            )
    if args.allow_write and not (result and result.get("already_complete")):
        from .knowledge import KnowledgeStore

        store = KnowledgeStore(knowledge_db)
        with closing(store._connect()) as connection:
            result = finalize_morphology_support_migration(
                connection,
                parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                expected_sources=expected,
                expected_source_records=args.expected_source_records,
                allow_write=True,
            )
    elif args.allow_write and result:
        result = {**result, "dry_run": False}
    if result is None:
        raise FileNotFoundError(knowledge_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("destructive_ready") else 2


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


def command_ingest_jmdict(args: argparse.Namespace) -> int:
    settings = _settings()
    destination = Path(args.database).resolve() if args.database else settings.jmdict_db

    def progress(count: int) -> None:
        if count % 10_000 == 0:
            print(f"indexed {count} JMdict form-reading rows", flush=True)

    result = build_jmdict_index(
        Path(args.source), destination, release=args.release, progress=progress
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_audit_japanese_readings(args: argparse.Namespace) -> int:
    """Find only established readings contradicted by an exact JMdict form."""

    settings = _settings()
    store = KnowledgeStore(settings.knowledge_db)
    index = JapaneseReadingIndex(settings.jmdict_db)
    metadata = index.metadata()
    latest: dict[str, dict[str, Any]] = {}
    for artifact in store.accepted_pronunciation_artifacts("ja"):
        latest[str(artifact["subject_key"])] = artifact
    issues: list[dict[str, Any]] = []
    queued: list[str] = []
    planner = PreparationPlanner(
        store,
        model=settings.llm_model,
        prompt_version=f"jmdict-reading-{metadata.get('release', 'pinned')}",
        source_fingerprint=metadata.get("source_sha256", ""),
    )
    for artifact in latest.values():
        payload = artifact["payload"]
        term = str(payload.get("term", "")).strip()
        current = str(payload.get("reading", "")).strip()
        candidates = index.lookup(term)
        allowed = list(dict.fromkeys(str(item["reading"]) for item in candidates))
        if not allowed or current in allowed:
            continue
        source = store.term_record(str(artifact["subject_entity_id"]))
        issue = {
            "source_term": source["text"],
            "japanese_term": term,
            "current_reading": current,
            "allowed_readings": allowed,
            "artifact_id": artifact["artifact_id"],
        }
        issues.append(issue)
        if args.apply and len(queued) < max(1, args.limit):
            plan = planner.plan_pronunciation(str(source["text"]), "ja")
            queued.extend(plan.jobs.values())
    print(
        json.dumps(
            {
                "release": metadata.get("release", ""),
                "audited": len(latest),
                "incorrect": len(issues),
                "queued_jobs": len(set(queued)),
                "issues": issues,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_generate(args: argparse.Namespace) -> int:
    settings = _settings()
    if args.mode in {"word", "knowledge", "root", "affix"}:
        knowledge = KnowledgeStore(settings.knowledge_db)
        planner = PreparationPlanner(
            knowledge,
            model=settings.llm_model,
            prompt_version="cli-lexical-v1",
        )
        plan = planner.plan_word(args.query)
        jobs = knowledge.jobs_for_subject(plan.subject_key)
        statuses = {
            str(job["job_id"]): str(job["status"])
            for job in jobs
        }
        selected_statuses = [
            statuses.get(str(job_id), "") for job_id in plan.jobs.values()
        ]
        if any(status in {"queued", "running"} for status in selected_statuses):
            status = "queued"
        elif selected_statuses and all(
            status == "complete" for status in selected_statuses
        ):
            status = "complete"
        else:
            status = "deferred"
        print(
            json.dumps(
                {
                    "status": status,
                    "requested_mode": args.mode,
                    "subject_entity_id": plan.subject_entity_id,
                    "subject_key": plan.subject_key,
                    "jobs": jobs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.mode not in {"answer", "question"}:
        raise ValueError(f"unsupported card mode: {args.mode!r}")
    card = _service(settings).create(args.query, args.mode)
    knowledge = KnowledgeStore(settings.knowledge_db)
    knowledge.acquire_card_book_card(card.to_dict())
    PreparationPlanner(
        knowledge,
        model=settings.llm_model,
        prompt_version="cli-content-enrichment-v1",
    ).plan_card_enrichment(card.card_id)
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


def command_export_knowledge(args: argparse.Namespace) -> int:
    """Write an accepted-only, sanitized lexical graph release."""

    settings = _settings()
    database = Path(args.database).resolve() if args.database else settings.knowledge_db
    result = export_public_lexical_graph(
        database,
        Path(args.output).resolve(),
        dataset_version=args.dataset_version,
    )
    payload = result.as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def command_sync_card_knowledge(_args: argparse.Namespace) -> int:
    """Backfill reviewed cards and queue their independent local enrichment."""

    settings = _settings()
    knowledge = KnowledgeStore(settings.knowledge_db)
    cards = CardStore(settings.cards_db).accepted_for_modes(("answer", "question"))
    planner = PreparationPlanner(
        knowledge,
        model=settings.llm_model,
        prompt_version="autonomous-content-enrichment-v3",
    )
    state = knowledge.card_book_enrichment_state()
    newly_acquired = 0
    cards_requiring_enrichment = 0
    jobs: set[str] = set()
    for card in reversed(cards):
        card_id = str(card["card_id"])
        if card_id not in state["reviewed"]:
            knowledge.acquire_card_book_card(card)
            newly_acquired += 1
        elif card_id not in state["needs_grammar"]:
            continue
        cards_requiring_enrichment += 1
        jobs.update(
            planner.plan_card_enrichment(
                card_id,
                include_investigation=False,
                missing_only=True,
            ).jobs.values()
        )
    print(
        json.dumps(
            {
                "cards": len(cards),
                "language_atoms": len(cards) * 3,
                "newly_acquired_cards": newly_acquired,
                "cards_requiring_enrichment": cards_requiring_enrichment,
                "enrichment_jobs": len(jobs),
                "knowledge": knowledge.status(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
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


def _lexical_seeder(settings: Settings) -> AutonomousLexicalSeeder:
    return AutonomousLexicalSeeder(
        CorpusIndex(settings.corpus_db),
        CardStore(settings.cards_db),
        KnowledgeStore(settings.knowledge_db),
        model=settings.llm_model,
    )


def _morphology_seeder(
    settings: Settings,
    *,
    max_in_flight_subjects: int = (
        AutonomousMorphologySeeder.DEFAULT_MAX_IN_FLIGHT_SUBJECTS
    ),
) -> AutonomousMorphologySeeder:
    return AutonomousMorphologySeeder(
        {
            "root": MorphologyIndex(settings.roots_db),
            "affix": MorphologyIndex(settings.affixes_db),
        },
        CardStore(settings.cards_db),
        KnowledgeStore(settings.knowledge_db),
        model=settings.llm_model,
        max_in_flight_subjects=max_in_flight_subjects,
    )


def _morphology_v3_enrichment_seeder(
    settings: Settings,
) -> MorphologyV3EnrichmentSeeder:
    return MorphologyV3EnrichmentSeeder(
        KnowledgeStore(settings.knowledge_db),
        model_name=settings.llm_model,
    )


def _semantic_enrichment_seeder(settings: Settings) -> SemanticEnrichmentSeeder:
    return SemanticEnrichmentSeeder(
        KnowledgeStore(settings.knowledge_db),
        model=settings.llm_model,
    )


def command_seed_lexical(args: argparse.Namespace) -> int:
    """Queue one unseen Word Origins headword as a shared atomic plan."""

    settings = _settings()
    result = _lexical_seeder(settings).run_once(args.seed)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def guarded_deck_seed(seeder: Any) -> DeckSeedResult:
    """Run autonomous work only while the device has safe power and thermals."""

    blocker = background_preparation_blocker()
    if blocker:
        if is_memory_pressure_blocker(blocker):
            lexical = (
                seeder
                if isinstance(seeder, AutonomousLexicalSeeder)
                else seeder.lexical
                if isinstance(seeder, BalancedProductSeeder)
                else None
            )
            if lexical is not None:
                return lexical.run_bounded_discovery_once()
        return DeckSeedResult(status="paused", message=blocker)
    if isinstance(seeder, AutonomousLexicalSeeder):
        return seeder.run_bounded_discovery_once()
    return seeder.run_once()


class DurableWorkerHeartbeat:
    """Keep worker liveness current even while one synchronous model job runs."""

    def __init__(
        self, store: KnowledgeStore, *, interval_seconds: float = 5.0
    ) -> None:
        self.store = store
        # The default health check expires after 15 seconds. Leave room for
        # scheduling and brief SQLite contention even for caller overrides.
        self.interval_seconds = max(1.0, min(float(interval_seconds), 5.0))
        self.stop_event = Event()
        self.lock = Lock()
        self.blocker = "atomic worker is starting"
        self.thread = Thread(
            target=self._run,
            name="lkt-worker-heartbeat",
            daemon=True,
        )

    def _write(self, status: str = "running", blocker: str | None = None) -> None:
        with self.lock:
            current_blocker = self.blocker if blocker is None else blocker
        try:
            self.store.record_worker_heartbeat(current_blocker, status=status)
        except (OSError, sqlite3.Error):
            # Card preparation remains authoritative if a transient heartbeat
            # write loses a race with another short SQLite transaction.
            pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self._write()
            if self.stop_event.wait(self.interval_seconds):
                break

    def start(self) -> None:
        self.thread.start()

    def update(self, blocker: str) -> None:
        with self.lock:
            self.blocker = blocker.strip()

    def close(self) -> None:
        self.stop_event.set()
        # SQLite writes have a bounded busy timeout. Wait for an in-flight
        # write to finish so it cannot replace the final stopped status.
        self.thread.join()
        self._write("stopped", "atomic worker stopped")


def run_atomic_watch(
    worker: Any,
    stop_event: Any,
    *,
    idle_seconds: float,
    job_delay: float,
    emit: Callable[[Any], None],
    idle_action: Callable[[], Any] | None = None,
    idle_action_interval: float = 120.0,
    periodic_action: Callable[[], Any] | None = None,
    periodic_action_interval: float = 120.0,
    planning_action: Callable[[], Any] | None = None,
    planning_action_interval: float = 120.0,
    projection_repair_action: Callable[[], Any] | None = None,
    preparation_blocker: Callable[[], str] = background_preparation_blocker,
    model_free_blocker: Callable[[], str] = model_free_preparation_blocker,
    heartbeat: Callable[[str], None] | None = None,
) -> int:
    """Run one persisted task at a time while preserving device headroom.

    Lightweight planning may add one bounded durable discovery round between
    jobs. Due periodic preparation runs after a completed job or while idle,
    never concurrently with inference. An infinite graph queue must not starve
    the independently paced book decks.
    """

    next_idle_action = monotonic()
    next_periodic_action = monotonic()
    next_planning_action = monotonic()
    while not stop_event.is_set():
        # Guard draining the existing queue as well as adding new deck work.
        # A model request can temporarily consume most of a small Pi; checking
        # before every job keeps that request from becoming an endless chain
        # that starves the browser, VNC, and SSH.
        blocker = preparation_blocker()
        if heartbeat is not None:
            heartbeat(blocker)
        if planning_action is not None and monotonic() >= next_planning_action:
            emit(planning_action())
            next_planning_action = monotonic() + max(
                1.0, planning_action_interval
            )
        model_free_blocked = model_free_blocker() if blocker else ""
        if projection_repair_action is not None and not model_free_blocked:
            repair = projection_repair_action()
            if repair is not None:
                emit(repair)
        result = None
        if blocker:
            # A lower memory floor may drain only handlers structurally proven
            # not to invoke the model. Power and thermal blockers are still
            # returned by both guards and therefore cannot be bypassed.
            if not model_free_blocked:
                result = worker.run_once(MODEL_FREE_ATOMIC_JOBS)
        else:
            result = worker.run_once()
        if result is None:
            if (
                periodic_action is not None
                and not blocker
                and not stop_event.is_set()
                and monotonic() >= next_periodic_action
            ):
                emit(periodic_action())
                next_periodic_action = monotonic() + max(
                    1.0, periodic_action_interval
                )
                stop_event.wait(job_delay)
                continue
            if blocker:
                stop_event.wait(idle_seconds)
                continue
            if idle_action is not None and monotonic() >= next_idle_action:
                emit(idle_action())
                next_idle_action = monotonic() + max(1.0, idle_action_interval)
                stop_event.wait(job_delay)
                continue
            stop_event.wait(idle_seconds)
            continue
        emit(result)
        if (
            periodic_action is not None
            and not stop_event.is_set()
            and monotonic() >= next_periodic_action
            and not preparation_blocker()
        ):
            emit(periodic_action())
            next_periodic_action = monotonic() + max(1.0, periodic_action_interval)
        stop_event.wait(job_delay)
    return 0


def _worker_emit_payload(result: Any) -> dict[str, Any]:
    """Normalize durable worker and planner results for JSON logging."""

    if isinstance(result, Mapping):
        return dict(result)
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    try:
        return vars(result)
    except TypeError as exc:
        raise TypeError(
            f"worker emitted unsupported result type {type(result).__name__}"
        ) from exc


def _plan_bounded_background_growth(
    semantic_seeder: Any,
    growth_seeders: tuple[Any, ...] | list[Any],
    *,
    rotation_claim: Callable[[], int],
    always_seeders: tuple[Any, ...] | list[Any] = (),
) -> dict[str, Any]:
    """Admit required work plus one durably rotated bounded growth lane."""

    seeders = tuple(growth_seeders)
    if not seeders:
        raise ValueError("background growth requires at least one generic seeder")

    # Semantic claim state enforces one global active subject. Do not return
    # early when it admits work: every interval still advances exactly one of
    # the existing bounded lexical/direct-morphology planners. Required
    # seeders are cheap durable admissions that must not disappear when a
    # long synchronous model job spans one or more wall-clock rotation slots.
    semantic_result = guarded_deck_seed(semantic_seeder)
    always_results = [
        _worker_emit_payload(guarded_deck_seed(seeder))
        for seeder in tuple(always_seeders)
    ]
    # Claim immediately before the generic attempt. A failed or interrupted
    # lane has still consumed its turn, so the next restart advances instead
    # of selecting the same expensive path forever.
    rotation_slot = max(0, int(rotation_claim()))
    growth_index = rotation_slot % len(seeders)
    growth_result = guarded_deck_seed(seeders[growth_index])
    return {
        "event": "bounded-background-growth",
        "rotation_slot": rotation_slot,
        "growth_index": growth_index,
        "semantic": _worker_emit_payload(semantic_result),
        "always": always_results,
        "growth": _worker_emit_payload(growth_result),
    }


def command_work_atomic(args: argparse.Namespace) -> int:
    settings = _settings()
    knowledge = KnowledgeStore(settings.knowledge_db)
    cards = CardStore(settings.cards_db)
    if args.recover_running:
        recovered = knowledge.recover_running_jobs()
        if recovered:
            print(
                json.dumps(
                    {"event": "recovered-interrupted-jobs", "count": recovered}
                ),
                flush=True,
            )
        recovered_preparations = cards.recover_interrupted_preparations()
        if recovered_preparations:
            print(
                json.dumps(
                    {
                        "event": "recovered-interrupted-preparations",
                        "count": recovered_preparations,
                    }
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
        cards,
        JapaneseReadingIndex(settings.jmdict_db),
    )
    if args.watch:
        # Queue-draining and explicitly filtered workers must not create new
        # autonomous jobs. The persistent lexical watch owns this policy.
        worker.continuous_growth = bool(args.autoprepare_lexical_deck)
        stop_event = Event()
        heartbeat = DurableWorkerHeartbeat(knowledge)
        lexical_seeder = (
            _lexical_seeder(settings) if args.autoprepare_lexical_deck else None
        )
        morphology_v3_seeder = _morphology_v3_enrichment_seeder(settings)
        semantic_seeder = _semantic_enrichment_seeder(settings)
        morphology_seeder = _morphology_seeder(
            settings,
            max_in_flight_subjects=args.morphology_max_in_flight_subjects,
        )
        planning_seeders = (
            *([lexical_seeder] if lexical_seeder is not None else []),
            morphology_seeder,
        )
        planning_interval = max(
            10.0, min(float(args.autoprepare_interval_seconds), 86_400.0)
        )

        language_repair_seeder = LanguageRepairSeeder(
            semantic_seeder.store, model=settings.llm_model,
        )

        def plan_background_growth() -> Any:
            return _plan_bounded_background_growth(
                semantic_seeder,
                planning_seeders,
                rotation_claim=knowledge.claim_background_growth_decision,
                always_seeders=(morphology_v3_seeder, language_repair_seeder),
            )
        book_seeder = (
            _deck_seeder(settings, args.autoprepare_modes)
            if args.autoprepare_book_deck
            else None
        )
        if book_seeder is not None:
            from .book_cadence import OccasionalBookSeeder

            seeder: Any | None = OccasionalBookSeeder(
                book_seeder, knowledge,
                interval_seconds=getattr(args, "book_interval_seconds", 1800.0),
            )
        elif lexical_seeder is not None:
            seeder = lexical_seeder
        else:
            seeder = book_seeder

        def stop_worker(_signum: int, _frame: Any) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
        heartbeat.start()
        try:
            return run_atomic_watch(
                worker,
                stop_event,
                idle_seconds=max(0.25, min(float(args.idle_seconds), 60.0)),
                job_delay=max(0.0, min(float(args.job_delay), 60.0)),
                emit=lambda result: print(
                    json.dumps(_worker_emit_payload(result), ensure_ascii=False),
                    flush=True,
                ),
                periodic_action=(
                    (lambda: guarded_deck_seed(seeder)) if seeder is not None else None
                ),
                periodic_action_interval=max(
                    10.0, min(float(args.autoprepare_interval_seconds), 86_400.0)
                ),
                planning_action=plan_background_growth,
                planning_action_interval=planning_interval,
                projection_repair_action=(
                    lexical_seeder._plan_due_projection_repair
                    if lexical_seeder is not None
                    else None
                ),
                heartbeat=heartbeat.update,
            )
        finally:
            heartbeat.close()
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

    reprocess_morphology = commands.add_parser(
        "reprocess-morphology",
        help="idempotently ingest both Root and Affix indexes into knowledge",
    )
    reprocess_morphology.add_argument(
        "--knowledge-db", help="override the normalized knowledge database"
    )
    reprocess_morphology.add_argument(
        "--root-index", help="override the reviewed Root index"
    )
    reprocess_morphology.add_argument(
        "--affix-index", help="override the reviewed Affix index"
    )
    reprocess_morphology.add_argument("--batch-size", type=int, default=100)
    reprocess_morphology.add_argument(
        "--allow-write",
        action="store_true",
        help="required acknowledgement before changing the knowledge database",
    )
    reprocess_morphology.set_defaults(handler=command_reprocess_morphology)

    finalize_morphology = commands.add_parser(
        "finalize-morphology-v7",
        help="audit or apply ownership-gated morphology v7 cleanup",
    )
    finalize_morphology.add_argument(
        "--knowledge-db", help="override the normalized knowledge database"
    )
    finalize_morphology.add_argument(
        "--root-index", help="override the reviewed Root index"
    )
    finalize_morphology.add_argument(
        "--affix-index", help="override the reviewed Affix index"
    )
    finalize_morphology.add_argument(
        "--expected-source-records", type=int, default=11516
    )
    finalize_morphology.add_argument(
        "--allow-write", action="store_true",
        help="required acknowledgement before finalizing ownership and aliases",
    )
    finalize_morphology.set_defaults(handler=command_finalize_morphology_v7)

    freedict = commands.add_parser(
        "ingest-freedict",
        help="build the compact exact English-Arabic correction index",
    )
    freedict.add_argument("source", help="path to the FreeDict eng-ara TEI file")
    freedict.add_argument("--database", help="override destination database")
    freedict.set_defaults(handler=command_ingest_freedict)

    jmdict = commands.add_parser(
        "ingest-jmdict",
        help="build the compact exact Japanese form/reading correction index",
    )
    jmdict.add_argument("source", help="path to the full jmdict-eng JSON")
    jmdict.add_argument("--database", help="override destination database")
    jmdict.add_argument("--release", default="", help="pinned upstream release ID")
    jmdict.set_defaults(handler=command_ingest_jmdict)

    japanese_audit = commands.add_parser(
        "audit-japanese-readings",
        help="report or backfill only accepted readings contradicted by JMdict",
    )
    japanese_audit.add_argument(
        "--apply", action="store_true", help="queue missing-only pronunciation repairs"
    )
    japanese_audit.add_argument("--limit", type=int, default=25)
    japanese_audit.set_defaults(handler=command_audit_japanese_readings)

    search = commands.add_parser("search", help="inspect retrieved book evidence")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=4)
    search.add_argument(
        "--corpus",
        choices=("word-origins", "answer", "question", "root", "affix"),
        default="word-origins",
    )
    search.set_defaults(handler=command_search)

    generate = commands.add_parser(
        "generate",
        help="queue lexical preparation or generate a reviewed book card",
    )
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

    export_knowledge = commands.add_parser(
        "export-knowledge",
        help="export a sanitized accepted-only lexical graph dataset",
    )
    export_knowledge.add_argument(
        "output",
        metavar="OUTPUT",
        help="destination directory for the standalone dataset",
    )
    export_knowledge.add_argument(
        "--database",
        metavar="PATH",
        help="override the normalized knowledge database",
    )
    export_knowledge.add_argument(
        "--dataset-version",
        metavar="VERSION",
        required=True,
        help="immutable release version recorded in the manifest",
    )
    export_knowledge.set_defaults(handler=command_export_knowledge)

    sync_card_knowledge = commands.add_parser(
        "sync-card-knowledge",
        help="backfill accepted Answer/Question text into normalized knowledge",
    )
    sync_card_knowledge.set_defaults(handler=command_sync_card_knowledge)

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
    plan_translation.add_argument("target_language", metavar="LANGUAGE")
    plan_translation.add_argument("--source-language", default="en")
    plan_translation.add_argument("--prompt-version", default="atomic-v2")
    plan_translation.add_argument("--source-fingerprint", default="")
    plan_translation.set_defaults(handler=command_plan_translation)

    plan_language = commands.add_parser(
        "plan-language",
        help="rebuild one translation and its dependent pronunciation",
    )
    plan_language.add_argument("query")
    plan_language.add_argument("target_language", metavar="LANGUAGE")
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
    retire_language.add_argument("target_language", metavar="LANGUAGE")
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
        "--prompt-version", default="autonomous-content-enrichment-v3"
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

    seed_lexical = commands.add_parser(
        "seed-lexical",
        help="queue one unseen Word Origins headword as a shared lexical plan",
    )
    seed_lexical.add_argument("--seed", default="")
    seed_lexical.set_defaults(handler=command_seed_lexical)

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
        help="occasionally prepare one unseen Answer/Question record between jobs",
    )
    work_atomic.add_argument(
        "--autoprepare-lexical-deck",
        action="store_true",
        help="queue one unseen shared lexical plan whenever the queue is idle",
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
        "--book-interval-seconds", type=float, default=1800.0,
        help="minimum cooldown between alternating book attempts; graph work continues",
    )
    work_atomic.add_argument(
        "--morphology-max-in-flight-subjects",
        type=int,
        default=AutonomousMorphologySeeder.DEFAULT_MAX_IN_FLIGHT_SUBJECTS,
        help=(
            "cap direct Root/Affix source subjects admitted ahead of the "
            "serial local-model worker"
        ),
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
