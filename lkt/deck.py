from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .knowledge import KnowledgeStore
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

        prepared = self._prepared_entry_ids()
        progress = []
        for order, mode in enumerate(self.modes):
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
                    prompt_version="autonomous-content-enrichment-v2",
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
            prepared=sum(len(items) for items in prepared.values()),
            total=sum(self.service.card_books[mode].count() for mode in self.modes),
            message="every configured book record already has an accepted card",
        )
