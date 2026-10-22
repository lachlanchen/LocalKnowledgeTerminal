from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.lexical_refinement import STRUCTURE_POLICY, structure_review_due
from lkt.partial_morphology import finalize_morpheme_analysis, word_history_anchor
from lkt.relation_support import _assert_morphology_finalization_invariants


def part(surface, kind="root", meaning="fixture meaning"):
    return {"surface": surface, "canonical_form": surface, "kind": kind,
            "language": "en", "meaning": meaning, "confidence": 0.8, "evidence_ids": []}


def complete():
    return {"parts": [part("re", "prefix"), part("paint"), part("ed", "suffix")]}


def partial():
    return {"parts": [part("repainted", "free"), part("re", "prefix"), part("paint"), part("xyz", "suffix")]}


class PartialMorphologyTests(unittest.TestCase):
    def finish(self, value, previous=None, required=None, word="repainted"):
        return finalize_morpheme_analysis(value, word, previous or [], required or [])

    def test_partial_keeps_root_and_prefix_and_records_the_actual_gap(self):
        result = self.finish(partial())
        self.assertEqual([p["surface"] for p in result["parts"]], ["re", "paint"])
        self.assertEqual(result["analysis_scope"], "partial")
        self.assertTrue(result["refinement_needed"])
        self.assertEqual(result["unresolved_spans"], [{"surface": "ed", "start": 7, "end": 9}])
        self.assertEqual(result["parts"][1]["source_span"], [2, 7])
        self.assertTrue(any(p["proposal"]["surface"] == "xyz" for p in result["unresolved_proposals"]))

    def test_actual_model_style_near_spelling_does_not_invent_a_suffix(self):
        value = {"parts": [part("apo", "prefix"), part("calyp"), part("s", "suffix"), part("is", "suffix")]}
        result = self.finish(value, word="apocalypse")
        self.assertEqual([p["surface"] for p in result["parts"]], ["apo", "calyp", "s"])
        self.assertEqual(result["unresolved_spans"], [{"surface": "e", "start": 9, "end": 10}])
        self.assertFalse(any(p["canonical_form"] == "is" for p in result["parts"]))

    def test_partial_affixes_without_a_root_are_still_retained(self):
        result = self.finish({"parts": [part("re", "prefix"), part("ed", "suffix")]})
        self.assertEqual(len(result["parts"]), 2)
        self.assertEqual(result["analysis_scope"], "partial")
        self.assertEqual(result["unresolved_spans"][0]["surface"], "paint")

    def test_empty_result_records_unknown_spans_without_fabricating_components(self):
        result = self.finish({"parts": [None, part("unrelated")]})
        self.assertEqual(result["parts"], [])
        self.assertEqual(result["unresolved_spans"][0]["surface"], "repainted")
        self.assertEqual(result["analysis_scope"], "partial")

    def test_missing_meaning_keeps_structure_without_inventing_a_gloss(self):
        value = partial()
        value["parts"][2].pop("meaning")
        result = self.finish(value)
        self.assertEqual([p["surface"] for p in result["parts"]], ["re", "paint"])
        self.assertNotIn("meaning", result["parts"][1])
        self.assertEqual(result["unresolved_spans"], [
            {"surface": "ed", "start": 7, "end": 9},
        ])

    def test_stronger_previous_complete_structure_is_retained(self):
        previous = complete()["parts"]
        result = self.finish(partial(), previous)
        self.assertEqual(result["parts"], previous)
        self.assertEqual(result["analysis_scope"], "complete")
        self.assertTrue(result["refinement_needed"])
        self.assertTrue(result["retained_previous_structure"])

    def test_previous_partial_components_fill_nonconflicting_gaps(self):
        result = self.finish(partial(), [part("ed", "suffix")])
        self.assertEqual([p["surface"] for p in result["parts"]], ["re", "paint", "ed"])
        self.assertEqual(result["unresolved_spans"], [])
        self.assertTrue(result["refinement_needed"])

    def test_previous_whole_word_parent_does_not_hide_new_components(self):
        result = self.finish(partial(), [part("repainted", "free")])
        self.assertEqual([p["surface"] for p in result["parts"]], ["re", "paint"])
        self.assertEqual(result["analysis_scope"], "partial")

    def test_order_and_overlap_do_not_create_duplicate_occurrences(self):
        result = self.finish({"parts": [part("paint"), part("re", "prefix"), part("paint")]})
        self.assertEqual([p["surface"] for p in result["parts"]], ["paint"])
        self.assertEqual([g["surface"] for g in result["unresolved_spans"]], ["re", "ed"])

    def test_book_partition_conflict_remains_explicitly_partial(self):
        result = self.finish(complete(), required=[{"surface": "repaint"}, {"surface": "ed"}])
        self.assertEqual(result["analysis_scope"], "partial")
        self.assertTrue(result["refinement_needed"])

    def test_inputs_derivative_proposals_and_complete_analysis_are_preserved(self):
        value = complete()
        value["derivatives"] = [{"term": "painter", "component_forms": ["paint"]}]
        before = deepcopy(value)
        result = self.finish(value)
        self.assertEqual(value, before)
        self.assertEqual(result["parts"], before["parts"])
        self.assertEqual(result["derivatives"], before["derivatives"])
        self.assertEqual(result["analysis_scope"], "complete")
        self.assertFalse(result["refinement_needed"])

    def test_word_history_anchor_references_the_existing_word_only(self):
        source = {"entity_id": "existing-word", "text": "repainted", "language": "en"}
        anchor = word_history_anchor(source)
        self.assertEqual(anchor["morpheme_id"], source["entity_id"])
        self.assertEqual(anchor["canonical_form"], source["text"])
        self.assertTrue(anchor["word_anchor"])
        self.assertEqual(anchor["evidence_ids"], [])

    def test_partial_refinement_uses_alternate_eligible_visits(self):
        for parts in ([], [part("paint")]):
            artifacts = [{"stage": "accepted-morpheme-split", "payload": {
                "parts": parts, "analysis_scope": "partial", "refinement_needed": True,
                "analysis_policy": STRUCTURE_POLICY,
            }}]
            for epoch in (1, 3, 5):
                self.assertFalse(structure_review_due(artifacts, epoch))
            for epoch in (2, 4, 6):
                self.assertTrue(structure_review_due(artifacts, epoch))


class PartialMorphologyPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name) / "knowledge.sqlite3")
        self.term = self.store.upsert_term("en", "repainted", status="accepted")
        self.subject = "term:" + self.term
        record = {"corpus_id": "fixture", "entry_id": "repainted", "headword": "repainted",
                  "kind": "word-origin", "excerpt": "Repainted means painted again."}
        evidence = self.store.add_evidence("fixture", "repainted", excerpt=record["excerpt"], payload=record)
        record["knowledge_evidence_id"] = evidence
        seed = self.store.enqueue_job("fixture", self.subject, subject_entity_id=self.term)
        self.store.save_job_artifact(seed, "retrieved-evidence", {"records": [record]}, language="en", validation_state="candidate")
        self.store.finish_job(seed)
        self.model = Mock(model_name="local-fixture")
        self.retriever = Mock()
        self.retriever.component_evidence.return_value = []
        self.retriever.origin_evidence.return_value = []
        self.worker = PreparationWorker(self.store, self.retriever, self.model, pronouncer=Mock())
        self.worker._lexical_view = Mock(return_value={"nodes": [], "edges": []})

    def job(self, version=""):
        job_id = self.store.enqueue_job("split-morphemes", self.subject, subject_entity_id=self.term, prompt_version=version)
        return {"job_id": job_id, "subject_entity_id": self.term, "subject_key": self.subject, "prompt_version": version}

    def respond(self, *values):
        self.model.complete_json.side_effect = [
            {"value": deepcopy(value), "model": "local-fixture", "metrics": {"requests": 1}} for value in values
        ]

    def accepted(self, job):
        return self.store.artifacts_for_job(job["job_id"], stage="accepted-morpheme-split")[-1]["payload"]

    def assert_invariants(self):
        with closing(self.store._connect()) as connection, connection:
            _assert_morphology_finalization_invariants(connection)

    def test_partial_graph_claims_are_persisted_without_a_fake_complete_partition(self):
        job = self.job()
        self.respond(partial(), partial())
        self.worker._split_morphemes(job)
        accepted = self.accepted(job)
        self.assertEqual(accepted["analysis_scope"], "partial")
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["re", "paint"])
        self.assertEqual(accepted["unresolved_spans"][0]["surface"], "ed")
        self.assertTrue(all(p["basis"] == "model" and p["evidence_ids"] == [] for p in accepted["parts"]))
        with closing(self.store._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT relation,json_extract(properties_json,'$.proposal')
                   FROM relation_assertions WHERE subject_entity_id=?
                     AND source_entity_id=? AND status='accepted'""",
                (self.term, self.term),
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("shares-component", 1)] * 2)
        self.assert_invariants()

    def test_later_complete_analysis_reconciles_partial_claims(self):
        first = self.job("first")
        self.respond(partial(), partial())
        self.worker._split_morphemes(first)
        second = self.job("second")
        self.respond(complete())
        self.worker._split_morphemes(second)
        self.assertEqual(self.accepted(second)["analysis_scope"], "complete")
        self.assertEqual(self.accepted(second)["unresolved_spans"], [])
        with closing(self.store._connect()) as connection, connection:
            count = connection.execute("SELECT count(*) FROM term_morphemes WHERE term_id=?", (self.term,)).fetchone()[0]
        self.assertEqual(count, 3)
        self.assert_invariants()

    def test_empty_partial_analysis_is_saved_without_morpheme_entities(self):
        job = self.job()
        self.respond({"parts": []}, {"parts": []})
        self.worker._split_morphemes(job)
        self.assertEqual(self.accepted(job)["parts"], [])
        self.assertEqual(self.accepted(job)["analysis_scope"], "partial")
        self.assert_invariants()

    def test_missing_middle_component_does_not_reserve_the_wrong_slot(self):
        first = self.job("partial")
        incomplete = {"parts": [part("re", "prefix"), part("ed", "suffix")]}
        self.respond(incomplete, incomplete)
        self.worker._split_morphemes(first)
        with closing(self.store._connect()) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM term_morphemes WHERE term_id=?", (self.term,),
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                """SELECT count(*) FROM relation_assertions a
                   JOIN relation_assertion_slots s ON s.assertion_id=a.assertion_id
                   WHERE a.subject_entity_id=? AND a.status='accepted'
                     AND a.relation='has-component' AND s.slot_mode='exclusive'""",
                (self.term,),
            ).fetchone()[0], 0)
        second = self.job("complete")
        value = complete()
        value["derivatives"] = [{"term": "painter", "component_forms": ["paint"]}]
        self.respond(value)
        self.worker._split_morphemes(second)
        accepted = self.accepted(second)
        self.assertEqual(accepted["analysis_scope"], "complete")
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["re", "paint", "ed"])
        self.assertEqual([p["term"] for p in accepted["related_terms"]], ["painter"])
        with closing(self.store._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT ordinal,surface FROM term_morphemes WHERE term_id=? ORDER BY ordinal",
                (self.term,),
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [(0, "re"), (1, "paint"), (2, "ed")])
            positions = connection.execute(
                """SELECT json_extract(properties_json,'$.ordinal') AS position
                   FROM relation_assertions WHERE subject_entity_id=?
                     AND relation='has-component' AND status='accepted' ORDER BY position""",
                (self.term,),
            ).fetchall()
            self.assertEqual([row[0] for row in positions], [0, 1, 2])
            self.assertEqual(connection.execute(
                """SELECT count(*) FROM relation_assertions WHERE subject_entity_id=?
                   AND source_entity_id=? AND relation='shares-component' AND status='accepted'""",
                (self.term, self.term),
            ).fetchone()[0], 0)
        self.assert_invariants()

    def test_store_deferred_component_cannot_be_reported_as_a_complete_split(self):
        evidence = self.store.add_evidence(
            "book-fixture", "painted", excerpt="Fixture identifies painted as a free base.",
        )
        incumbent = self.store.upsert_morpheme("en", "painted", "free", "painted", status="accepted")
        assertion = self.store.accept_relation_assertion(
            self.term, self.term, incumbent, "has-component", basis="book", confidence=0.9,
            evidence_ids=[evidence], owner_type="morphology-parser", owner_id="book-fixture",
            properties={"ordinal": 1, "surface": "painted", "component_kind": "free"},
        )
        self.store.link_morpheme(self.term, incumbent, 1, "painted", basis="book", confidence=0.9)
        job = self.job("competing-model")
        value = finalize_morpheme_analysis(complete(), "repainted", [], [])
        self.store.atomic_semantic_write(self.term, lambda: self.worker._persist_accepted_morpheme_split(
            job, self.store.term_record(self.term), value["parts"], value, {"model": "local-fixture"},
        ))
        accepted = self.accepted(job)
        self.assertEqual(accepted["analysis_scope"], "partial")
        self.assertTrue(accepted["refinement_needed"])
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["re", "ed"])
        self.assertEqual(accepted["unresolved_spans"], [{"surface": "paint", "start": 2, "end": 7}])
        self.assertTrue(any(p["proposal"]["surface"] == "paint" for p in accepted["unresolved_proposals"]))
        with closing(self.store._connect()) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM relation_assertions WHERE assertion_id=?", (assertion,),
            ).fetchone()[0], "accepted")
            rows = connection.execute(
                "SELECT ordinal,morpheme_id FROM term_morphemes WHERE term_id=?", (self.term,),
            ).fetchall()
            by_ordinal = {row[0]: row[1] for row in rows}
            self.assertEqual(by_ordinal[1], incumbent)
            self.assertEqual(set(by_ordinal), {0, 1, 2})
            self.assertEqual(by_ordinal[0], accepted["parts"][0]["morpheme_id"])
            self.assertEqual(by_ordinal[2], accepted["parts"][1]["morpheme_id"])
        self.assert_invariants()

    def test_derivatives_accumulate_beyond_one_batch_and_the_old_32_limit(self):
        first_value = complete()
        words = ["paint" + chr(97 + index // 26) + chr(97 + index % 26) for index in range(40)]
        first_value["derivatives"] = [{"term": word, "component_forms": ["paint"]} for word in words]
        first = self.job("first")
        self.respond(first_value)
        self.worker._split_morphemes(first)
        self.assertEqual(len(self.accepted(first)["related_terms"]), 40)
        second_value = complete()
        second_value["derivatives"] = [{"term": "painter", "component_forms": ["paint"]}]
        second = self.job("second")
        self.respond(second_value)
        self.worker._split_morphemes(second)
        self.assertEqual(len(self.accepted(second)["related_terms"]), 41)
        self.assert_invariants()

    def test_origin_history_can_use_a_word_without_any_morpheme_split(self):
        job_id = self.store.enqueue_job("expand-origin-branches", self.subject, subject_entity_id=self.term)
        self.model.complete_json.return_value = {
            "value": {"component_id": self.term, "steps": [{
                "form": "peint", "language": "fro", "period": "Old French",
                "meaning": "painted", "confidence": 0.65, "evidence_ids": [],
            }]},
            "model": "local-fixture", "metrics": {"requests": 1},
        }
        self.worker._expand_origin_branches({"job_id": job_id, "subject_entity_id": self.term, "subject_key": self.subject})
        payload = self.store.artifacts_for_job(job_id, stage="accepted-origin-branches")[-1]["payload"]
        self.assertEqual(payload["branches"][0]["component_id"], self.term)
        with closing(self.store._connect()) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM morphemes").fetchone()[0], 0)
