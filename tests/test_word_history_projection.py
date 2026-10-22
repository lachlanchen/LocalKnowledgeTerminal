from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import unittest

from lkt.atomic import PreparationWorker, _canonical_component_projection_parts
from lkt.knowledge import KnowledgeStore
from lkt.store import CardStore, card_validation_errors
from tests import test_partial_morphology as morphology
from tests import test_partial_repair_planning as planning


class WordHistoryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.case = planning.PartialRepairPlanningTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.worker, self.term = self.case.store, self.case.worker, self.case.term
        self.worker.card_store = CardStore(Path(self.store.database).with_name("cards.sqlite3"))
        self.worker._lexical_view = PreparationWorker._lexical_view.__get__(self.worker)

    def prepare_history(self, *, split=True):
        self.case.prepare({"parts": []})
        self.case.checkpoints()
        self.history = self.store.add_historical_form(
            "fro", "peint", period_label="Old French", meaning="painted",
            status="accepted", quality_score=0.65,
        )
        self.store.accept_relation_assertion(
            self.term, self.history, self.term, "developed-into",
            basis="model", confidence=0.65, owner_type="fixture-history",
            owner_id="word-history", properties={"component_id": self.term},
        )
        self.store.save_job_artifact(
            self.case.job["job_id"], "accepted-origin-branches",
            {"branches": [{"component_id": self.term, "component_kind": "free", "steps": [{
                "historical_form_id": self.history, "form": "peint", "language": "fro",
                "period": "Old French", "meaning": "painted", "confidence": 0.65,
                "basis": "model", "evidence_ids": [], "edge_evidence_ids": [],
            }]}]}, language="en", validation_state="accepted", quality_score=0.65,
        )
        if not split:
            with closing(self.store._connect()) as connection:
                connection.execute(
                    "UPDATE job_artifacts SET validation_state='candidate' "
                    "WHERE stage='accepted-morpheme-split' AND job_id=?",
                    (self.case.job["job_id"],),
                )
                connection.commit()
        self.store.refresh_lexical_projection_repair(self.term)

    def compose(self):
        claim = self.store.claim_lexical_projection_repair()
        self.assertIsNotNone(claim)
        fingerprint = claim["desired_fingerprint"]
        job_id = self.store.enqueue_job(
            "compose-origin-card", self.case.fixture.subject, subject_entity_id=self.term,
            prompt_version="autonomous-lexical-v4-projection-local-p" + fingerprint + "-f0",
            source_fingerprint=fingerprint,
        )
        self.assertTrue(self.store.bind_lexical_projection_repair(
            self.term, fingerprint, claim["lease_token"], job_id,
        ))
        self.case.fixture.model.complete_json.reset_mock()
        result = self.worker.run_once(job_types=("compose-origin-card",))
        self.assertEqual(result.status, "complete", repr(result))
        self.case.fixture.model.complete_json.assert_not_called()
        payload = self.store.artifacts_for_job(job_id, stage="accepted-origin-card")[-1]["payload"]
        state = self.store.lexical_projection_repair_state(self.term)
        self.assertEqual(state["status"], "applied")
        return payload

    def clear_current_cursor(self):
        with closing(self.store._connect()) as connection:
            connection.execute(
                "DELETE FROM lexical_projection_bootstrap_state "
                "WHERE scheduler_key='lexical-projection-contract-v8'"
            )
            connection.commit()

    def test_empty_live_memberships_do_not_fall_back_to_old_artifact(self):
        old = [{"morpheme_id": "retired", "canonical_form": "paint"}]
        self.assertEqual(_canonical_component_projection_parts(self.term, old, []), ([], {}))
        self.assertEqual(_canonical_component_projection_parts(self.term, old, None),
                         (old, {"retired": "retired"}))

    def test_last_component_withdrawal_retires_published_branch_cards(self):
        self.case.prepare()
        self.case.checkpoints()
        before = self.compose()
        self.assertEqual({key.split(":")[0] for key in before["derived_card_ids"]}, {"root", "affix"})
        components = self.case.supported()
        retired_ids = {part["morpheme_id"] for part in components}
        for component in components:
            self.case.retire(component)
        after = self.compose()
        self.assertEqual(after["derived_card_ids"], {})
        for graph_name in ("morphology_graph", "lexical_view"):
            nodes = after["card"]["extensions"][graph_name]["nodes"]
            self.assertFalse(retired_ids & {node["id"] for node in nodes})
        with closing(self.worker.card_store._connect()) as connection:
            for card_id in before["derived_card_ids"].values():
                row = connection.execute(
                    "SELECT status FROM cards WHERE card_id=?", (card_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "archived")
        self.assertEqual(after["card"]["extensions"]["morphology_graph"]["status"],
                         "awaiting-enrichment")
        self.assertEqual(self.worker.card_store.recent_summaries(mode="word"), [])
        self.assertEqual(self.worker.card_store.recent_summaries(mode="root"), [])
        self.assertEqual(self.worker.card_store.recent_summaries(mode="affix"), [])
        self.case.fixture.assert_invariants()

    def test_source_word_history_publishes_without_fake_morpheme_or_empty_parts_slide(self):
        self.prepare_history()
        result = self.compose()
        graph = result["card"]["extensions"]["morphology_graph"]
        self.assertEqual({node["id"] for node in graph["nodes"]}, {self.term, self.history})
        self.assertEqual([(edge["source"], edge["target"]) for edge in graph["edges"]],
                         [(self.history, self.term)])
        self.assertNotIn("parts", {area["id"] for area in graph["focus_areas"]})
        self.assertTrue(any(area["kind"] == "word-history" for area in graph["focus_areas"]))
        self.assertEqual(result["derived_card_ids"], {})
        self.assertEqual(self.case.supported(), [])

    def test_missing_split_checkpoint_does_not_block_word_history_repair(self):
        self.prepare_history(split=False)
        result = self.compose()
        artifact_ids = result["knowledge_artifact_ids"]
        self.assertEqual(len(artifact_ids), 2)
        self.assertEqual(result["card"]["subtitle"], "Word history")

    def test_components_with_no_history_branches_still_publish(self):
        self.case.prepare({"parts": [morphology.part("paint")]})
        self.case.checkpoints()
        self.store.save_job_artifact(
            self.case.job["job_id"], "accepted-origin-branches", {"branches": []},
            language="en", validation_state="accepted", quality_score=0.6,
        )
        result = self.compose()
        self.assertEqual({key.split(":")[0] for key in result["derived_card_ids"]}, {"root"})

    def test_withdrawn_source_history_is_not_resurrected_without_a_split(self):
        self.prepare_history(split=False)
        self.compose()
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT assertion_id FROM relation_assertions WHERE subject_entity_id=? "
                "AND source_entity_id=? AND target_entity_id=? AND relation='developed-into'",
                (self.term, self.history, self.term),
            ).fetchone()
        self.case.retire({"assertion_id": row[0]})
        self.assertEqual(self.store.accepted_word_history_edges(self.term), [])
        result = self.compose()
        graph = result["card"]["extensions"]["morphology_graph"]
        self.assertEqual([node["id"] for node in graph["nodes"]], [self.term])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(result["card"]["subtitle"], "History awaiting enrichment")

    def test_version_eight_bootstrap_discovers_word_only_history(self):
        self.prepare_history(split=False)
        with closing(self.store._connect()) as connection:
            connection.execute("DELETE FROM lexical_projection_repairs WHERE term_entity_id=?", (self.term,))
            connection.execute(
                "INSERT OR REPLACE INTO lexical_projection_bootstrap_state "
                "(scheduler_key,cursor_entity_id,scanned_count,complete,updated_at) "
                "VALUES ('lexical-projection-contract-v7',?,1,1,'2026-01-01')", (self.term,),
            )
            connection.commit()
        self.clear_current_cursor()
        first = self.store.bootstrap_lexical_projection_repairs(limit=1)
        self.assertEqual((first["scanned"], first["marked"], first["complete"]), (1, 1, False))
        restarted = KnowledgeStore(self.store.database, serving=True)
        final = restarted.bootstrap_lexical_projection_repairs(limit=1)
        self.assertTrue(final["complete"])
        self.assertEqual(final["scanned_total"], 1)
        self.assertEqual(restarted.lexical_projection_repair_state(self.term)["desired_modes"], [])
        self.compose()

    def test_bootstrap_revisits_existing_empty_cleanup_rows(self):
        self.case.prepare({"parts": [morphology.part("paint")]})
        self.case.checkpoints()
        self.compose()
        self.case.retire(self.case.supported()[0])
        self.clear_current_cursor()
        page = self.store.bootstrap_lexical_projection_repairs(limit=1)
        self.assertEqual(page["scanned"], 1)
        self.assertEqual(self.store.lexical_projection_repair_state(self.term)["desired_modes"], [])

    def test_unowned_history_does_not_create_repair_or_reappear(self):
        self.prepare_history()
        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE relation_assertion_supports SET status='retired' WHERE assertion_id IN "
                "(SELECT assertion_id FROM relation_assertions WHERE subject_entity_id=?)", (self.term,),
            )
            connection.execute("DELETE FROM lexical_projection_repairs WHERE term_entity_id=?", (self.term,))
            connection.commit()
        self.clear_current_cursor()
        self.assertEqual(self.store.accepted_word_history_edges(self.term), [])
        self.assertEqual(self.store.bootstrap_lexical_projection_repairs()["scanned"], 0)

    def test_pending_projection_is_explicit_and_does_not_weaken_other_validation(self):
        self.case.prepare({"parts": [morphology.part("paint")]})
        self.case.checkpoints()
        self.case.retire(self.case.supported()[0])
        card = self.compose()["card"]
        self.assertEqual(card_validation_errors(card), [])
        self.assertEqual(card["extensions"]["morphology_graph"]["status"], "awaiting-enrichment")
        self.assertEqual(self.worker.card_store.recent_summaries(mode="word"), [])
        for defect in ("unmarked", "root", "affix", "wrong-center", "wrong-type",
                       "wrong-word", "missing-meaning", "missing-evidence",
                       "missing-edges", "wrong-policy"):
            with self.subTest(defect=defect):
                invalid = deepcopy(card)
                graph = invalid["extensions"]["morphology_graph"]
                if defect == "unmarked":
                    graph.pop("status")
                elif defect in ("root", "affix"):
                    invalid["mode"] = defect
                elif defect == "wrong-center":
                    graph["center_id"] = "not-the-word"
                elif defect == "wrong-type":
                    graph["nodes"][0]["type"] = "root"
                elif defect == "wrong-word":
                    graph["nodes"][0]["form"] = "different"
                elif defect == "missing-meaning":
                    graph["nodes"][0]["meaning"] = ""
                elif defect == "missing-evidence":
                    invalid["evidence"] = []
                elif defect == "missing-edges":
                    graph["edges"] = None
                else:
                    invalid["extensions"]["knowledge_policy"] = "model-output"
                self.assertTrue(card_validation_errors(invalid))

    def test_enriched_pending_projection_returns_to_rotation(self):
        self.prepare_history(split=False)
        with closing(self.store._connect()) as connection:
            assertion = connection.execute(
                "SELECT assertion_id FROM relation_assertions WHERE subject_entity_id=? "
                "AND source_entity_id=? AND target_entity_id=? AND relation='developed-into'",
                (self.term, self.history, self.term),
            ).fetchone()[0]
        self.case.retire({"assertion_id": assertion})
        pending = self.compose()
        self.assertEqual(pending["card"]["extensions"]["morphology_graph"]["status"],
                         "awaiting-enrichment")
        self.assertEqual(self.worker.card_store.recent_summaries(mode="word"), [])
        self.store.accept_relation_assertion(
            self.term, self.history, self.term, "developed-into",
            basis="model", confidence=0.7, owner_type="fixture-history",
            owner_id="new-enrichment", properties={"component_id": self.term},
        )
        enriched = self.compose()
        graph = enriched["card"]["extensions"]["morphology_graph"]
        self.assertNotEqual(graph.get("status"), "awaiting-enrichment")
        self.assertEqual([(edge["source"], edge["target"]) for edge in graph["edges"]],
                         [(self.history, self.term)])
        self.assertEqual([card["card_id"] for card in self.worker.card_store.recent_summaries(mode="word")],
                         [enriched["card_id"]])

    def test_history_edges_do_not_import_another_words_history(self):
        self.prepare_history()
        other = self.store.upsert_term("en", "painter", status="accepted")
        self.store.accept_relation_assertion(
            other, self.history, self.term, "developed-into", basis="model",
            confidence=0.6, owner_type="fixture-cross-word", owner_id="cross",
        )
        self.assertEqual(self.store.accepted_word_history_edges(other), [])
        self.assertIsNone(self.store.lexical_projection_repair_state(other))
