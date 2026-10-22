from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.morphology_meaning_prompting import _definition_forms, build_morphology_meaning_prompt
from lkt.preparation import SemanticEnrichmentSeeder
from tests.test_semantic_lane import _morphology_fixture, _UnusedRetriever, _SemanticModel


class ReviewModel(_SemanticModel):
    def __init__(self, definition="first generated explanation", confidence=0.7, callback=None):
        super().__init__("E1")
        self.definition = definition
        self.confidence = confidence
        self.callback = callback

    def complete_json(self, system, prompt, *, max_tokens=256):
        value = super().complete_json(system, prompt, max_tokens=max_tokens)
        value["value"]["meaning"] = self.definition
        value["value"]["confidence"] = self.confidence
        if self.callback:
            self.callback()
        return value


class SemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")
        self.term, self.root, self.evidence = _morphology_fixture(self.store)
        self.seeder = SemanticEnrichmentSeeder(self.store, model="local-review-test")

    def generate(self, definition="first generated explanation", confidence=0.7, callback=None):
        plan = self.seeder.run_once()
        self.assertTrue(plan["selected"])
        self.assertEqual(plan["entity_id"], self.root)
        model = ReviewModel(definition, confidence, callback)
        result = PreparationWorker(self.store, _UnusedRetriever(), model).run_once(
            ("prepare-entity-meaning",)
        )
        return plan, result, model

    def evidence_revision(self, number=2):
        evidence = self.store.add_evidence(
            "root-dictionary:test", f"revised-{number}", source_hash=f"source-{number}",
            excerpt=f"vert (=turn); detailed source revision {number}",
            payload={"headword": "vert", "kind": "root-dictionary"},
        )
        self.store.link_evidence(self.root, evidence, claim="revised root entry", confidence=0.95)
        return evidence

    def meanings(self):
        return self.store.semantic_enrichment_context(self.root)["accepted_meanings"]

    def test_new_evidence_reopens_model_meaning_without_claiming_it_was_applied(self):
        _, result, _ = self.generate()
        self.assertEqual(result.status, "complete")
        before = self.store.lexical_semantic_enrichment_state(self.root)
        old = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        dirty = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertEqual(dirty["status"], "pending")
        self.assertNotEqual(dirty["desired_fingerprint"], before["applied_fingerprint"])
        self.assertEqual(dirty["applied_fingerprint"], before["applied_fingerprint"])
        self.assertEqual(self.meanings()[0]["meaning_id"], old)
        plan = self.seeder.run_once()
        self.assertEqual(plan["entity_id"], self.root)
        self.assertEqual(self.store.lexical_semantic_enrichment_state(self.root)["status"], "planned")

    def test_successful_review_replaces_only_prior_owned_output_and_updates_cache(self):
        self.generate()
        old = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        plan, result, model = self.generate("improved generated explanation")
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(model.prompts), 1)
        meanings = self.meanings()
        self.assertEqual([item["definition"] for item in meanings], ["improved generated explanation"])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT status FROM entities WHERE entity_id=?", (old,)).fetchone()[0], "archived")
            self.assertEqual(db.execute("SELECT meaning FROM morphemes WHERE entity_id=?", (self.root,)).fetchone()[0], "improved generated explanation")
        state = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertEqual(state["status"], "applied")
        self.assertEqual(state["applied_fingerprint"], plan["desired_fingerprint"])
        self.assertTrue(state["next_eligible_at"])
        self.assertEqual(self.store.accepted_morphology_components(self.term)[0]["meaning"], "improved generated explanation")

    def test_matching_text_can_lower_confidence_and_update_model_provenance(self):
        self.generate("same explanation", confidence=0.7)
        old_id = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        plan, result, _ = self.generate("same explanation", confidence=0.2)
        self.assertEqual(result.status, "complete")
        meaning = self.meanings()[0]
        self.assertEqual(meaning["meaning_id"], old_id)
        self.assertAlmostEqual(meaning["confidence"], 0.2)
        self.assertEqual(meaning["properties"]["semantic_enrichment_job_id"], plan["job_id"])
        self.assertEqual(meaning["properties"]["source_fingerprint"], plan["desired_fingerprint"])

    def test_book_and_reviewed_meanings_are_preserved(self):
        self.generate()
        book = self.store.add_entity_meaning(self.root, "independent printed sense", basis="book", evidence_ids=(self.evidence,))
        reviewed = self.store.add_entity_meaning(self.root, "independent reviewed sense", basis="reviewed")
        self.evidence_revision()
        _, result, _ = self.generate("new generated explanation")
        self.assertEqual(result.status, "complete")
        ids = {item["meaning_id"] for item in self.meanings()}
        self.assertIn(book, ids)
        self.assertIn(reviewed, ids)

    def test_book_only_meaning_does_not_force_a_model_review(self):
        self.store.add_entity_meaning(self.root, "printed definition", basis="book")
        self.evidence_revision()
        context = self.store.semantic_enrichment_context(self.root)
        self.assertFalse(context["needs_semantic_review"])
        self.assertTrue(context["has_current_meaning"])
        self.assertNotEqual(self.seeder.run_once().get("entity_id"), self.root)

    def test_different_language_meaning_does_not_fill_english_gap(self):
        self.store.add_entity_meaning(self.root, "other language definition", language="zh", basis="book")
        context = self.store.semantic_enrichment_context(self.root, "en")
        self.assertFalse(context["has_accepted_meaning"])
        self.assertEqual(context["accepted_meanings"], [])
        self.assertEqual(self.seeder.run_once()["entity_id"], self.root)

    def test_current_result_does_not_requeue_itself_or_change_input_fingerprint(self):
        plan, result, _ = self.generate()
        self.assertEqual(result.status, "complete")
        for _ in range(3):
            self.store.refresh_lexical_semantic_enrichment(self.root)
            context = self.store.semantic_enrichment_context(self.root)
            state = self.store.lexical_semantic_enrichment_state(self.root)
            self.assertEqual(context["desired_fingerprint"], plan["desired_fingerprint"])
            self.assertFalse(context["needs_semantic_review"])
            self.assertEqual(state["status"], "applied")

    def test_second_review_has_cooldown_despite_new_evidence(self):
        self.generate()
        self.evidence_revision()
        self.generate("revised explanation")
        previous = self.store.lexical_semantic_enrichment_state(self.root)
        self.evidence_revision(3)
        dirty = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertEqual(dirty["status"], "pending")
        self.assertTrue(previous["next_eligible_at"])
        self.assertEqual(dirty["next_eligible_at"], previous["next_eligible_at"])
        self.assertNotEqual(self.store.claim_lexical_semantic_enrichment().get("entity_id"), self.root)

    def test_failure_preserves_last_good_meaning(self):
        self.generate()
        old = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        _, result, model = self.generate("")
        self.assertEqual(result.status, "retry")
        worker = PreparationWorker(self.store, _UnusedRetriever(), model)
        worker.run_once(("prepare-entity-meaning",))
        self.assertEqual(self.meanings()[0]["meaning_id"], old)
        self.assertEqual(self.store.lexical_semantic_enrichment_state(self.root)["status"], "cooldown")

    def test_changed_source_during_inference_cannot_publish_stale_output(self):
        self.generate()
        old = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        plan, result, model = self.generate("stale new explanation", callback=lambda: self.evidence_revision(3))
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(self.meanings()[0]["meaning_id"], old)
        self.assertEqual(self.store.artifacts_for_job(plan["job_id"], stage="accepted-entity-meaning"), [])

    def test_completion_failure_rolls_back_new_meaning_and_accepted_artifact(self):
        self.generate()
        old = self.meanings()[0]["meaning_id"]
        self.evidence_revision()
        with mock.patch.object(self.store, "complete_lexical_semantic_enrichment", return_value=False):
            plan, result, _ = self.generate("must roll back")
        self.assertEqual(result.status, "failed")
        self.assertEqual([item["meaning_id"] for item in self.meanings()], [old])
        self.assertEqual(self.store.artifacts_for_job(plan["job_id"], stage="accepted-entity-meaning"), [])
        self.assertEqual(len(self.store.artifacts_for_job(plan["job_id"], stage="entity-meaning-model-draft")), 1)

    def test_existing_model_meaning_is_not_a_completion_receipt(self):
        self.store.add_entity_meaning(self.root, "legacy model explanation", basis="model")
        context = self.store.semantic_enrichment_context(self.root)
        self.assertTrue(context["needs_semantic_review"])
        self.assertFalse(self.store.complete_lexical_semantic_enrichment(self.root, context["desired_fingerprint"]))
        self.assertNotEqual(self.store.lexical_semantic_enrichment_state(self.root)["status"], "applied")

    def test_new_missing_meaning_gets_turn_after_review_and_survives_reopen(self):
        self.generate()
        self.evidence_revision()
        self.generate("revised explanation")
        reopened = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3", serving=True)
        claim = reopened.claim_lexical_semantic_enrichment()
        self.assertEqual(claim["semantic_lane"], "missing")
        self.assertEqual(claim["entity_id"], self.term)

    def test_active_claim_does_not_consume_alternation_receipt(self):
        claim = self.store.claim_lexical_semantic_enrichment()
        with closing(self.store._connect()) as db:
            before = db.execute("SELECT value FROM schema_meta WHERE key='semantic_meaning_last_lane_v1'").fetchone()[0]
        self.assertIsNone(self.store.claim_lexical_semantic_enrichment())
        with closing(self.store._connect()) as db:
            after = db.execute("SELECT value FROM schema_meta WHERE key='semantic_meaning_last_lane_v1'").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(claim["semantic_lane"], "missing")

    def test_policy_upgrade_reopens_owned_meaning_with_bounded_bootstrap(self):
        self.generate()
        before = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
        with mock.patch("lkt.semantic_contract.MORPHOLOGY_MEANING_PROMPT_POLICY", "next-policy"):
            page = self.store.bootstrap_lexical_semantic_enrichments(1)
            self.assertEqual(page["scanned"], 1)
            context = self.store.semantic_enrichment_context(self.root)
            self.assertNotEqual(context["desired_fingerprint"], before)
            self.assertTrue(context["needs_semantic_review"])
            self.assertTrue(context["accepted_meanings"])

    def test_unowned_model_alternative_is_not_retired_by_semantic_worker(self):
        self.generate()
        independent = self.store.add_entity_meaning(self.root, "another model-owned sense", basis="model")
        self.evidence_revision()
        _, result, _ = self.generate("new semantic explanation")
        self.assertEqual(result.status, "complete")
        self.assertIn(independent, {item["meaning_id"] for item in self.meanings()})

    def test_draft_or_mismatched_artifact_cannot_certify_revision(self):
        meaning = self.store.add_entity_meaning(self.root, "legacy explanation", basis="model")
        plan = self.seeder.run_once()
        self.store.save_job_artifact(
            plan["job_id"], "accepted-entity-meaning",
            {"entity_id": self.root, "meaning_id": meaning, "language": "en", "source_fingerprint": "wrong"},
            validation_state="accepted",
        )
        self.assertFalse(self.store.complete_lexical_semantic_enrichment(self.root, plan["desired_fingerprint"], enrichment_job_id=plan["job_id"]))

    def model_input(self, snapshot):
        return build_morphology_meaning_prompt(
            entity_id=self.root, label="vert", kind="root", language="en",
            language_name="English", context=snapshot["context"],
        )[:2]

    def test_duplicate_neighbor_link_does_not_review_identical_model_input(self):
        self.generate()
        before = self.store.semantic_enrichment_context(self.root)
        self.store.link_evidence(
            self.term, self.evidence, claim="also linked to derivative", confidence=0.9,
        )
        after = self.store.semantic_enrichment_context(self.root)
        self.assertEqual(self.model_input(before), self.model_input(after))
        self.assertEqual(before["desired_fingerprint"], after["desired_fingerprint"])
        self.assertEqual(len(after["context"]["evidence"]), 1)
        self.assertEqual(after["context"]["evidence"][0]["entity_id"], self.root)
        self.assertFalse(after["needs_semantic_review"])
        self.assertEqual(self.store.lexical_semantic_enrichment_state(self.root)["status"], "applied")
        self.assertEqual(self.seeder.run_once()["entity_id"], self.term)

    def test_link_claim_and_confidence_do_not_reopen_same_source(self):
        self.generate()
        before = self.store.semantic_enrichment_context(self.root)
        self.store.link_evidence(
            self.root, self.evidence, claim="exact reviewed source for vert", confidence=0.99,
        )
        self.store.link_evidence(
            self.root, self.evidence, claim="another annotation of the same passage", confidence=0.8,
        )
        after = self.store.semantic_enrichment_context(self.root)
        self.assertEqual(before["context"], after["context"])
        self.assertEqual(before["desired_fingerprint"], after["desired_fingerprint"])
        self.assertFalse(after["needs_semantic_review"])

    def test_duplicate_links_cannot_use_up_the_unique_source_budget(self):
        other = self.store.add_evidence(
            "root-dictionary:test", "derivative-history", source_hash="history-v1",
            excerpt="revert: a different historical discussion of vert",
        )
        self.store.link_evidence(self.term, other, claim="derivative history")
        for index in range(194):
            self.store.link_evidence(
                self.root, self.evidence, claim=f"duplicate annotation {index}",
            )
        context = self.store.semantic_enrichment_context(self.root)["context"]
        self.assertEqual(
            {item["evidence_id"] for item in context["evidence"]},
            {self.evidence, other},
        )
        self.assertEqual(len(context["evidence"]), 2)

    def test_actual_source_content_hash_and_locator_changes_reopen_review(self):
        self.generate()
        for field, value in (
            ("excerpt", "vert (= turn); a corrected historical account"),
            ("source_hash", "corrected-source-hash"),
            ("locator", "corrected book location"),
        ):
            with self.subTest(field=field):
                before = self.store.semantic_enrichment_context(self.root)
                with closing(self.store._connect()) as db, db:
                    db.execute(
                        f"UPDATE evidence_records SET {field}=? WHERE evidence_id=?",
                        (value, self.evidence),
                    )
                self.store.refresh_lexical_semantic_enrichment(self.root)
                after = self.store.semantic_enrichment_context(self.root)
                self.assertNotEqual(before["desired_fingerprint"], after["desired_fingerprint"])
                self.assertTrue(after["needs_semantic_review"])
                self.assertTrue(after["accepted_meanings"])

    def test_target_ownership_change_preserves_meaningful_prompt_scope(self):
        with closing(self.store._connect()) as db, db:
            db.execute(
                "UPDATE entity_evidence SET entity_id=? WHERE entity_id=? AND evidence_id=?",
                (self.term, self.root, self.evidence),
            )
        before = self.store.semantic_enrichment_context(self.root)
        self.store.link_evidence(self.root, self.evidence, claim="target source ownership")
        after = self.store.semantic_enrichment_context(self.root)
        self.assertEqual(before["context"]["evidence"][0]["entity_id"], self.term)
        self.assertEqual(after["context"]["evidence"][0]["entity_id"], self.root)
        self.assertNotEqual(self.model_input(before), self.model_input(after))
        self.assertNotEqual(before["desired_fingerprint"], after["desired_fingerprint"])

    def test_ocr_hint_does_not_join_chinese_prose_to_latin_root(self):
        self.assertEqual(_definition_forms("\u8352\u51c9\u4e4b\u5730\uff07sol\uff08\uff1dalone\uff09"), ["sol"])
        self.assertEqual(_definition_forms("\u5b66\uff08\uff1dlearning\uff09"), ["\u5b66"])


if __name__ == "__main__":
    unittest.main()
