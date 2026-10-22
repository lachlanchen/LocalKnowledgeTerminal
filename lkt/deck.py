from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .corpus import CorpusIndex
from .knowledge import KnowledgeStore
from .languages import normalize_language_code, supports_english_morphology
from .morphology import MorphologyIndex
from .preparation import PreparationPlan, PreparationPlanner
from .service import CardService
from .store import CardStore


_LEXICAL_PROVENANCE_LANGUAGE_LABELS = {
    "ancient greek": "grc",
    "classical greek": "grc",
    "classical latin": "la",
    "english": "en",
    "french": "fr",
    "late latin": "la",
    "latin": "la",
    "medieval latin": "la",
    "middle english": "enm",
    "middle french": "frm",
    "old english": "ang",
    "old french": "fro",
    "proto-germanic": "gem-pro",
    "proto-indo-european": "ine-pro",
}


def _schedulable_lexical_language(value: Any) -> str:
    """Return a language code, never a descriptive provenance label."""

    raw = re.sub(r"\s+", " ", str(value)).strip().casefold().replace("_", "-")
    if (
        raw in _LEXICAL_PROVENANCE_LANGUAGE_LABELS
        or raw.replace("-", " ") in _LEXICAL_PROVENANCE_LANGUAGE_LABELS
    ):
        return ""
    try:
        return normalize_language_code(raw)
    except ValueError:
        return ""


def _plan_lexical_term(
    planner: PreparationPlanner,
    term: str,
    language: str,
) -> PreparationPlan:
    """Route semantics for every language and morphology only for English."""

    language = _schedulable_lexical_language(language)
    if not language:
        raise ValueError("lexical language is not schedulable")
    if supports_english_morphology(language):
        return planner.plan_word(term, language=language)
    return planner.plan_word_card(term, language=language)


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
    round_id: str = ""
    discoveries: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_LEXICAL_PUBLICATION_JOB_TYPES = (
    "retrieve-evidence", "prepare-meaning", "split-morphemes",
    "expand-origin-branches", "compose-origin-card",
)


def _active_lexical_publication_job(job: dict[str, Any], prompt_prefix: str = "") -> bool:
    """Optional language work and another producer's revisits own separate slots."""
    version = str(job.get("prompt_version", ""))
    return (
        str(job.get("status", "")) in {"queued", "running"}
        and str(job.get("job_type", "compose-origin-card")) in _LEXICAL_PUBLICATION_JOB_TYPES
        and (
            not prompt_prefix
            or not version.startswith("continuous-origin-")
            or version.startswith(prompt_prefix)
        )
    )


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
    """Claim one bounded lexical batch for all four lexical product modes."""

    MODES = ("knowledge", "word", "root", "affix")
    DISCOVERY_BATCH_LIMIT = 5
    MAX_ACTIVE_DISCOVERY_SUBJECTS = 6
    GROWTH_CADENCE = ("graph", "correction", "source", "source")

    def __init__(
        self,
        corpus: CorpusIndex,
        store: CardStore,
        knowledge: KnowledgeStore,
        *,
        model: str,
        prompt_version: str = "autonomous-lexical-v4",
    ):
        self.corpus = corpus
        self.store = store
        self.knowledge = knowledge
        self.model = model
        self.prompt_version = prompt_version
        self._active_discovery_subject_keys: set[str] = set()

    def _planned_keys(self) -> set[str]:
        durable_lookup = getattr(
            self.knowledge, "discovered_or_planned_term_keys", None
        )
        planned = (
            durable_lookup("en")
            if callable(durable_lookup)
            else self.knowledge.planned_term_keys("en")
        )
        for card in self.store.accepted_for_modes(self.MODES):
            query = self._key(card.get("query", ""))
            if query:
                planned.add(query)
        return planned

    @staticmethod
    def _key(value: Any) -> str:
        return " ".join(
            unicodedata.normalize("NFKC", str(value)).casefold().split()
        )

    def _investigation_groups(self, excluded: set[str]) -> list[dict[str, Any]]:
        lookup = getattr(self.knowledge, "investigation_suggestion_groups", None)
        if not callable(lookup):
            return []
        return list(lookup(excluded, language="en"))

    def _graph_neighbor_terms(self, excluded: set[str]) -> list[dict[str, Any]]:
        """Return persisted graph/card neighbors that can receive a full plan."""

        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for card in self.store.accepted_for_modes(("word", "root", "affix")):
            extensions = card.get("extensions")
            extensions = extensions if isinstance(extensions, dict) else {}
            source_key = str(extensions.get("knowledge_subject", "")).strip()
            source_entity_id = (
                source_key.split(":", 1)[1]
                if source_key.startswith("term:")
                else ""
            )
            for raw in card.get("related_terms", ()):
                item = raw if isinstance(raw, dict) else {"term": raw}
                term = str(item.get("term", "")).strip()
                key = self._key(term)
                if not key or key in excluded:
                    continue
                candidate = candidates.setdefault(
                    ("en", key),
                    {
                        "term": term,
                        "language": "en",
                        "note": str(item.get("note", "")).strip(),
                        "sources": set(),
                        "source_kind": "card-derivative",
                        "priority": 3,
                        "retry_count": 0,
                        "novel_connections": True,
                        "connection_count": 1,
                        "coverage_priority": 0,
                    },
                )
                if source_entity_id:
                    candidate["sources"].add(source_entity_id)

        frontier_lookup = getattr(
            self.knowledge, "lexical_frontier_candidates", None
        )
        frontier = frontier_lookup(limit=96) if callable(frontier_lookup) else []
        for raw in frontier:
            raw_language = str(raw.get("language", "")).strip()
            language = _schedulable_lexical_language(raw_language)
            term = str(raw.get("text", raw.get("term", ""))).strip()
            key = self._key(term)
            if (
                not language
                or not key
                or language == "en" and key in excluded
            ):
                if not language and key:
                    quarantine = getattr(
                        self.knowledge, "quarantine_lexical_discovery", None
                    )
                    if callable(quarantine):
                        quarantine(
                            {
                                **raw,
                                "term": term,
                                "language": raw_language,
                                "source_kind": str(
                                    raw.get("source_kind", "graph-frontier")
                                ),
                            },
                            reason=(
                                "historical language label is provenance metadata, "
                                "not a schedulable lexical language code"
                            ),
                            context_key="graph-frontier-language-filter-v1",
                        )
                continue
            candidate_key = (language, key)
            priority = int(raw.get("evidence_rank", 2))
            candidate = candidates.get(candidate_key)
            if candidate is None or priority < int(candidate.get("priority", 3)):
                sources = set(candidate.get("sources", ())) if candidate else set()
                candidates[candidate_key] = {
                    "term": term,
                    "language": language,
                    "note": str(raw.get("note", "")).strip(),
                    "sources": sources,
                    "source_kind": str(raw.get("source_kind", "graph-frontier")),
                    "priority": priority,
                    "retry_count": int(raw.get("retry_count", 0)),
                    "novel_connections": bool(raw.get("novel_connections", True)),
                    "connection_count": int(raw.get("connection_count", 1)),
                    "coverage_priority": int(raw.get("coverage_priority", 0)),
                }
                candidate = candidates[candidate_key]
            source_entity_id = str(raw.get("source_entity_id", "")).strip()
            if source_entity_id:
                candidate["sources"].add(source_entity_id)
        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                -int(item.get("coverage_priority", 0)),
                not bool(item["novel_connections"]),
                int(item["retry_count"]) > 0,
                int(item["priority"]),
                -len(item["sources"]),
                -int(item["connection_count"]),
                str(item["language"]),
                self._key(item["term"]),
            ),
        )
        return [
            {
                **{key: value for key, value in item.items() if key != "sources"},
                "source_entity_id": next(iter(sorted(item["sources"])), ""),
            }
            for item in ordered
        ]

    def _frontier_prompt_version(self, candidate: dict[str, Any]) -> str:
        retries = int(candidate.get("retry_count", 0))
        if not retries:
            return self.prompt_version
        return (
            f"{self.prompt_version}-frontier-"
            f"{str(candidate.get('source_kind', 'graph')).replace('_', '-')}-"
            f"retry-{retries + 1}"
        )

    @staticmethod
    def _select_graph_neighbor(
        candidates: list[dict[str, Any]], growth_epoch: int
    ) -> dict[str, Any]:
        """Prefer incomplete coverage before alternating graph branches."""

        families = (
            frozenset(("derivative", "card-derivative", "shared-morpheme")),
            frozenset(("historical-form",)),
        )
        preferred = families[(growth_epoch // 4) % len(families)]
        coverage_priority = max(
            int(candidate.get("coverage_priority", 0))
            for candidate in candidates
        )
        coverage_candidates = [
            candidate
            for candidate in candidates
            if int(candidate.get("coverage_priority", 0)) == coverage_priority
        ]
        return next(
            (
                candidate
                for candidate in coverage_candidates
                if str(candidate.get("source_kind", "")) in preferred
            ),
            coverage_candidates[0],
        )

    def _sparse_projection_candidate(
        self, growth_epoch: int
    ) -> dict[str, Any] | None:
        """Inspect one rotating, bounded window of visible lexical subjects."""

        by_subject: dict[str, list[dict[str, Any]]] = {}
        for card in self.store.accepted_for_modes(("word", "root", "affix")):
            extensions = card.get("extensions")
            extensions = extensions if isinstance(extensions, dict) else {}
            subject_key = str(extensions.get("knowledge_subject", "")).strip()
            if not subject_key.startswith("term:"):
                continue
            subject_id = subject_key.split(":", 1)[1]
            mode = str(card.get("mode", "")).strip()
            if mode not in {"word", "root", "affix"}:
                continue
            by_subject.setdefault(subject_id, []).append(
                {
                    "subject_entity_id": subject_id,
                    "mode": mode,
                    "branch_focus_id": str(
                        extensions.get("branch_focus_id", "")
                    ).strip(),
                }
            )
        subject_ids = sorted(by_subject)
        if not subject_ids:
            return None
        window_size = min(64, len(subject_ids))
        start = (growth_epoch * 17) % len(subject_ids)
        selected_ids = {
            subject_ids[(start + offset) % len(subject_ids)]
            for offset in range(window_size)
        }
        visible_views = [
            view
            for subject_id in subject_ids
            if subject_id in selected_ids
            for view in by_subject[subject_id]
        ]
        lookup = getattr(self.knowledge, "lexical_sparse_candidate", None)
        if not callable(lookup):
            return None
        return lookup(
            visible_views,
            prompt_prefix=self.prompt_version,
            limit=window_size,
        )

    def _plan_recursive_word(
        self,
        term: str,
        *,
        prompt_version: str,
        status: str,
        message: str,
        progress: dict[str, Any],
        language: str = "en",
    ) -> DeckSeedResult:
        planner = PreparationPlanner(
            self.knowledge,
            model=self.model,
            prompt_version=prompt_version,
            source_fingerprint=self.corpus.metadata().get("source_sha256", ""),
        )
        plan = _plan_lexical_term(planner, term, language)
        if not self._plan_has_pending_work(plan):
            return DeckSeedResult(
                status="deferred",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message=f"recursive plan for {term} already reached a terminal state",
            )
        return DeckSeedResult(
            status=status,
            mode="lexical",
            prepared=int(progress["planned"]),
            total=int(progress["total"]),
            message=message,
        )

    def _reconcile_revisit_epochs(self) -> None:
        reconcile = getattr(self.knowledge, "reconcile_lexical_revisit_epochs", None)
        if callable(reconcile):
            reconcile()

    def _plan_due_projection_repair(self) -> DeckSeedResult | None:
        """Claim and bind one model-free projection independently of discovery."""

        claim = getattr(self.knowledge, "claim_lexical_projection_repair", None)
        if not callable(claim):
            return None
        try:
            # Claiming already performs transactional recovery and bounded
            # historical auditing. A second reconciliation here made each
            # model-free repair pay for the same graph-maintenance pass twice.
            repair = claim()
        except Exception:
            # Projection maintenance must not halt normal bounded discovery.
            return None
        if repair is None:
            return None

        term_entity_id = str(repair.get("term_entity_id", "")).strip()
        fingerprint = str(repair.get("desired_fingerprint", "")).strip()
        lease_token = str(repair.get("lease_token", "")).strip()
        planner = PreparationPlanner(
            self.knowledge,
            model=self.model,
            prompt_version=self.prompt_version,
        )
        try:
            plan = planner.plan_projection_repair(
                term_entity_id,
                fingerprint,
                failure_count=int(repair.get("failure_count", 0)),
            )
            compose_job_id = str(plan.jobs["compose-origin-card"])
        except Exception as exc:
            fail = getattr(self.knowledge, "fail_lexical_projection_repair", None)
            if callable(fail):
                fail(
                    term_entity_id,
                    fingerprint,
                    str(exc),
                    lease_token=lease_token,
                )
            else:
                release = getattr(
                    self.knowledge, "release_lexical_projection_repair", None
                )
                if callable(release):
                    release(term_entity_id, fingerprint, lease_token)
            return None

        bind = getattr(self.knowledge, "bind_lexical_projection_repair", None)
        release = getattr(self.knowledge, "release_lexical_projection_repair", None)
        try:
            bound = bool(
                callable(bind)
                and bind(
                    term_entity_id,
                    fingerprint,
                    lease_token,
                    compose_job_id,
                )
            )
        except Exception:
            bound = False
        if not bound:
            if callable(release):
                release(term_entity_id, fingerprint, lease_token)
            return None

        job = next(
            (
                item
                for item in self.knowledge.jobs_for_subject(plan.subject_key)
                if str(item.get("job_id", "")) == compose_job_id
            ),
            {},
        )
        if str(job.get("status", "")) in {"complete", "failed"}:
            reconcile = getattr(
                self.knowledge, "reconcile_lexical_projection_repairs", None
            )
            if callable(reconcile):
                reconcile()
            return None
        desired_modes = ", ".join(
            str(item) for item in repair.get("desired_modes", ())
        )
        return DeckSeedResult(
            status="projection-repair-queued",
            mode="lexical",
            message=(
                f"recomposing {desired_modes or 'morphology'} projection for "
                f"{repair.get('term', term_entity_id)} from accepted checkpoints"
            ),
        )

    def _claim_and_plan_revisit(
        self, progress: dict[str, Any]
    ) -> DeckSeedResult | None:
        claim = getattr(self.knowledge, "claim_lexical_revisit_candidate", None)
        if not callable(claim):
            return None
        revisit = claim(prompt_prefix=self.prompt_version, language=None)
        if revisit is None:
            return None
        epoch_id = str(revisit["epoch_id"])
        complete = getattr(self.knowledge, "complete_lexical_revisit_epoch")
        try:
            planner = PreparationPlanner(
                self.knowledge,
                model=self.model,
                prompt_version=str(revisit["prompt_version"]),
                source_fingerprint=self.corpus.metadata().get("source_sha256", ""),
            )
            plan = _plan_lexical_term(
                planner,
                str(revisit["term"]),
                str(revisit.get("language", "en")),
            )
            if not self._plan_has_pending_work(plan):
                complete(
                    epoch_id,
                    outcome="no-work",
                    result_summary="deterministic revisit plan had no pending work",
                )
                return DeckSeedResult(
                    status="deferred",
                    mode="lexical",
                    prepared=int(progress["planned"]),
                    total=int(progress["total"]),
                    message=(
                        f"revisit for {revisit['term']} was unchanged; its bounded "
                        "cooldown increased without banning the word"
                    ),
                )
            bind = getattr(self.knowledge, "bind_lexical_revisit_epoch")
            if not bind(epoch_id, job_subject_key=plan.subject_key):
                raise RuntimeError("lexical revisit lease could not bind its plan")
            self._active_discovery_subject_keys.add(plan.subject_key)
        except Exception as exc:
            complete(epoch_id, outcome="failed", result_summary=str(exc)[:500])
            return DeckSeedResult(
                status="deferred",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message=(
                    f"revisit claim for {revisit['term']} remains recoverable: "
                    f"{str(exc)[:220]}"
                ),
            )
        novelty = (
            "new local graph/evidence fingerprint"
            if revisit.get("novelty_kind") == "graph-changed"
            else "expired bounded cooldown"
        )
        return DeckSeedResult(
            status="revisit-queued",
            mode="lexical",
            prepared=int(progress["planned"]),
            total=int(progress["total"]),
            message=(
                f"revisiting {revisit['term']} after {novelty}; epoch "
                f"{revisit['epoch_number']} is durable and loop-safe"
            ),
        )

    def _select_discovery_batch(
        self, seed: str, excluded: set[str]
    ) -> list[dict[str, str]]:
        """Select every source before durable claims or lexical planning."""

        selections: list[dict[str, str]] = []
        blocked = set(excluded)
        groups = self._investigation_groups(blocked)
        if groups:
            group = groups[0]
            for suggestion in list(group.get("terms", ()))[:2]:
                term = str(suggestion.get("term", "")).strip()
                key = self._key(term)
                if not key or key in blocked:
                    continue
                selections.append(
                    {
                        "language": "en",
                        "term": term,
                        "source_kind": "qa-investigation",
                        "source_entity_id": str(group.get("source_entity_id", "")),
                        "source_artifact_id": str(group.get("source_artifact_id", "")),
                        "source_entry_id": "",
                    }
                )
                blocked.add(key)

        base_seed = seed.strip() or f"{time.time_ns()}:lexical:{len(blocked)}"
        draw_batch = getattr(self.corpus, "draw_unseen_words", None)
        if callable(draw_batch):
            for evidence in draw_batch(
                f"{base_seed}:word-origins", blocked, 25
            ):
                key = self._key(evidence.headword)
                if not key or key in blocked:
                    continue
                selections.append(
                    {
                        "language": "en",
                        "term": str(evidence.headword).strip(),
                        "source_kind": "word-origins",
                        "source_entity_id": "",
                        "source_artifact_id": "",
                        "source_entry_id": str(evidence.entry_id),
                    }
                )
                blocked.add(key)
                if len(selections) >= 5:
                    break
            return selections

        # Compatibility path for test doubles and third-party corpus adapters.
        attempts = 0
        while len(selections) < 5 and attempts < 25:
            evidence = self.corpus.draw_unseen_word(
                f"{base_seed}:word-origins:{attempts}", blocked
            )
            attempts += 1
            if evidence is None:
                break
            key = self._key(evidence.headword)
            if not key or key in blocked:
                continue
            selections.append(
                {
                    "language": "en",
                    "term": str(evidence.headword).strip(),
                    "source_kind": "word-origins",
                    "source_entity_id": "",
                    "source_artifact_id": "",
                    "source_entry_id": str(evidence.entry_id),
                }
            )
            blocked.add(key)
        return selections

    def _plan_claimed_round(
        self,
        discoveries: list[dict[str, Any]],
        *,
        progress: dict[str, Any],
        source_fingerprint: str,
        prompt_version: str = "",
    ) -> DeckSeedResult:
        round_id = str(discoveries[0].get("round_id", "")) if discoveries else ""
        planner = PreparationPlanner(
            self.knowledge,
            model=self.model,
            prompt_version=prompt_version or self.prompt_version,
            source_fingerprint=source_fingerprint,
        )
        queued_jobs = 0
        quarantined: list[dict[str, Any]] = []
        planned_discoveries: list[dict[str, Any]] = []
        for discovery in discoveries:
            language = _schedulable_lexical_language(
                discovery.get("language", "en")
            )
            if not language:
                quarantine = getattr(
                    self.knowledge, "quarantine_lexical_discovery", None
                )
                if callable(quarantine):
                    quarantine(
                        discovery,
                        reason=(
                            "historical language label is provenance metadata, "
                            "not a schedulable lexical language code"
                        ),
                        context_key="claimed-discovery-language-filter-v1",
                    )
                quarantined.append(discovery)
                continue
            try:
                plan = _plan_lexical_term(
                    planner,
                    str(discovery["term"]),
                    language,
                )
                queued_jobs += len(plan.jobs)
                if self._plan_has_pending_work(plan):
                    self._active_discovery_subject_keys.add(plan.subject_key)
                self.knowledge.mark_lexical_discovery_planned(
                    str(discovery["discovery_id"])
                )
                planned_discoveries.append(discovery)
            except ValueError as exc:
                quarantine = getattr(
                    self.knowledge, "quarantine_lexical_discovery", None
                )
                if callable(quarantine):
                    quarantine(
                        discovery,
                        reason=f"deterministic lexical planning error: {exc}",
                        context_key="claimed-discovery-planner-filter-v1",
                    )
                quarantined.append(discovery)
                continue
            except Exception as exc:
                return DeckSeedResult(
                    status="deferred",
                    mode="lexical",
                    prepared=int(progress["planned"]),
                    total=int(progress["total"]),
                    message=(
                        f"lexical discovery round {round_id} remains recoverable: "
                        f"{str(exc)[:240]}"
                    ),
                    round_id=round_id,
                    discoveries=tuple(
                        {
                            "term": str(item.get("term", "")),
                            "source_kind": str(item.get("source_kind", "")),
                        }
                        for item in discoveries
                    ),
                )
        if not planned_discoveries and quarantined:
            return DeckSeedResult(
                status="quarantined",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message=(
                    f"quarantined {len(quarantined)} malformed lexical "
                    "candidate(s); the next frontier remains eligible"
                ),
                round_id=round_id,
                discoveries=tuple(
                    {
                        "term": str(item.get("term", "")),
                        "source_kind": str(item.get("source_kind", "")),
                    }
                    for item in quarantined
                ),
            )
        origin_entry = next(
            (
                str(item.get("source_entry_id", ""))
                for item in discoveries
                if item.get("source_kind") == "word-origins"
            ),
            "",
        )
        return DeckSeedResult(
            status="queued",
            mode="lexical",
            source_entry_id=origin_entry,
            prepared=len(self.corpus.lexical_headwords().intersection(self._planned_keys()))
            if isinstance(self.corpus.lexical_headwords(), set)
            else len(
                {self._key(item) for item in self.corpus.lexical_headwords()}.intersection(
                    self._planned_keys()
                )
            ),
            total=int(progress["total"]),
            message=(
                f"planned {len(planned_discoveries)} unseen words, quarantined "
                f"{len(quarantined)}, and queued {queued_jobs} missing-only jobs; "
                "English terms feed Word, Origin, Root, and Affix while other "
                "languages feed the shared semantic Word graph"
            ),
            round_id=round_id,
            discoveries=tuple(
                {
                    "term": str(item.get("term", "")),
                    "source_kind": str(item.get("source_kind", "")),
                }
                for item in discoveries
            ),
        )

    @staticmethod
    def _graph_discovery_selection(candidate: dict[str, Any]) -> dict[str, str]:
        return {
            "language": str(candidate.get("language", "en")),
            "term": str(candidate.get("term", "")).strip(),
            "source_kind": str(
                candidate.get("source_kind", "graph-frontier")
            ).strip(),
            "source_entity_id": str(
                candidate.get("source_entity_id", "")
            ).strip(),
            "source_artifact_id": "",
            "source_entry_id": "",
        }

    def _claim_and_plan_round(
        self,
        selections: list[dict[str, str]],
        *,
        progress: dict[str, Any],
        source_fingerprint: str,
        prompt_version: str = "",
    ) -> DeckSeedResult:
        schedulable: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        quarantine = getattr(
            self.knowledge, "quarantine_lexical_discovery", None
        )
        for selection in selections:
            language = _schedulable_lexical_language(selection.get("language", "en"))
            if language:
                schedulable.append({**selection, "language": language})
                continue
            skipped.append(selection)
            if callable(quarantine):
                quarantine(
                    selection,
                    reason=(
                        "historical language label is provenance metadata, "
                        "not a schedulable lexical language code"
                    ),
                    context_key="discovery-language-filter-v1",
                )
        if not schedulable:
            return DeckSeedResult(
                status="quarantined",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message=(
                    f"quarantined {len(skipped)} unschedulable lexical "
                    "candidate(s) while retaining their graph provenance"
                ),
                discoveries=tuple(
                    {
                        "term": str(item.get("term", "")),
                        "source_kind": str(item.get("source_kind", "")),
                    }
                    for item in skipped
                ),
            )
        observe = getattr(
            self.knowledge, "record_lexical_discovery_observations", None
        )
        if callable(observe):
            observe(schedulable, context_key=f"discovery:{source_fingerprint}")
        claim = self.knowledge.claim_lexical_discovery_round(schedulable)
        if claim is None:
            active_claim = getattr(
                self.knowledge, "has_active_lexical_discovery_claim", None
            )
            if callable(active_claim) and not active_claim():
                return DeckSeedResult(
                    status="exhausted",
                    mode="lexical",
                    prepared=int(progress["planned"]),
                    total=int(progress["total"]),
                    message=(
                        "every offered lexical discovery is already durable or "
                        "planned; continuing to the graph frontier"
                    ),
                )
            return DeckSeedResult(
                status="deferred",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message="another durable discovery claim changed the unseen set",
            )
        return self._plan_claimed_round(
            list(claim["discoveries"]),
            progress=progress,
            source_fingerprint=source_fingerprint,
            prompt_version=prompt_version,
        )

    def _claim_graph_neighbor_round(
        self,
        candidate: dict[str, Any],
        *,
        progress: dict[str, Any],
        source_fingerprint: str,
    ) -> DeckSeedResult:
        return self._claim_and_plan_round(
            [self._graph_discovery_selection(candidate)],
            progress=progress,
            source_fingerprint=source_fingerprint,
            prompt_version=self._frontier_prompt_version(candidate),
        )

    def _run_scheduled_growth(
        self,
        seed: str,
        planned: set[str],
        *,
        progress: dict[str, Any],
        source_fingerprint: str,
    ) -> DeckSeedResult | None:
        """Run one durable four-slot growth decision without starving books."""

        decision_claim = getattr(
            self.knowledge, "claim_lexical_growth_decision", None
        )
        if callable(decision_claim):
            growth_epoch = int(decision_claim())
        else:
            epoch_lookup = getattr(self.knowledge, "lexical_plan_epoch_count", None)
            growth_epoch = (
                int(epoch_lookup()) if callable(epoch_lookup) else len(planned)
            )
        growth_mode = self.GROWTH_CADENCE[
            growth_epoch % len(self.GROWTH_CADENCE)
        ]
        graph_neighbors_cache: list[dict[str, Any]] | None = None
        correction_attempt: DeckSeedResult | None = None
        graph_attempted = False
        revisit_attempted = False

        def graph_neighbors() -> list[dict[str, Any]]:
            nonlocal graph_neighbors_cache
            if graph_neighbors_cache is None:
                graph_neighbors_cache = self._graph_neighbor_terms(planned)
            return graph_neighbors_cache

        # One quarter of durable plans grow an already observed graph frontier.
        # An exhausted candidate is unavailable work, so it must not consume the
        # source-discovery opportunity for this worker boundary.
        if growth_mode == "graph" and graph_neighbors():
            graph_attempted = True
            graph_result = self._claim_graph_neighbor_round(
                self._select_graph_neighbor(graph_neighbors(), growth_epoch),
                progress=progress,
                source_fingerprint=source_fingerprint,
            )
            if graph_result.status != "exhausted":
                return graph_result

        # One quarter is reserved for bounded correction or revisit work. Keep
        # the established sparse/revisit alternation, but fall through when the
        # correction has no runnable work so new source terms keep arriving.
        if growth_mode == "correction":
            sparse_turn = (growth_epoch // 4) % 2 == 0
            sparse = (
                self._sparse_projection_candidate(growth_epoch)
                if sparse_turn
                else None
            )
            if sparse:
                sparse_modes = ", ".join(
                    str(item.get("mode", ""))
                    for item in sparse.get("sparse_views", ())
                )
                correction_attempt = self._plan_recursive_word(
                    str(sparse["term"]),
                    prompt_version=str(sparse["prompt_version"]),
                    status="sparse-repair-queued",
                    message=(
                        f"enriching sparse {sparse_modes} projection for "
                        f"{sparse['term']} with local RAG; branch focuses remain durable"
                    ),
                    progress=progress,
                    language=str(sparse.get("language", "en")),
                )
                if correction_attempt.status != "deferred":
                    return correction_attempt
            revisit_attempted = True
            revisit_result = self._claim_and_plan_revisit(progress)
            if revisit_result is not None:
                correction_attempt = revisit_result
                if revisit_result.status != "deferred":
                    return revisit_result

        # Two quarters retain the complete mixed-source batch: up to two Q&A
        # suggestions followed by Word Origins entries, capped at five terms.
        selections = self._select_discovery_batch(seed, planned)
        if selections:
            discovery_result = self._claim_and_plan_round(
                selections,
                progress=progress,
                source_fingerprint=source_fingerprint,
            )
            if discovery_result.status != "exhausted":
                return discovery_result

        # Once source discovery is exhausted, continuous graph work remains
        # eligible regardless of the scheduled slot. Each path still claims at
        # most one runnable plan at this zero-active worker boundary.
        remaining_graph_neighbors = [] if graph_attempted else graph_neighbors()
        if remaining_graph_neighbors:
            graph_attempted = True
            graph_result = self._claim_graph_neighbor_round(
                self._select_graph_neighbor(remaining_graph_neighbors, growth_epoch),
                progress=progress,
                source_fingerprint=source_fingerprint,
            )
            if graph_result.status != "exhausted":
                return graph_result
        if correction_attempt is not None:
            return correction_attempt
        if not revisit_attempted:
            revisit_result = self._claim_and_plan_revisit(progress)
            if revisit_result is not None:
                return revisit_result
        return None

    def progress(self) -> dict[str, Any]:
        candidates = {self._key(item) for item in self.corpus.lexical_headwords()}
        planned = len(candidates.intersection(self._planned_keys()))
        word_repair_remaining = len(self._missing_word_queries())
        repair_remaining = len(self._missing_origin_queries())
        accepted = {
            self._key(card.get("query", ""))
            for card in self.store.accepted_for_modes(self.MODES)
            if str(card.get("query", "")).strip()
        }
        accepted_count = len(candidates.intersection(accepted))
        total = len(candidates)
        unplanned = max(0, total - planned)
        repair_total = word_repair_remaining + repair_remaining
        return {
            "ready": True,
            "planned": planned,
            "accepted": accepted_count,
            "total": total,
            "remaining": unplanned,
            "repairs": {
                "word": word_repair_remaining,
                "origin": repair_remaining,
                "total": repair_total,
            },
            "work_remaining": unplanned + repair_total,
            "complete": (
                planned >= total
                and word_repair_remaining == 0
                and repair_remaining == 0
            ),
            "modes": list(self.MODES),
            "continuous": True,
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

    def _missing_word_queries(
        self, *, exclude_prompt_version: str = ""
    ) -> tuple[str, ...]:
        """Find terminally failed corpus terms that still lack a Word Card."""

        terminal_lookup = getattr(
            self.knowledge, "terminal_failed_term_keys", None
        )
        if not callable(terminal_lookup):
            return ()
        fingerprint = self.corpus.metadata().get("source_sha256", "")
        terminal = terminal_lookup(
            "en",
            exclude_prompt_version=exclude_prompt_version,
            source_fingerprint=fingerprint,
        )
        accepted = {
            str(card.get("query", "")).strip().casefold()
            for card in self.store.accepted_for_modes(("knowledge",))
            if str(card.get("query", "")).strip()
        }
        display_by_key = {
            headword.casefold(): headword
            for headword in self.corpus.lexical_headwords()
            if headword.strip()
        }
        return tuple(
            display_by_key[key]
            for key in display_by_key
            if key in terminal and key not in accepted
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
        self._reconcile_revisit_epochs()
        planned = self._planned_keys()
        progress = self.progress()
        total = int(progress["total"])
        source_fingerprint = self.corpus.metadata().get("source_sha256", "")
        pending_lookup = getattr(
            self.knowledge, "unplanned_lexical_discoveries", None
        )
        pending = list(pending_lookup()) if callable(pending_lookup) else []
        if pending:
            return self._plan_claimed_round(
                pending,
                progress=progress,
                source_fingerprint=source_fingerprint,
            )
        word_repair_version = f"{self.prompt_version}-word-repair-v1"
        terminal_word_repairs = list(self._missing_word_queries())
        for repair_query in self._missing_word_queries(
            exclude_prompt_version=word_repair_version
        ):
            repair_planner = PreparationPlanner(
                self.knowledge,
                model=self.model,
                prompt_version=word_repair_version,
                source_fingerprint=source_fingerprint,
            )
            repair_plan = repair_planner.plan_word(repair_query)
            if not self._plan_has_pending_work(repair_plan):
                continue
            return DeckSeedResult(
                status="repair-queued",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=total,
                message=(
                    f"queued one bounded full-Word repair for {repair_query}; "
                    "the deterministic repair epoch will not spin"
                ),
            )
        terminal_repairs: list[str] = []
        for repair_query in self._missing_origin_queries():
            planner = PreparationPlanner(
                self.knowledge,
                model=self.model,
                prompt_version=self.prompt_version,
                source_fingerprint=source_fingerprint,
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
                    terminal_repairs.append(repair_query)
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
        growth_result = self._run_scheduled_growth(
            seed,
            planned,
            progress=progress,
            source_fingerprint=source_fingerprint,
        )
        if growth_result is not None:
            return growth_result
        if True:
            # Do not let an exhausted repair monopolize discovery. Once every
            # unseen headword is planned, create a fresh bounded job revision so
            # Knowledge-without-Word cannot become a permanent false completion.
            for repair_query in terminal_repairs:
                retry_planner = PreparationPlanner(
                    self.knowledge,
                    model=self.model,
                    prompt_version=(
                        f"{self.prompt_version}-origin-repair-{time.time_ns()}"
                    ),
                    source_fingerprint=self.corpus.metadata().get(
                        "source_sha256", ""
                    ),
                )
                try:
                    retry_plan = retry_planner.plan_lexical_history_repair(
                        repair_query
                    )
                except ValueError:
                    continue
                if self._plan_has_pending_work(retry_plan):
                    return DeckSeedResult(
                        status="repair-queued",
                        mode="lexical",
                        prepared=int(progress["planned"]),
                        total=total,
                        message=(
                            "queued a fresh bounded history revision for "
                            f"{repair_query}; accepted evidence remains reusable"
                        ),
                    )
            if terminal_word_repairs or terminal_repairs:
                return DeckSeedResult(
                    status="repair-blocked",
                    mode="lexical",
                    prepared=int(progress["planned"]),
                    total=total,
                    message=(
                        "terminal bounded Word or history repairs remain; lexical "
                        "publication is not complete"
                    ),
                )
            return DeckSeedResult(
                status="complete",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=total,
                message="every eligible Word Origins headword is already planned",
            )

    def run_discovery_once(self, seed: str = "") -> DeckSeedResult:
        """Durably claim one new graph or book frontier without running Qwen."""

        self._reconcile_revisit_epochs()
        progress = self.progress()
        source_fingerprint = self.corpus.metadata().get("source_sha256", "")
        for _attempt in range(self.DISCOVERY_BATCH_LIMIT):
            pending = self.knowledge.unplanned_lexical_discoveries()
            if not pending:
                break
            result = self._plan_claimed_round(
                pending,
                progress=progress,
                source_fingerprint=source_fingerprint,
            )
            if result.status != "quarantined":
                return result
        else:
            return DeckSeedResult(
                status="deferred",
                mode="lexical",
                prepared=int(progress["planned"]),
                total=int(progress["total"]),
                message=(
                    "quarantine drain reached its bounded limit; remaining "
                    "frontiers continue on the next planning interval"
                ),
            )
        planned = self._planned_keys()
        growth_result = self._run_scheduled_growth(
            seed,
            planned,
            progress=progress,
            source_fingerprint=source_fingerprint,
        )
        if growth_result is not None:
            return growth_result
        return DeckSeedResult(
            status="complete",
            mode="lexical",
            prepared=int(progress["planned"]),
            total=int(progress["total"]),
            message="no unseen lexical or graph frontier is currently eligible",
        )

    def run_bounded_discovery_once(self, seed: str = "") -> DeckSeedResult:
        """Keep discovery live while bounding all active term subjects."""

        self._reconcile_revisit_epochs()
        projection = self._plan_due_projection_repair()
        if projection is not None:
            return projection
        if self.knowledge.unplanned_lexical_discoveries():
            return self.run_discovery_once(seed)
        durable_active_lookup = getattr(
            self.knowledge, "active_lexical_discovery_subject_keys", None
        )
        if callable(durable_active_lookup):
            self._active_discovery_subject_keys.update(durable_active_lookup())
        revisit_active_lookup = getattr(
            self.knowledge, "active_lexical_revisit_subject_keys", None
        )
        revisit_active_subject_keys: set[str] = set()
        if callable(revisit_active_lookup):
            revisit_active_subject_keys.update(revisit_active_lookup())
            self._active_discovery_subject_keys.update(
                revisit_active_subject_keys
            )
        self._active_discovery_subject_keys = {
            subject_key
            for subject_key in self._active_discovery_subject_keys
            if any(
                _active_lexical_publication_job(
                    job, getattr(self, "prompt_version", "")
                )
                for job in self.knowledge.jobs_for_subject(subject_key)
            )
        }
        if self._active_discovery_subject_keys:
            return DeckSeedResult(
                status="busy",
                mode="lexical",
                message=(
                    f"waiting for {len(self._active_discovery_subject_keys)} "
                    "active discovery subjects before "
                    "claiming another bounded discovery round"
                ),
            )
        return self.run_discovery_once(seed)

    def run_bounded_once(self, seed: str = "") -> DeckSeedResult:
        """Keep autonomous discovery to one unfinished lexical batch at a time."""

        active = self.knowledge.active_term_preparation_count()
        if active:
            return DeckSeedResult(
                status="busy",
                mode="lexical",
                message=f"waiting for {active} active lexical plans to finish",
            )
        return self.run_once(seed)


class AutonomousMorphologySeeder:
    """Queue polished Root and Affix records through atomic lexical preparation."""

    MODES = ("root", "affix")
    BATCH_SIZE = 5
    DEFAULT_MAX_IN_FLIGHT_SUBJECTS = 2
    MAX_ELIGIBILITY_SCAN = 64
    REVISIT_INTERVAL_SECONDS = 6 * 60 * 60
    _DECLARATION_PATTERNS = {
        "root": re.compile(r"(?:词根|字根|\b(?:word\s+)?root\b)", re.IGNORECASE),
        "affix": re.compile(
            r"(?:词缀|前缀|后缀|\b(?:prefix|suffix|affix)\b)",
            re.IGNORECASE,
        ),
    }
    _MORPHOLOGY_FORMULA = re.compile(
        r"(?:\+|(?:^|[\s,;:、*])[-]?[a-z][a-z'\-’]{1,24}-?\s*"
        r"(?:<|=|\(\s*=))",
        re.IGNORECASE,
    )

    def __init__(
        self,
        morphology: dict[str, MorphologyIndex],
        store: CardStore,
        knowledge: KnowledgeStore,
        *,
        model: str,
        modes: Iterable[str] = MODES,
        prompt_version: str = "autonomous-morphology-v1",
        max_in_flight_subjects: int = DEFAULT_MAX_IN_FLIGHT_SUBJECTS,
    ):
        self.morphology = dict(morphology)
        self.store = store
        self.knowledge = knowledge
        self.model = model
        self.prompt_version = prompt_version
        self.max_in_flight_subjects = max(
            1, min(int(max_in_flight_subjects), self.MAX_ELIGIBILITY_SCAN)
        )
        self.modes = tuple(
            dict.fromkeys(
                mode.strip()
                for mode in modes
                if mode.strip() in self.MODES
                and mode.strip() in self.morphology
            )
        )
        if not self.modes:
            raise ValueError("autonomous morphology needs a root or affix index")
        # A reviewed page can still contain OCR continuation, pronunciation,
        # example, or index fragments. Remember rejected source rows for this
        # process so an idle cycle advances instead of repeatedly selecting one.
        self._ineligible_record_ids = {mode: set() for mode in self.modes}

    def _in_flight_preparations(self) -> dict[str, dict[str, set[str]]]:
        """Return active direct-source subjects and records for each mode.

        Origin publication owns this budget, not optional pronunciation,
        translation, grammar, or Word Card jobs. Those continue independently
        after publication ends, without preventing admission of another source.
        """

        active = {
            mode: {"subjects": set(), "entry_ids": set()} for mode in self.modes
        }
        connection = self.knowledge._connect()
        try:
            rows = connection.execute(
                """SELECT DISTINCT subject_key, source_fingerprint
                   FROM preparation_jobs
                   WHERE status IN ('queued', 'running')
                     AND prompt_version LIKE ?
                     AND job_type IN (?, ?, ?, ?, ?)""",
                (f"{self.prompt_version}%", *_LEXICAL_PUBLICATION_JOB_TYPES),
            ).fetchall()
        finally:
            connection.close()
        prefixes = {
            mode: (
                f"{str(self.morphology[mode].metadata().get('source_sha256', '')).strip()}"
                f":{mode}:"
            )
            for mode in self.modes
        }
        for row in rows:
            subject_key = str(row[0] or "").strip()
            source_fingerprint = str(row[1] or "").strip()
            if not subject_key or not source_fingerprint:
                continue
            for mode, prefix in prefixes.items():
                if not source_fingerprint.startswith(prefix):
                    continue
                active[mode]["subjects"].add(subject_key)
                source_tail = source_fingerprint[len(prefix) :]
                entry_id = source_tail.split(":", 1)[0].strip()
                if entry_id:
                    active[mode]["entry_ids"].add(entry_id)
                break
        return active

    @staticmethod
    def _in_flight_count(
        active: dict[str, dict[str, set[str]]],
    ) -> int:
        return sum(len(item["subjects"]) for item in active.values())

    @classmethod
    def _eligible_evidence(cls, mode: str, evidence: Any) -> bool:
        """Require source-owned morphology, not merely a dictionary-shaped row."""

        headword = unicodedata.normalize(
            "NFKC", str(getattr(evidence, "headword", ""))
        ).strip().casefold()
        excerpt = unicodedata.normalize(
            "NFKC", str(getattr(evidence, "excerpt", ""))
        ).strip().casefold()
        if not headword or not excerpt or mode not in cls._DECLARATION_PATTERNS:
            return False
        headword_token = re.compile(
            rf"(?<![\w]){re.escape(headword)}(?![\w])", re.IGNORECASE
        )
        if cls._DECLARATION_PATTERNS[mode].search(excerpt):
            return bool(headword_token.search(excerpt))
        headword_entry = re.match(
            rf"^[^\w]*{re.escape(headword)}(?=$|[\s*#\[\](){{}}:;,.])",
            excerpt,
            re.IGNORECASE,
        )
        return bool(headword_entry and cls._MORPHOLOGY_FORMULA.search(excerpt))

    def _prepared_record_ids(self) -> dict[str, set[str]]:
        prepared = {mode: set() for mode in self.modes}
        corpus_ids = {
            mode: self.morphology[mode].metadata().get("corpus_id", "")
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
        v3 = self.knowledge.morphology_v3_status()
        prepared = self._prepared_record_ids()
        in_flight = self._in_flight_preparations()
        modes: dict[str, dict[str, int | bool]] = {}
        for mode in self.modes:
            total = self.morphology[mode].count()
            accepted = min(len(prepared[mode]), total)
            modes[mode] = {
                "accepted": accepted,
                "total": total,
                "remaining": max(0, total - accepted),
                "complete": accepted >= total,
            }
        accepted_total = sum(int(item["accepted"]) for item in modes.values())
        source_total = sum(int(item["total"]) for item in modes.values())
        legacy = {
            "ready": True,
            "accepted": accepted_total,
            "total": source_total,
            "remaining": max(0, source_total - accepted_total),
            "complete": accepted_total >= source_total,
            "modes": modes,
        }
        return {
            **legacy,
            "legacy_cards": dict(legacy),
            "v3": v3,
            "observed": int(v3.get("observed", 0)),
            "materialized": int(v3.get("materialized", 0)),
            "ambiguous": int(v3.get("ambiguous", 0)),
            "qwen_ready": int(v3.get("qwen_ready", 0)),
            "enrichment": dict(v3.get("enrichment", {})),
            "graph_revision": int(v3.get("graph_revision", 0)),
            "publication_admission": {
                "in_flight": self._in_flight_count(in_flight),
                "limit": self.max_in_flight_subjects,
                "modes": {mode: len(value["subjects"]) for mode, value in in_flight.items()},
                "language_jobs_hold_slots": False,
            },
        }

    def run_once(self, seed: str = "") -> DeckSeedResult:
        prepared = self._prepared_record_ids()
        in_flight = self._in_flight_preparations()
        active_count = self._in_flight_count(in_flight)
        if active_count >= self.max_in_flight_subjects:
            return DeckSeedResult(
                status="busy",
                mode="morphology",
                message=(
                    f"waiting for {active_count} direct morphology subjects; "
                    f"the serial-worker admission budget is "
                    f"{self.max_in_flight_subjects}"
                ),
            )
        ranked = sorted(
            self.modes,
            key=lambda mode: (
                bool(in_flight[mode]["subjects"]),
                (len(prepared[mode]) + len(in_flight[mode]["subjects"]))
                / max(1, self.morphology[mode].count()),
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
        index = self.morphology[mode]
        total = index.count()
        in_flight = self._in_flight_preparations()
        active_count = self._in_flight_count(in_flight)
        available_slots = self.max_in_flight_subjects - active_count
        if available_slots <= 0:
            return DeckSeedResult(
                status="busy",
                mode=mode,
                prepared=len(prepared),
                total=total,
                message=(
                    f"waiting for {active_count} direct morphology subjects; "
                    f"the serial-worker admission budget is "
                    f"{self.max_in_flight_subjects}"
                ),
            )
        admission_limit = min(
            self.BATCH_SIZE, available_slots,
            max(1, self.max_in_flight_subjects // len(self.modes)),
        )
        base_seed = seed.strip() or f"{time.time_ns()}:{mode}:{len(prepared)}"
        metadata = index.metadata()
        corpus_id = str(metadata.get("corpus_id", ""))
        corpus_fingerprint = str(metadata.get("source_sha256", ""))
        outcome_lookup = getattr(self.knowledge, "morphology_source_outcomes", None)
        outcomes = (
            outcome_lookup(mode, corpus_id, corpus_fingerprint)
            if callable(outcome_lookup)
            else {}
        )
        now_epoch = int(time.time())
        durable_blocked = {
            entry_id
            for entry_id, outcome in outcomes.items()
            if str(outcome.get("status", "")) == "ineligible"
            or int(outcome.get("next_eligible_epoch", 0) or 0) > now_epoch
        }
        blocked = (
            set(prepared)
            | self._ineligible_record_ids[mode]
            | durable_blocked
            | in_flight[mode]["entry_ids"]
        )
        evidence_batch: list[Any] = []
        rejected = 0
        unseen_exhausted = False
        draw_batch = getattr(index, "draw_unseen_batch", None)
        if callable(draw_batch):
            candidates = draw_batch(
                f"{base_seed}:batch", blocked, self.MAX_ELIGIBILITY_SCAN
            )
            unseen_exhausted = len(candidates) < self.MAX_ELIGIBILITY_SCAN
        else:
            candidates = []
            for offset in range(self.MAX_ELIGIBILITY_SCAN):
                evidence = index.draw_unseen(
                    f"{base_seed}:batch:{offset}", blocked
                )
                if evidence is None:
                    unseen_exhausted = True
                    break
                candidates.append(evidence)
                blocked.add(str(evidence.entry_id))
        for evidence in candidates:
            entry_id = str(evidence.entry_id)
            blocked.add(entry_id)
            if not self._eligible_evidence(mode, evidence):
                self._ineligible_record_ids[mode].add(entry_id)
                recorder = getattr(
                    self.knowledge, "record_morphology_source_outcome", None
                )
                if callable(recorder):
                    recorder(
                        mode,
                        corpus_id,
                        entry_id,
                        corpus_fingerprint,
                        status="ineligible",
                    )
                rejected += 1
                continue
            evidence_batch.append(evidence)
            if len(evidence_batch) >= admission_limit:
                break
        revisit_epoch: int | None = None
        if not evidence_batch:
            if not unseen_exhausted:
                return DeckSeedResult(
                    status="deferred",
                    mode=mode,
                    prepared=len(prepared),
                    total=total,
                    message=(
                        f"skipped {rejected} bounded {mode} source fragments "
                        "without a declaration or headword morphology formula"
                    ),
                )
            revisit_epoch = int(time.time() // self.REVISIT_INTERVAL_SECONDS)
            revisit_blocked = set(self._ineligible_record_ids[mode])
            if callable(draw_batch):
                revisit_candidates = draw_batch(
                    f"{seed.strip()}:{mode}:revisit:{revisit_epoch}",
                    revisit_blocked,
                    self.MAX_ELIGIBILITY_SCAN,
                )
            else:
                revisit_candidates = []
                for offset in range(self.MAX_ELIGIBILITY_SCAN):
                    evidence = index.draw_unseen(
                        f"{seed.strip()}:{mode}:revisit:{revisit_epoch}:{offset}",
                        revisit_blocked,
                    )
                    if evidence is None:
                        break
                    revisit_candidates.append(evidence)
                    revisit_blocked.add(str(evidence.entry_id))
            for evidence in revisit_candidates:
                entry_id = str(evidence.entry_id)
                revisit_blocked.add(entry_id)
                if not self._eligible_evidence(mode, evidence):
                    self._ineligible_record_ids[mode].add(entry_id)
                    continue
                evidence_batch.append(evidence)
                break
            if not evidence_batch:
                return DeckSeedResult(
                    status="complete",
                    mode=mode,
                    prepared=len(prepared),
                    total=total,
                    message=(
                        f"the polished {mode} corpus has no morphology-declared "
                        "record in the bounded revisit scan"
                    ),
                )

        queued_jobs = 0
        queued_by_entry: dict[str, int] = {}
        errors: list[str] = []
        first_entry_id = str(evidence_batch[0].entry_id)
        for evidence in evidence_batch:
            source_fingerprint = ":".join(
                part
                for part in (
                    str(metadata.get("source_sha256", "")).strip(),
                    mode,
                    str(evidence.entry_id).strip(),
                    str(revisit_epoch) if revisit_epoch is not None else "",
                )
                if part
            )
            try:
                plan = PreparationPlanner(
                    self.knowledge,
                    model=self.model,
                    prompt_version=(
                        f"{self.prompt_version}-revisit-{revisit_epoch}"
                        if revisit_epoch is not None
                        else self.prompt_version
                    ),
                    source_fingerprint=source_fingerprint,
                ).plan_word(evidence.headword)
            except Exception as exc:
                errors.append(f"{evidence.headword}: {str(exc)[:180]}")
                continue
            active_publication_jobs = {
                str(job["job_id"])
                for job in self.knowledge.jobs_for_subject(plan.subject_key)
                if _active_lexical_publication_job(job)
            }
            entry_queued = sum(
                str(job_id) in active_publication_jobs
                for job_id in plan.jobs.values()
            )
            queued_by_entry[str(evidence.entry_id)] = entry_queued
            queued_jobs += entry_queued
        recorder = getattr(self.knowledge, "record_morphology_source_outcome", None)
        if callable(recorder):
            terminal_retry = int(time.time()) + self.REVISIT_INTERVAL_SECONDS
            for evidence in evidence_batch:
                recorder(
                    mode,
                    corpus_id,
                    str(evidence.entry_id),
                    corpus_fingerprint,
                    status="queued" if queued_by_entry.get(str(evidence.entry_id)) else "terminal",
                    next_eligible_epoch=(
                        0 if queued_by_entry.get(str(evidence.entry_id)) else terminal_retry
                    ),
                )
        if not queued_jobs and revisit_epoch is not None:
            return DeckSeedResult(
                status="complete",
                mode=mode,
                source_entry_id=first_entry_id,
                prepared=len(prepared),
                total=total,
                message=(
                    f"every polished {mode} record is represented and "
                    f"revisit epoch {revisit_epoch} is already planned"
                ),
            )
        if not queued_jobs:
            return DeckSeedResult(
                status="deferred",
                mode=mode,
                source_entry_id=first_entry_id,
                prepared=len(prepared),
                total=total,
                message=(
                    f"the persisted {mode} batch has no queued or running work; "
                    f"{'; '.join(errors)[:220] or 'a later repair revision may retry it'}"
                ),
            )
        return DeckSeedResult(
            status="queued",
            mode=mode,
            source_entry_id=first_entry_id,
            prepared=len(prepared),
            total=total,
            message=(
                f"selected {len(evidence_batch)} polished {mode} "
                f"{'revisit' if revisit_epoch is not None else 'records'} and "
                f"queued {queued_jobs} "
                "persisted atomic jobs; the shared lexical graph owns publication"
            ),
        )


class MorphologyV3EnrichmentSeeder:
    """Plan one durable local-Qwen cluster without owning graph writes."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        model_name: str,
        prompt_version: str = "morphology-v3-enrichment-v1",
        scan_limit: int = 192,
    ):
        self.store = store
        self.model_name = model_name.strip() or "local-qwen"
        self.prompt_version = prompt_version.strip()
        self.scan_limit = max(12, min(int(scan_limit), 512))

    def run_once(self, _seed: str = "") -> dict[str, Any]:
        planned = self.store.plan_morphology_v3_enrichment(
            model=self.model_name,
            prompt_version=self.prompt_version,
            scan_limit=self.scan_limit,
        )
        if planned is None:
            return {
                "selected": False,
                "action": "morphology-v3-enrichment",
                "reason": "no eligible retained observation cluster on this bounded page",
                "progress": self.progress(),
            }
        return {
            "selected": True,
            "action": "morphology-v3-enrichment",
            "planned": planned,
            "progress": self.progress(),
        }

    def progress(self) -> dict[str, Any]:
        return self.store.morphology_v3_status()


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
        self._next_mode_index = 0
        self._round = 0
        self._cooldown_until: dict[str, int] = {}

    def counts(self) -> dict[str, int]:
        counts = {mode: 0 for mode in self.MODES}
        for card in self.store.accepted_for_modes(self.MODES):
            mode = str(card.get("mode", ""))
            if mode in counts:
                counts[mode] += 1
        return counts

    def run_once(self) -> DeckSeedResult:
        self._round += 1
        counts = self.counts()
        attempted: set[str] = set()
        pending_result: DeckSeedResult | None = None
        rotated = (
            self.MODES[self._next_mode_index :]
            + self.MODES[: self._next_mode_index]
        )
        eligible = tuple(
            mode
            for mode in self.MODES
            if self._cooldown_until.get(mode, 0) <= self._round
        )
        ranked = sorted(
            eligible or self.MODES,
            key=lambda mode: (counts[mode], rotated.index(mode)),
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
            if result.status in {"busy", "repair-queued", "repair-blocked"}:
                pending_result = pending_result or result
                continue
            self._next_mode_index = (self.MODES.index(mode) + 1) % len(self.MODES)
            if result.status == "deferred":
                # Skip exactly the next balance pass so a persistently sparse,
                # failing mode cannot monopolize every autonomous opportunity.
                self._cooldown_until[mode] = self._round + 2
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
