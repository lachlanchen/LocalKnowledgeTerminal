from __future__ import annotations

from contextlib import closing
from pathlib import Path
import unittest

from lkt.atomic import PreparationWorker
from lkt.knowledge import CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION, KnowledgeStore
from lkt.relation_support import reduce_relation_assertions, retire_relation_support
from lkt.store import CardStore
from tests import test_partial_morphology as fixtures
from tests import test_partial_projection as mixed_fixtures


class PartialRepairPlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PartialMorphologyPersistenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.term = self.fixture.term
        self.worker = self.fixture.worker

    def prepare(self, value=None):
        value = fixtures.partial() if value is None else value
        self.job = self.fixture.job("partial-planning-fixture")
        self.fixture.respond(value, value)
        self.worker._split_morphemes(self.job)
        self.accepted = self.fixture.accepted(self.job)
        return self.accepted

    def supported(self):
        return self.store.accepted_morphology_components(self.term, include_partial=True)

    def checkpoints(self):
        excerpt = "Repainted means painted again."
        evidence = self.store.add_evidence(
            "fixture", "repainted-publication", excerpt=excerpt,
            payload={"corpus_id": "fixture", "entry_id": "repainted-publication",
                     "headword": "repainted", "kind": "word-origin", "excerpt": excerpt},
        )
        for stage, payload in (
            ("accepted-meaning", {"definition": "Painted again.", "part_of_speech": "verb",
                                  "evidence_ids": [evidence]}),
            ("accepted-origin-branches", {"branches": [
                {"component_id": part["morpheme_id"], "component_kind": part["kind"], "steps": []}
                for part in self.accepted["parts"]
            ]}),
        ):
            self.store.save_job_artifact(self.job["job_id"], stage, payload,
                                         language="en", validation_state="accepted", quality_score=0.8)

    def retire(self, component, *, reduce=True):
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            owners = connection.execute(
                "SELECT owner_type,owner_id FROM relation_assertion_supports "
                "WHERE assertion_id=? AND status='accepted'", (component["assertion_id"],),
            ).fetchall()
            for owner in owners:
                retire_relation_support(connection, assertion_id=component["assertion_id"],
                                        owner_type=owner[0], owner_id=owner[1])
            if reduce:
                reduce_relation_assertions(connection, [component["assertion_id"]])
            self.store._mark_projection_repairs_for_entities(connection, {self.term})
            connection.commit()

    def test_partial_acceptance_marks_both_modes_without_an_explicit_refresh(self):
        self.prepare()
        state = self.store.lexical_projection_repair_state(self.term)
        self.assertIsNotNone(state)
        self.assertEqual(state["desired_modes"], ["affix", "root"])
        self.assertEqual(state["status"], "pending")
        self.assertEqual(self.store.accepted_morphology_components(self.term), [])
        self.assertEqual(self.store.accepted_morphology_components(
            self.term, include_partial=True, authoritative_only=True,
        ), [])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM term_morphemes WHERE term_id=?", (self.term,),
            ).fetchone()[0], 0)
        self.fixture.assert_invariants()

    def test_mixed_book_prefix_and_partial_root_plan_the_same_modes_as_the_view(self):
        mixed = mixed_fixtures.MixedPartialPublicationTests(methodName="runTest")
        mixed.setUp()
        self.addCleanup(mixed.doCleanups)
        state = mixed.store.refresh_lexical_projection_repair(mixed.term)
        self.assertEqual(state["desired_modes"], ["affix", "root"])
        self.assertEqual([part["kind"] for part in mixed.supported()], ["prefix", "root", "suffix"])

    def test_bootstrap_rescans_partial_only_subjects_after_completed_version_six(self):
        self.prepare()
        with closing(self.store._connect()) as connection:
            connection.execute("DELETE FROM lexical_projection_repairs WHERE term_entity_id=?", (self.term,))
            connection.execute(
                "DELETE FROM lexical_projection_bootstrap_state "
                "WHERE scheduler_key='lexical-projection-contract-v8'"
            )
            connection.execute(
                """INSERT OR REPLACE INTO lexical_projection_bootstrap_state
                   (scheduler_key,cursor_entity_id,scanned_count,complete,updated_at)
                   VALUES ('lexical-projection-contract-v6',?,1,1,'2026-01-01')""", (self.term,),
            )
            connection.commit()
        self.assertEqual(CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION, 8)
        first = self.store.bootstrap_lexical_projection_repairs(limit=1)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["marked"], 1)
        self.assertFalse(first["complete"])
        restarted = KnowledgeStore(self.store.database, serving=True)
        final = restarted.bootstrap_lexical_projection_repairs(limit=1)
        self.assertEqual(final["scanned"], 0)
        self.assertTrue(final["complete"])
        self.assertEqual(final["scanned_total"], 1)
        self.assertEqual(restarted.lexical_projection_repair_state(self.term)["desired_modes"], ["affix", "root"])

    def test_connection_scoped_lookup_sees_uncommitted_partial_membership(self):
        self.prepare()
        root = next(part for part in self.supported() if part["kind"] == "root")
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE relation_assertion_supports SET status='retired' WHERE assertion_id=?",
                (root["assertion_id"],),
            )
            snapshot = self.store._projection_repair_snapshot(connection, self.term)
            self.assertEqual(snapshot[1], ("affix",))
            connection.rollback()
        self.assertEqual(self.store.refresh_lexical_projection_repair(self.term)["desired_modes"], ["affix", "root"])

    def test_book_role_precedence_is_shared_by_planning_and_rendering(self):
        evidence = self.store.add_evidence("fixture", "prefix", excerpt="re- means again")
        prefix = self.store.upsert_morpheme("en", "re-", "prefix", "again", status="accepted")
        self.store.accept_relation_assertion(
            self.term, self.term, prefix, "has-component", basis="book", confidence=0.95,
            evidence_ids=[evidence], owner_type="fixture-book", owner_id="prefix",
            properties={"ordinal": 0, "surface": "re"},
        )
        wrong_role = self.store.upsert_morpheme("und", "re", "root", "fixture proposal", status="accepted")
        self.store.accept_relation_assertion(
            self.term, self.term, wrong_role, "shares-component", basis="model", confidence=0.7,
            owner_type="fixture-model", owner_id="wrong-role",
            properties={"analysis_scope": "partial", "surface": "re", "source_span": [0, 2]},
        )
        self.assertEqual([part["kind"] for part in self.supported()], ["prefix"])
        self.assertEqual(self.store.lexical_projection_repair_state(self.term)["desired_modes"], ["affix"])

    def test_generic_derivative_membership_does_not_create_a_source_repair(self):
        root = self.store.upsert_morpheme("en", "paint", "root", "fixture meaning", status="accepted")
        self.store.accept_relation_assertion(
            self.term, self.term, root, "shares-component", basis="model", confidence=0.7,
            owner_type="fixture-model", owner_id="generic", properties={"surface": "paint"},
        )
        self.assertIsNone(self.store.lexical_projection_repair_state(self.term))
        self.assertEqual(self.store.bootstrap_lexical_projection_repairs()["scanned"], 0)

    def test_cross_word_derivative_is_not_imported_as_its_owners_partial_split(self):
        other = self.store.upsert_term("en", "painter", status="accepted")
        root = self.store.upsert_morpheme("en", "paint", "root", "fixture meaning", status="accepted")
        self.store.accept_relation_assertion(
            self.term, other, root, "shares-component", basis="model", confidence=0.7,
            owner_type="fixture-model", owner_id="derivative",
            properties={"analysis_scope": "partial", "surface": "paint", "source_span": [0, 5]},
        )
        self.assertIsNone(self.store.lexical_projection_repair_state(self.term))
        self.assertIsNone(self.store.lexical_projection_repair_state(other))
        self.assertEqual(self.store.bootstrap_lexical_projection_repairs()["scanned"], 0)

    def test_unowned_accepted_partial_row_is_not_a_bootstrap_candidate(self):
        self.prepare({"parts": [fixtures.part("paint")]})
        root = self.supported()[0]
        self.retire(root, reduce=False)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM relation_assertions WHERE assertion_id=?", (root["assertion_id"],),
            ).fetchone()[0], "accepted")
            connection.execute("DELETE FROM lexical_projection_repairs WHERE term_entity_id=?", (self.term,))
            connection.commit()
        self.assertEqual(self.supported(), [])
        self.assertEqual(self.store.bootstrap_lexical_projection_repairs()["scanned"], 0)
        self.assertIsNone(self.store.lexical_projection_repair_state(self.term))

    def test_history_change_dirties_another_word_sharing_the_partial_root(self):
        self.prepare({"parts": [fixtures.part("paint")]})
        root = self.supported()[0]["morpheme_id"]
        other = self.store.upsert_term("en", "painter", status="accepted")
        self.store.accept_relation_assertion(
            other, other, root, "shares-component", basis="model", confidence=0.7,
            owner_type="fixture-model", owner_id="other-word",
            properties={"analysis_scope": "partial", "surface": "paint", "source_span": [0, 5]},
        )
        historical = self.store.add_historical_form(
            "fro", "peint", period_label="Old French", meaning="painted",
            status="accepted", quality_score=0.6,
        )
        self.store.accept_relation_assertion(
            self.term, root, historical, "has-history", basis="model", confidence=0.6,
            owner_type="fixture-history", owner_id="shared-root",
            properties={"component_id": root},
        )
        before = self.store.lexical_projection_repair_state(other)
        self.assertEqual(before["desired_modes"], ["root"])
        changed = self.store.add_historical_form(
            "fro", "peint", period_label="Old French", meaning="a refined history gloss",
            status="accepted", quality_score=0.75,
        )
        self.assertEqual(changed, historical)
        after = self.store.lexical_projection_repair_state(other)
        self.assertNotEqual(after["desired_fingerprint"], before["desired_fingerprint"])
        self.assertEqual(after["status"], "pending")

    def test_withdrawn_partial_root_changes_modes_without_becoming_positional(self):
        self.prepare()
        before = self.store.lexical_projection_repair_state(self.term)
        root = next(part for part in self.supported() if part["kind"] == "root")
        self.retire(root)
        after = self.store.lexical_projection_repair_state(self.term)
        self.assertEqual(after["desired_modes"], ["affix"])
        self.assertNotEqual(after["desired_fingerprint"], before["desired_fingerprint"])
        self.fixture.assert_invariants()

    def test_retiring_the_last_partial_component_queues_empty_mode_cleanup(self):
        self.prepare({"parts": [fixtures.part("paint")]})
        self.checkpoints()
        self.retire(self.supported()[0])
        state = self.store.lexical_projection_repair_state(self.term)
        self.assertEqual(state["desired_modes"], [])
        claim = self.store.claim_lexical_projection_repair()
        self.assertIsNotNone(claim)
        self.assertEqual(claim["desired_modes"], [])
        self.fixture.assert_invariants()

    def test_partial_only_repair_is_claimed_composed_and_marked_applied(self):
        self.prepare()
        self.checkpoints()
        claim = self.store.claim_lexical_projection_repair()
        self.assertEqual(claim["desired_modes"], ["affix", "root"])
        fingerprint = claim["desired_fingerprint"]
        job_id = self.store.enqueue_job(
            "compose-origin-card", self.fixture.subject, subject_entity_id=self.term,
            prompt_version="autonomous-lexical-v4-projection-local-p" + fingerprint + "-f0",
            source_fingerprint=fingerprint,
        )
        self.assertTrue(self.store.bind_lexical_projection_repair(
            self.term, fingerprint, claim["lease_token"], job_id,
        ))
        self.worker.card_store = CardStore(Path(self.store.database).with_name("cards.sqlite3"))
        self.worker._lexical_view = PreparationWorker._lexical_view.__get__(self.worker)
        self.fixture.model.complete_json.reset_mock()
        result = self.worker.run_once(job_types=("compose-origin-card",))
        self.assertEqual(result.job_id, job_id)
        self.assertEqual(result.status, "complete")
        self.fixture.model.complete_json.assert_not_called()
        accepted = self.store.artifacts_for_job(job_id, stage="accepted-origin-card")[-1]["payload"]
        self.assertEqual({key.split(":")[0] for key in accepted["derived_card_ids"]}, {"root", "affix"})
        state = self.store.lexical_projection_repair_state(self.term)
        self.assertEqual(state["status"], "applied")
        self.assertEqual(state["desired_fingerprint"], state["applied_fingerprint"])
        self.fixture.assert_invariants()
