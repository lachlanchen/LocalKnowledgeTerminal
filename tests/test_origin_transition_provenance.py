"""Book attestation must not turn model-inferred ancestry into proven facts."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lkt.atomic import _origin_transition_provenance
from lkt.knowledge import KnowledgeStore


class OriginTransitionProvenanceTests(unittest.TestCase):
    def test_cooccurrence_is_context_not_directed_relationship_evidence(self):
        result = _origin_transition_provenance(
            {"basis": "book", "confidence": 0.95, "evidence_ids": ["e1"]},
            "later", [{"evidence_id": "e1", "excerpt": "Earlier and later are related."}],
        )
        self.assertEqual(result["edge_basis"], "model")
        self.assertEqual(result["edge_confidence"], 0.7)
        self.assertEqual(result["edge_evidence_ids"], [])
        self.assertEqual(result["edge_context_evidence_ids"], ["e1"])

    def test_explicit_book_extraction_can_keep_relationship_evidence(self):
        result = _origin_transition_provenance(
            {"basis": "book", "confidence": 0.95, "evidence_ids": ["e1"]},
            "later", [{"evidence_id": "e1", "excerpt": "Earlier developed into later."}],
            extracted_from_book=True,
        )
        self.assertEqual(result["edge_basis"], "book")
        self.assertEqual(result["edge_evidence_ids"], ["e1"])
        self.assertEqual(result["edge_context_evidence_ids"], [])

    def make_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = KnowledgeStore(Path(directory.name) / "knowledge.sqlite3")
        with closing(store._connect()) as connection:
            for name, kind in (("word", "term"), ("earlier", "historical-form"),
                               ("later", "historical-form")):
                connection.execute(
                    """INSERT INTO entities
                       (entity_id,entity_type,canonical_key,label,status,payload,created_at,updated_at)
                       VALUES (?,?,?,?,'accepted','{}','2026-01-01','2026-01-01')""",
                    (name, kind, name, name),
                )
            connection.commit()
        evidence_id = store.add_evidence("test", "entry", excerpt="Earlier and later are related.")
        return store, evidence_id

    def seed_legacy(self, store, evidence_id, basis="book"):
        return store.accept_relation_assertion(
            "word", "earlier", "later", "developed-into", basis=basis,
            confidence=0.95, evidence_ids=[evidence_id],
            properties={"source_term_id": "word", "component_id": "base"},
        )

    def test_revisit_corrects_legacy_book_promotion(self):
        store, evidence_id = self.make_store()
        self.seed_legacy(store, evidence_id)
        store.accept_origin_transition(
            "word", "earlier", "later", component_id="base", basis="model",
            confidence=0.7, context_evidence_ids=[evidence_id],
        )
        with closing(store._connect()) as connection:
            rows = connection.execute(
                "SELECT basis,confidence FROM relation_assertions WHERE status='accepted'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["basis"], "model")
        self.assertLessEqual(rows[0]["confidence"], 0.7)

    def test_revisit_preserves_reviewed_relationship(self):
        store, evidence_id = self.make_store()
        self.seed_legacy(store, evidence_id, basis="reviewed")
        store.accept_origin_transition(
            "word", "earlier", "later", component_id="base", basis="model",
            confidence=0.7, context_evidence_ids=[evidence_id],
        )
        with closing(store._connect()) as connection:
            rows = connection.execute(
                "SELECT basis FROM relation_assertions WHERE status='accepted'"
            ).fetchall()
        self.assertEqual([row["basis"] for row in rows], ["reviewed"])

    def test_failed_replacement_rolls_back_legacy_retirement(self):
        store, evidence_id = self.make_store()
        self.seed_legacy(store, evidence_id)
        with patch.object(store, "accept_relation_assertion", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                store.accept_origin_transition(
                    "word", "earlier", "later", component_id="base", basis="model",
                    confidence=0.7, context_evidence_ids=[evidence_id],
                )
        with closing(store._connect()) as connection:
            rows = connection.execute(
                "SELECT basis FROM relation_assertions WHERE status='accepted'"
            ).fetchall()
        self.assertEqual([row["basis"] for row in rows], ["book"])


if __name__ == "__main__":
    unittest.main()
