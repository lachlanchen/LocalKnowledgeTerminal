from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from lkt.knowledge import KnowledgeStore
from tests.test_semantic_lane import _morphology_fixture


class SemanticInvalidationBatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")
        with mock.patch.object(self.store, "_mark_projection_repairs_for_entities", return_value=0):
            self.term, self.root, self.evidence = _morphology_fixture(self.store)
            for index in range(6):
                term = self.store.upsert_term("en", f"fixture-{index:03}", status="accepted")
                self.store.accept_relation_assertion(
                    term, term, self.root, "has-component", basis="model", confidence=0.6,
                )
        with closing(self.store._connect()) as connection, connection:
            self.store._mark_projection_repairs_for_entities(connection, (self.root,))

    @staticmethod
    def scheduling_state(store):
        with closing(store._connect()) as connection:
            return {
                "projections": [tuple(row) for row in connection.execute(
                    "SELECT term_entity_id,desired_fingerprint,desired_modes_json,status "
                    "FROM lexical_projection_repairs ORDER BY term_entity_id"
                )],
                "semantics": [tuple(row) for row in connection.execute(
                    "SELECT entity_id,language,desired_fingerprint,status "
                    "FROM lexical_semantic_enrichments ORDER BY entity_id,language"
                )],
            }

    def mutations(self, store=None):
        store = store or self.store
        for index in range(6):
            store.link_evidence(
                self.root, self.evidence, claim=f"batch annotation {index}", confidence=0.8,
            )
        return store.add_entity_meaning(
            self.root, "a new explanation for the shared root", basis="model",
        )

    def assert_no_mutation_survived(self):
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM entity_evidence WHERE claim LIKE 'batch annotation %'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM meanings WHERE definition='a new explanation for the shared root'"
            ).fetchone()[0], 0)
        self.assertIsNone(getattr(self.store._write_context, "semantic", None))

    def test_each_dependent_snapshot_is_built_once_after_all_mutations(self):
        with ExitStack() as stack:
            counters = {
                name: stack.enter_context(mock.patch.object(
                    self.store, name, wraps=getattr(self.store, name),
                ))
                for name in ("_projection_repair_snapshot", "_semantic_enrichment_snapshot")
            }

            def persist():
                result = self.mutations()
                self.assertTrue(self.store._write_context.semantic.deferred_projection_entities)
                self.assertTrue(all(counter.call_count == 0 for counter in counters.values()))
                return result

            meaning = self.store.atomic_semantic_write(self.root, persist)
            for counter in counters.values():
                counts = Counter(call.args[1] for call in counter.call_args_list)
                self.assertGreater(len(counts), 1)
                self.assertTrue(all(count == 1 for count in counts.values()), counts)
        self.assertIn(meaning, {
            item["meaning_id"]
            for item in self.store.semantic_enrichment_context(self.root)["accepted_meanings"]
        })

    def test_batched_final_scheduling_state_matches_eager_writes(self):
        path = Path(self.temp.name) / "eager.sqlite3"
        with closing(self.store._connect()) as source, closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
        eager = KnowledgeStore(path, serving=True)

        def eager_persist():
            eager._write_context.semantic.deferred_projection_entities = None
            return self.mutations(eager)

        eager.atomic_semantic_write(self.root, eager_persist)
        self.store.atomic_semantic_write(self.root, self.mutations)
        self.assertEqual(self.scheduling_state(self.store), self.scheduling_state(eager))

    def test_callback_failure_rolls_back_without_refreshing_snapshots(self):
        before = self.scheduling_state(self.store)

        def persist():
            self.mutations()
            raise RuntimeError("callback failed")

        with mock.patch.object(self.store, "_projection_repair_snapshot") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "callback failed"):
                self.store.atomic_semantic_write(self.root, persist)
            snapshot.assert_not_called()
        self.assert_no_mutation_survived()
        self.assertEqual(self.scheduling_state(self.store), before)

    def test_failed_flush_rolls_back_graph_and_partial_scheduling_updates(self):
        before = self.scheduling_state(self.store)
        refresh = self.store._refresh_projection_repair_state
        calls = 0

        def fail_after_one(connection, term_entity_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("flush failed")
            return refresh(connection, term_entity_id, **kwargs)

        with mock.patch.object(
            self.store, "_refresh_projection_repair_state", side_effect=fail_after_one,
        ):
            with self.assertRaisesRegex(RuntimeError, "flush failed"):
                self.store.atomic_semantic_write(self.root, self.mutations)
        self.assertEqual(calls, 2)
        self.assert_no_mutation_survived()
        self.assertEqual(self.scheduling_state(self.store), before)
        self.assertTrue(self.store.atomic_semantic_write(self.root, self.mutations))

    def test_borrowed_rollback_is_checked_before_flushing(self):
        def persist():
            self.mutations()
            with closing(self.store._connect()) as connection:
                connection.rollback()

        with mock.patch.object(self.store, "_projection_repair_snapshot") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "was rolled back"):
                self.store.atomic_semantic_write(self.root, persist)
            snapshot.assert_not_called()
        self.assert_no_mutation_survived()

    def test_subject_withdrawal_is_checked_before_flushing(self):
        def persist():
            self.mutations()
            with closing(self.store._connect()) as connection:
                connection.execute("UPDATE entities SET status='archived' WHERE entity_id=?", (self.root,))

        with mock.patch.object(self.store, "_projection_repair_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "not writable"):
                self.store.atomic_semantic_write(self.root, persist)
            snapshot.assert_not_called()
        self.assert_no_mutation_survived()
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM entities WHERE entity_id=?", (self.root,)
            ).fetchone()[0], "accepted")

    def test_nested_transaction_remains_rejected_without_leaking_batch_state(self):
        def persist():
            self.mutations()
            return self.store.atomic_semantic_write(self.term, lambda: None)

        with self.assertRaisesRegex(RuntimeError, "nested semantic"):
            self.store.atomic_semantic_write(self.root, persist)
        self.assert_no_mutation_survived()
        self.assertEqual(self.store.atomic_semantic_write(self.root, lambda: "healthy"), "healthy")

    def test_later_transaction_does_not_reuse_previous_dirty_entities(self):
        self.store.atomic_semantic_write(self.root, self.mutations)
        other = self.store.upsert_morpheme("en", "unrelated", "root", status="accepted")
        with mock.patch.object(
            self.store, "_semantic_enrichment_snapshot",
            wraps=self.store._semantic_enrichment_snapshot,
        ) as snapshot:
            self.store.atomic_semantic_write(
                other, lambda: self.store.add_entity_meaning(other, "another explanation", basis="model"),
            )
        self.assertNotIn(self.root, {call.args[1] for call in snapshot.call_args_list})
        self.assertIsNone(getattr(self.store._write_context, "semantic", None))

    def test_authoritative_context_reads_see_uncommitted_graph_changes(self):
        evidence = self.store.add_evidence(
            "root-dictionary:test", "new-source", source_hash="new-source-hash",
            excerpt="vert (= turn); an additional explanation",
        )

        def persist():
            self.store.link_evidence(self.root, evidence, claim="new source in transaction")
            context = self.store.semantic_enrichment_context(self.root)
            self.assertIn(evidence, {
                item["evidence_id"] for item in context["context"]["evidence"]
            })
            return context["desired_fingerprint"]

        fingerprint = self.store.atomic_semantic_write(self.root, persist)
        state = self.store.lexical_semantic_enrichment_state(self.root)
        self.assertEqual(state["desired_fingerprint"], fingerprint)

    def test_no_changes_does_not_refresh_dependencies(self):
        with mock.patch.object(self.store, "_mark_projection_repairs_for_entities") as mark:
            self.assertEqual(self.store.atomic_semantic_write(self.root, lambda: "unchanged"), "unchanged")
            mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
