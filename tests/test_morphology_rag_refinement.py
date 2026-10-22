from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_partial_morphology as fixtures


class MorphologyRagRefinementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PartialMorphologyPersistenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.worker = self.fixture.worker
        self.term = self.fixture.term
        record = self.store.artifacts_for_subject(
            self.fixture.subject, stage="retrieved-evidence",
        )[-1]["payload"]["records"][0]
        self.evidence = record["knowledge_evidence_id"]

    def job(self, epoch):
        job_id = self.store.enqueue_job(
            "split-morphemes", self.fixture.subject, subject_entity_id=self.term,
            prompt_version="rag-refinement-test", source_fingerprint=epoch,
        )
        return {"job_id": job_id, "subject_entity_id": self.term,
                "subject_key": self.fixture.subject,
                "prompt_version": "rag-refinement-test"}

    def split(self, epoch, value=None):
        job = self.job(epoch)
        self.fixture.respond(fixtures.complete() if value is None else value)
        self.worker._split_morphemes(job)
        return job, self.fixture.accepted(job)

    def claims(self):
        with closing(self.store._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM relation_assertions WHERE subject_entity_id=?",
                (self.term,),
            )]

    def status(self, assertion_id):
        with closing(self.store._connect()) as db:
            return db.execute(
                "SELECT status FROM relation_assertions WHERE assertion_id=?",
                (assertion_id,),
            ).fetchone()[0]

    def seed_incumbent(self, basis="model"):
        target = self.store.upsert_morpheme(
            "en", "repainted", "free", "painted again", status="accepted",
        )
        assertion = self.store.accept_relation_assertion(
            self.term, self.term, target, "has-component", basis=basis,
            confidence=0.8, properties={"ordinal": 0, "surface": "repainted"},
            evidence_ids=[self.evidence] if basis in {"book", "reviewed"} else [],
            context_evidence_ids=[self.evidence], owner_type="atomic-split",
            owner_id=self.term, owner_version="legacy",
        )
        self.store.link_morpheme(
            self.term, target, 0, "repainted", basis=basis, confidence=0.8,
        )
        return assertion, target

    def candidate(self, form="paint"):
        return self.store.upsert_morpheme(
            "en", form, "root", "fixture meaning", status="accepted",
        )

    def propose(self, target, epoch, confidence=0.8, context=True):
        return self.store.apply_relation_decision(
            self.term, self.term, target, "has-component", basis="model",
            confidence=confidence, properties={"ordinal": 0, "surface": "re"},
            context_evidence_ids=[self.evidence] if context else [],
            prompt_version="rag-refinement-test", epoch_fingerprint=epoch,
            reason="atomic morpheme split",
        )

    def test_real_model_input_context_reaches_claims_without_becoming_citations(self):
        _, accepted = self.split("context")
        inference = accepted["morphology_inference"]
        self.assertTrue(inference["input_fingerprint"])
        self.assertTrue(inference["draft_artifact_id"])
        self.assertEqual(inference["context"]["selected_evidence_ids"], [self.evidence])
        for claim in self.claims():
            self.assertEqual(claim["basis"], "model")
            self.assertEqual(json.loads(claim["properties_json"])["context_evidence_ids"], [self.evidence])
        self.assertTrue(all(part["context_evidence_ids"] == [self.evidence] for part in accepted["parts"]))
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assertion_evidence").fetchone()[0], 0)

    def test_generated_provenance_cannot_replace_code_owned_input_context(self):
        unrelated = self.store.add_evidence("unrelated", "not-in-the-prompt")
        value = fixtures.complete()
        value["morphology_inference"] = {"context": {"selected_evidence_ids": [unrelated]}}
        for part in value["parts"]:
            part["evidence_ids"] = [unrelated, "made-up-evidence"]
            part["context_evidence_ids"] = [unrelated]
        _, accepted = self.split("generated-ids", value)
        self.assertEqual(accepted["morphology_inference"]["context"]["selected_evidence_ids"], [self.evidence])
        for claim in self.claims():
            self.assertEqual(json.loads(claim["properties_json"])["context_evidence_ids"], [self.evidence])
            self.assertEqual(claim["basis"], "model")

    def test_partial_memberships_keep_context_without_fabricating_slots(self):
        job = self.job("partial")
        self.fixture.respond(fixtures.partial(), fixtures.partial())
        self.worker._split_morphemes(job)
        accepted = self.fixture.accepted(job)
        self.assertEqual(accepted["analysis_scope"], "partial")
        for claim in self.claims():
            self.assertEqual(claim["relation"], "shares-component")
            self.assertEqual(json.loads(claim["properties_json"])["context_evidence_ids"], [self.evidence])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM term_morphemes").fetchone()[0], 0)
        self.fixture.assert_invariants()

    def test_derivative_proposals_retain_context_but_remain_model_claims(self):
        value = fixtures.complete()
        value["derivatives"] = [{"term": "painter", "component_forms": ["paint"]}]
        self.split("derivatives", value)
        derivative = next(claim for claim in self.claims() if claim["source_entity_id"] != self.term)
        self.assertEqual(derivative["relation"], "shares-component")
        self.assertEqual(derivative["basis"], "model")
        self.assertEqual(json.loads(derivative["properties_json"])["context_evidence_ids"], [self.evidence])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM assertion_evidence WHERE assertion_id=?",
                (derivative["assertion_id"],),
            ).fetchone()[0], 0)

    def test_two_fresh_same_context_visits_can_replace_a_capped_whole_word_split(self):
        incumbent, _ = self.seed_incumbent()
        first_job, first = self.split("first")
        self.assertEqual(first["analysis_scope"], "partial")
        self.assertEqual(self.status(incumbent), "accepted")
        _, second = self.split("second")
        self.assertEqual(second["analysis_scope"], "complete")
        self.assertEqual([part["surface"] for part in second["parts"]], ["re", "paint", "ed"])
        self.assertEqual(self.status(incumbent), "archived")
        self.assertNotEqual(first["morphology_inference"]["draft_artifact_id"], second["morphology_inference"]["draft_artifact_id"])
        self.assertEqual(first["morphology_inference"]["context"], second["morphology_inference"]["context"])
        with closing(self.store._connect()) as db:
            first_observation = db.execute(
                "SELECT reason,epoch_fingerprint FROM relation_claim_observations WHERE job_id=?",
                (first_job["job_id"],),
            ).fetchone()
            self.assertIn("second independent", first_observation["reason"])
            self.assertTrue(first_observation["epoch_fingerprint"])
            self.assertEqual(db.execute(
                "SELECT count(*) FROM relation_assertion_events WHERE assertion_id=? AND action='archive'",
                (incumbent,),
            ).fetchone()[0], 1)
        self.fixture.assert_invariants()

    def test_cached_draft_retry_does_not_manufacture_an_independent_vote(self):
        incumbent, _ = self.seed_incumbent()
        job, first = self.split("same-job")
        context = self.store.artifacts_for_job(job["job_id"], stage="morpheme-prompt-context")[-1]["payload"]["context"]
        self.fixture.model.complete_json.reset_mock()
        with patch("lkt.atomic.pack_morpheme_context", return_value=deepcopy(context)):
            self.worker._split_morphemes(job)
        self.fixture.model.complete_json.assert_not_called()
        replay = self.fixture.accepted(job)
        self.assertEqual(replay["morphology_inference"]["fresh_model_calls"], 0)
        self.assertEqual(first["morphology_inference"]["draft_artifact_id"], replay["morphology_inference"]["draft_artifact_id"])
        self.assertEqual(self.status(incumbent), "accepted")
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM relation_claim_observations WHERE decision='pending'").fetchone()[0], 1)

    def test_failed_final_persistence_rolls_back_correction_and_observation(self):
        incumbent, _ = self.seed_incumbent()
        self.split("before-failure")
        job = self.job("failed-correction")
        self.fixture.respond(fixtures.complete())
        original = self.store.save_job_artifact

        def save(*args, **kwargs):
            if args[1] == "accepted-morpheme-split":
                raise sqlite3.OperationalError("fixture finalization failure")
            return original(*args, **kwargs)

        with patch.object(self.store, "save_job_artifact", side_effect=save):
            with self.assertRaises(sqlite3.OperationalError):
                self.worker._split_morphemes(job)
        self.assertEqual(self.status(incumbent), "accepted")
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM relation_claim_observations WHERE decision='supersede'").fetchone()[0], 0)
        self.fixture.assert_invariants()

    def test_equal_confidence_needs_two_evidenced_epochs_and_preserves_reason(self):
        incumbent, _ = self.seed_incumbent()
        candidate = self.candidate()
        first = self.propose(candidate, "one")
        replay = self.propose(candidate, "one")
        self.assertEqual(first["slot_decision"], "pending")
        self.assertEqual(replay["slot_decision"], "pending")
        self.assertIn("second independent", first["reason"])
        second = self.propose(candidate, "two")
        self.assertEqual(second["slot_decision"], "supersede")
        self.assertEqual(self.status(incumbent), "archived")

    def test_unevidenced_revisits_do_not_supersede(self):
        incumbent, _ = self.seed_incumbent()
        candidate = self.candidate()
        for epoch in ("one", "two", "three"):
            decision = self.propose(candidate, epoch, confidence=1.0, context=False)
            self.assertEqual(decision["slot_decision"], "pending")
            self.assertIn("evidenced prompt epoch", decision["reason"])
        self.assertEqual(self.status(incumbent), "accepted")

    def test_lower_confidence_observations_do_not_supply_a_strong_vote(self):
        incumbent, _ = self.seed_incumbent()
        candidate = self.candidate()
        self.assertEqual(self.propose(candidate, "weak", confidence=0.7)["slot_decision"], "keep")
        self.assertEqual(self.propose(candidate, "first-strong")["slot_decision"], "pending")
        self.assertEqual(self.status(incumbent), "accepted")
        self.assertEqual(self.propose(candidate, "second-strong")["slot_decision"], "supersede")

    def test_book_authority_survives_repeated_model_votes(self):
        incumbent, _ = self.seed_incumbent("book")
        candidate = self.candidate()
        for epoch in ("one", "two", "three"):
            self.assertEqual(self.propose(candidate, epoch, confidence=1.0)["slot_decision"], "keep")
        self.assertEqual(self.status(incumbent), "accepted")

    def test_reviewed_authority_survives_repeated_model_votes(self):
        incumbent, _ = self.seed_incumbent("reviewed")
        candidate = self.candidate()
        for epoch in ("one", "two", "three"):
            self.assertEqual(self.propose(candidate, epoch, confidence=1.0)["slot_decision"], "keep")
        self.assertEqual(self.status(incumbent), "accepted")

    def test_votes_against_a_different_incumbent_are_not_reused(self):
        self.seed_incumbent()
        candidate = self.candidate("paint")
        other = self.candidate("repaint")
        self.assertEqual(self.propose(candidate, "old-vote")["slot_decision"], "pending")
        self.assertEqual(self.propose(other, "other-one")["slot_decision"], "pending")
        self.assertEqual(self.propose(other, "other-two")["slot_decision"], "supersede")
        self.assertEqual(self.propose(candidate, "new-one")["slot_decision"], "pending")
        self.assertEqual(self.propose(candidate, "new-two")["slot_decision"], "supersede")

    def test_votes_before_a_reaccepted_incumbent_cannot_trigger_a_flip_back(self):
        _, original = self.seed_incumbent()
        candidate = self.candidate()
        self.propose(candidate, "first-one")
        self.assertEqual(self.propose(candidate, "first-two")["slot_decision"], "supersede")
        self.assertEqual(self.propose(original, "return-one")["slot_decision"], "pending")
        self.assertEqual(self.propose(original, "return-two")["slot_decision"], "supersede")
        self.assertEqual(self.propose(candidate, "fresh-one")["slot_decision"], "pending")
        self.assertEqual(self.propose(candidate, "fresh-two")["slot_decision"], "supersede")

    def test_history_transitions_can_also_refine_at_the_model_confidence_cap(self):
        component = self.candidate()
        source = self.store.add_historical_form("la", "earlier")
        first = self.store.add_historical_form("fr", "later-a")
        second = self.store.add_historical_form("fr", "later-b")

        def claim(target, epoch):
            return self.store.apply_relation_decision(
                self.term, source, target, "developed-into", basis="model",
                confidence=0.8, properties={"component_id": component},
                context_evidence_ids=[self.evidence], prompt_version="history-fixture",
                epoch_fingerprint=epoch,
            )

        incumbent = claim(first, "initial")
        self.assertEqual(claim(second, "one")["slot_decision"], "pending")
        self.assertEqual(claim(second, "two")["slot_decision"], "supersede")
        self.assertEqual(self.status(incumbent["assertion_id"]), "archived")
