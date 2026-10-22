from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore


class KnowledgeProjectionContractTests(unittest.TestCase):
    @staticmethod
    def store(temp: str) -> KnowledgeStore:
        return KnowledgeStore(Path(temp) / "knowledge.sqlite3")

    def test_explicit_branch_focus_never_changes_to_same_form_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)

            subject = store.upsert_term("en", "inspection")
            requested = store.upsert_morpheme(
                "en", "spect", "root", quality_score=0.2
            )
            richer_alias = store.upsert_morpheme(
                "la", "SPECT", "root", quality_score=1.0
            )
            store.link_morpheme(subject, requested, 0, "spect")
            store.link_morpheme(subject, richer_alias, 1, "spect")

            view = store.lexical_subgraph(
                subject,
                "root",
                {
                    "nodes": 24,
                    "edges": 32,
                    "depth": 4,
                    "branch_focus_id": requested,
                },
            )
            self.assertEqual(view["branch_focus_id"], requested)

            sparse_subject = store.upsert_term("en", "conduction")
            sparse_requested = store.upsert_morpheme("en", "duc", "root")
            connected_alias = store.upsert_morpheme("la", "DUC", "root")
            store.link_morpheme(sparse_subject, connected_alias, 0, "duc")
            history = store.add_historical_form("la", "ducere")
            store.add_edge(
                connected_alias,
                history,
                "developed-into",
                basis="book",
                properties={"component_id": connected_alias},
            )

            sparse_view = store.lexical_subgraph(
                sparse_subject,
                "root",
                {
                    "nodes": 24,
                    "edges": 32,
                    "depth": 4,
                    "branch_focus_id": sparse_requested,
                },
            )
            self.assertEqual(sparse_view["branch_focus_id"], sparse_requested)

    def test_cached_morpheme_gloss_is_not_canonical_semantic_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            root = store.upsert_morpheme("en", "spect", "root", "look")

            with closing(store._connect()) as connection:
                snapshot = store._semantic_enrichment_snapshot(
                    connection, root, "en"
                )

            self.assertIsNotNone(snapshot)
            self.assertFalse(snapshot["has_accepted_meaning"])
            self.assertEqual(snapshot["accepted_meanings"], [])

    def test_canonical_meaning_ranking_uses_authority_quality_and_recency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term = store.upsert_term("en", "inspection")
            evidence = store.add_evidence(
                "word-origins",
                "inspection",
                source_hash="v1",
                locator="entry",
                excerpt="Inspection is the act of examining closely.",
            )
            store.add_entity_meaning(
                term,
                "an obsolete weak gloss",
                basis="book",
                quality_score=0.4,
                confidence=0.95,
                evidence_ids=(evidence,),
            )
            store.add_entity_meaning(
                term,
                "the act of examining closely",
                basis="book",
                quality_score=0.9,
                confidence=0.95,
                evidence_ids=(evidence,),
            )
            store.add_entity_meaning(
                term,
                "a corrected act of careful examination",
                basis="book",
                quality_score=0.9,
                confidence=0.95,
                evidence_ids=(evidence,),
            )
            store.add_entity_meaning(
                term,
                "a speculative model gloss",
                basis="model",
                quality_score=1.0,
                confidence=1.0,
            )

            with closing(store._connect()) as connection:
                records = store._accepted_meaning_records(connection, (term,))[term]

            self.assertEqual(
                [record["definition"] for record in records],
                [
                    "a corrected act of careful examination",
                    "the act of examining closely",
                    "an obsolete weak gloss",
                    "a speculative model gloss",
                ],
            )

    def test_composite_period_language_placeholder_is_not_a_visible_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            subject = store.upsert_term("en", "immanent")
            placeholder = store.add_historical_form(
                "la", "Classical Latin", period_label="Classical"
            )
            store.add_edge(
                subject, placeholder, "derived-from", basis="book"
            )

            view = store.lexical_subgraph(
                subject, "word", {"nodes": 16, "edges": 24, "depth": 4}
            )

            self.assertNotIn(placeholder, {node["id"] for node in view["nodes"]})

    def test_edge_bundles_preserve_opposing_directions_and_exact_triples(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            subject = store.upsert_term("en", "inspection")
            root = store.upsert_morpheme("la", "spect", "root", "look")
            store.link_morpheme(subject, root, 0, "spect", basis="book")
            store.add_edge(subject, root, "has-history", basis="reviewed")
            first = store.add_edge(root, subject, "developed-into", basis="book")
            replay = store.add_edge(root, subject, "developed-into", basis="book")
            self.assertEqual(replay, first)

            view = store.lexical_subgraph(
                subject, "word", {"nodes": 16, "edges": 24, "depth": 4}
            )
            forward = [
                edge
                for edge in view["edges"]
                if edge["source"] == subject and edge["target"] == root
            ]
            reverse = [
                edge
                for edge in view["edges"]
                if edge["source"] == root and edge["target"] == subject
            ]

            self.assertEqual(len(forward), 1)
            self.assertEqual(forward[0]["relation"], "has-component")
            self.assertIn(
                "has-history",
                {
                    str(relation.get("relation", ""))
                    if isinstance(relation, dict)
                    else str(relation)
                    for relation in forward[0].get("relations", [])
                },
            )
            self.assertEqual(len(reverse), 1)
            self.assertEqual(reverse[0]["relation"], "developed-into")
            visible_triples = {
                (edge["source"], edge["target"], edge["relation"])
                for edge in view["edges"]
            }
            self.assertEqual(len(visible_triples), len(view["edges"]))

    def test_knowledge_projection_collapses_events_and_parallel_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            subject = store.upsert_term("en", "rusticate")
            root = store.upsert_morpheme("la", "rustic", "root", "country")
            store.link_morpheme(subject, root, 0, "rustic", basis="book")
            historical = store.add_historical_form(
                "la", "rusticare", period_label="Late Latin"
            )
            event = store.add_history_event(
                subject,
                "form-transition",
                "The Latin form developed into the English word.",
                language="la",
                period_label="Late Latin",
                basis="book",
            )
            store.add_edge(subject, event, "has-origin-event", basis="book")
            store.add_edge(historical, event, "has-history", basis="book")
            store.add_edge(event, root, "developed-toward", basis="book")
            store.add_edge(event, root, "explains-component", basis="book")
            store.add_edge(subject, root, "has-history", basis="reviewed")

            view = store.lexical_subgraph(
                subject, "knowledge", {"nodes": 24, "edges": 32, "depth": 4}
            )

            self.assertNotIn(
                "history-event", {str(node["type"]) for node in view["nodes"]}
            )
            history_node = next(
                node for node in view["nodes"] if node["id"] == historical
            )
            self.assertIn(
                "Late Latin",
                {
                    str(context.get("period_label", ""))
                    for context in history_node["payload"]["history_contexts"]
                },
            )
            endpoint_pairs = [
                (str(edge["source"]), str(edge["target"]))
                for edge in view["edges"]
            ]
            self.assertEqual(len(endpoint_pairs), len(set(endpoint_pairs)))
            bundled = next(
                edge
                for edge in view["edges"]
                if edge["source"] == subject and edge["target"] == root
            )
            self.assertEqual(
                {
                    str(relation["relation"])
                    for relation in bundled.get("relations", [])
                },
                {"has-component", "has-history"},
            )

    def test_same_label_historical_nodes_remain_distinct_across_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            subject = store.upsert_term("en", "avert")
            root = store.upsert_morpheme("la", "vert", "root", "turn")
            latin = store.add_historical_form(
                "la", "avertire", meaning="turn away", period_label="medieval"
            )
            italian = store.add_historical_form(
                "it", "Avertire", meaning="turn away", period_label="medieval"
            )
            store.link_morpheme(subject, root, 0, "vert", basis="book")
            latin_edge = store.add_edge(
                latin, root, "developed-into", basis="book"
            )
            italian_edge = store.add_edge(
                italian, root, "developed-into", basis="book"
            )

            view = store.lexical_subgraph(
                subject, "word", {"nodes": 24, "edges": 32, "depth": 4}
            )

            matching = [
                node
                for node in view["nodes"]
                if node["type"] == "historical-form"
                and str(node["label"]).casefold() == "avertire"
            ]
            self.assertEqual({node["id"] for node in matching}, {latin, italian})
            visible_edge_ids = {
                source_edge_id
                for edge in view["edges"]
                for source_edge_id in edge.get("properties", {})
                .get("projection_edge_bundle", {})
                .get("source_edge_ids", [edge.get("id", "")])
            }
            self.assertTrue({latin_edge, italian_edge} <= visible_edge_ids)


if __name__ == "__main__":
    unittest.main()
