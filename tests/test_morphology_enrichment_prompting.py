from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.morphology_enrichment_prompting import (
    MORPHOLOGY_ENRICHMENT_MAX_TOKENS,
    MORPHOLOGY_ENRICHMENT_PROMPT_POLICY,
    build_morphology_enrichment_prompt,
)


def context(subtype="ambiguous-kind"):
    return {
        "focus": {"term": "revert", "component_form": "vert", "component_kind": "ambiguous"},
        "observations": [{
            "term": "revert", "raw_form": "vert", "gloss": "turn",
            "proposed_kind": "suffix", "confidence": 0.96273, "subtype": subtype,
            "properties": {"source_language_label": "parser-not-a-language"},
        }],
        "source": {"headword": "revert", "raw_excerpt": "duplicate source must not appear"},
        "evidence_ids": ["book-1"],
    }


def records():
    return [{"evidence_id": "book-1", "locator": "page 42", "excerpt": "re (= back) + vert (= turn)"}]


class FocusedEnrichmentPromptTests(unittest.TestCase):
    def test_component_task_requests_only_connected_component_shape(self):
        system, prompt, trace = build_morphology_enrichment_prompt(context(), records())
        self.assertEqual(trace["task"], "connect-components")
        self.assertIn('"term_components"', prompt)
        self.assertNotIn('"history":', prompt)
        self.assertNotIn('"relations":', prompt)
        self.assertNotIn('"components":', prompt)
        self.assertIn("Position at the start/end", system)
        self.assertIn("one to three", prompt)

    def test_parser_kinds_confidence_ids_and_language_labels_are_not_authority(self):
        payload = context()
        payload["focus"]["term_entity_id"] = "internal-term-id"
        system, prompt, trace = build_morphology_enrichment_prompt(payload, records())
        for forbidden in ("0.96273", "proposed_kind", "parser-not-a-language", "internal-term-id"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("not facts", prompt)
        self.assertIn("never language-label nodes", system)
        self.assertEqual(trace["selected_evidence_ids"], ["book-1"])
        self.assertNotIn("book-1", prompt)

    def test_actual_excerpt_precedes_parser_hints_and_source_is_not_duplicated(self):
        _, prompt, _ = build_morphology_enrichment_prompt(context(), records())
        self.assertLess(prompt.index("re (= back)"), prompt.index("TENTATIVE PARSER"))
        self.assertNotIn("duplicate source must not appear", prompt)

    def test_history_task_offers_non_history_fallback(self):
        for subtype in ("history", "source"):
            with self.subTest(subtype=subtype):
                _, prompt, trace = build_morphology_enrichment_prompt(context(subtype), records())
                self.assertEqual(trace["task"], "trace-history")
                self.assertIn('"history"', prompt)
                self.assertIn('"term_components"', prompt)
                self.assertNotIn('"relations":', prompt)
                self.assertIn("not itself an etymology", prompt)
                self.assertIn('"component_language"', prompt)

    def test_relation_task_keeps_target_meaning_and_language(self):
        for subtype in ("allomorph", "opposition"):
            _, prompt, trace = build_morphology_enrichment_prompt(context(subtype), records())
            self.assertEqual(trace["task"], "relate-components")
            self.assertIn('"target_meaning"', prompt)
            self.assertIn('"target_language"', prompt)
            self.assertNotIn('"history":', prompt)

    def test_inputs_are_immutable_and_prompt_fingerprint_is_stable(self):
        payload, evidence = context(), records()
        before = deepcopy((payload, evidence))
        first = build_morphology_enrichment_prompt(payload, evidence)
        self.assertEqual((payload, evidence), before)
        self.assertEqual(first, build_morphology_enrichment_prompt(payload, evidence))

    def test_large_inputs_have_bounded_excerpt_hint_and_prompt_sizes(self):
        payload = context()
        payload["observations"] *= 100
        payload["observations"][0]["gloss"] = "meaning " * 10000
        evidence = [{"evidence_id": str(i), "excerpt": str(i) + "long " * 10000} for i in range(100)]
        system, prompt, trace = build_morphology_enrichment_prompt(payload, evidence)
        self.assertLessEqual(trace["selected_records"], 4)
        self.assertLessEqual(trace["excerpt_characters"], 2200)
        self.assertLessEqual(trace["parser_hint_characters"], 600)
        self.assertLess(len(system) + len(prompt), 7000)

    def test_late_focus_text_survives_long_record(self):
        evidence = [{"evidence_id": "book-1", "excerpt": "preamble " * 2000 + "revert = turn back. " + "tail " * 1000}]
        _, prompt, _ = build_morphology_enrichment_prompt(context(), evidence)
        self.assertIn("revert = turn back.", prompt)

    def test_duplicate_excerpt_is_not_repeated(self):
        _, prompt, trace = build_morphology_enrichment_prompt(context(), records() * 3)
        self.assertEqual(trace["selected_records"], 1)
        self.assertEqual(prompt.count("re (= back) + vert (= turn)"), 1)

    def test_source_fallback_does_not_invent_evidence_ids(self):
        _, prompt, trace = build_morphology_enrichment_prompt(context(), [])
        self.assertIn("duplicate source must not appear", prompt)
        self.assertTrue(trace["source_excerpt_fallback"])
        self.assertEqual(trace["selected_evidence_ids"], [])

    def test_empty_or_imperfect_context_is_still_a_model_task(self):
        _, prompt, trace = build_morphology_enrichment_prompt(
            {"focus": None, "source": None, "observations": [None, "bad"]}, [None],
        )
        self.assertEqual(trace["selected_records"], 0)
        self.assertIn("Return whatever useful subset", prompt)


class FocusedEnrichmentWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name) / "knowledge.sqlite3")
        self.job_id = self.store.enqueue_job("enrich-morphology-v3", "cluster:focused-test")
        self.model = Mock()
        self.worker = PreparationWorker(self.store, object(), self.model)
        self.context = {"cluster_id": "test", "input_fingerprint": "input", "payload": context(), "evidence": records()}

    def run_enrichment(self):
        with patch.object(self.store, "morphology_enrichment_context", return_value=self.context), patch.object(self.store, "complete_morphology_v3_enrichment"):
            self.worker._enrich_morphology_v3({"job_id": self.job_id})
        return self.store.artifacts_for_job(self.job_id, stage="accepted-morphology-v3-enrichment")[-1]["payload"]

    def test_partial_connected_response_is_persisted_with_trace_in_one_call(self):
        raw = {"term_components": [{"term": "revert", "component_form": "vert", "component_kind": "root", "meaning": "turn", "confidence": 0.7}]}
        self.model.complete_json.return_value = {"value": raw, "metrics": {"requests": 1}}
        result = self.run_enrichment()
        self.model.complete_json.assert_called_once()
        self.assertEqual(self.model.complete_json.call_args.kwargs["max_tokens"], MORPHOLOGY_ENRICHMENT_MAX_TOKENS)
        self.assertEqual(result["claims"][0]["component"], "vert")
        self.assertEqual(result["inference"]["policy"], MORPHOLOGY_ENRICHMENT_PROMPT_POLICY)
        saved = self.store.artifacts_for_job(self.job_id, stage="morphology-v3-model-draft")[-1]["payload"]
        self.assertEqual(saved["draft"], raw)
        self.assertEqual(saved["inference"], result["inference"])

    def test_old_saved_draft_replays_without_new_model_request(self):
        self.store.save_job_artifact(self.job_id, "morphology-v3-model-draft", {
            "draft": {"term_components": [{"term": "revert", "component_form": "vert", "component_kind": "root"}]},
            "metrics": {"requests": 1, "elapsed_seconds": 123},
        })
        result = self.run_enrichment()
        self.model.complete_json.assert_not_called()
        self.assertEqual(result["metrics"]["elapsed_seconds"], 123)
        self.assertEqual(result["inference"]["policy"], "legacy-v3-unfocused")
        self.assertEqual(len(result["claims"]), 1)

    def test_extra_useful_claims_are_not_cut_to_per_visit_prompt_hint(self):
        self.model.complete_json.return_value = {"term_components": [
            {"term": term, "component_form": "vert", "component_kind": "root", "meaning": "turn"}
            for term in ("revert", "convert", "divert", "invert")
        ]}
        result = self.run_enrichment()
        self.assertEqual(len(result["claims"]), 4)

    def test_invalid_claim_does_not_discard_valid_partial_result(self):
        self.model.complete_json.return_value = {"term_components": [
            {"term": "revert", "component_form": "vert", "component_kind": "root", "meaning": "turn"},
            {"term": "revert", "component_form": "parser label", "component_kind": "ambiguous"},
        ]}
        result = self.run_enrichment()
        self.assertEqual(len(result["claims"]), 1)
        self.assertTrue(result["warnings"])
