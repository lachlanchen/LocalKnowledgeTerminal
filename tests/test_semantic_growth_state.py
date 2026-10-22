from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore


class SemanticGrowthStateTests(unittest.TestCase):
    def store(self, temp: str) -> KnowledgeStore:
        return KnowledgeStore(Path(temp) / "knowledge.sqlite3")

    def component_word(
        self, store: KnowledgeStore, word: str, *, meaning: str = ""
    ) -> tuple[str, str]:
        term_id = store.upsert_term("en", word)
        component_id = store.upsert_morpheme(
            "en", f"{word}-root", "root", meaning
        )
        store.accept_relation_assertion(
            term_id,
            term_id,
            component_id,
            "has-component",
            basis="book",
            confidence=0.9,
            properties={"ordinal": 0, "surface": word},
        )
        return term_id, component_id

    def checkpoints(self, store: KnowledgeStore, term_id: str, marker: str = "a") -> str:
        job_id = store.enqueue_job(
            "retrieve-evidence",
            f"term:{term_id}",
            subject_entity_id=term_id,
            language="en",
            prompt_version=f"semantic-checkpoints-{marker}",
        )
        for stage in (
            "accepted-meaning",
            "accepted-morpheme-split",
            "accepted-origin-branches",
        ):
            store.save_job_artifact(
                job_id,
                stage,
                {"stage": stage, "marker": marker},
                validation_state="accepted",
            )
        return job_id

    def test_blank_upsert_preserves_nonblank_morpheme_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            component_id = store.upsert_morpheme(
                "en", "spect", "root", "to look"
            )
            self.assertEqual(
                store.upsert_morpheme("en", "spect", "root"), component_id
            )
            with closing(store._connect()) as connection:
                row = connection.execute(
                    """SELECT morpheme.meaning, entity.payload
                       FROM morphemes AS morpheme
                       JOIN entities AS entity
                         ON entity.entity_id = morpheme.entity_id
                       WHERE morpheme.entity_id = ?""",
                    (component_id,),
                ).fetchone()
            self.assertEqual(row["meaning"], "to look")
            self.assertEqual(json.loads(row["payload"])["meaning"], "to look")

    def test_canonical_component_meaning_hydrates_and_redirties_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, component_id = self.component_word(store, "inspect")
            self.checkpoints(store, term_id)
            claim = store.claim_lexical_projection_repair()
            self.assertIsNotNone(claim)
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            applied = store.lexical_projection_repair_state(term_id)
            evidence_id = store.add_evidence(
                "roots-book",
                "spect-entry",
                source_hash="book-v1",
                locator="page 12",
                excerpt="spect means to look",
            )
            store.add_entity_meaning(
                component_id,
                "to look or see",
                basis="book",
                confidence=0.98,
                evidence_ids=(evidence_id,),
            )
            component = store.accepted_morphology_components(term_id)[0]
            self.assertEqual(component["meaning"], "to look or see")
            with closing(store._connect()) as connection:
                cached = connection.execute(
                    "SELECT meaning FROM morphemes WHERE entity_id = ?",
                    (component_id,),
                ).fetchone()[0]
            self.assertEqual(cached, "to look or see")
            dirty = store.lexical_projection_repair_state(term_id)
            self.assertEqual(dirty["status"], "pending")
            self.assertNotEqual(
                dirty["desired_fingerprint"], applied["applied_fingerprint"]
            )
            semantic = store.lexical_semantic_enrichment_state(component_id)
            self.assertEqual(semantic["status"], "applied")

    def test_meaning_bearing_derivative_is_ranked_before_empty_neighbor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            root_id = store.upsert_morpheme("en", "dict", "root", "say")
            empty_id = store.upsert_term("en", "dictum")
            explained_id = store.upsert_term("en", "dictionary")
            store.accept_relation_assertion(
                root_id,
                empty_id,
                root_id,
                "shares-component",
                basis="book",
                confidence=0.99,
            )
            store.accept_relation_assertion(
                root_id,
                explained_id,
                root_id,
                "shares-component",
                basis="model",
                confidence=0.4,
            )
            store.add_entity_meaning(
                explained_id, "a reference work defining words", basis="reviewed"
            )
            graph = store.lexical_subgraph(
                root_id, "root", {"nodes": 2, "edges": 2, "depth": 2}
            )
            node_ids = {str(node["id"]) for node in graph["nodes"]}
            self.assertIn(explained_id, node_ids)
            self.assertNotIn(empty_id, node_ids)
            explained = next(
                node for node in graph["nodes"] if node["id"] == explained_id
            )
            self.assertEqual(
                explained["payload"]["meaning"],
                "a reference work defining words",
            )

    def test_checkpoint_change_redirties_local_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.component_word(store, "checkpoint", meaning="mark")
            self.checkpoints(store, term_id, "first")
            claim = store.claim_lexical_projection_repair()
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            applied = store.lexical_projection_repair_state(term_id)
            self.checkpoints(store, term_id, "second")
            dirty = store.lexical_projection_repair_state(term_id)
            self.assertEqual(dirty["status"], "pending")
            self.assertNotEqual(
                dirty["desired_fingerprint"], applied["applied_fingerprint"]
            )

    def test_derivative_meaning_redirties_shared_subject_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, root_id = self.component_word(store, "derive", meaning="draw")
            derivative_id = store.upsert_term("en", "derivation")
            store.accept_relation_assertion(
                term_id,
                derivative_id,
                root_id,
                "shares-component",
                confidence=0.6,
            )
            self.checkpoints(store, term_id)
            claim = store.claim_lexical_projection_repair()
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            unrelated_id, _ = self.component_word(
                store, "unrelated", meaning="not connected"
            )
            unrelated_before = store.lexical_projection_repair_state(unrelated_id)
            store.add_entity_meaning(
                derivative_id, "the act of deriving something", basis="reviewed"
            )
            self.assertEqual(
                store.lexical_projection_repair_state(term_id)["status"], "pending"
            )
            unrelated_after = store.lexical_projection_repair_state(unrelated_id)
            self.assertEqual(
                unrelated_after["desired_fingerprint"],
                unrelated_before["desired_fingerprint"],
            )

    def test_semantic_claim_adopts_orphan_and_applies_after_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            component_id = store.upsert_morpheme("en", "orphan", "root")
            claim = store.claim_lexical_semantic_enrichment()
            self.assertEqual(claim["entity_id"], component_id)
            self.assertEqual(claim["job_type"], "prepare-entity-meaning")
            self.assertIsNone(
                self.store(temp).claim_lexical_semantic_enrichment()
            )
            job_id = store.enqueue_job(
                claim["job_type"],
                f"semantic:{component_id}:en",
                subject_entity_id=component_id,
                language="en",
                source_fingerprint=claim["desired_fingerprint"],
                prompt_version="semantic-v1",
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                       WHERE entity_id = ? AND language = 'en'""",
                    (component_id,),
                )
                connection.commit()
            reopened = self.store(temp)
            outcomes = reopened.reconcile_lexical_semantic_enrichments()
            self.assertIn(
                {"entity_id": component_id, "outcome": "orphan-adopted"},
                outcomes,
            )
            state = reopened.lexical_semantic_enrichment_state(component_id)
            self.assertEqual(state["status"], "planned")
            self.assertEqual(state["enrichment_job_id"], job_id)
            reopened.add_entity_meaning(component_id, "a recovered meaning")
            self.assertTrue(
                reopened.complete_lexical_semantic_enrichment(
                    component_id,
                    claim["desired_fingerprint"],
                    enrichment_job_id=job_id,
                )
            )
            self.assertEqual(
                reopened.lexical_semantic_enrichment_state(component_id)["status"],
                "applied",
            )

    def test_same_fingerprint_cools_but_new_evidence_is_immediately_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            component_id = store.upsert_morpheme("en", "cool", "root")
            claim = store.claim_lexical_semantic_enrichment()
            self.assertTrue(
                store.fail_lexical_semantic_enrichment(
                    component_id,
                    claim["desired_fingerprint"],
                    "model unavailable",
                    lease_token=claim["lease_token"],
                )
            )
            self.assertIsNone(store.claim_lexical_semantic_enrichment())
            evidence_id = store.add_evidence(
                "root-book",
                "cool-entry",
                source_hash="source-v2",
                excerpt="cool is a retained root entry",
            )
            store.link_evidence(component_id, evidence_id, claim="root entry")
            refreshed = store.lexical_semantic_enrichment_state(component_id)
            self.assertEqual(refreshed["status"], "pending")
            self.assertEqual(refreshed["next_eligible_at"], "")
            self.assertNotEqual(
                refreshed["desired_fingerprint"], claim["desired_fingerprint"]
            )
            retry = store.claim_lexical_semantic_enrichment()
            self.assertEqual(retry["entity_id"], component_id)

    def test_semantic_bootstrap_is_bounded_and_complete_beyond_64(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            for index in range(70):
                store.upsert_morpheme("en", f"bootstrap-{index:03d}", "root")
            with closing(store._connect()) as connection:
                graph_revision = int(
                    connection.execute(
                        """SELECT value FROM schema_meta
                           WHERE key = 'relation_graph_revision'"""
                    ).fetchone()[0]
                )
                connection.execute("DELETE FROM lexical_semantic_enrichments")
                connection.execute(
                    """UPDATE lexical_semantic_bootstrap_state
                       SET cursor_entity_id = '', scanned_count = 0,
                           complete = 0, observed_graph_revision = ?, updated_at = ''
                       WHERE scheduler_key = 'semantic-v12'""",
                    (graph_revision,),
                )
                connection.commit()
            pages = [
                store.bootstrap_lexical_semantic_enrichments(32)
                for _ in range(3)
            ]
            self.assertEqual([page["scanned"] for page in pages], [32, 32, 6])
            self.assertTrue(pages[-1]["complete"])
            self.assertEqual(
                store.bootstrap_lexical_semantic_enrichments(32)["scanned"], 0
            )
            with closing(store._connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM lexical_semantic_enrichments"
                ).fetchone()[0]
            self.assertEqual(count, 70)


if __name__ == "__main__":
    unittest.main()
