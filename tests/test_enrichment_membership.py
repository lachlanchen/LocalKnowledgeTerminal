from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from lkt.atomic import PreparationWorker
from lkt.enrichment_membership import (
    ENRICHMENT_MEMBERSHIP_POLICY, classify_term_component_claims, enrichment_confidence,
)
from lkt.knowledge import KnowledgeStore
from lkt.relation_support import _assert_morphology_finalization_invariants


def claim(term, form, kind="root", **extra):
    return {"term": term, "component_form": form, "component_kind": kind,
            "meaning": "fixture meaning", "confidence": 0.9, **extra}


class EnrichmentMembershipClassificationTests(unittest.TestCase):
    def test_family_alternatives_do_not_become_simultaneous_parts(self):
        items = [claim("trial", form) for form in ("tri", "trial", "tre", "try")]
        before = deepcopy(items)
        result = classify_term_component_claims(items)
        self.assertEqual([r["analysis_scope"] for r in result],
                         ["family", "partial", "family", "family"])
        self.assertEqual(result[1]["source_span"], [0, 5])
        self.assertEqual(items, before)

    def test_disjoint_partial_components_are_kept_without_a_complete_split(self):
        result = classify_term_component_claims([
            claim("inspection", "in-", "prefix"), claim("inspection", "spect"),
            claim("inspection", "-ion", "suffix"),
        ])
        self.assertTrue(all(r["analysis_scope"] == "partial" for r in result))
        self.assertEqual([r["source_span"] for r in result], [[0, 2], [2, 7], [7, 10]])

    def test_explicit_surface_can_differ_from_historical_canonical_form(self):
        result = classify_term_component_claims([
            claim("agent", "act", surface="ag"),
            claim("radically", "-ical", "suffix"),
        ])
        self.assertEqual([r["source_span"] for r in result], [[0, 2], [3, 7]])

    def test_repeated_surface_needs_an_explicit_occurrence(self):
        result = classify_term_component_claims([
            claim("cancan", "can"), claim("cancan", "can", source_span=[3, 6]),
        ])
        self.assertEqual(result[0]["analysis_scope"], "family")
        self.assertEqual(result[1]["source_span"], [3, 6])

    def test_malformed_claims_and_non_english_surfaces_are_not_fabricated(self):
        result = classify_term_component_claims([None, "wrong", claim("", ""), claim("word", "unrelated")])
        self.assertTrue(all(r["analysis_scope"] == "family" for r in result))

    def test_duplicate_same_component_is_not_an_overlapping_alternative(self):
        result = classify_term_component_claims([claim("reader", "read"), claim("reader", "read")])
        self.assertTrue(all(r["analysis_scope"] == "partial" for r in result))

    def test_confidence_matches_the_other_local_model_lane(self):
        self.assertEqual(enrichment_confidence(0.99), 0.8)
        self.assertEqual(enrichment_confidence(0.65), 0.65)
        self.assertEqual(enrichment_confidence(float("nan")), 0.55)


class EnrichmentMembershipRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name) / "knowledge.sqlite3")
        self.evidence = self.store.add_evidence(
            "fixture-book", "entry", excerpt="A root family and related words.",
            payload={"corpus_id": "fixture-book", "entry_id": "entry",
                     "headword": "trial", "kind": "morphology-root"},
        )
        self.job_id = self.store.enqueue_job("enrich-morphology-v3", "fixture:enrichment")

    def legacy(self, term="trial", forms=("tri", "trial", "tre", "try")):
        term_id = self.store.upsert_term("en", term, status="accepted")
        records = []
        for form in forms:
            component = self.store.upsert_morpheme("en", form, "root", "fixture meaning", status="accepted")
            self.store.link_morpheme_once(term_id, component, form, basis="model", confidence=0.9)
            assertion = self.store.accept_relation_assertion(
                term_id, term_id, component, "has-component", basis="model", confidence=0.9,
                properties={"surface": form, "enrichment_job_id": self.job_id,
                            "context_evidence_ids": [self.evidence]},
                owner_type="atomic-split", owner_id=term_id,
            )
            records.append((component, assertion))
        return term_id, records

    def assert_invariants(self):
        with closing(self.store._connect()) as connection:
            _assert_morphology_finalization_invariants(connection)

    def test_legacy_conversion_preserves_family_knowledge_and_removes_positional_maps(self):
        term, records = self.legacy()
        result = self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        self.assertEqual(result["converted_supports"], 4)
        components = self.store.accepted_morphology_components(term, include_partial=True)
        self.assertEqual([p["canonical_form"].casefold() for p in components], ["trial"])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM term_morphemes WHERE term_id=?", (term,)).fetchone()[0], 0)
            rows = connection.execute(
                "SELECT relation,confidence,properties_json FROM relation_assertions "
                "WHERE subject_entity_id=? AND status='accepted'", (term,),
            ).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["relation"] == "shares-component" and row["confidence"] == 0.8 for row in rows))
            self.assertTrue(all(json.loads(row["properties_json"])["context_evidence_ids"] == [self.evidence] for row in rows))
            self.assertEqual(connection.execute("SELECT count(*) FROM entities WHERE entity_type='morpheme' AND status='accepted'").fetchone()[0], 4)
        self.assert_invariants()

    def test_independent_book_support_is_not_retired(self):
        term, records = self.legacy(forms=("trial",))
        component, assertion = records[0]
        self.store.accept_relation_assertion(
            term, term, component, "has-component", basis="book", confidence=0.95,
            evidence_ids=[self.evidence], properties={"surface": "trial"},
            owner_type="book-fixture", owner_id="book",
        )
        self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT status,basis FROM relation_assertions WHERE assertion_id=?", (assertion,)).fetchone()
            self.assertEqual(tuple(row), ("accepted", "book"))
            self.assertEqual(connection.execute("SELECT status FROM relation_assertion_supports WHERE assertion_id=? AND owner_type='book-fixture'", (assertion,)).fetchone()[0], "accepted")
            self.assertEqual(connection.execute("SELECT count(*) FROM term_morphemes WHERE term_id=?", (term,)).fetchone()[0], 1)
        self.assert_invariants()

    def test_unrelated_atomic_support_survives_even_on_the_same_assertion(self):
        term, records = self.legacy(forms=("trial",))
        component, assertion = records[0]
        self.store.accept_relation_assertion(
            term, term, component, "has-component", basis="model", confidence=0.95,
            properties={"surface": "trial"}, owner_type="independent-analysis", owner_id="other",
        )
        self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT status FROM relation_assertions WHERE assertion_id=?", (assertion,)).fetchone()[0], "accepted")
        self.assert_invariants()

    def test_explicit_repair_is_idempotent(self):
        term, _ = self.legacy()
        self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        with closing(self.store._connect()) as connection:
            before = connection.execute("SELECT value FROM schema_meta WHERE key='relation_graph_revision'").fetchone()[0]
        second = self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        self.assertEqual(second["changed_assertions"], 0)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT value FROM schema_meta WHERE key='relation_graph_revision'").fetchone()[0], before)

    def test_bounded_scan_resumes_across_store_restart(self):
        self.legacy("trial")
        self.legacy("reader", ("read", "reader", "reed"))
        first = self.store.repair_model_enrichment_membership_scopes(limit=1)
        self.assertEqual(first["scanned"], 1)
        self.assertFalse(first["complete"])
        restarted = KnowledgeStore(self.store.database, serving=True)
        second = restarted.repair_model_enrichment_membership_scopes(limit=1)
        final = restarted.repair_model_enrichment_membership_scopes(limit=1)
        self.assertEqual(second["scanned"], 1)
        self.assertTrue(final["complete"])
        self.assertEqual(final["scanned_total"], 2)

    def test_failure_rolls_back_conversion_and_retirement_together(self):
        term, records = self.legacy()
        with patch.object(self.store, "_mark_projection_repairs_for_entities", side_effect=RuntimeError("fixture rollback")):
            with self.assertRaisesRegex(RuntimeError, "fixture rollback"):
                self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM relation_assertions WHERE subject_entity_id=? AND status='accepted' AND relation='has-component'", (term,)).fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM relation_assertion_supports WHERE status='retired'").fetchone()[0], 0)
        self.assertIsNone(getattr(self.store._write_context, "semantic", None))
        self.assert_invariants()

    def test_non_enrichment_job_tag_does_not_authorize_conversion(self):
        term, records = self.legacy()
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE preparation_jobs SET job_type='split-morphemes' WHERE job_id=?", (self.job_id,))
            connection.commit()
        result = self.store.repair_model_enrichment_membership_scopes(term_entity_ids=[term])
        self.assertEqual(result["converted_supports"], 0)

    def test_new_producer_keeps_partial_and_family_claims_with_explicit_ownership(self):
        model = Mock()
        raw = {"term_components": [claim("trial", form) for form in ("tri", "trial", "tre", "try")],
               "relations": [{"source_form": "trial", "source_kind": "root",
                              "target_form": "try", "target_kind": "root",
                              "relation": "allomorph-of", "confidence": 0.9}]}
        model.complete_json.return_value = {"value": raw, "metrics": {"requests": 1}}
        worker = PreparationWorker(self.store, object(), model)
        context = {"cluster_id": "fixture", "input_fingerprint": "input", "evidence": [],
                   "payload": {"focus": {"term": "trial"}, "evidence_ids": [self.evidence]}}
        with patch.object(self.store, "morphology_enrichment_context", return_value=context), patch.object(self.store, "complete_morphology_v3_enrichment"):
            worker._enrich_morphology_v3({"job_id": self.job_id})
        artifact = self.store.artifacts_for_job(self.job_id, stage="accepted-morphology-v3-enrichment")[-1]["payload"]
        self.assertEqual(artifact["membership_policy"], ENRICHMENT_MEMBERSHIP_POLICY)
        self.assertEqual(len(artifact["claims"]), 5)
        model.complete_json.assert_called_once()
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM term_morphemes").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM relation_assertions WHERE relation='has-component'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM relation_assertions WHERE relation='shares-component' AND status='accepted'").fetchone()[0], 4)
            owners = connection.execute("SELECT DISTINCT owner_type,owner_id FROM relation_assertion_supports WHERE status='accepted'").fetchall()
            self.assertEqual({tuple(row) for row in owners}, {("morphology-enrichment", self.job_id)})
        self.assert_invariants()
