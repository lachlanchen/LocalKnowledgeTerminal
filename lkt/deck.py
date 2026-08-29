from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .corpus import CorpusIndex
from .knowledge import KnowledgeStore
from .morphology import MorphologyIndex
from .preparation import PreparationPlanner
from .service import CardService
from .store import CardStore


@dataclass(frozen=True)
class DeckSeedResult:
    """One bounded autonomous preparation decision."""

    status: str
    mode: str = ""
    source_entry_id: str = ""
    card_id: str = ""
    prepared: int = 0
    total: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutonomousDeckSeeder:
    """Prepare one unseen reviewed book record whenever the worker is idle.

    Source text and citations remain owned by the card-book index. Qwen creates
    the presentation draft locally, the normal publication gate validates it,
    and only then does the card join its mode-local carousel.
    """

    def __init__(
        self,
        service: CardService,
        store: CardStore,
        knowledge: KnowledgeStore,
        *,
        modes: Iterable[str] = ("answer", "question"),
    ):
        self.service = service
        self.store = store
        self.knowledge = knowledge
        self.modes = tuple(
            dict.fromkeys(
                mode.strip()
                for mode in modes
                if mode.strip() in {"answer", "question"}
                and mode.strip() in service.card_books
            )
        )
        if not self.modes:
            raise ValueError("autonomous deck preparation needs answer or question")

    def _prepared_entry_ids(self) -> dict[str, set[str]]:
        prepared = {mode: set() for mode in self.modes}
        for card in self.store.accepted_for_modes(self.modes):
            mode = str(card.get("mode", ""))
            evidence = card.get("evidence")
            if mode not in prepared or not isinstance(evidence, list):
                continue
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                entry_id = str(item.get("entry_id", "")).strip()
                if entry_id:
                    prepared[mode].add(entry_id)
        return prepared

    def progress(self) -> dict[str, Any]:
        """Return source-level completion without scheduling model work."""

        prepared = self._prepared_entry_ids()
        modes: dict[str, dict[str, int | bool]] = {}
        for mode in self.modes:
            total = self.service.card_books[mode].count()
            accepted = min(len(prepared[mode]), total)
            modes[mode] = {
                "accepted": accepted,
                "total": total,
                "remaining": max(0, total - accepted),
                "complete": accepted >= total,
            }
        accepted_total = sum(int(item["accepted"]) for item in modes.values())
        source_total = sum(int(item["total"]) for item in modes.values())
        return {
            "ready": True,
            "accepted": accepted_total,
            "total": source_total,
            "remaining": max(0, source_total - accepted_total),
            "complete": accepted_total >= source_total,
            "modes": modes,
        }

    def run_once(self, seed: str = "") -> DeckSeedResult:
        """Prepare exactly one unseen source record, or report completion."""

        return self._run_once(self.modes, seed)

    def run_mode(self, mode: str, seed: str = "") -> DeckSeedResult:
        """Prepare one record for a specific visible book mode."""

        if mode not in self.modes:
            raise ValueError(f"autonomous book mode is unavailable: {mode!r}")
        return self._run_once((mode,), seed)

    def _run_once(self, modes: Iterable[str], seed: str) -> DeckSeedResult:
        modes = tuple(modes)

        prepared = self._prepared_entry_ids()
        progress = []
        for order, mode in enumerate(modes):
            total = self.service.card_books[mode].count()
            done = len(prepared[mode])
            progress.append((done / total if total else 1.0, order, mode, done, total))
        progress.sort()
        for _ratio, _order, mode, done, total in progress:
            if done >= total:
                continue
            draw_seed = seed.strip() or f"{time.time_ns()}:{mode}:{done}"
            evidence = self.service.card_books[mode].draw_unseen(
                draw_seed, prepared[mode]
            )
            if evidence is None:
                continue
            # The model sees the reviewed English line as its request, while
            # the stable source identity remains the retrieval-owned entry ID.
            query = evidence.headword
            try:
                card = self.service.create_from_evidence(query, mode, [evidence])
            except Exception as exc:
                # A weak model draft remains outside the accepted carousel and
                # can be retried on a later idle cycle without stopping the
                # durable atomic worker.
                return DeckSeedResult(
                    status="deferred",
                    mode=mode,
                    source_entry_id=evidence.entry_id,
                    prepared=done,
                    total=total,
                    message=str(exc)[:300],
                )
            try:
                self.knowledge.acquire_card_book_card(card.to_dict())
                PreparationPlanner(
                    self.knowledge,
                    model=self.service.model.model_name,
                    prompt_version="autonomous-content-enrichment-v3",
                ).plan_card_enrichment(card.card_id)
                message = "local model card accepted and language enrichment queued"
            except Exception as exc:
                # Publication is already complete. Keep the visible reviewed
                # card and report that normalized enrichment needs a later sync.
                message = f"card accepted; knowledge enrichment deferred: {str(exc)[:220]}"
            return DeckSeedResult(
                status="prepared",
                mode=mode,
                source_entry_id=evidence.entry_id,
                card_id=card.card_id,
                prepared=done + 1,
                total=total,
                message=message,
            )
        return DeckSeedResult(
            status="complete",
            prepared=sum(len(prepared[mode]) for mode in modes),
            total=sum(self.service.card_books[mode].count() for mode in modes),
            message="every configured book record already has an accepted card",
        )


class AutonomousLexicalSeeder:
    """Queue one unseen lexical plan for all four lexical product modes."""

    MODES = ("knowledge", "word", "root", "affix")

    def __init__(
        self,
        corpus: CorpusIndex,
        store: CardStore,
        knowledge: KnowledgeStore,
        *,
        model: str,
        prompt_version: str = "autonomous-lexical-v3",
    ):
        self.corpus = corpus
        self.store = store
        self.knowledge = knowledge
        self.model = model
        self.prompt_version = prompt_version

    def _planned_keys(self) -> set[str]:
        planned = self.knowledge.planned_term_keys("en")
        for card in self.store.accepted_for_modes(self.MODES):
            query = str(card.get("query", "")).strip().casefold()
            if query:
                planned.add(query)
        return planned

    def progress(self) -> dict[str, Any]:
        candidates = {item.casefold() for item in self.corpus.lexical_headwords()}
        planned = len(candidates.intersection(self._planned_keys()))
        accepted = {
            str(card.get("query", "")).strip().casefold()
            for card in self.store.accepted_for_modes(self.MODES)
            if str(card.get("query", "")).strip()
        }
        accepted_count = len(candidates.intersection(accepted))
        total = len(candidates)
        return {
            "ready": True,
            "planned": planned,
            "accepted": accepted_count,
            "total": total,
            "remaining": max(0, total - planned),
            "complete": planned >= total,
            "modes": list(self.MODES),
        }

    def _missing_origin_queries(self) -> tuple[str, ...]:
        """Find accepted Word Cards whose failed origin paths may be repairable."""

        modes_by_query: dict[str, set[str]] = {}
        display_by_query: dict[str, str] = {}
        for card in self.store.accepted_for_modes(self.MODES):
            query = str(card.get("query", "")).strip()
            key = query.casefold()
            if not key:
                continue
            display_by_query.setdefault(key, query)
            modes_by_query.setdefault(key, set()).add(str(card.get("mode", "")))
        return tuple(
            display_by_query[key]
            for key, modes in modes_by_query.items()
            if "knowledge" in modes and "word" not in modes
        )

    def _plan_has_pending_work(self, plan: Any) -> bool:
        statuses = {
            str(job["job_id"]): str(job["status"])
            for job in self.knowledge.jobs_for_subject(plan.subject_key)
        }
        return any(
            statuses.get(str(job_id)) in {"queued", "running"}
            for job_id in plan.jobs.values()
        )

    def run_once(self, seed: str = "") -> DeckSeedResult:
        planned = self._planned_keys()
        progress = self.progress()
        total = int(progress["total"])
        for repair_query in self._missing_origin_queries():
            planner = PreparationPlanner(
                self.knowledge,
                model=self.model,
                prompt_version=self.prompt_version,
                source_fingerprint=self.corpus.metadata().get("source_sha256", ""),
            )
            try:
                plan = planner.plan_lexical_history_repair(repair_query)
            except ValueError:
                # Old partial records may predate one of the reusable language
                # atoms. Try another repair candidate before new discovery.
                continue
            else:
                # A deterministic plan can resolve to previously exhausted
                # job IDs. Do not report those as newly queued forever or let
                # one terminally failed word starve Root/Affix and later words.
                if not self._plan_has_pending_work(plan):
                    continue
                return DeckSeedResult(
                    status="repair-queued",
                    mode="lexical",
                    prepared=int(progress["planned"]),
                    total=total,
                    message=(
                        f"queued {len(plan.jobs)} missing history jobs for "
                        f"{repair_query}; accepted language atoms were reused"
                    ),
                )
        evidence = self.corpus.draw_unseen_word(
            seed.strip() or f"{time.time_ns()}:lexical:{len(planned)}",
            planned,
        )
        if evidence is None:
            return DeckSeedResult(
                status="complete",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=total,
                message="every eligible Word Origins headword is already planned",
            )
        planner = PreparationPlanner(
            self.knowledge,
            model=self.model,
            prompt_version=self.prompt_version,
            source_fingerprint=self.corpus.metadata().get("source_sha256", ""),
        )
        plan = planner.plan_word(evidence.headword)
        return DeckSeedResult(
            status="queued",
            mode="lexical",
            source_entry_id=evidence.entry_id,
            prepared=min(int(progress["planned"]) + 1, total),
            total=total,
            message=(
                f"queued {len(plan.jobs)} missing-only jobs; one accepted plan "
                "feeds Word Card, Word Origin, Root, and Affix"
            ),
        )

    def run_bounded_once(self, seed: str = "") -> DeckSeedResult:
        """Keep autonomous discovery to one unfinished lexical subject at a time."""

        active = self.knowledge.active_term_preparation_count()
        if active:
            return DeckSeedResult(
                status="busy",
                mode="lexical",
                message=f"waiting for {active} active lexical plan to finish",
            )
        return self.run_once(seed)


class AutonomousMorphologySeeder:
    """Grow Root and Affix from their own polished books with local Qwen RAG."""

    MODES = ("root", "affix")

    def __init__(
        self,
        service: CardService,
        store: CardStore,
        *,
        modes: Iterable[str] = MODES,
    ):
        self.service = service
        self.store = store
        self.modes = tuple(
            dict.fromkeys(
                mode.strip()
                for mode in modes
                if mode.strip() in self.MODES
                and mode.strip() in service.morphology
                and mode.strip() in service.rag_engines
            )
        )
        if not self.modes:
            raise ValueError("autonomous morphology needs a root or affix index")

    def _prepared_record_ids(self) -> dict[str, set[str]]:
        prepared = {mode: set() for mode in self.modes}
        corpus_ids = {
            mode: self.service.morphology[mode].metadata().get("corpus_id", "")
            for mode in self.modes
        }
        for card in self.store.accepted_for_modes(self.modes):
            mode = str(card.get("mode", ""))
            evidence = card.get("evidence")
            if mode not in prepared or not isinstance(evidence, list):
                continue
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                if str(item.get("corpus_id", "")) != corpus_ids[mode]:
                    continue
                record_id = str(item.get("entry_id", "")).strip()
                if record_id:
                    prepared[mode].add(record_id)
        return prepared

    def progress(self) -> dict[str, Any]:
        prepared = self._prepared_record_ids()
        modes: dict[str, dict[str, int | bool]] = {}
        for mode in self.modes:
            total = self.service.morphology[mode].count()
            accepted = min(len(prepared[mode]), total)
            modes[mode] = {
                "accepted": accepted,
                "total": total,
                "remaining": max(0, total - accepted),
                "complete": accepted >= total,
            }
        accepted_total = sum(int(item["accepted"]) for item in modes.values())
        source_total = sum(int(item["total"]) for item in modes.values())
        return {
            "ready": True,
            "accepted": accepted_total,
            "total": source_total,
            "remaining": max(0, source_total - accepted_total),
            "complete": accepted_total >= source_total,
            "modes": modes,
        }

    def run_once(self, seed: str = "") -> DeckSeedResult:
        prepared = self._prepared_record_ids()
        ranked = sorted(
            self.modes,
            key=lambda mode: (
                len(prepared[mode]) / max(1, self.service.morphology[mode].count()),
                self.modes.index(mode),
            ),
        )
        for mode in ranked:
            result = self.run_mode(mode, seed)
            if result.status != "complete":
                return result
        return DeckSeedResult(
            status="complete",
            mode="morphology",
            message="every polished Root and Affix record already has a card",
        )

    def run_mode(self, mode: str, seed: str = "") -> DeckSeedResult:
        if mode not in self.modes:
            raise ValueError(f"autonomous morphology mode is unavailable: {mode!r}")
        prepared = self._prepared_record_ids()[mode]
        index: MorphologyIndex = self.service.morphology[mode]
        total = index.count()
        evidence = index.draw_unseen(
            seed.strip() or f"{time.time_ns()}:{mode}:{len(prepared)}", prepared
        )
        if evidence is None:
            return DeckSeedResult(
                status="complete",
                mode=mode,
                prepared=len(prepared),
                total=total,
                message=f"every polished {mode} record already has an accepted card",
            )

        # Keep the selected primary book record first and let the mode-local
        # RAG engine attach only useful companion evidence from the other book.
        retrieved = self.service.rag_engines[mode].retrieve(evidence.headword)
        evidence_set = [evidence]
        seen = {(evidence.corpus_id, evidence.entry_id)}
        for item in retrieved:
            key = (item.corpus_id, item.entry_id)
            if key not in seen:
                evidence_set.append(item)
                seen.add(key)
        try:
            card = self.service.create_from_evidence(
                evidence.headword, mode, evidence_set
            )
        except Exception as exc:
            return DeckSeedResult(
                status="deferred",
                mode=mode,
                source_entry_id=evidence.entry_id,
                prepared=len(prepared),
                total=total,
                message=str(exc)[:300],
            )
        return DeckSeedResult(
            status="prepared",
            mode=mode,
            source_entry_id=evidence.entry_id,
            card_id=card.card_id,
            prepared=min(len(prepared) + 1, total),
            total=total,
            message=(
                f"local Qwen accepted one {mode} graph from its polished book; "
                "retrieval, draft, normalized JSON, and publication were saved"
            ),
        )


class BalancedProductSeeder:
    """Grow the six visible product modes as balanced, bounded rounds."""

    MODES = ("question", "answer", "knowledge", "word", "root", "affix")
    LEXICAL_MODES = frozenset(("knowledge", "word"))

    def __init__(
        self,
        book: AutonomousDeckSeeder,
        lexical: AutonomousLexicalSeeder,
        store: CardStore,
        morphology: AutonomousMorphologySeeder | None = None,
    ):
        self.book = book
        self.lexical = lexical
        self.store = store
        self.morphology = morphology

    def counts(self) -> dict[str, int]:
        counts = {mode: 0 for mode in self.MODES}
        for card in self.store.accepted_for_modes(self.MODES):
            mode = str(card.get("mode", ""))
            if mode in counts:
                counts[mode] += 1
        return counts

    def run_once(self) -> DeckSeedResult:
        counts = self.counts()
        attempted: set[str] = set()
        pending_result: DeckSeedResult | None = None
        ranked = sorted(
            self.MODES,
            key=lambda mode: (counts[mode], self.MODES.index(mode)),
        )
        for mode in ranked:
            action = "lexical" if mode in self.LEXICAL_MODES else mode
            if action in attempted:
                continue
            attempted.add(action)
            if action == "lexical":
                result = self.lexical.run_bounded_once()
            elif action in {"root", "affix"} and self.morphology is not None:
                result = self.morphology.run_mode(action)
            elif action in {"root", "affix"}:
                result = self.lexical.run_bounded_once()
            elif action in self.book.modes:
                result = self.book.run_mode(action)
            else:
                continue
            # A lexical repair plan or busy report performs no inference in
            # this call and may not increase a visible deck. Preserve that
            # result, but let one genuinely least-filled Root/Affix/book mode
            # use this balance pass. Model work remains sequential.
            if result.status in {"busy", "repair-queued"}:
                pending_result = pending_result or result
                continue
            if result.status != "complete":
                return result
        if pending_result is not None:
            return pending_result
        return DeckSeedResult(
            status="complete",
            mode="all",
            message="every configured autonomous source is already planned",
        )


class AutonomousSeedCoordinator:
    """Alternate independent seeders so no product mode monopolizes idle time."""

    def __init__(self, seeders: Iterable[Any]):
        self.seeders = tuple(seeders)
        if not self.seeders:
            raise ValueError("autonomous seed coordination needs at least one seeder")
        self._next = 0

    def run_once(self) -> DeckSeedResult:
        for _attempt in range(len(self.seeders)):
            index = self._next % len(self.seeders)
            self._next = (index + 1) % len(self.seeders)
            result = self.seeders[index].run_once()
            if result.status != "complete":
                return result
        return DeckSeedResult(
            status="complete",
            mode="all",
            message="every configured autonomous source is already planned",
        )
