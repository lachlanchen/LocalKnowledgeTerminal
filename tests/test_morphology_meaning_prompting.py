from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import Mock

from lkt.atomic import PreparationWorker
from lkt.morphology_meaning_prompting import build_morphology_meaning_prompt


FOCUS = "morpheme-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
EVIDENCE = "evidence-11111111-2222-3333-4444-555555555555"


def context():
    return {
        "entities": [
            {"id": FOCUS, "type": "morpheme", "label": "spect"},
            {"id": "term-uuid-one", "type": "term", "label": "inspection"},
            {"id": "term-uuid-two", "type": "term", "label": "unrelated"},
        ],
        "assertions": [
            {"source": "term-uuid-one", "target": FOCUS, "relation": "has-component"},
            {"source": "term-uuid-two", "target": "term-uuid-one", "relation": "derived-from"},
        ],
        "evidence": [{
            "evidence_id": EVIDENCE, "locator": "Root Dictionary page 12",
            "excerpt": "spect: look or see; inspect, inspection, spectator.",
        }],
    }


def build(data):
    return build_morphology_meaning_prompt(
        entity_id=FOCUS, label="spect", kind="root", language="en",
        language_name="English", context=data,
    )


class MorphologyMeaningPromptTests(unittest.TestCase):
    def test_readable_neighbors_and_code_owned_evidence_manifest(self):
        data = context()
        before = deepcopy(data)
        system, prompt, trace = build(data)
        self.assertIn("inspection", prompt)
        self.assertNotIn("unrelated", prompt)
        self.assertNotIn(FOCUS, prompt)
        self.assertNotIn(EVIDENCE, prompt)
        self.assertNotIn("term-uuid", prompt)
        self.assertIn("spect: look or see", prompt)
        self.assertEqual(trace["evidence_aliases"], {"E1": EVIDENCE})
        self.assertEqual(trace["selected_evidence_ids"], [EVIDENCE])
        self.assertEqual(data, before)
        self.assertEqual(trace["prompt_characters"], len(system) + len(prompt))

    def test_late_focus_text_survives_long_ocr_record(self):
        data = context()
        data["evidence"][0]["excerpt"] = (
            "Unrelated introductory material. " * 120 +
            "spect means to look or see; inspection uses this root. " +
            "Additional unrelated material. " * 80
        )
        _, prompt, trace = build(data)
        self.assertIn("spect means to look or see", prompt)
        self.assertLessEqual(trace["excerpt_characters"], 360)

    def test_focus_records_rank_ahead_of_unrelated_prefix_matches(self):
        data = context()
        data["evidence"] = [
            {"evidence_id": f"noise-{index}", "excerpt": "spectacle-like noise " * 40}
            for index in range(6)
        ] + data["evidence"]
        _, prompt, trace = build(data)
        self.assertEqual(trace["evidence_aliases"]["E1"], EVIDENCE)
        self.assertIn("spect: look or see", prompt)
        self.assertLessEqual(trace["selected_records"], 3)
        self.assertLessEqual(trace["excerpt_characters"], 1080)

    def test_duplicates_and_invalid_records_do_not_waste_context(self):
        data = context()
        data["evidence"] += [None, "invalid", deepcopy(data["evidence"][0])]
        _, _, trace = build(data)
        self.assertEqual(trace["selected_records"], 1)

    def test_context_only_case_has_no_fabricated_evidence(self):
        data = context()
        data["evidence"] = []
        _, prompt, trace = build(data)
        self.assertIn("inspection", prompt)
        self.assertIn("evidence_ids: exactly []", prompt)
        self.assertEqual(trace["selected_evidence_ids"], [])
        self.assertEqual(trace["evidence_aliases"], {})

    def test_prompt_budget_is_independent_of_graph_uuid_volume(self):
        data = context()
        data["evidence"] = [
            {"evidence_id": f"book-{index}", "locator": "page 1", "excerpt": f"spect {index} " + "book details " * 1000}
            for index in range(80)
        ]
        data["entities"] += [
            {"id": f"node-{index}", "label": f"example{index}"} for index in range(100)
        ]
        data["assertions"] += [
            {"source": FOCUS, "target": f"node-{index}", "relation": "derived-from"}
            for index in range(100)
        ]
        _, _, trace = build(data)
        self.assertLessEqual(trace["prompt_characters"], 2600)
        self.assertEqual(trace["selected_records"], 3)
        self.assertEqual(trace["graph_neighbors"], 4)


class MorphologyMeaningWorkerTests(unittest.TestCase):
    def worker(self, evidence_ids):
        worker = PreparationWorker.__new__(PreparationWorker)
        worker.store = Mock()
        worker.store.atomic_semantic_write.side_effect = lambda _entity, persist: persist()
        worker.model = Mock(model_name="local-test")
        worker.retriever = Mock()
        worker.retriever.retrieve.return_value = []
        worker._semantic_state_for_job = Mock(return_value={"status": "claimed"})
        worker.store.semantic_enrichment_context.return_value = {
            "entity_type": "morpheme", "entity_kind": "root",
            "job_type": "prepare-entity-meaning", "desired_fingerprint": "context-v1",
            "context": context(),
        }
        worker.store.artifacts_for_job.return_value = []
        worker.store.save_job_artifact.side_effect = ["draft-id", "accepted-id"]
        worker.store.add_entity_meaning.return_value = "meaning-id"
        worker.store.complete_lexical_semantic_enrichment.return_value = True
        worker.model.complete_json.return_value = {
            "value": {"meaning": "To look or see.", "confidence": 0.7,
                      "evidence_ids": evidence_ids},
            "model": "local-test", "metrics": {},
        }
        return worker

    def run_meaning(self, worker):
        return worker._prepare_entity_meaning({
            "job_id": "meaning-job", "subject_entity_id": FOCUS, "language": "en",
            "source_fingerprint": "context-v1", "prompt_version": "test-v1",
        })

    def test_aliases_resolve_to_real_retrieved_records_and_trace_is_persisted(self):
        worker = self.worker(["E1", "E1", "invented"])
        self.assertEqual(self.run_meaning(worker), "accepted-id")
        worker.model.complete_json.assert_called_once()
        self.assertEqual(
            worker.store.add_entity_meaning.call_args.kwargs["evidence_ids"], [EVIDENCE],
        )
        draft = worker.store.save_job_artifact.call_args_list[0].args[2]
        accepted = worker.store.save_job_artifact.call_args_list[1].args[2]
        self.assertEqual(draft["meaning_inference"]["selected_evidence_ids"], [EVIDENCE])
        self.assertEqual(accepted["meaning_inference"], draft["meaning_inference"])
        self.assertEqual(accepted["evidence_ids"], [EVIDENCE])

    def test_original_retrieved_ids_remain_compatible(self):
        worker = self.worker([EVIDENCE])
        self.run_meaning(worker)
        self.assertEqual(
            worker.store.add_entity_meaning.call_args.kwargs["evidence_ids"], [EVIDENCE],
        )

    def test_no_evidence_result_remains_cautious_without_rejecting_the_gloss(self):
        worker = self.worker([])
        worker.store.semantic_enrichment_context.return_value["context"]["evidence"] = []
        self.run_meaning(worker)
        arguments = worker.store.add_entity_meaning.call_args.kwargs
        self.assertEqual(arguments["evidence_ids"], [])
        self.assertLessEqual(arguments["confidence"], 0.35)
