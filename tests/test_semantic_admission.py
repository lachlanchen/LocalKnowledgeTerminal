from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.preparation import SemanticEnrichmentSeeder
from lkt.semantic_contract import SEMANTIC_JOB_PROMPT_MARKER, SemanticContextUnavailable
from tests.test_semantic_lane import _morphology_fixture, _UnusedRetriever, _SemanticModel


class SemanticAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")
        self.term, self.root, self.evidence = _morphology_fixture(self.store)
        self.seeder = SemanticEnrichmentSeeder(self.store, model="local-admission-test")

    @contextmanager
    def outside_maintenance_page(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                self.store, "_bootstrap_lexical_semantic_enrichments_connection",
                return_value={},
            ))
            stack.enter_context(mock.patch.object(
                self.store, "_reconcile_lexical_semantic_enrichments_connection",
                return_value=[],
            ))
            yield

    def state(self):
        return self.store.lexical_semantic_enrichment_state(self.root)

    def job(self, job_id):
        with closing(self.store._connect()) as connection:
            return dict(connection.execute(
                "SELECT * FROM preparation_jobs WHERE job_id=?", (job_id,)
            ).fetchone())

    def lane(self):
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='semantic_meaning_last_lane_v1'"
            ).fetchone()
            return row[0] if row else None

    def revision(self):
        evidence = self.store.add_evidence(
            "root-dictionary:test", "revised-entry", source_hash="revised-source",
            excerpt="vert (= turn); additional book explanation",
        )
        self.store.link_evidence(self.root, evidence, claim="revised explanation")
        return evidence

    def test_candidate_outside_catalog_page_is_fresh_before_leasing(self):
        with mock.patch(
            "lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "new-admission-policy",
        ), self.outside_maintenance_page():
            current = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
            result = self.seeder.run_once()
            self.assertTrue(result["selected"])
            self.assertEqual(result["desired_fingerprint"], current)
            self.assertEqual(self.state()["desired_fingerprint"], current)

    def test_stale_queued_policy_is_terminal_without_a_model_call(self):
        plan = self.seeder.run_once()
        model = _SemanticModel("E1")
        with mock.patch(
            "lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "new-worker-policy",
        ):
            current = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
            result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
                ("prepare-entity-meaning",)
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(model.prompts, [])
            self.assertEqual(self.state()["desired_fingerprint"], current)
            self.assertNotEqual(self.state()["enrichment_job_id"], plan["job_id"])
            self.assertEqual(self.job(plan["job_id"])["status"], "failed")

    def test_active_binding_is_reconciled_before_older_pending_rows(self):
        plan = self.seeder.run_once()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("UPDATE lexical_semantic_enrichments SET updated_at='2000' WHERE entity_id<>?", (self.root,))
            connection.execute("UPDATE lexical_semantic_enrichments SET updated_at='9999' WHERE entity_id=?", (self.root,))
        with mock.patch(
            "lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "new-active-policy",
        ):
            current = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
            self.store.reconcile_lexical_semantic_enrichments(limit=1)
            self.assertEqual(self.state()["desired_fingerprint"], current)
            self.assertNotEqual(self.state()["enrichment_job_id"], plan["job_id"])
        self.assertEqual(self.job(plan["job_id"])["status"], "queued")

    def test_expired_claim_is_not_hidden_by_pending_backlog(self):
        self.store.claim_lexical_semantic_enrichment()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("UPDATE lexical_semantic_enrichments SET updated_at='2000' WHERE entity_id<>?", (self.root,))
            connection.execute(
                "UPDATE lexical_semantic_enrichments SET updated_at='9999',lease_expires_at='2000' WHERE entity_id=?",
                (self.root,),
            )
        self.store.reconcile_lexical_semantic_enrichments(limit=1)
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(self.state()["lease_token"], "")

    def test_changed_running_morpheme_job_keeps_global_single_flight(self):
        plan = self.seeder.run_once()
        running = self.store.claim_next_job(("prepare-entity-meaning",))
        self.assertEqual(running["job_id"], plan["job_id"])
        before_lane = self.lane()
        with mock.patch(
            "lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "new-running-policy",
        ):
            self.assertIsNone(self.store.claim_lexical_semantic_enrichment())
            self.assertEqual(self.job(plan["job_id"])["status"], "running")
            self.assertEqual(self.job(plan["job_id"])["locked_at"], running["locked_at"])
            self.assertEqual(self.lane(), before_lane)
            self.store.finish_job(plan["job_id"])
            self.assertIsNotNone(self.store.claim_lexical_semantic_enrichment())

    def test_detached_running_semantic_term_is_not_treated_as_idle(self):
        self.store.add_entity_meaning(self.root, "turn", basis="book")
        plan = self.seeder.run_once()
        self.assertEqual(plan["job_type"], "prepare-meaning")
        retrieval = self.store.claim_next_job(("retrieve-evidence",))
        self.store.finish_job(retrieval["job_id"])
        running = self.store.claim_next_job(("prepare-meaning",))
        self.assertEqual(running["job_id"], plan["job_id"])
        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                "UPDATE lexical_semantic_enrichments SET status='pending',enrichment_job_id=NULL,bound_fingerprint='',lease_expires_at='' WHERE entity_id=?",
                (self.term,),
            )
        with self.outside_maintenance_page():
            self.assertIsNone(self.store.claim_lexical_semantic_enrichment())
        self.assertEqual(self.job(plan["job_id"])["status"], "running")

    def test_ordinary_running_meaning_does_not_block_semantic_prefetch(self):
        ordinary = self.store.enqueue_job(
            "prepare-meaning", f"term:{self.term}", subject_entity_id=self.term,
            language="en", priority=40, prompt_version="ordinary-atomic",
            source_fingerprint="ordinary-source",
        )
        self.assertEqual(self.store.claim_next_job(("prepare-meaning",))["job_id"], ordinary)
        plan = self.seeder.run_once()
        self.assertTrue(plan["selected"])
        self.assertEqual(plan["entity_id"], self.root)
        self.assertEqual(self.job(ordinary)["status"], "running")

    def test_source_change_after_claim_recovers_without_planning_or_crashing(self):
        original = self.store.semantic_enrichment_context
        changed = False

        def context(entity_id, language="en"):
            nonlocal changed
            if not changed:
                changed = True
                self.revision()
            return original(entity_id, language)

        with mock.patch.object(self.store, "semantic_enrichment_context", side_effect=context):
            result = self.seeder.run_once()
        self.assertFalse(result["selected"])
        self.assertEqual(result["reason"], "semantic inputs changed before admission")
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(self.state()["lease_token"], "")
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM preparation_jobs").fetchone()[0], 0)
        self.assertTrue(self.seeder.run_once()["selected"])

    def test_book_meaning_arriving_after_claim_is_preserved_without_a_job(self):
        original = self.store.semantic_enrichment_context

        def context(entity_id, language="en"):
            self.store.add_entity_meaning(self.root, "new printed explanation", basis="book")
            return original(entity_id, language)

        with mock.patch.object(self.store, "semantic_enrichment_context", side_effect=context):
            result = self.seeder.run_once()
        self.assertFalse(result["selected"])
        self.assertEqual(self.state()["status"], "applied")
        self.assertEqual(original(self.root)["accepted_meanings"][0]["definition"], "new printed explanation")

    def test_unrelated_planner_exception_is_not_silently_swallowed(self):
        with mock.patch(
            "lkt.preparation.PreparationPlanner.plan_semantic_enrichment",
            side_effect=RuntimeError("unrelated planner defect"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unrelated planner defect"):
                self.seeder.run_once()
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(self.state()["lease_token"], "")

    def test_current_candidate_is_skipped_without_wasting_a_lease(self):
        with mock.patch.object(self.store, "_mark_projection_repairs_for_entities", return_value=0), \
             mock.patch.object(self.store, "_mark_semantic_enrichments_for_entities", return_value=0):
            self.store.add_entity_meaning(self.root, "printed root explanation", basis="book")
        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES ('semantic_meaning_last_lane_v1','missing') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        with self.outside_maintenance_page():
            claim = self.store.claim_lexical_semantic_enrichment()
        self.assertEqual(claim["entity_id"], self.term)
        self.assertEqual(self.state()["status"], "applied")
        self.assertEqual(self.state()["attempt_count"], 0)

    def test_invalid_candidate_work_is_bounded_and_does_not_consume_lane(self):
        with closing(self.store._connect()) as connection, connection:
            connection.execute("UPDATE lexical_semantic_enrichments SET next_eligible_at='9999'")
        identifiers = [
            self.store.upsert_morpheme("en", "invalid" + chr(97 + index), "root", status="accepted")
            for index in range(10)
        ]
        with closing(self.store._connect()) as connection, connection:
            connection.executemany("UPDATE morphemes SET kind='unknown' WHERE entity_id=?", [(item,) for item in identifiers])
        before = self.lane()
        with self.outside_maintenance_page(), mock.patch.object(
            self.store, "_semantic_enrichment_snapshot", wraps=self.store._semantic_enrichment_snapshot,
        ) as snapshot:
            self.assertIsNone(self.store.claim_lexical_semantic_enrichment())
            self.assertEqual(snapshot.call_count, 8)
        self.assertEqual(self.lane(), before)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM lexical_semantic_enrichments WHERE last_error='semantic subject is no longer eligible'"
            ).fetchone()[0], 8)

    def test_current_orphan_job_discovered_by_refresh_is_adopted(self):
        with mock.patch(
            "lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "new-orphan-policy",
        ):
            current = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
            job = self.store.enqueue_job(
                "prepare-entity-meaning", f"semantic:{self.root}:en",
                subject_entity_id=self.root, language="en", priority=7,
                prompt_version=SEMANTIC_JOB_PROMPT_MARKER + "orphan",
                source_fingerprint=current,
            )
            before = self.lane()
            with self.outside_maintenance_page():
                self.assertIsNone(self.store.claim_lexical_semantic_enrichment())
            self.assertEqual(self.state()["status"], "planned")
            self.assertEqual(self.state()["enrichment_job_id"], job)
            self.assertEqual(self.state()["desired_fingerprint"], current)
            self.assertEqual(self.lane(), before)

    def test_book_meaning_arriving_before_execution_avoids_inference(self):
        plan = self.seeder.run_once()
        self.store.add_entity_meaning(self.root, "printed replacement", basis="book")
        model = _SemanticModel("E1")
        result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
            ("prepare-entity-meaning",)
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.state()["status"], "applied")
        self.assertEqual(self.job(plan["job_id"])["status"], "failed")

    def test_ineligible_queued_subject_is_terminal_not_an_ordinary_retry(self):
        plan = self.seeder.run_once()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("UPDATE morphemes SET kind='unknown' WHERE entity_id=?", (self.root,))
        model = _SemanticModel("E1")
        result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
            ("prepare-entity-meaning",)
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(model.prompts, [])
        self.store.reconcile_lexical_semantic_enrichments(limit=1)
        self.assertNotEqual(self.state()["status"], "planned")
        self.assertEqual(self.job(plan["job_id"])["status"], "failed")

    def test_source_change_between_worker_preflight_and_context_is_stale(self):
        self.seeder.run_once()
        original = self.store.semantic_enrichment_context
        changed = False

        def context(entity_id, language="en"):
            nonlocal changed
            if not changed:
                changed = True
                self.revision()
            return original(entity_id, language)

        model = _SemanticModel("E1")
        with mock.patch.object(self.store, "semantic_enrichment_context", side_effect=context):
            result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
                ("prepare-entity-meaning",)
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.state()["desired_fingerprint"], original(self.root)["desired_fingerprint"])
        self.assertNotEqual(self.state()["status"], "planned")

    def test_current_accepted_artifact_can_still_be_recovered_without_inference(self):
        plan = self.seeder.run_once()
        model = _SemanticModel("E1")
        worker = PreparationWorker(self.store, _UnusedRetriever(), model)
        first = worker.run_once(("prepare-entity-meaning",))
        self.assertEqual(first.status, "complete")
        artifact = self.store.artifacts_for_job(plan["job_id"], stage="accepted-entity-meaning")[0]
        recovered = worker._prepare_entity_meaning(self.job(plan["job_id"]))
        self.assertEqual(recovered, artifact["artifact_id"])
        self.assertEqual(len(model.prompts), 1)


    def invalidate_root(self):
        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                "UPDATE morphemes SET kind='unknown' WHERE entity_id=?", (self.root,)
            )

    def test_ineligible_context_has_a_specific_value_error_subtype(self):
        self.invalidate_root()
        with self.assertRaises(SemanticContextUnavailable) as raised:
            self.store.semantic_enrichment_context(self.root)
        self.assertIsInstance(raised.exception, ValueError)
        self.assertIn("entity is not eligible for semantic enrichment", str(raised.exception))

    def test_eligibility_lost_after_claim_recovers_without_creating_a_job(self):
        original = self.store.semantic_enrichment_context

        def context(entity_id, language="en"):
            self.invalidate_root()
            return original(entity_id, language)

        with mock.patch.object(self.store, "semantic_enrichment_context", side_effect=context):
            result = self.seeder.run_once()
        self.assertFalse(result["selected"])
        self.assertEqual(result["reason"], "semantic inputs changed before admission")
        self.assertEqual(self.state()["lease_token"], "")
        self.assertNotIn(self.state()["status"], {"claimed", "planned"})
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM preparation_jobs"
            ).fetchone()[0], 0)
        next_plan = self.seeder.run_once()
        self.assertTrue(next_plan["selected"])
        self.assertNotEqual(next_plan["entity_id"], self.root)

    def test_eligibility_lost_during_inference_preserves_draft_not_accepted_output(self):
        plan = self.seeder.run_once()
        with closing(self.store._connect()) as connection:
            before = self.store._accepted_meaning_records(connection, (self.root,))
        model = _SemanticModel("E1")
        original = model.complete_json

        def complete(*args, **kwargs):
            result = original(*args, **kwargs)
            self.invalidate_root()
            return result

        with mock.patch.object(model, "complete_json", side_effect=complete):
            result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
                ("prepare-entity-meaning",)
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(self.job(plan["job_id"])["status"], "failed")
        self.assertEqual(len(self.store.artifacts_for_job(
            plan["job_id"], stage="entity-meaning-model-draft"
        )), 1)
        self.assertEqual(self.store.artifacts_for_job(
            plan["job_id"], stage="accepted-entity-meaning"
        ), [])
        with closing(self.store._connect()) as connection:
            self.assertEqual(
                self.store._accepted_meaning_records(connection, (self.root,)), before
            )

    def test_unrelated_context_value_error_is_not_hidden_by_admission(self):
        with mock.patch.object(
            self.store, "semantic_enrichment_context",
            side_effect=ValueError("unrelated context defect"),
        ):
            with self.assertRaisesRegex(ValueError, "unrelated context defect"):
                self.seeder.run_once()
        self.assertEqual(self.state()["lease_token"], "")
        self.assertEqual(self.state()["status"], "pending")

    def test_unrelated_context_value_error_keeps_worker_retry_behavior(self):
        plan = self.seeder.run_once()
        model = _SemanticModel("E1")
        with mock.patch.object(
            self.store, "semantic_enrichment_context",
            side_effect=ValueError("unrelated context defect"),
        ):
            result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
                ("prepare-entity-meaning",)
            )
        self.assertEqual(result.status, "retry")
        self.assertEqual(self.job(plan["job_id"])["status"], "queued")
        self.assertIn("unrelated context defect", self.job(plan["job_id"])["error"])
        self.assertEqual(model.prompts, [])


if __name__ == "__main__":
    unittest.main()
