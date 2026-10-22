from __future__ import annotations

import unittest

from lkt.atomic import PreparationWorker


class _LexicalStore:
    def __init__(self, view: dict[str, object]) -> None:
        self.view = view
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def lexical_subgraph(
        self,
        subject_entity_id: str,
        mode: str,
        limits: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((subject_entity_id, mode, dict(limits)))
        return self.view


def _worker(store: _LexicalStore) -> PreparationWorker:
    worker = object.__new__(PreparationWorker)
    worker.store = store
    return worker


class ProjectionContentTests(unittest.TestCase):
    def test_accepted_focus_fallback_adds_topology_and_rehashes(self) -> None:
        store = _LexicalStore(
            {
                "branch_focus_id": "",
                "projection_hash": "stale",
                "nodes": [
                    {
                        "id": "subject-1",
                        "type": "term",
                        "label": "inspect",
                        "payload": {"meaning": "examine"},
                    }
                ],
                "edges": [],
            }
        )

        view = _worker(store)._lexical_view(
            "subject-1",
            "root",
            ["root-1"],
            accepted_focus_node={
                "id": "root-1",
                "type": "root",
                "form": "spect",
                "meaning": "look",
                "basis": "book",
                "evidence_ids": ["evidence-1"],
            },
        )

        self.assertEqual(view["branch_focus_id"], "root-1")
        self.assertIn(
            ("subject-1", "root-1", "has-component"),
            {
                (edge["source"], edge["target"], edge["relation"])
                for edge in view["edges"]
            },
        )
        self.assertEqual(len(view["projection_hash"]), 64)
        self.assertNotEqual(view["projection_hash"], "stale")

    def test_card_graph_deduplicates_nodes_and_exact_edge_triples(self) -> None:
        nodes, edges = PreparationWorker._deduplicate_card_graph(
            [
                {"id": "word", "type": "word"},
                {"id": "root", "type": "root"},
                {"id": "root", "type": "root"},
            ],
            [
                {"source": "root", "target": "word", "relationship": "root-of"},
                {"source": "root", "target": "word", "relationship": "root-of"},
            ],
        )

        self.assertEqual([node["id"] for node in nodes], ["word", "root"])
        self.assertEqual(len(edges), 1)

    def test_completeness_includes_displayed_artifact_related_terms(self) -> None:
        view = {
            "nodes": [],
            "meaning_completeness": {
                "status": "complete",
                "missing_count": 0,
                "missing_entity_ids": [],
                "truncated": False,
            },
        }

        PreparationWorker._include_related_term_completeness(
            view,
            [
                {"term": "inspection", "note": "related"},
                {"term": "spectator", "meaning": "one who watches"},
            ],
        )

        self.assertEqual(view["meaning_completeness"]["status"], "partial")
        self.assertEqual(view["meaning_completeness"]["missing_count"], 1)
        self.assertEqual(
            view["meaning_completeness"]["missing_related_terms"],
            ["inspection"],
        )

    def test_branch_focus_is_queried_and_partial_meanings_are_bounded(self) -> None:
        missing_terms = [
            {
                "id": f"term-{index}",
                "type": "term",
                "payload": {"meaning": ""},
            }
            for index in range(20)
        ]
        store = _LexicalStore(
            {
                "branch_focus_id": "root-1",
                "nodes": [
                    {
                        "id": "root-1",
                        "type": "morpheme",
                        "payload": {"kind": "root", "meaning": ""},
                    },
                    *missing_terms,
                ],
                "edges": [],
            }
        )

        view = _worker(store)._lexical_view(
            "subject-1",
            "root",
            ["root-1", "root-1"],
        )

        self.assertEqual(store.calls[0][2]["branch_focus_id"], "root-1")
        self.assertEqual(view["focus_entity_ids"], ["root-1"])
        self.assertEqual(view["meaning_completeness"]["status"], "partial")
        self.assertEqual(view["meaning_completeness"]["missing_count"], 21)
        self.assertEqual(
            len(view["meaning_completeness"]["missing_entity_ids"]),
            16,
        )
        self.assertTrue(view["meaning_completeness"]["truncated"])
        self.assertTrue(
            view["meaning_completeness"]["focus_meaning_pending"]
        )

    def test_lexical_view_rejects_a_different_returned_focus(self) -> None:
        store = _LexicalStore(
            {
                "branch_focus_id": "root-2",
                "nodes": [
                    {
                        "id": "root-2",
                        "type": "morpheme",
                        "payload": {"kind": "root", "meaning": "turn"},
                    }
                ],
                "edges": [],
            }
        )

        with self.assertRaisesRegex(ValueError, "expected 'root-1'"):
            _worker(store)._lexical_view("subject-1", "root", ["root-1"])

        self.assertEqual(store.calls[0][2]["branch_focus_id"], "root-1")

    def test_blank_graph_meaning_preserves_accepted_split_gloss(self) -> None:
        self.assertEqual(
            PreparationWorker._accepted_component_meaning(
                {"meaning": ""},
                {"meaning": "preserved split gloss"},
            ),
            "preserved split gloss",
        )
        self.assertEqual(
            PreparationWorker._accepted_component_meaning(
                {"meaning": "authoritative graph gloss"},
                {"meaning": "split gloss"},
            ),
            "authoritative graph gloss",
        )

    def test_branch_focus_resolves_exactly_one_component(self) -> None:
        parts = [
            {"morpheme_id": "root-1", "kind": "root"},
            {"morpheme_id": "root-2", "kind": "root"},
            {"morpheme_id": "suffix-1", "kind": "suffix"},
        ]
        self.assertEqual(
            PreparationWorker._branch_component_id(
                {"id": "root-history-2", "node_ids": ["history", "root-2"]},
                parts,
                "root",
            ),
            "root-2",
        )
        self.assertEqual(
            PreparationWorker._branch_component_id(
                {"id": "suffix-1", "node_ids": ["suffix-1"]},
                parts,
                "affix",
            ),
            "suffix-1",
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            PreparationWorker._branch_component_id(
                {"id": "bad", "node_ids": ["root-1", "root-2"]},
                parts,
                "root",
            )


if __name__ == "__main__":
    unittest.main()
