from __future__ import annotations

import copy
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lkt.atomic import PreparationWorker
from lkt.growth import _plan_history
from lkt.knowledge import KnowledgeStore
from lkt.lexical_refinement import (
    ORIGIN_PATH_POLICY, STRUCTURE_POLICY, retire_replaced_model_origin_paths,
    revise_origin_paths, structure_review_due,
)


class RefinementPolicyTests(unittest.TestCase):
    @staticmethod
    def split(*, basis="model", policy=STRUCTURE_POLICY):
        return [{"stage": "accepted-morpheme-split", "payload": {
            "analysis_policy": policy,
            "parts": [{"surface": "fixture", "kind": "free", "basis": basis}],
        }}]

    def test_legacy_model_analysis_gets_an_upgrade(self):
        self.assertTrue(structure_review_due(self.split(policy="legacy"), 1))

    def test_current_model_analysis_is_not_repeated_every_visit(self):
        for epoch in [1, 2, 3, 5, 6, 7]:
            self.assertFalse(structure_review_due(self.split(), epoch))
        for epoch in [4, 8]:
            self.assertTrue(structure_review_due(self.split(), epoch))

    def test_book_owned_split_is_not_forced_through_model_again(self):
        self.assertFalse(structure_review_due(self.split(basis="book", policy="legacy"), 4))

    def test_missing_or_empty_structure_is_reviewed(self):
        self.assertTrue(structure_review_due([], 1))
        self.assertTrue(structure_review_due([
            {"stage": "accepted-morpheme-split", "payload": {"parts": []}},
        ], 1))

    def test_history_plan_orders_reanalysis_before_origin_without_language_jobs(self):
        store = Mock()
        store.claim_lexical_revisit_candidate.return_value = {
            "epoch_id": "epoch", "epoch_number": 1, "prompt_version": "fixture-policy",
            "term_id": "fixture-term", "fingerprint": "fixture-fingerprint",
        }
        store.artifacts_for_subject.return_value = [
            {"stage": "accepted-meaning", "language": "en"},
            *self.split(policy="legacy"),
        ]
        store.enqueue_job.side_effect = lambda kind, *args, **kwargs: kind
        store.bind_lexical_revisit_epoch.return_value = True
        with patch("lkt.growth._finish_history_epochs", return_value=False):
            _plan_history(store, "local-test-model")
        calls = store.enqueue_job.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            "retrieve-evidence", "split-morphemes", "expand-origin-branches", "compose-origin-card",
        ])
        self.assertIn("split-morphemes", calls[2].kwargs["depends_on"])
        self.assertTrue(store.claim_lexical_revisit_candidate.call_args.kwargs["history_only"])

    def test_new_path_is_not_concatenated_with_old_path(self):
        current = [{"component_id": "root", "steps": [{"form": "new-oldest"}, {"form": "new-later"}]}]
        previous = [{"component_id": "root", "steps": [{"form": "old-oldest"}, {"form": "old-later"}]}]
        original = copy.deepcopy((current, previous))
        paths, revised = revise_origin_paths(current, previous, "root")
        self.assertEqual([s["form"] for s in paths[0]["steps"]], ["new-oldest", "new-later"])
        self.assertEqual(revised, {"root"})
        self.assertEqual((current, previous), original)

    def test_other_components_and_empty_uncertain_drafts_keep_history(self):
        previous = [{"component_id": key, "steps": [{"form": key + "-history"}]} for key in ["root", "affix"]]
        current = [{"component_id": key, "steps": []} for key in ["root", "affix"]]
        paths, revised = revise_origin_paths(current, previous, "root")
        self.assertEqual([p["steps"] for p in paths], [p["steps"] for p in previous])
        self.assertEqual(revised, set())


class ModelRefinementIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")

    def split_word(self, word, parts):
        store = self.store
        term = store.upsert_term("en", word)
        subject = "term:" + term
        seed = store.enqueue_job("fixture", subject, subject_entity_id=term)
        store.save_job_artifact(seed, "retrieved-evidence", {"records": [{
            "kind": "dictionary", "corpus_id": "fixture-dictionary", "entry_id": word,
            "headword": word, "excerpt": "A glossary entry for " + word,
        }]}, language="en")
        store.finish_job(seed)
        job = store.enqueue_job("split-morphemes", subject, subject_entity_id=term,
                                language="en", prompt_version="fixture-split", max_attempts=1)

        class Retriever:
            def component_evidence(self, *args):
                return []

        class Model:
            model_name = "local-fixture-model"

            def complete_json(self, system, prompt, **kwargs):
                return {"value": {"parts": parts, "derivatives": []}, "model": self.model_name}

        result = PreparationWorker(store, Retriever(), Model()).run_once(("split-morphemes",))
        self.assertEqual(result.status, "complete")
        return store.artifacts_for_job(job, stage="accepted-morpheme-split")[-1]["payload"]

    @staticmethod
    def part(surface, canonical, kind, language="en"):
        return {"surface": surface, "canonical_form": canonical, "kind": kind,
                "language": language, "meaning": "a lexical component", "confidence": 0.8,
                "evidence_ids": []}

    def test_surface_spelling_and_canonical_base_are_both_preserved(self):
        result = self.split_word("running", [
            self.part("runn", "run", "root"), self.part("ing", "-ing", "suffix"),
        ])
        self.assertEqual([(p["surface"], p["canonical_form"]) for p in result["parts"]],
                         [("runn", "run"), ("ing", "-ing")])
        self.assertEqual(result["parts"][0]["basis"], "model")
        self.assertEqual(result["analysis_policy"], STRUCTURE_POLICY)

    def test_canonical_spelling_change_is_not_limited_to_one_word(self):
        result = self.split_word("happiness", [
            self.part("happi", "happy", "root"), self.part("ness", "-ness", "suffix"),
        ])
        self.assertEqual(result["parts"][0]["canonical_form"], "happy")

    def test_component_languages_are_not_limited_to_english_and_latin(self):
        result = self.split_word("microscope", [
            self.part("micro", "micro-", "prefix", "grc"),
            self.part("scope", "scope", "root", "grc"),
        ])
        self.assertEqual({part["language"] for part in result["parts"]}, {"grc"})

    def test_hyphenated_word_preserves_its_letters_without_a_fake_morpheme(self):
        result = self.split_word("re-enter", [
            self.part("re", "re-", "prefix"), self.part("enter", "enter", "root"),
        ])
        self.assertEqual([part["surface"] for part in result["parts"]], ["re", "enter"])

    def make_origin(self):
        store = self.store
        term = store.upsert_term("en", "fixtureword")
        root = store.upsert_morpheme("en", "fixture", "root", "an example")
        earlier = store.add_historical_form("la", "oldform", period_label="Latin", meaning="an example")
        old_id = store.accept_origin_transition(term, earlier, root, component_id=root,
                                                basis="model", confidence=0.5)
        return term, root, earlier, old_id

    def test_retirement_preserves_book_claims_and_other_subjects(self):
        store = self.store
        term, root, earlier, old_id = self.make_origin()
        newer = store.add_historical_form("la", "newform", period_label="Latin", meaning="an example")
        new_id = store.accept_origin_transition(term, newer, root, component_id=root,
                                                basis="model", confidence=0.6)
        other = store.upsert_term("en", "anotherword")
        other_id = store.accept_origin_transition(other, earlier, root, component_id=root,
                                                  basis="book", confidence=0.9)
        book_form = store.add_historical_form("la", "bookform", period_label="Latin", meaning="an example")
        book_id = store.accept_relation_assertion(term, book_form, root, "developed-into",
                                                  basis="book", confidence=0.9)
        retired = retire_replaced_model_origin_paths(store, term,
            active_assertion_ids={new_id}, current_component_ids={root}, revised_component_ids={root})
        self.assertEqual(retired, [old_id])
        with closing(store._connect()) as connection, connection:
            states = {row["assertion_id"]: row["status"] for row in connection.execute(
                "SELECT assertion_id,status FROM relation_assertions")}
        self.assertEqual(states[old_id], "archived")
        for identifier in [new_id, other_id, book_id]:
            self.assertEqual(states[identifier], "accepted")

    def test_unvisited_component_model_history_remains_active(self):
        term, root, earlier, old_id = self.make_origin()
        self.assertEqual(retire_replaced_model_origin_paths(self.store, term,
            active_assertion_ids=set(), current_component_ids={root}, revised_component_ids=set()), [])

    def test_replaced_component_can_release_its_model_path(self):
        term, root, earlier, old_id = self.make_origin()
        self.assertEqual(retire_replaced_model_origin_paths(self.store, term,
            active_assertion_ids=set(), current_component_ids=set(), revised_component_ids=set()), [old_id])

    def test_worker_replaces_path_without_inventing_a_bridge(self):
        store = self.store
        term, root, earlier, old_id = self.make_origin()
        subject = "term:" + term
        seed = store.enqueue_job("fixture", subject, subject_entity_id=term)
        store.save_job_artifact(seed, "accepted-morpheme-split", {"parts": [{
            **self.part("fixtureword", "fixture", "root"), "morpheme_id": root, "basis": "model",
        }]}, language="en", validation_state="accepted")
        store.save_job_artifact(seed, "retrieved-evidence", {"records": []}, language="en")
        store.save_job_artifact(seed, "accepted-origin-branches", {"branches": [{
            "component_id": root, "component_kind": "root", "component_form": "fixture",
            "steps": [{"form": "oldform", "language": "la", "period": "Latin",
                       "meaning": "an example", "confidence": 0.5, "evidence_ids": [], "basis": "model"}],
        }]}, language="en", validation_state="accepted")
        store.finish_job(seed)
        job = store.enqueue_job("expand-origin-branches", subject, subject_entity_id=term,
                                language="en", prompt_version="fixture-origin", max_attempts=1)

        class Retriever:
            def origin_evidence(self, *args):
                return []

        class Model:
            model_name = "local-fixture-model"

            def complete_json(self, system, prompt, **kwargs):
                return {"value": {"component_id": root, "steps": [
                    {"form": form, "language": "la", "period": "Latin", "meaning": "an example",
                     "confidence": 0.6, "evidence_ids": []} for form in ["earlierform", "laterform"]
                ]}, "model": self.model_name}

        result = PreparationWorker(store, Retriever(), Model()).run_once(("expand-origin-branches",))
        self.assertEqual(result.status, "complete")
        payload = store.artifacts_for_job(job, stage="accepted-origin-branches")[-1]["payload"]
        self.assertEqual(payload["origin_path_policy"], ORIGIN_PATH_POLICY)
        self.assertEqual([step["form"] for step in payload["branches"][0]["steps"]],
                         ["earlierform", "laterform"])
        with closing(store._connect()) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM relation_assertions WHERE assertion_id=?", (old_id,)
            ).fetchone()[0], "archived")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM relation_assertions WHERE subject_entity_id=? AND status='accepted'",
                (term,),
            ).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
