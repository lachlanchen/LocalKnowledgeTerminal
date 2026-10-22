from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.relation_support import _assert_morphology_finalization_invariants
from lkt.store import CardStore


class GrowthIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root / "knowledge.sqlite3")

    def archive(self, entity_id):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE entities SET status='archived' WHERE entity_id=?", (entity_id,))
            c.commit()

    def test_archived_endpoints_cannot_reenter_any_structural_writer(self):
        term = self.store.upsert_term("en", "tactical")
        component = self.store.upsert_morpheme("en", "tact", "root")
        self.archive(term)
        for write in (
            lambda: self.store.link_morpheme_once(term, component, "tact"),
            lambda: self.store.link_morpheme(term, component, 0, "tact"),
            lambda: self.store.add_edge(term, component, "has-component"),
            lambda: self.store.accept_relation_assertion(term, term, component, "has-component"),
        ):
            with self.assertRaisesRegex(ValueError, "archived graph endpoint"):
                write()
        with closing(self.store._connect()) as c:
            for table in ("term_morphemes", "entity_edges", "relation_assertions"):
                self.assertEqual(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.archive(component)
        term2 = self.store.upsert_term("en", "intact")
        with self.assertRaisesRegex(ValueError, "archived graph endpoint"):
            self.store.link_morpheme_once(term2, component, "tact")

    def test_repair_retires_owned_links_and_is_idempotent(self):
        term = self.store.upsert_term("en", "graduate")
        component = self.store.upsert_morpheme("en", "grad", "root")
        self.store.link_morpheme_once(term, component, "grad")
        self.store.accept_relation_assertion(
            term, term, component, "has-component", properties={"surface": "grad"}
        )
        self.archive(term)
        result = self.store.repair_archived_morphology_links()
        self.assertEqual(result["assertions_archived"], 1)
        self.assertEqual(result["unowned_deferred"], 0)
        with closing(self.store._connect()) as c:
            _assert_morphology_finalization_invariants(c)
            self.assertEqual(c.execute("SELECT status FROM relation_assertion_supports").fetchone()[0], "retired")
            self.assertEqual(c.execute("SELECT count(*) FROM term_morphemes").fetchone()[0], 0)
            c.execute("PRAGMA query_only=ON")
            version = c.execute("PRAGMA data_version").fetchone()[0]
            self.assertEqual(self.store.repair_archived_morphology_links()["assertions_archived"], 0)
            self.assertEqual(c.execute("PRAGMA data_version").fetchone()[0], version)

    def test_enrichment_keeps_good_claim_when_focus_term_was_archived(self):
        term = self.store.upsert_term("en", "graduate")
        self.archive(term)
        job_id = self.store.enqueue_job("enrich-morphology-v3", "cluster:test")
        context = {"cluster_id": "test", "input_fingerprint": "input", "evidence": [],
                   "payload": {"focus": {"term": "graduate", "term_entity_id": term}}}
        model = Mock()
        model.complete_json.return_value = {"term_components": [
            {"term": "graduate", "component_form": "grad", "component_kind": "root"},
            {"term": "reader", "component_form": "read", "component_kind": "root"},
        ]}
        worker = PreparationWorker(self.store, object(), model)
        with patch.object(self.store, "morphology_enrichment_context", return_value=context), patch.object(self.store, "complete_morphology_v3_enrichment"):
            worker._enrich_morphology_v3({"job_id": job_id})
        artifact = self.store.artifacts_for_job(job_id, stage="accepted-morphology-v3-enrichment")[-1]["payload"]
        self.assertEqual([x["term"] for x in artifact["claims"]], ["reader"])
        self.assertTrue(any("archived graph endpoint" in x for x in artifact["warnings"]))
        with closing(self.store._connect()) as c:
            _assert_morphology_finalization_invariants(c)
            self.assertEqual(c.execute("SELECT status FROM entities WHERE entity_id=?", (term,)).fetchone()[0], "archived")

    def test_worker_advances_word_card_inputs_ahead_of_replenished_repairs(self):
        term = self.store.upsert_term("en", "reader")
        subject = f"term:{term}"
        translation = self.store.enqueue_job("prepare-translation", subject, subject_entity_id=term, language="fr", priority=50)
        # Production plans historically left language jobs as implicit inputs.
        grammar = self.store.enqueue_job("prepare-grammar-properties", subject, subject_entity_id=term, priority=70)
        self.store.enqueue_job("compose-word-card", subject, subject_entity_id=term, priority=90, depends_on=(grammar,))
        repair = self.store.enqueue_job("compose-origin-card", subject, subject_entity_id=term, priority=5)
        worker = PreparationWorker(self.store, object(), object(), card_store=CardStore(self.root / "cards.sqlite3"))
        with patch.object(worker, "_prepare_translation", return_value="translation-artifact"):
            result = worker.run_once()
        self.assertEqual(result.job_id, translation)
        self.assertEqual(result.status, "complete")
        self.assertIsNone(self.store.claim_word_card_progress())
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute("SELECT status FROM preparation_jobs WHERE job_id=?", (repair,)).fetchone()[0], "queued")
        for index in range(6):
            job = self.store.enqueue_job("retrieve-evidence", f"other:{index}")
            self.store.finish_job(job)
        self.assertEqual(self.store.claim_word_card_progress()["job_id"], grammar)
