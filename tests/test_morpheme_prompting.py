from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lkt.atomic import (
    PreparationWorker,
    WordEvidenceRetriever,
    _apply_cited_component_roles,
    _retrieved_component_evidence_ids,
)
from lkt.knowledge import KnowledgeStore
from lkt.lexical_refinement import STRUCTURE_POLICY
from lkt.morpheme_prompting import (
    MORPHEME_PROMPT_POLICY,
    build_morpheme_prompt,
    component_candidates,
    morpheme_review_reason,
    morpheme_structure_error,
    pack_morpheme_context,
)


def part(surface, canonical, kind, meaning="synthetic fixture meaning"):
    return {
        "surface": surface, "canonical_form": canonical, "kind": kind,
        "language": "en", "meaning": meaning, "confidence": 0.7, "evidence_ids": [],
    }


def record(name, *, surface="", kind="", headword="", excerpt="synthetic context"):
    return {
        "corpus_id": "test-corpus", "entry_id": name,
        "knowledge_evidence_id": "evidence-" + name,
        "source_title": "Synthetic test material",
        "headword": headword or surface or name,
        "kind": "morphology-affix" if kind in {"prefix", "suffix"} else "morphology-root",
        "component_hint": kind, "component_surface": surface, "excerpt": excerpt,
    }


class MorphemePromptTests(unittest.TestCase):
    def test_lookup_candidates_include_boundary_affixes_and_internal_roots(self):
        candidates = list(component_candidates("unequalled"))
        self.assertIn(("prefix", "un", 0, 2), candidates)
        self.assertIn(("suffix", "ed", 8, 10), candidates)
        self.assertIn(("root", "equal", 2, 7), candidates)
        self.assertTrue(all(surface and 0 <= start < end <= 10 for _, surface, start, end in candidates))
        self.assertTrue(all(len(surface) < 10 for kind, surface, _, _ in candidates if kind != "root"))
        self.assertEqual(list(component_candidates("a")), [])

    def test_evidence_packing_balances_roles_instead_of_dumping_all_matches(self):
        records = [record("noise-" + str(index), excerpt="irrelevant context " * 200) for index in range(30)]
        records += [
            record("target", headword="unequalled", excerpt="the target definition"),
            record("prefix", surface="un", kind="prefix", headword="un-", excerpt="prefix: not"),
            record("suffix", surface="ed", kind="suffix", headword="-ed", excerpt="suffix: past participle"),
            record("root", surface="equal", kind="root", headword="EQUAL", excerpt="root = equal"),
        ]
        untouched = copy.deepcopy(records)
        context = pack_morpheme_context("unequalled", records, [], [], {}, "visit")
        selected = context["diagnostics"]["selected_evidence_ids"]
        self.assertTrue({"evidence-target", "evidence-prefix", "evidence-suffix", "evidence-root"} <= set(selected))
        self.assertLessEqual(len(context["evidence"]), 8)
        self.assertLessEqual(sum(len(item["excerpt"]) for item in context["evidence"]), 2400)
        self.assertEqual(records, untouched)

    def test_duplicate_records_merge_candidate_spans_without_repeating_book_text(self):
        first = record("same", surface="read", kind="root", headword="READ")
        second = {**first, "component_start": 2, "component_end": 6}
        context = pack_morpheme_context("reread", [first, second], [], [], {}, "visit")
        self.assertEqual(len(context["evidence"]), 1)
        self.assertEqual(len(context["evidence"][0]["candidate_matches"]), 2)

    def test_late_target_and_plain_dictionary_definition_are_kept(self):
        late = record("late", headword="unequalled", excerpt="unrelated material " * 100 + "unequalled: the useful target passage.")
        definition = record("definition", headword="unequalled", excerpt="")
        definition["definition"] = "without an equal"
        context = pack_morpheme_context("unequalled", [late, definition], [], [], {}, "visit")
        text = " ".join(item["excerpt"] for item in context["evidence"])
        self.assertIn("useful target passage", text)
        self.assertIn("without an equal", text)

    def test_required_provenance_survives_selection_but_is_not_requested_from_model(self):
        fixed = record("fixed", headword="fixture", excerpt="fixed source")
        records = [record("noise-" + str(index)) for index in range(20)] + [fixed]
        shape = [{"surface": "fixture", "kind": "root", "evidence_ids": ["evidence-fixed"]}]
        context = pack_morpheme_context("fixture", records, shape, [], {}, "visit")
        self.assertIn("evidence-fixed", context["diagnostics"]["selected_evidence_ids"])
        self.assertTrue(all(item["evidence_id"] == "" for item in context["evidence"]))
        self.assertEqual(context["required_parts"], [{"surface": "fixture", "kind": "root"}])

    def test_graph_payload_is_not_sent_and_neighbor_selection_can_rotate(self):
        graph = {"nodes": [
            {"entity_type": "term", "label": "neighbor" + chr(97 + index),
             "payload": {"meaning": "GIANT GRAPH PAYLOAD" * 1000}}
            for index in range(26)
        ], "edges": [{"diagnostic": "GIANT GRAPH PAYLOAD" * 1000}]}
        selections = set()
        for index in range(12):
            context = pack_morpheme_context("target", [], [], [], graph, str(index))
            self.assertLessEqual(len(context["known_neighbors"]), 12)
            selections.add(tuple(context["known_neighbors"]))
            prompt = build_morpheme_prompt("target", context)
            self.assertNotIn("GIANT GRAPH PAYLOAD", prompt)
            self.assertLess(len(prompt), 4000)
        self.assertGreater(len(selections), 1)

    def test_review_is_self_contained_and_does_not_copy_large_derivative_arrays(self):
        context = pack_morpheme_context("target", [], [], [], {}, "visit")
        draft = {"parts": [part("target", "target", "free", ["one sense", "another sense"])],
                 "derivatives": [{"note": "HUGE DERIVATIVE ARRAY" * 1000}]}
        prompt = build_morpheme_prompt("target", context, review_reason="reconsider structure", draft=draft)
        self.assertIn("LINGUISTIC REVIEW", prompt)
        self.assertIn("canonical_form:", prompt)
        self.assertIn("kind: prefix, root, suffix, or free", prompt)
        self.assertIn("one sense or another sense", prompt)
        self.assertIn("derivatives: []", prompt)
        self.assertNotIn("HUGE DERIVATIVE ARRAY", prompt)
        self.assertIn("missing printed formula does not make a word indivisible", prompt)

    def test_derivative_batch_is_not_described_as_a_lifetime_limit(self):
        context = pack_morpheme_context("target", [], [], [], {}, "visit")
        prompt = build_morpheme_prompt("target", context)
        self.assertIn("up to 3 new derivatives", prompt)
        self.assertIn("not a limit on a word's eventual neighbors", prompt)
        self.assertIn("do not submit synonyms as derivatives", prompt)

    def test_same_word_dictionary_hit_does_not_trigger_a_redundant_review(self):
        draft = {"parts": [part("target", "target", "free")]}
        same = [record("self", surface="target", kind="root")]
        self.assertEqual(morpheme_review_reason(draft, "target", [], same, []), "")
        shorter = [record("short", surface="tar", kind="root")]
        self.assertIn("shorter candidate", morpheme_review_reason(draft, "target", [], shorter, []))

    def test_structural_checks_allow_spelling_changes_but_reject_missing_letters(self):
        draft = {"parts": [part("happi", "happy", "root"), part("ness", "-ness", "suffix")]}
        self.assertEqual(morpheme_structure_error(draft, "happiness", []), "")
        draft["parts"][0]["surface"] = "hap"
        self.assertIn("cover every word letter", morpheme_structure_error(draft, "happiness", []))

    def test_cited_role_preserves_the_canonical_spelling_adjustment(self):
        parts = [part("runn", "run", "root")]
        fixed = [{"ordinal": 0, "surface": "runn", "kind": "root", "evidence_ids": ["book-role"]}]
        result = _apply_cited_component_roles(parts, fixed)
        self.assertEqual(result[0]["canonical_form"], "run")
        self.assertEqual(result[0]["surface"], "runn")
        self.assertEqual(parts[0]["evidence_ids"], [])

    def test_plain_exact_dictionary_record_is_context_without_becoming_role_proof(self):
        item = SimpleNamespace(
            locator="fixture", pages=[1],
            to_dict=lambda: {"headword": "un-", "excerpt": "plain definition"},
        )
        index = mock.Mock()
        index.exact.return_value = [item]
        retriever = mock.Mock(roots=index, affixes=index)
        retriever._hash.return_value = "source-hash"
        result = WordEvidenceRetriever.component_evidence(retriever, "un-", "prefix")
        index.exact.assert_called_once_with("un", 4)
        self.assertEqual(len(result), 1)
        self.assertEqual(_retrieved_component_evidence_ids("un-", "prefix", result), [])


class FocusedMorphemeWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(Path(temporary.name) / "knowledge.sqlite3")
        self.hints = {
            ("un", "prefix"): [record("un", surface="un", kind="prefix", headword="un-", excerpt="not")],
            ("ed", "suffix"): [record("ed", surface="ed", kind="suffix", headword="-ed", excerpt="past participle")],
            ("equal", "root"): [record("equal", surface="equal", kind="root", headword="EQUAL", excerpt="same or level")],
        }

    def prepare(self, word, responses):
        term = self.store.upsert_term("en", word, status="accepted")
        base = record("target-" + word, headword=word, excerpt="Synthetic lexical definition for this test.")
        evidence = self.store.add_evidence(base["corpus_id"], base["entry_id"], excerpt=base["excerpt"], payload=base)
        base["knowledge_evidence_id"] = evidence
        seed = self.store.enqueue_job("fixture", "term:" + term, subject_entity_id=term)
        self.store.save_job_artifact(
            seed, "retrieved-evidence", {"records": [base]}, language="en", validation_state="candidate",
        )
        self.store.finish_job(seed)
        retriever = mock.Mock()
        retriever.component_evidence.side_effect = lambda form, kind: copy.deepcopy(self.hints.get((form, kind), []))
        model = mock.Mock(model_name="local-fixture")
        model.complete_json.side_effect = [
            {"value": response, "model": "local-fixture", "metrics": {"requests": 1, "elapsed_seconds": index + 2}}
            for index, response in enumerate(responses)
        ]
        worker = PreparationWorker(self.store, retriever, model, pronouncer=mock.Mock())
        worker._lexical_view = mock.Mock(return_value={"nodes": [], "edges": []})
        job = self.store.enqueue_job("split-morphemes", "term:" + term, subject_entity_id=term, model=model.model_name)
        result = worker.run_once(("split-morphemes",))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "complete", str(result))
        accepted = self.store.artifacts_for_job(job, stage="accepted-morpheme-split")[-1]["payload"]
        return accepted, model, retriever, job

    def test_worker_uses_prefix_root_suffix_context_in_one_model_request(self):
        parts = [part("un", "un-", "prefix", "not"), part("equall", "equal", "root", "same or level"),
                 part("ed", "-ed", "suffix", "past participle")]
        accepted, model, retriever, job = self.prepare("unequalled", [{"parts": parts, "derivatives": []}])
        model.complete_json.assert_called_once()
        calls = {(call.args[0], call.args[1]) for call in retriever.component_evidence.call_args_list}
        self.assertTrue({("un", "prefix"), ("ed", "suffix"), ("equal", "root")} <= calls)
        self.assertEqual([item["canonical_form"] for item in accepted["parts"]], ["un-", "equal", "-ed"])
        self.assertEqual(accepted["analysis_policy"], STRUCTURE_POLICY)
        self.assertTrue(all(item["basis"] == "model" for item in accepted["parts"]))
        self.assertEqual(accepted["morphology_inference"]["policy"], MORPHEME_PROMPT_POLICY)
        self.assertEqual(len(accepted["morphology_inference"]["inference_trace"]), 1)
        context = self.store.artifacts_for_job(job, stage="morpheme-prompt-context")[-1]["payload"]["context"]
        roles = {match["kind"] for item in context["evidence"] for match in item["candidate_matches"]}
        self.assertTrue({"prefix", "root", "suffix"} <= roles)

    def test_bad_reviewer_does_not_discard_a_structurally_usable_first_draft(self):
        first = {"parts": [part("unequalled", "unequalled", "free", ["unmatched", "without equal"])]}
        accepted, model, _, job = self.prepare("unequalled", [first, {"parts": [42]}])
        self.assertEqual(model.complete_json.call_count, 2)
        self.assertEqual(accepted["parts"][0]["surface"], "unequalled")
        trace = accepted["morphology_inference"]
        self.assertTrue(trace["retained_first_draft"])
        self.assertEqual([item["stage"] for item in trace["inference_trace"]], ["draft", "review"])
        reviewed = self.store.artifacts_for_job(job, stage="model-morpheme-review-draft")[-1]
        self.assertEqual(reviewed["validation_state"], "rejected")
        self.assertIn("shorter candidate", reviewed["payload"]["review_reason"])

    def test_valid_review_can_replace_a_forced_split_without_losing_first_derivatives(self):
        first = {"parts": [part("un", "un-", "prefix"), part("equalled", "-equalled", "suffix")]}
        second = {"parts": [part("unequalled", "unequalled", "free")], "derivatives": []}
        accepted, model, _, _ = self.prepare("unequalled", [first, second])
        self.assertEqual(model.complete_json.call_count, 2)
        self.assertEqual(accepted["parts"][0]["kind"], "free")
        self.assertFalse(accepted["morphology_inference"]["retained_first_draft"])

    def test_ingestion_does_not_truncate_valid_proposals_to_the_prompt_batch_hint(self):
        derivatives = [
            {"term": word, "note": "shared lexical base", "component_forms": ["equal"]}
            for word in ("equally", "equality", "equalize", "equalizer", "equalization", "unequal")
        ]
        accepted, model, _, _ = self.prepare("equal", [{
            "parts": [part("equal", "equal", "root")], "derivatives": derivatives,
        }])
        model.complete_json.assert_called_once()
        self.assertEqual(len(accepted["related_terms"]), 6)


if __name__ == "__main__":
    unittest.main()
