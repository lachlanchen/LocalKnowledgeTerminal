from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from lkt.atomic import PreparationWorker
from lkt.knowledge import CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION, KnowledgeStore
from lkt.store import CardStore


class ProjectionCleanupPolicyTests(unittest.TestCase):
    @staticmethod
    def _insert_card(
        database: Path,
        card_id: str,
        mode: str,
        query: str,
        branch: str | None,
    ) -> None:
        extensions = {}
        if branch is not None:
            extensions = {
                "branch_focus_id": branch,
                "lexical_view": {"branch_focus_id": branch},
            }
        payload = {
            "card_id": card_id,
            "mode": mode,
            "query": query,
            "title": query,
            "extensions": extensions,
        }
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """INSERT INTO cards(
                       card_id, mode, query, title, created_at, payload, status,
                       revision_of, updated_at, validation_state, validation_errors
                   ) VALUES (?, ?, ?, ?, 'now', ?, 'active', '', 'now',
                             'accepted', '[]')""",
                (card_id, mode, query, query, json.dumps(payload)),
            )

    def test_explicit_empty_authoritative_set_archives_keyed_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "cards.sqlite3"
            store = CardStore(database)
            self._insert_card(database, "old-a", "affix", "inspection", "prefix-in")
            self._insert_card(database, "old-b", "affix", "Inspection", "suffix-old")
            self._insert_card(database, "branchless", "affix", "inspection", None)
            self._insert_card(database, "other", "affix", "respect", "prefix-re")

            refused = store.archive_obsolete_morphology_branches(
                "affix", "inspection", (), authoritative=True
            )
            applied = store.archive_obsolete_morphology_branches(
                "affix",
                "inspection",
                (),
                authoritative=True,
                allow_empty_authoritative=True,
            )

            self.assertEqual(refused["archived_count"], 0)
            self.assertEqual(applied["archived_ids"], ["old-a", "old-b"])
            with closing(sqlite3.connect(database)) as connection, connection:
                statuses = dict(connection.execute("SELECT card_id, status FROM cards"))
            self.assertEqual(statuses["branchless"], "active")
            self.assertEqual(statuses["other"], "active")

    def test_removed_last_component_is_claimable_as_empty_mode_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            term_id = store.upsert_term("en", "mutable", status="accepted")
            component_id = store.upsert_morpheme("en", "mut", "root", "change")
            store.apply_relation_decision(
                term_id,
                term_id,
                component_id,
                "has-component",
                confidence=0.9,
            )
            job_id = store.enqueue_job(
                "projection-test-checkpoints",
                f"term:{term_id}",
                subject_entity_id=term_id,
                language="en",
                model="test-local-qwen",
                prompt_version="projection-empty-mode-test",
            )
            for stage in (
                "accepted-meaning",
                "accepted-morpheme-split",
                "accepted-origin-branches",
            ):
                store.save_job_artifact(
                    job_id, stage, {"stage": stage}, validation_state="accepted"
                )
            state = store.lexical_projection_repair_state(term_id)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET applied_fingerprint = desired_fingerprint, status = 'applied'
                       WHERE term_entity_id = ?""",
                    (term_id,),
                )
                connection.execute(
                    """UPDATE relation_assertions
                       SET status = 'rejected', updated_at = '2099-01-01T00:00:00+00:00'
                       WHERE subject_entity_id = ? AND relation = 'has-component'""",
                    (term_id,),
                )
            refreshed = store.refresh_lexical_projection_repair(term_id)
            claim = store.claim_lexical_projection_repair()

            self.assertIsNotNone(state)
            self.assertEqual(refreshed["desired_modes"], [])
            self.assertEqual(refreshed["status"], "pending")
            self.assertIsNotNone(claim)
            self.assertEqual(claim["term_entity_id"], term_id)
            self.assertEqual(claim["desired_modes"], [])

    def test_repair_persistence_cleans_both_modes_even_when_one_is_empty(self) -> None:
        @dataclass
        class Output:
            card_id: str
            mode: str
            query: str
            extensions: dict = field(default_factory=dict)

        class Cards:
            def __init__(self) -> None:
                self.cleanup: list[tuple[str, str, tuple[str, ...], bool]] = []

            def save(self, _card) -> None:
                return None

            def publish(self, *_args, **_kwargs) -> None:
                return None

            def supersede_others(self, *_args) -> int:
                return 0

            def archive_obsolete_morphology_branches(
                self, mode, query, keys, *, authoritative, allow_empty_authoritative
            ):
                self.cleanup.append(
                    (mode, query, tuple(keys), allow_empty_authoritative)
                )
                return {"archived_count": 0, "archived_ids": []}

        cards = Cards()
        worker = object.__new__(PreparationWorker)
        worker.card_store = cards
        outputs = [
            Output("word", "word", "mutable"),
            Output(
                "root",
                "root",
                "mutable",
                {"branch_focus_id": "root-mut"},
            ),
        ]

        worker._persist_composed_origin_cards(
            outputs, quality=0.9, authoritative_empty_modes=True
        )

        self.assertEqual(
            cards.cleanup,
            [
                ("root", "mutable", ("root-mut",), True),
                ("affix", "mutable", (), True),
            ],
        )

    def test_projection_contract_reinvalidates_existing_outputs(self) -> None:
        self.assertEqual(CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION, 8)


if __name__ == "__main__":
    unittest.main()
