from __future__ import annotations

from copy import deepcopy
import unittest

from lkt.morphology_meaning_prompting import (
    MORPHOLOGY_MEANING_PROMPT_POLICY,
    build_morphology_meaning_prompt,
)
from tests import test_morphology_meaning_prompting as existing


def source_context():
    return {
        "entities": [
            {"id": "fragment", "type": "morpheme", "label": "o"},
            {"id": "word", "type": "term", "label": "solo"},
        ],
        "assertions": [
            {"source": "word", "target": "fragment", "relation": "has-component"},
        ],
        "evidence": [
            {"entity_id": "word", "evidence_id": "index",
             "corpus_id": "english-root-dictionary",
             "excerpt": "solo 150 speculative 43 stalk 201 stern 168"},
            {"entity_id": "word", "evidence_id": "sense-one",
             "excerpt": "any activity that is performed alone without assistance"},
            {"entity_id": "word", "evidence_id": "sense-two",
             "excerpt": "composed or performed by a single voice or instrument"},
            {"entity_id": "word", "evidence_id": "root-entry",
             "corpus_id": "english-root-dictionary",
             "excerpt": "solitude; solo: sol (=alone); soloist"},
            {"entity_id": "word", "evidence_id": "affix-entry",
             "corpus_id": "english-affix-dictionary",
             "excerpt": "-sol (=alone, single); desolate: de (=away); solo"},
        ],
    }


def build(data, *, form="o", kind="root"):
    return build_morphology_meaning_prompt(
        entity_id="fragment", label=form, kind=kind, language="en",
        language_name="English", context=data,
    )


class MorphologyMeaningScopeTests(unittest.TestCase):
    def test_real_definition_passages_replace_index_and_whole_word_glosses(self):
        data = source_context()
        before = deepcopy(data)
        _, prompt, trace = build(data)
        self.assertEqual(trace["policy"], MORPHOLOGY_MEANING_PROMPT_POLICY)
        self.assertEqual(trace["selected_evidence_ids"][:2], ["root-entry", "affix-entry"])
        self.assertNotIn("solo 150", prompt)
        self.assertIn("sol (=alone)", prompt)
        self.assertEqual(trace["omitted_reference_ids"], ["index"])
        self.assertEqual(data, before)

    def test_related_word_owner_is_visible_and_not_a_definition_of_fragment(self):
        _, prompt, trace = build(source_context())
        self.assertIn('"about": "solo"', prompt)
        self.assertIn('"scope": "related-word"', prompt)
        self.assertEqual(trace["definition_evidence_ids"], [])
        self.assertIn("does not define every substring", prompt)
        self.assertNotIn("fragment", prompt)

    def test_real_one_letter_definition_is_not_banned(self):
        data = source_context()
        data["evidence"].append({
            "entity_id": "fragment", "evidence_id": "actual-short-form",
            "excerpt": "o (=a linking vowel); its role varies with the formation",
        })
        _, prompt, trace = build(data)
        self.assertEqual(trace["selected_evidence_ids"][0], "actual-short-form")
        self.assertEqual(trace["definition_evidence_ids"], ["actual-short-form"])
        self.assertIn('"scope": "target"', prompt)

    def test_normalized_ocr_form_gloss_is_ranked_but_original_text_is_retained(self):
        data = source_context()
        text = "\uff0dsol\uff08\uff1dalone\uff0csingle\uff09"
        data["evidence"] = [{"evidence_id": "ocr", "excerpt": text}]
        _, prompt, trace = build(data, form="sol")
        self.assertIn(text, prompt)
        self.assertEqual(trace["definition_evidence_ids"], ["ocr"])

    def test_prefix_suffix_and_non_latin_forms_can_have_definitions(self):
        for form, excerpt, kind in [
            ("re-", "re (=again) + form", "prefix"),
            ("-ness", "ness: state or quality", "suffix"),
            ("\u5b66", "\u5b66 (=learning)", "root"),
        ]:
            with self.subTest(form=form):
                data = source_context()
                data["evidence"] = [{"evidence_id": "definition", "excerpt": excerpt}]
                _, _, trace = build(data, form=form, kind=kind)
                self.assertEqual(trace["definition_evidence_ids"], ["definition"])

    def test_number_in_real_definition_does_not_make_it_an_index(self):
        data = source_context()
        data["evidence"] = [{"evidence_id": "definition", "excerpt": "bi means two; used in 2-part formations"}]
        _, _, trace = build(data, form="bi", kind="prefix")
        self.assertEqual(trace["selected_evidence_ids"], ["definition"])
        self.assertEqual(trace["definition_evidence_ids"], ["definition"])

    def test_only_index_and_cross_reference_do_not_create_definition_citations(self):
        data = source_context()
        data["evidence"] = [data["evidence"][0], {
            "evidence_id": "pointer", "excerpt": "see SOLE",
        }]
        _, prompt, trace = build(data)
        self.assertEqual(trace["selected_evidence_ids"], [])
        self.assertEqual(set(trace["omitted_reference_ids"]), {"index", "pointer"})
        self.assertIn("evidence_ids: exactly []", prompt)
        self.assertIn("solo", prompt)

    def test_source_order_does_not_hide_substantive_morphology(self):
        data = source_context()
        data["evidence"] = data["evidence"][:3] * 20 + data["evidence"][3:]
        _, _, trace = build(data)
        self.assertEqual(trace["selected_evidence_ids"][:2], ["root-entry", "affix-entry"])
        self.assertEqual(trace["selected_records"], 3)

    def test_bare_focus_mention_is_context_not_definition(self):
        data = source_context()
        data["evidence"] = [{"evidence_id": "mention", "excerpt": "The spelling contains o beside other letters."}]
        _, _, trace = build(data)
        self.assertEqual(trace["selected_evidence_ids"], ["mention"])
        self.assertEqual(trace["definition_evidence_ids"], [])

    def test_existing_graph_classification_can_be_questioned(self):
        _, prompt, _ = build(source_context())
        self.assertIn("graph hint", prompt)
        self.assertIn("contradict", prompt)
        self.assertIn("uncertainty", prompt)


class MorphologyMeaningScopeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.MorphologyMeaningWorkerTests()

    def test_direct_definition_is_context_not_an_exact_truth_stamp(self):
        worker = self.fixture.worker(["E1"])
        worker.model.complete_json.return_value["value"]["confidence"] = 0.99
        self.fixture.run_meaning(worker)
        args = worker.store.add_entity_meaning.call_args.kwargs
        self.assertEqual(args["metadata"]["grounding"], "definition-local-context")
        self.assertLessEqual(args["confidence"], 0.8)
        self.assertEqual(args["metadata"]["meaning_prompt_policy"], MORPHOLOGY_MEANING_PROMPT_POLICY)

    def test_related_word_gloss_is_kept_with_honest_scope_and_confidence(self):
        worker = self.fixture.worker(["E1"])
        context = worker.store.semantic_enrichment_context.return_value["context"]
        context["evidence"][0]["excerpt"] = "A close examination of something."
        context["evidence"][0]["entity_id"] = "term-uuid-one"
        worker.model.complete_json.return_value["value"]["confidence"] = 0.99
        self.fixture.run_meaning(worker)
        args = worker.store.add_entity_meaning.call_args.kwargs
        self.assertEqual(args["metadata"]["grounding"], "related-local-context")
        self.assertLessEqual(args["confidence"], 0.55)
        self.assertEqual(args["evidence_ids"], [existing.EVIDENCE])
        self.assertEqual(args["metadata"]["context_evidence_ids"], [existing.EVIDENCE])

    def test_uncited_useful_result_is_not_discarded_when_books_are_present(self):
        worker = self.fixture.worker([])
        self.assertEqual(self.fixture.run_meaning(worker), "accepted-id")
        args = worker.store.add_entity_meaning.call_args.kwargs
        self.assertEqual(args["evidence_ids"], [])
        self.assertEqual(args["metadata"]["grounding"], "model-context-only")
        self.assertLessEqual(args["confidence"], 0.35)
        self.assertEqual(args["metadata"]["context_evidence_ids"], [existing.EVIDENCE])

    def test_invented_ids_are_dropped_without_failing_the_whole_answer(self):
        worker = self.fixture.worker(["invented", "E999"])
        self.assertEqual(self.fixture.run_meaning(worker), "accepted-id")
        worker.store.link_evidence.assert_not_called()
        self.assertEqual(worker.store.add_entity_meaning.call_args.kwargs["evidence_ids"], [])
        worker.model.complete_json.assert_called_once()

    def test_old_raw_evidence_ids_remain_compatible_after_scoping(self):
        worker = self.fixture.worker([existing.EVIDENCE])
        self.fixture.run_meaning(worker)
        self.assertEqual(worker.store.add_entity_meaning.call_args.kwargs["evidence_ids"], [existing.EVIDENCE])
