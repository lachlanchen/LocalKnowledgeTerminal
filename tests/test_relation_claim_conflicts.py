from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from lkt.knowledge import KnowledgeStore


class RelationClaimConflictTests(unittest.TestCase):
    @staticmethod
    def component_entities(
        store: KnowledgeStore, name: str
    ) -> tuple[str, str, str]:
        term_id = store.upsert_term("en", name)
        first = store.upsert_morpheme("la", f"{name}-a", "root")
        second = store.upsert_morpheme("la", f"{name}-b", "root")
        return term_id, first, second

    @staticmethod
    def revision(store: KnowledgeStore) -> int:
        with closing(store._connect()) as connection:
            return int(
                connection.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'relation_graph_revision'"
                ).fetchone()[0]
            )

    @staticmethod
    def component_claim(
        store: KnowledgeStore,
        subject_id: str,
        target_id: str,
        **kwargs: object,
    ) -> dict[str, object]:
        return store.apply_relation_decision(
            subject_id,
            subject_id,
            target_id,
            "has-component",
            properties={"ordinal": 0},
            **kwargs,
        )

    @staticmethod
    def preparation_job(
        store: KnowledgeStore,
        subject_id: str,
        suffix: str,
        *,
        prompt_version: str = "morphology-v3",
    ) -> str:
        job_id = f"morphology-job-{suffix}"
        timestamp = "2026-09-02T00:00:00+00:00"
        with closing(store._connect()) as connection:
            connection.execute(
                """INSERT INTO preparation_jobs(
                       job_id, job_key, job_type, subject_entity_id,
                       subject_key, language, priority, max_attempts, model,
                       prompt_version, source_fingerprint, created_at, updated_at
                   ) VALUES (?, ?, 'enrich-morphology-v3', ?, ?, '', 40, 2,
                             'local-qwen', ?, ?, ?, ?)""",
                (
                    job_id,
                    f"morphology-job-key-{suffix}",
                    subject_id,
                    f"morphology-subject-{suffix}",
                    prompt_version,
                    f"morphology-source-epoch-{suffix}",
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        return job_id

    @staticmethod
    def inject_exclusive_claim(
        store: KnowledgeStore,
        assertion_id: str,
        subject_id: str,
        source_id: str,
        target_id: str,
        relation: str,
        incumbent_assertion_id: str,
        *,
        basis: str = "model",
        confidence: float = 0.5,
        properties: dict[str, object] | None = None,
        evidence_ids: tuple[str, ...] = (),
    ) -> None:
        timestamp = "2026-09-01T00:00:00+00:00"
        with closing(store._connect()) as connection:
            slot = connection.execute(
                """SELECT slot_key, policy_version
                   FROM relation_assertion_slots WHERE assertion_id = ?""",
                (incumbent_assertion_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO relation_assertions(
                       assertion_id, subject_entity_id, source_entity_id,
                       target_entity_id, relation, basis, confidence, status,
                       properties_json, revision, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, 1, ?, ?)""",
                (
                    assertion_id,
                    subject_id,
                    source_id,
                    target_id,
                    relation,
                    basis,
                    confidence,
                    __import__("json").dumps(properties or {}, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO relation_assertion_slots(
                       assertion_id, slot_key, slot_mode, policy_version, created_at
                   ) VALUES (?, ?, 'exclusive', ?, ?)""",
                (assertion_id, slot["slot_key"], slot["policy_version"], timestamp),
            )
            for evidence_id in evidence_ids:
                connection.execute(
                    """INSERT INTO assertion_evidence(assertion_id, evidence_id)
                       VALUES (?, ?)""",
                    (assertion_id, evidence_id),
                )
            connection.commit()

    def test_v8_to_v9_backfill_preserves_semantics_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, first, _ = self.component_entities(store, "migration-slot")
            accepted = self.component_claim(store, subject, first, confidence=0.54)
            revision = self.revision(store)
            with closing(store._connect()) as connection:
                connection.execute("DROP TABLE relation_claim_observations")
                connection.execute("DROP TABLE relation_assertion_slots")
                connection.execute(
                    "UPDATE schema_meta SET value = '8' WHERE key = 'schema_version'"
                )
                connection.commit()

            migrated = KnowledgeStore(database)
            self.assertEqual(self.revision(migrated), revision)
            with closing(migrated._connect()) as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                slot = connection.execute(
                    """SELECT slot_key, slot_mode, policy_version
                       FROM relation_assertion_slots WHERE assertion_id = ?""",
                    (accepted["assertion_id"],),
                ).fetchone()
                status = connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id = ?",
                    (accepted["assertion_id"],),
                ).fetchone()[0]
            self.assertEqual(version, "13")
            self.assertTrue(slot["slot_key"])
            self.assertEqual(slot["slot_mode"], "exclusive")
            self.assertTrue(slot["policy_version"])
            self.assertEqual(status, "accepted")

    def test_concurrent_v8_startup_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject, first, _ = self.component_entities(store, "concurrent")
            accepted = self.component_claim(
                store, subject, first, confidence=0.54
            )
            revision = self.revision(store)
            with closing(store._connect()) as connection:
                connection.execute("DROP TABLE relation_claim_observations")
                connection.execute("DROP TABLE relation_assertion_slots")
                connection.execute(
                    "UPDATE schema_meta SET value = '8' WHERE key = 'schema_version'"
                )
                connection.commit()

            barrier = Barrier(8)

            def open_store(_: int) -> str:
                barrier.wait(timeout=10)
                opened = KnowledgeStore(database)
                return opened.status()["schema_version"]

            with ThreadPoolExecutor(max_workers=8) as pool:
                versions = list(pool.map(open_store, range(8)))

            self.assertEqual(set(versions), {"13"})
            self.assertEqual(self.revision(store), revision)
            with closing(store._connect()) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                slots = connection.execute(
                    """SELECT slot_mode, policy_version
                       FROM relation_assertion_slots WHERE assertion_id = ?""",
                    (accepted["assertion_id"],),
                ).fetchall()
            self.assertEqual(integrity, "ok")
            self.assertEqual(len(slots), 1)
            self.assertEqual(
                (slots[0]["slot_mode"], slots[0]["policy_version"]),
                ("exclusive", "relation-slot-v2"),
            )

    def test_unknown_and_derivative_set_claims_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, first, second = self.component_entities(store, "coexist")
            unknown_one = store.apply_relation_decision(
                subject, subject, first, "associated-with", confidence=0.51
            )
            unknown_two = store.apply_relation_decision(
                subject, subject, second, "associated-with", confidence=0.52
            )
            derivative_one = store.apply_relation_decision(
                subject, subject, first, "shares-component", confidence=0.61
            )
            derivative_two = store.apply_relation_decision(
                subject, subject, second, "shares-component", confidence=0.62
            )
            with closing(store._connect()) as connection:
                rows = connection.execute(
                    """SELECT a.assertion_id, a.status, s.slot_mode
                       FROM relation_assertions a
                       JOIN relation_assertion_slots s USING(assertion_id)
                       WHERE a.assertion_id IN (?, ?, ?, ?)""",
                    (
                        unknown_one["assertion_id"],
                        unknown_two["assertion_id"],
                        derivative_one["assertion_id"],
                        derivative_two["assertion_id"],
                    ),
                ).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual({row["status"] for row in rows}, {"accepted"})
            self.assertEqual(
                {row["slot_mode"] for row in rows}, {"none", "set-member"}
            )

    def test_book_conflict_coexists_and_book_blocks_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, first, second = self.component_entities(store, "protected")
            third = store.upsert_morpheme("la", "protected-c", "root")
            first_evidence = store.add_evidence("roots", "protected-entry-a")
            second_evidence = store.add_evidence("roots", "protected-entry-b")
            context = store.add_evidence("origins", "protected-context")
            first_claim = self.component_claim(
                store,
                subject,
                first,
                basis="book",
                confidence=0.82,
                evidence_ids=(first_evidence,),
            )
            conflict = self.component_claim(
                store,
                subject,
                second,
                basis="book",
                confidence=0.84,
                evidence_ids=(second_evidence,),
            )
            blocked = self.component_claim(
                store,
                subject,
                third,
                confidence=0.99,
                evidence_ids=(context,),
                prompt_version="morphology-v2",
            )
            with closing(store._connect()) as connection:
                accepted = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE subject_entity_id = ? AND relation = 'has-component'
                         AND status = 'accepted'""",
                    (subject,),
                ).fetchone()[0]
                decisions = {
                    row[0]
                    for row in connection.execute(
                        """SELECT decision FROM relation_claim_observations
                           WHERE slot_key <> ''"""
                    )
                }
                third_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertions WHERE assertion_id = ?",
                    (blocked["assertion_id"],),
                ).fetchone()[0]
            self.assertEqual(first_claim["action"], "accept")
            self.assertEqual(conflict["slot_decision"], "conflict")
            self.assertEqual(blocked["slot_decision"], "keep")
            self.assertEqual((accepted, third_count), (1, 0))
            self.assertTrue({"conflict", "keep"}.issubset(decisions))

    def test_two_independent_model_epochs_supersede_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, first, second = self.component_entities(store, "consensus")
            incumbent = self.component_claim(
                store, subject, first, confidence=0.50
            )
            revision = self.revision(store)
            evidence_one = store.add_evidence("origins", "consensus-context-a")
            evidence_two = store.add_evidence("origins", "consensus-context-b")
            pending = self.component_claim(
                store,
                subject,
                second,
                confidence=0.70,
                evidence_ids=(evidence_one,),
                prompt_version="morphology-v2",
            )
            replay = self.component_claim(
                store,
                subject,
                second,
                confidence=0.70,
                evidence_ids=(evidence_one,),
                prompt_version="morphology-v2",
            )
            self.assertEqual(self.revision(store), revision)
            promoted = self.component_claim(
                store,
                subject,
                second,
                confidence=0.70,
                evidence_ids=(evidence_two,),
                prompt_version="morphology-v3",
            )
            with closing(store._connect()) as connection:
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE subject_entity_id = ? AND relation = 'has-component'""",
                        (subject,),
                    )
                }
                observations = connection.execute(
                    """SELECT decision, COUNT(*) AS count
                       FROM relation_claim_observations
                       GROUP BY decision ORDER BY decision"""
                ).fetchall()
                archives = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id = ? AND action = 'archive'""",
                    (incumbent["assertion_id"],),
                ).fetchone()[0]
            self.assertEqual((pending["slot_decision"], replay["slot_decision"]), ("pending", "pending"))
            self.assertEqual(promoted["slot_decision"], "supersede")
            self.assertEqual(self.revision(store), revision + 1)
            self.assertEqual(statuses[incumbent["assertion_id"]], "archived")
            self.assertEqual(statuses[promoted["assertion_id"]], "accepted")
            self.assertEqual(archives, 1)
            self.assertEqual(
                {row["decision"]: row["count"] for row in observations},
                {"pending": 1, "supersede": 1},
            )

    def test_morphology_wrapper_promotes_job_epochs_and_excludes_archived_loser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, incumbent_target, proposal_target = self.component_entities(
                store, "wrapper"
            )
            later_target = store.upsert_morpheme("la", "wrapper-c", "root")
            incumbent = self.component_claim(
                store, subject, incumbent_target, confidence=0.50
            )
            evidence_one = store.add_evidence("origins", "wrapper-context-a")
            evidence_two = store.add_evidence("origins", "wrapper-context-b")
            evidence_three = store.add_evidence("origins", "wrapper-context-c")
            job_one = self.preparation_job(store, subject, "wrapper-a")
            job_two = self.preparation_job(store, subject, "wrapper-b")
            job_three = self.preparation_job(store, subject, "wrapper-c")

            def propose(target_id: str, evidence_id: str, job_id: str) -> str:
                return store.accept_relation_assertion(
                    subject,
                    subject,
                    target_id,
                    "has-component",
                    confidence=0.70,
                    properties={
                        "ordinal": 0,
                        "context_evidence_ids": [evidence_id],
                        "enrichment_job_id": job_id,
                    },
                )

            self.assertEqual(propose(proposal_target, evidence_one, job_one), "")
            self.assertEqual(propose(proposal_target, evidence_one, job_one), "")
            promoted_id = propose(proposal_target, evidence_two, job_two)
            self.assertTrue(promoted_id)
            self.assertEqual(propose(later_target, evidence_three, job_three), "")

            with closing(store._connect()) as connection:
                observations = connection.execute(
                    """SELECT proposed_assertion_id, incumbent_assertion_ids_json,
                              decision, context_evidence_ids_json,
                              epoch_fingerprint, prompt_version, job_id
                       FROM relation_claim_observations
                       ORDER BY created_at, observation_id"""
                ).fetchall()
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE subject_entity_id = ?
                             AND relation = 'has-component'""",
                        (subject,),
                    )
                }
            self.assertEqual([row["decision"] for row in observations], [
                "pending", "supersede", "pending"
            ])
            self.assertEqual(
                [row["job_id"] for row in observations],
                [job_one, job_two, job_three],
            )
            self.assertTrue(all(row["prompt_version"] == "morphology-v3" for row in observations))
            self.assertEqual(len({row["epoch_fingerprint"] for row in observations}), 3)
            self.assertEqual(statuses[incumbent["assertion_id"]], "archived")
            self.assertEqual(statuses[promoted_id], "accepted")
            final_incumbents = set(
                __import__("json").loads(observations[-1]["incumbent_assertion_ids_json"])
            )
            self.assertEqual(final_incumbents, {promoted_id})

    def test_history_transition_slot_uses_persisted_source_without_display_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject = store.upsert_term("en", "history-slot")
            component = store.upsert_morpheme("la", "histor", "root")
            source = store.add_historical_form("la", "historicus")
            first = store.add_historical_form("fr", "historique")
            second = store.add_historical_form("fr", "histoire")
            accepted = store.apply_relation_decision(
                subject,
                source,
                first,
                "developed-into",
                confidence=0.55,
                properties={"component_id": component},
            )
            competing = store.apply_relation_decision(
                subject,
                source,
                second,
                "developed-into",
                confidence=0.70,
                properties={"component_id": component},
            )
            self.assertEqual(accepted["action"], "accept")
            self.assertEqual(competing["action"], "observe")
            self.assertEqual(competing["slot_decision"], "pending")
            with closing(store._connect()) as connection:
                slots = connection.execute(
                    """SELECT slot_key, slot_mode, policy_version
                       FROM relation_assertion_slots WHERE assertion_id = ?""",
                    (accepted["assertion_id"],),
                ).fetchone()
            self.assertEqual(slots["slot_mode"], "exclusive")
            self.assertEqual(slots["policy_version"], "relation-slot-v2")

    def test_observation_failure_rolls_back_supersession(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject, first, second = self.component_entities(store, "rollback-slot")
            incumbent = self.component_claim(
                store, subject, first, confidence=0.50
            )
            evidence_one = store.add_evidence("origins", "rollback-context-a")
            evidence_two = store.add_evidence("origins", "rollback-context-b")
            self.component_claim(
                store,
                subject,
                second,
                confidence=0.70,
                evidence_ids=(evidence_one,),
                prompt_version="morphology-v2",
            )
            revision = self.revision(store)
            with self.assertRaises(sqlite3.DatabaseError):
                self.component_claim(
                    store,
                    subject,
                    second,
                    confidence=0.70,
                    evidence_ids=(evidence_two,),
                    prompt_version="morphology-v3",
                    job_id="missing-preparation-job",
                )
            with closing(store._connect()) as connection:
                incumbent_status = connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id = ?",
                    (incumbent["assertion_id"],),
                ).fetchone()[0]
                proposal_count = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE subject_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (subject, second),
                ).fetchone()[0]
                supersede_count = connection.execute(
                    """SELECT COUNT(*) FROM relation_claim_observations
                       WHERE decision = 'supersede'"""
                ).fetchone()[0]
            self.assertEqual(incumbent_status, "accepted")
            self.assertEqual((proposal_count, supersede_count), (0, 0))
            self.assertEqual(self.revision(store), revision)

    def test_startup_prefers_valid_history_target_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject = store.upsert_term("en", "generic-history-owner")
            component = store.upsert_morpheme("la", "generic-root", "root")
            source = store.add_historical_form("la", "generic-earlier")
            valid_target = store.add_historical_form("fr", "generic-later")
            weaker = store.apply_relation_decision(
                subject,
                source,
                component,
                "developed-into",
                confidence=0.99,
                properties={"component_id": component},
            )
            valid_id = "legacy-valid-history-claim"
            self.inject_exclusive_claim(
                store,
                valid_id,
                subject,
                source,
                valid_target,
                "developed-into",
                weaker["assertion_id"],
                confidence=0.40,
                properties={"component_id": component},
            )
            revision = self.revision(store)

            repaired = KnowledgeStore(database)
            self.assertEqual(self.revision(repaired), revision + 1)
            with closing(repaired._connect()) as connection:
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE assertion_id IN (?, ?)""",
                        (weaker["assertion_id"], valid_id),
                    )
                }
                edges = {
                    row["target_entity_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT target_entity_id, status FROM entity_edges
                           WHERE source_entity_id = ? AND relation = 'developed-into'""",
                        (source,),
                    )
                }
                archives = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id = ? AND action = 'archive'""",
                    (weaker["assertion_id"],),
                ).fetchone()[0]
                reconciliations = connection.execute(
                    """SELECT COUNT(*) FROM relation_claim_observations
                       WHERE decision = 'supersede'
                         AND policy_version = 'relation-slot-v2'"""
                ).fetchone()[0]
            self.assertEqual(statuses[weaker["assertion_id"]], "archived")
            self.assertEqual(statuses[valid_id], "accepted")
            self.assertEqual(edges, {component: "archived", valid_target: "accepted"})
            self.assertEqual((archives, reconciliations), (1, 1))

            replay = repaired.apply_relation_decision(
                subject,
                source,
                component,
                "developed-into",
                confidence=1.0,
                properties={"component_id": component},
            )
            self.assertEqual((replay["action"], replay["slot_decision"]), ("observe", "keep"))
            reopened = KnowledgeStore(database)
            self.assertEqual(self.revision(reopened), revision + 1)

    def test_startup_preserves_ambiguous_protected_conflict_as_coexistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject = store.upsert_term("en", "protected-history-owner")
            component = store.upsert_morpheme("la", "protected-history-root", "root")
            source = store.add_historical_form("la", "protected-history-source")
            first_target = store.add_historical_form("fr", "protected-history-a")
            second_target = store.add_historical_form("fr", "protected-history-b")
            first_evidence = store.add_evidence("origins", "protected-history-entry-a")
            second_evidence = store.add_evidence("origins", "protected-history-entry-b")
            first = store.apply_relation_decision(
                subject,
                source,
                first_target,
                "developed-into",
                basis="book",
                confidence=0.82,
                evidence_ids=(first_evidence,),
                properties={"component_id": component},
            )
            second_id = "legacy-protected-history-claim"
            self.inject_exclusive_claim(
                store,
                second_id,
                subject,
                source,
                second_target,
                "developed-into",
                first["assertion_id"],
                basis="book",
                confidence=0.84,
                properties={"component_id": component},
                evidence_ids=(second_evidence,),
            )
            revision = self.revision(store)

            repaired = KnowledgeStore(database)
            self.assertEqual(self.revision(repaired), revision + 1)
            with closing(repaired._connect()) as connection:
                claims = connection.execute(
                    """SELECT assertion.status, slot.slot_mode
                       FROM relation_assertions AS assertion
                       JOIN relation_assertion_slots AS slot USING(assertion_id)
                       WHERE assertion.assertion_id IN (?, ?)
                       ORDER BY assertion.assertion_id""",
                    (first["assertion_id"], second_id),
                ).fetchall()
                conflicts = connection.execute(
                    """SELECT COUNT(*) FROM relation_claim_observations
                       WHERE decision = 'conflict'
                         AND reason LIKE '%source-backed%'"""
                ).fetchone()[0]
                archive_count = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id IN (?, ?) AND action = 'archive'""",
                    (first["assertion_id"], second_id),
                ).fetchone()[0]
            self.assertEqual(
                {(row["status"], row["slot_mode"]) for row in claims},
                {("accepted", "set-member")},
            )
            self.assertEqual((conflicts, archive_count), (1, 0))
            reopened = KnowledgeStore(database)
            self.assertEqual(self.revision(reopened), revision + 1)
            with closing(reopened._connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        """SELECT COUNT(*) FROM relation_claim_observations
                           WHERE decision = 'conflict'
                             AND reason LIKE '%source-backed%'"""
                    ).fetchone()[0],
                    1,
                )

    def test_startup_collision_failure_rolls_back_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject = store.upsert_term("en", "rollback-history-owner")
            component = store.upsert_morpheme("la", "rollback-history-root", "root")
            source = store.add_historical_form("la", "rollback-history-source")
            target = store.add_historical_form("fr", "rollback-history-target")
            incumbent = store.apply_relation_decision(
                subject,
                source,
                component,
                "developed-into",
                properties={"component_id": component},
            )
            injected_id = "legacy-rollback-history-claim"
            self.inject_exclusive_claim(
                store,
                injected_id,
                subject,
                source,
                target,
                "developed-into",
                incumbent["assertion_id"],
                properties={"component_id": component},
            )
            revision = self.revision(store)
            with patch.object(
                KnowledgeStore,
                "_insert_relation_claim_observation",
                side_effect=sqlite3.DatabaseError("forced reconciliation failure"),
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    KnowledgeStore(database)
            with closing(store._connect()) as connection:
                statuses = {
                    row[0]
                    for row in connection.execute(
                        """SELECT status FROM relation_assertions
                           WHERE assertion_id IN (?, ?)""",
                        (incumbent["assertion_id"], injected_id),
                    )
                }
                modes = {
                    row[0]
                    for row in connection.execute(
                        """SELECT slot_mode FROM relation_assertion_slots
                           WHERE assertion_id IN (?, ?)""",
                        (incumbent["assertion_id"], injected_id),
                    )
                }
                archives = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_events
                       WHERE assertion_id IN (?, ?) AND action = 'archive'""",
                    (incumbent["assertion_id"], injected_id),
                ).fetchone()[0]
            self.assertEqual((statuses, modes, archives), ({"accepted"}, {"exclusive"}, 0))
            self.assertEqual(self.revision(store), revision)


if __name__ == "__main__":
    unittest.main()
