from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lkt.atomic import PreparationWorker, WordEvidenceRetriever
from lkt.knowledge import KnowledgeStore
from lkt.models import Evidence
from lkt.preparation import SemanticEnrichmentSeeder
from lkt.semantic_retrieval import needs_book_retrieval, retrieve_semantic_books
from tests.test_semantic_lane import (
    _evidence_free_morphology_fixture, _SemanticModel, _UnusedRetriever,
)


def book_record(**changes):
    record = {
        "corpus_id": "test-root-book", "entry_id": "printed-entry",
        "headword": "vertatic", "locator": "page 12",
        "excerpt": "vert (= turn), as in vertatic.",
        "source_title": "Fixture Root Book", "pages": [12],
    }
    record.update(changes)
    return record


class Retriever:
    def __init__(self, records=(), on_call=None):
        self.records = list(records)
        self.calls = []
        self.on_call = on_call

    def retrieve(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.on_call:
            action, self.on_call = self.on_call, None
            action()
        return deepcopy(self.records)


class Index:
    def __init__(self, records=()):
        self.records = list(records)
        self.calls = []

    def search(self, query, limit):
        self.calls.append(("search", query, limit))
        return self.records[:limit]

    def exact(self, query, limit):
        self.calls.append(("exact", query, limit))
        return self.records[:limit]


class SemanticBookLookupTests(unittest.TestCase):
    def context(self):
        return {
            "entities": [
                {"id": "focus", "type": "morpheme", "label": "vert"},
                *({"id": f"word-{i}", "type": "term", "label": f"word{i}"} for i in range(5)),
                {"id": "unrelated", "type": "term", "label": "unrelated"},
            ],
            "assertions": [
                {"source": f"word-{i}", "target": "focus", "relation": "has-component"}
                for i in range(5)
            ],
        }

    def search(self, retriever, language="en"):
        return retrieve_semantic_books(
            retriever, entity_id="focus", label="vert",
            source_language=language, context=self.context(),
        )

    def test_empty_and_reference_only_context_needs_retrieval(self):
        self.assertTrue(needs_book_retrieval([]))
        self.assertTrue(needs_book_retrieval([
            {"evidence_id": "reference", "excerpt": "see vert"},
            {"evidence_id": "empty", "excerpt": ""},
        ]))
        self.assertFalse(needs_book_retrieval([
            {"evidence_id": "definition", "excerpt": "vert (= turn)."},
        ]))

    def test_queries_are_bounded_to_focus_and_connected_words(self):
        retriever = Retriever()
        records, trace = self.search(retriever)
        self.assertEqual(records, [])
        self.assertEqual(trace["queries"], ["vert", "word0", "word1"])
        self.assertEqual(len(retriever.calls), 3)
        self.assertNotIn("unrelated", trace["queries"])

    def test_results_are_bounded_and_deduplicated_by_real_corpus_identity(self):
        retriever = Retriever([
            book_record(evidence_id="invented-id"),
            book_record(evidence_id="another-invented-id"),
            *[book_record(entry_id=f"entry-{i}") for i in range(10)],
        ])
        records, trace = self.search(retriever)
        self.assertEqual(len(records), 6)
        self.assertEqual(len(retriever.calls), 1)
        self.assertNotIn("evidence_id", records[0])
        self.assertEqual(trace["retrieved_records"], 6)
        self.assertEqual(len({(r["corpus_id"], r["entry_id"]) for r in records}), 6)
        self.assertEqual(retriever.records[0]["evidence_id"], "invented-id")

    def test_reference_rows_and_malformed_hits_do_not_hide_useful_passages(self):
        retriever = Retriever([
            17, {}, book_record(entry_id="ref", excerpt="see vert"),
            book_record(entry_id="empty", excerpt=""),
            book_record(entry_id="useful"),
        ])
        records, trace = self.search(retriever)
        self.assertEqual([r["entry_id"] for r in records], ["useful"])
        self.assertGreaterEqual(trace["omitted_records"], 4)

    def test_native_evidence_and_source_language_are_preserved(self):
        evidence = Evidence(
            entry_id="entry", headword="vert", section="Root dictionary",
            date_label="", pages=(12,), excerpt="vert (= turn).",
            corpus_id="root-dictionary:test", locator="page 12",
        )
        retriever = Retriever([evidence])
        records, _ = self.search(retriever, language="la")
        self.assertEqual(records[0]["pages"], [12])
        self.assertEqual(records[0]["corpus_id"], evidence.corpus_id)
        self.assertTrue(all(call[1]["source_language"] == "la" for call in retriever.calls))

    def test_lookup_errors_are_not_reported_as_no_match(self):
        def broken():
            raise OSError("local index unavailable")
        with self.assertRaisesRegex(OSError, "local index unavailable"):
            self.search(Retriever(on_call=broken))


class SemanticBookWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")
        self.term, self.root = _evidence_free_morphology_fixture(self.store, form="vert")
        self.seeder = SemanticEnrichmentSeeder(self.store, model="local-book-test")

    def worker(self, retriever):
        model = _SemanticModel("E1")
        return PreparationWorker(self.store, retriever, model), model

    def row_count(self, table):
        with closing(self.store._connect()) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_book_hits_refresh_inputs_before_model_then_complete_on_fresh_claim(self):
        plan = self.seeder.run_once()
        worker, model = self.worker(Retriever([book_record(evidence_id="not-authority")]))
        result = worker.run_once(("prepare-entity-meaning",))
        self.assertEqual(result.status, "failed")
        self.assertEqual(model.prompts, [])
        state = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertNotEqual(state["desired_fingerprint"], plan["desired_fingerprint"])
        self.assertEqual(state["status"], "pending")
        self.assertEqual(self.store.artifacts_for_job(
            plan["job_id"], stage="accepted-entity-meaning"
        ), [])
        retrieval = self.store.artifacts_for_job(
            plan["job_id"], stage="semantic-book-retrieval"
        )[0]["payload"]
        self.assertEqual(len(retrieval["evidence_ids"]), 1)

        fresh = self.seeder.run_once()
        self.assertEqual(fresh["entity_id"], self.root)
        fresh_worker, model = self.worker(_UnusedRetriever())
        result = fresh_worker.run_once(("prepare-entity-meaning",))
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(model.prompts), 1)
        accepted = self.store.artifacts_for_job(
            fresh["job_id"], stage="accepted-entity-meaning"
        )[0]["payload"]
        self.assertEqual(accepted["evidence_ids"], retrieval["evidence_ids"])
        self.assertEqual(accepted["grounding"], "definition-local-context")
        self.assertNotIn("not-authority", accepted["evidence_ids"])
        state = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertEqual(state["desired_fingerprint"], state["applied_fingerprint"])

    def test_no_match_still_accepts_cautious_useful_model_output(self):
        plan = self.seeder.run_once()
        retriever = Retriever()
        worker, model = self.worker(retriever)
        result = worker.run_once(("prepare-entity-meaning",))
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(model.prompts), 1)
        self.assertTrue(retriever.calls)
        accepted = self.store.artifacts_for_job(
            plan["job_id"], stage="accepted-entity-meaning"
        )[0]["payload"]
        self.assertEqual(accepted["evidence_ids"], [])
        self.assertLessEqual(accepted["confidence"], 0.35)
        self.assertEqual(accepted["grounding"], "model-context-only")
        self.assertEqual(accepted["meaning_inference"]["book_retrieval"]["retrieved_records"], 0)

    def test_existing_usable_context_does_not_search_again(self):
        evidence = self.store.add_evidence(
            "root-dictionary:test", "existing", excerpt="vert (= turn)."
        )
        self.store.link_evidence(self.root, evidence)
        self.seeder.run_once()
        worker, model = self.worker(_UnusedRetriever())
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "complete")
        self.assertEqual(len(model.prompts), 1)

    def test_production_retriever_uses_morphology_indexes_not_only_word_search(self):
        evidence = Evidence(
            entry_id="entry", headword="vert", section="Root dictionary",
            date_label="", pages=(12,), excerpt="vert (= turn).",
            corpus_id="root-dictionary:test", locator="page 12",
        )
        roots, affixes, origins = Index([evidence]), Index(), Index()
        retriever = WordEvidenceRetriever(origins, roots, affixes, mock.Mock())
        self.seeder.run_once()
        worker, model = self.worker(retriever)
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        self.assertIn(("search", "vert", 1), roots.calls)
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.row_count("evidence_records"), 1)

    def test_ineligibility_during_lookup_does_not_attach_sources_or_call_model(self):
        def invalidate():
            with closing(self.store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE morphemes SET kind='unknown' WHERE entity_id=?", (self.root,)
                )
        self.seeder.run_once()
        worker, model = self.worker(Retriever([book_record()], on_call=invalidate))
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        self.assertEqual(self.row_count("evidence_records"), 0)
        self.assertEqual(model.prompts, [])

    def test_changed_inputs_during_lookup_do_not_attach_obsolete_results(self):
        def change():
            evidence = self.store.add_evidence(
                "new-source", "changed", excerpt="vert (= turn); revised context."
            )
            self.store.link_evidence(self.root, evidence)
        self.seeder.run_once()
        worker, model = self.worker(Retriever([book_record()], on_call=change))
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        self.assertEqual(self.row_count("evidence_records"), 1)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT corpus_id FROM evidence_records"
            ).fetchone()[0], "new-source")
        self.assertEqual(model.prompts, [])

    def test_failed_retrieval_checkpoint_rolls_back_sources_and_links(self):
        self.seeder.run_once()
        worker, model = self.worker(Retriever([book_record()]))
        with mock.patch.object(
            self.store, "save_job_artifact", side_effect=RuntimeError("checkpoint unavailable")
        ):
            self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "retry")
        self.assertEqual(self.row_count("evidence_records"), 0)
        self.assertEqual(self.row_count("entity_evidence"), 0)
        self.assertEqual(model.prompts, [])

    def test_existing_identical_corpus_record_keeps_its_original_identity(self):
        record = book_record()
        existing = self.store.add_evidence(
            record["corpus_id"], record["entry_id"], source_hash="legacy-source-hash",
            excerpt=record["excerpt"], locator=record["locator"],
        )
        plan = self.seeder.run_once()
        worker, _ = self.worker(Retriever([record]))
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        artifact = self.store.artifacts_for_job(plan["job_id"], stage="semantic-book-retrieval")[0]
        self.assertEqual(artifact["payload"]["evidence_ids"], [existing])
        self.assertEqual(self.row_count("evidence_records"), 1)

    def test_changed_corpus_text_is_versioned_without_erasing_previous_record(self):
        record = book_record()
        self.store.add_evidence(
            record["corpus_id"], record["entry_id"], source_hash="older",
            excerpt="previous printed text", locator=record["locator"],
        )
        self.seeder.run_once()
        worker, _ = self.worker(Retriever([record]))
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        self.assertEqual(self.row_count("evidence_records"), 2)

    def test_relinking_same_context_is_idempotent(self):
        def attach():
            return self.store.link_retrieved_semantic_evidence(self.root, [book_record()])
        first = self.store.atomic_semantic_write(self.root, attach)
        fingerprint = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
        second = self.store.atomic_semantic_write(self.root, attach)
        self.assertEqual(first, second)
        self.assertEqual(self.row_count("evidence_records"), 1)
        self.assertEqual(self.row_count("entity_evidence"), 1)
        self.assertEqual(
            self.store.semantic_enrichment_context(self.root)["desired_fingerprint"], fingerprint
        )

    def test_lookup_failure_remains_retryable_without_invented_success(self):
        def broken():
            raise OSError("book index unavailable")
        self.seeder.run_once()
        worker, model = self.worker(Retriever(on_call=broken))
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "retry")
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.row_count("evidence_records"), 0)

    def test_policy_change_revisits_evidence_free_meaning_and_preserves_old_output(self):
        with mock.patch("lkt.semantic_contract.SEMANTIC_BOOK_RETRIEVAL_POLICY", "legacy"):
            self.seeder.run_once()
            worker, _ = self.worker(Retriever())
            self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "complete")
            previous = self.store.semantic_enrichment_context(self.root)
        self.store.refresh_lexical_semantic_enrichment(self.root)
        current = self.store.semantic_enrichment_context(self.root)
        self.assertNotEqual(previous["desired_fingerprint"], current["desired_fingerprint"])
        self.assertTrue(current["needs_semantic_review"])
        self.assertEqual(previous["accepted_meanings"], current["accepted_meanings"])

    def test_policy_change_does_not_invalidate_already_grounded_inputs(self):
        evidence = self.store.add_evidence("root-dictionary:test", "entry", excerpt="vert (= turn).")
        self.store.link_evidence(self.root, evidence)
        with mock.patch("lkt.semantic_contract.SEMANTIC_BOOK_RETRIEVAL_POLICY", "legacy"):
            previous = self.store.semantic_enrichment_context(self.root)["desired_fingerprint"]
        self.assertEqual(
            self.store.semantic_enrichment_context(self.root)["desired_fingerprint"], previous
        )

    def test_reference_only_context_gets_book_backfill_not_permanent_fallback(self):
        evidence = self.store.add_evidence("root-dictionary:test", "reference", excerpt="see vert")
        self.store.link_evidence(self.root, evidence)
        self.seeder.run_once()
        retriever = Retriever([book_record()])
        worker, model = self.worker(retriever)
        self.assertEqual(worker.run_once(("prepare-entity-meaning",)).status, "failed")
        self.assertTrue(retriever.calls)
        self.assertEqual(model.prompts, [])


if __name__ == "__main__":
    unittest.main()
