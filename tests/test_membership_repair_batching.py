from __future__ import annotations

from collections import Counter
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch

from tests import test_enrichment_membership as fixtures


class MembershipRepairBatchingTests(TestCase):
    setUp = fixtures.EnrichmentMembershipRepairTests.setUp
    legacy = fixtures.EnrichmentMembershipRepairTests.legacy
    assert_invariants = fixtures.EnrichmentMembershipRepairTests.assert_invariants

    def test_batch_refreshes_each_affected_projection_once_after_all_claims(self):
        first, _ = self.legacy()
        second, _ = self.legacy("retrial")
        with patch.object(
            self.store, "_refresh_projection_repair_state",
            wraps=self.store._refresh_projection_repair_state,
        ) as refresh, patch.object(
            self.store, "_mark_semantic_enrichments_for_entities",
            wraps=self.store._mark_semantic_enrichments_for_entities,
        ) as semantic:
            result = self.store.repair_model_enrichment_membership_scopes(
                term_entity_ids=[first, second],
            )
        self.assertEqual(result["converted_supports"], 8)
        subjects = Counter(call.args[1] for call in refresh.call_args_list)
        self.assertTrue(subjects)
        self.assertIn(first, subjects)
        self.assertIn(second, subjects)
        self.assertTrue(all(count == 1 for count in subjects.values()), subjects)
        self.assertEqual(semantic.call_count, 1)
        self.assert_invariants()

    def test_final_refresh_failure_rolls_back_claims_and_cursor(self):
        term, _ = self.legacy()
        with patch.object(
            self.store, "_refresh_projection_repair_state",
            side_effect=RuntimeError("flush failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "flush failed"):
                self.store.repair_model_enrichment_membership_scopes()
        self.assertIsNone(getattr(self.store._write_context, "semantic", None))
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM relation_assertions WHERE subject_entity_id=? "
                "AND relation='has-component' AND status='accepted'", (term,),
            ).fetchone()[0], 4)
            self.assertIsNone(connection.execute(
                "SELECT value FROM schema_meta WHERE key='enrichment-membership-repair-v2'",
            ).fetchone())
        self.assertEqual(
            self.store.repair_model_enrichment_membership_scopes()["converted_supports"], 4,
        )
        self.assert_invariants()

    def test_completed_repair_does_not_refresh_unchanged_projections(self):
        term, _ = self.legacy()
        self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        with patch.object(
            self.store, "_refresh_projection_repair_state",
            wraps=self.store._refresh_projection_repair_state,
        ) as refresh:
            result = self.store.repair_model_enrichment_membership_scopes(
                term_entity_ids=[term],
            )
        self.assertEqual(result["changed_assertions"], 0)
        refresh.assert_not_called()

    def test_ordinary_relation_writes_still_refresh_immediately(self):
        term, records = self.legacy()
        with patch.object(
            self.store, "_refresh_projection_repair_state",
            wraps=self.store._refresh_projection_repair_state,
        ) as refresh:
            self.store.accept_relation_assertion(
                term, term, records[0][0], "shares-component",
                basis="model", confidence=0.8,
                properties={"surface": "tri", "context_evidence_ids": [self.evidence]},
                owner_type="independent-test", owner_id="outside-batch",
            )
        self.assertTrue(refresh.called)
        self.assertIsNone(getattr(self.store._write_context, "semantic", None))
