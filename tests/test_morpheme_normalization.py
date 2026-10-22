from __future__ import annotations

import copy
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.morpheme_prompting import (
    MORPHEME_NORMALIZATION_POLICY,
    build_morpheme_prompt,
    morpheme_structure_error,
    normalize_morpheme_draft,
)


def part(surface, kind="root", *, canonical=None, meaning="fixture meaning"):
    return {
        "surface": surface, "canonical_form": canonical or surface,
        "kind": kind, "language": "en", "meaning": meaning,
        "confidence": 0.8, "evidence_ids": [],
    }


def split():
    return [part("con", "prefix", meaning="together"),
            part("strain", meaning="draw tight")]


class MorphemeNormalizationTests(unittest.TestCase):
    def test_whole_word_container_is_removed_without_mutating_raw_output(self):
        value = {"parts": [part("constrain", "free"), *split()],
                 "derivatives": [{"term": "constraint", "component_forms": ["strain"]}],
                 "extra": {"keep": ["original metadata"]}}
        original = copy.deepcopy(value)
        normalized, notes = normalize_morpheme_draft(value, "constrain")
        self.assertEqual(value, original)
        self.assertEqual(normalized["parts"], split())
        self.assertEqual(normalized["derivatives"], original["derivatives"])
        self.assertEqual(normalized["extra"], original["extra"])
        self.assertEqual(notes[0]["removed_part_indices"], [0])
        self.assertEqual(notes[0]["retained_part_indices"], [1, 2])
        normalized["parts"][0]["evidence_ids"].append("test-only")
        self.assertEqual(value, original)

    def test_container_may_be_last_or_between_real_parts(self):
        for parts in ([*split(), part("constrain", "word")],
                      [split()[0], part("constrain"), split()[1]]):
            with self.subTest(parts=parts):
                normalized, notes = normalize_morpheme_draft({"parts": parts}, "constrain")
                self.assertEqual(normalized["parts"], split())
                self.assertTrue(notes)

    def test_multiple_redundant_containers_are_removed(self):
        value = {"parts": [part("constrain"), split()[0], part("constrain", "free"), split()[1]]}
        normalized, notes = normalize_morpheme_draft(value, "constrain")
        self.assertEqual(normalized["parts"], split())
        self.assertEqual(notes[0]["removed_part_indices"], [0, 2])

    def test_punctuation_and_case_do_not_create_fake_components(self):
        value = {"parts": [part("WELL-KNOWN", "free"), part("well", "free"), part("known", "free")]}
        normalized, notes = normalize_morpheme_draft(value, "well-known")
        self.assertEqual([p["surface"] for p in normalized["parts"]], ["well", "known"])
        self.assertTrue(notes)

    def test_sole_whole_word_free_analysis_is_preserved(self):
        value = {"parts": [part("constrain", "free")]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_incomplete_alternative_does_not_discard_the_word(self):
        value = {"parts": [part("constrain", "free"), part("con", "prefix"), part("str")]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_alternative_without_lexical_base_is_not_promoted(self):
        value = {"parts": [part("constrain", "free"), part("con", "prefix"), part("strain", "suffix")]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_missing_meanings_or_canonical_forms_are_not_invented(self):
        parts = split()
        parts[1].pop("canonical_form")
        value = {"parts": [part("constrain", "free"), *parts]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))
        parts = split()
        parts[1].pop("meaning")
        value = {"parts": [part("constrain", "free"), *parts]}
        normalized, notes = normalize_morpheme_draft(value, "constrain")
        self.assertEqual(normalized["parts"], parts)
        self.assertNotIn("meaning", normalized["parts"][1])
        self.assertTrue(notes)

    def test_non_object_parts_are_not_silently_discarded(self):
        value = {"parts": [part("constrain", "free"), *split(), None]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_legitimate_repeated_parts_remain_in_order(self):
        value = {"parts": [part("go-go", "free"), part("go", "free"), part("go", "free")]}
        normalized, notes = normalize_morpheme_draft(value, "gogo")
        self.assertEqual([p["surface"] for p in normalized["parts"]], ["go", "go"])
        self.assertTrue(notes)

    def test_book_boundary_conflict_is_not_hidden(self):
        value = {"parts": [part("constrain", "free"), *split()]}
        required = [{"surface": "cons"}, {"surface": "train"}]
        self.assertEqual(normalize_morpheme_draft(value, "constrain", required), (value, []))

    def test_matching_book_boundaries_preserve_model_evidence_and_roles(self):
        parts = split()
        parts[0]["evidence_ids"] = ["existing-model-context-id"]
        value = {"parts": [part("constrain", "free"), *parts]}
        required = [{"surface": "con", "kind": "prefix"}, {"surface": "strain", "kind": "root"}]
        normalized, notes = normalize_morpheme_draft(value, "constrain", required)
        self.assertEqual(normalized["parts"], parts)
        self.assertEqual(morpheme_structure_error(normalized, "constrain", required), "")
        self.assertTrue(notes)

    def test_whole_word_alternatives_do_not_become_an_empty_split(self):
        value = {"parts": [part("constrain", "free"), part("constrain"), part("constrain", "word")]}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_already_valid_split_is_unchanged(self):
        value = {"parts": split()}
        self.assertEqual(normalize_morpheme_draft(value, "constrain"), (value, []))

    def test_nonmatching_word_or_non_object_output_is_unchanged(self):
        value = {"parts": [part("constrain", "free"), *split()]}
        self.assertEqual(normalize_morpheme_draft(value, "constraint"), (value, []))
        self.assertEqual(normalize_morpheme_draft(value, ""), (value, []))
        self.assertEqual(normalize_morpheme_draft("invalid", "constrain"), ("invalid", []))

    def test_eight_actual_parts_can_survive_an_extra_parent(self):
        parts = [part(letter, "root" if index == 0 else "suffix")
                 for index, letter in enumerate("abcdefgh")]
        normalized, notes = normalize_morpheme_draft(
            {"parts": [part("abcdefgh", "free"), *parts]}, "abcdefgh",
        )
        self.assertEqual(normalized["parts"], parts)
        self.assertTrue(notes)

    def test_prompt_requests_flat_parts_without_a_parent(self):
        context = {"required_parts": [], "evidence": [], "previous_parts": [], "known_neighbors": []}
        prompt = build_morpheme_prompt("constrain", context)
        self.assertIn("one flat partition", prompt)
        self.assertIn("Do not include the whole word alongside its components", prompt)


class NormalizedMorphemeWorkerTests(unittest.TestCase):
    def prepare(self, responses):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(Path(temporary.name) / "knowledge.sqlite3")
        term = self.store.upsert_term("en", "constrain", status="accepted")
        base = {"corpus_id": "fixture-dictionary", "entry_id": "fixture-constrain",
                "headword": "constrain", "kind": "dictionary-sense",
                "source_title": "Synthetic test dictionary", "excerpt": "to compel or restrict"}
        evidence = self.store.add_evidence(base["corpus_id"], base["entry_id"], excerpt=base["excerpt"], payload=base)
        base["knowledge_evidence_id"] = evidence
        seed = self.store.enqueue_job("fixture", "term:" + term, subject_entity_id=term)
        self.store.save_job_artifact(seed, "retrieved-evidence", {"records": [base]}, language="en", validation_state="candidate")
        self.store.finish_job(seed)
        retriever = mock.Mock()
        retriever.component_evidence.return_value = []
        model = mock.Mock(model_name="local-fixture")
        model.complete_json.side_effect = [
            {"value": response, "model": "local-fixture", "metrics": {"requests": 1}}
            for response in responses
        ]
        worker = PreparationWorker(self.store, retriever, model, pronouncer=mock.Mock())
        worker._lexical_view = mock.Mock(return_value={"nodes": [], "edges": []})
        job = self.store.enqueue_job("split-morphemes", "term:" + term, subject_entity_id=term)
        result = worker.run_once(("split-morphemes",))
        self.assertEqual(result.status, "complete", str(result))
        accepted = self.store.artifacts_for_job(job, stage="accepted-morpheme-split")[-1]["payload"]
        return accepted, model, job, term

    def test_initial_container_is_normalized_without_a_second_model_call(self):
        derivatives = [{"term": word, "note": "shares the lexical base", "component_forms": ["constrain", "strain"]}
                       for word in ("constraint", "constrained", "constraining")]
        raw = {"parts": [part("constrain", "free"), *split()], "derivatives": derivatives}
        accepted, model, job, term = self.prepare([copy.deepcopy(raw)])
        model.complete_json.assert_called_once()
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["con", "strain"])
        self.assertEqual([p["term"] for p in accepted["related_terms"]], [p["term"] for p in derivatives])
        info = accepted["morphology_inference"]
        self.assertEqual(info["normalization_policy"], MORPHEME_NORMALIZATION_POLICY)
        self.assertEqual(info["review_reason"], "")
        self.assertEqual(info["normalizations"][0]["stage"], "draft")
        saved = self.store.artifacts_for_job(job, stage="model-morpheme-draft")[-1]["payload"]
        self.assertEqual(saved["value"], raw)
        self.assertEqual(saved["normalizations"][0]["removed_part_indices"], [0])
        with closing(self.store._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT m.form,m.kind FROM entity_edges e JOIN morphemes m ON m.entity_id=e.target_entity_id "
                "WHERE e.source_entity_id=? AND e.relation='has-component' AND e.status='accepted'", (term,),
            ).fetchall()
        self.assertEqual({(r["form"], r["kind"]) for r in rows}, {("con-", "prefix"), ("strain", "root")})

    def test_review_container_is_normalized_without_losing_first_derivatives(self):
        derivative = {"term": "constraint", "note": "shares the lexical base", "component_forms": ["strain"]}
        first = {"parts": [part("con", "prefix"), part("strain", "suffix")], "derivatives": [derivative]}
        reviewed = {"parts": [part("constrain", "free"), *split()], "derivatives": []}
        accepted, model, job, _ = self.prepare([first, copy.deepcopy(reviewed)])
        self.assertEqual(model.complete_json.call_count, 2)
        self.assertEqual([p["surface"] for p in accepted["parts"]], ["con", "strain"])
        self.assertEqual([p["term"] for p in accepted["related_terms"]], ["constraint"])
        self.assertEqual(accepted["morphology_inference"]["normalizations"][0]["stage"], "review")
        saved = self.store.artifacts_for_job(job, stage="model-morpheme-review-draft")[-1]
        self.assertEqual(saved["payload"]["value"], reviewed)
        self.assertEqual(saved["payload"]["remaining_structure_error"], "")
        self.assertEqual(saved["validation_state"], "candidate")


if __name__ == "__main__":
    unittest.main()
