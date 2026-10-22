from __future__ import annotations

import unittest
from unittest.mock import patch

from lkt.evidence_policy import filter_card_evidence
from lkt.web import acquire_random_renderable_card, live_card_rotation_eligible


def _card(
    card_id: str,
    *,
    mode: str = "root",
    query: str = "inspection",
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    subject: str = "term-inspection",
    branch: str = "root-spect",
) -> dict:
    return {
        "card_id": card_id,
        "mode": mode,
        "query": query,
        "title": query,
        "extensions": {
            "lexical_projection": {"status": "live"},
            "lexical_view": {
                "canonical_subject_entity_id": subject,
                "subject_entity_id": subject,
                "branch_focus_id": branch,
                "nodes": nodes or [],
                "edges": edges or [],
            },
        },
    }


class LiveCardPolicyTests(unittest.TestCase):
    def test_sparse_and_detached_branches_are_not_rotation_eligible(self) -> None:
        sparse = _card(
            "sparse",
            mode="word",
            nodes=[{"id": "term-unknown", "type": "term", "label": "unknown"}],
            edges=[],
        )
        detached = _card(
            "detached",
            query="martial",
            subject="term-martial",
            branch="root-al",
            nodes=[
                {"id": "root-al", "type": "morpheme", "label": "al"},
                {"id": "term-genial", "type": "term", "label": "genial"},
            ],
            edges=[
                {
                    "source": "term-genial",
                    "target": "root-al",
                    "relation": "has-component",
                }
            ],
        )

        self.assertFalse(live_card_rotation_eligible(sparse))
        self.assertFalse(live_card_rotation_eligible(detached))

    def test_direct_and_root_centric_branches_remain_eligible(self) -> None:
        direct = _card(
            "direct",
            nodes=[
                {"id": "term-inspection", "type": "term", "label": "inspection"},
                {"id": "root-spect", "type": "morpheme", "label": "spect"},
            ],
            edges=[
                {
                    "source": "term-inspection",
                    "target": "root-spect",
                    "relation": "has-component",
                }
            ],
        )
        root_centric = _card(
            "root-centric",
            query="SCRUT",
            subject="term-scrut",
            branch="root-scrut",
            nodes=[
                {"id": "root-scrut", "type": "morpheme", "label": "scrut"},
                {"id": "term-scrutiny", "type": "term", "label": "scrutiny"},
            ],
            edges=[
                {
                    "source": "term-scrutiny",
                    "target": "root-scrut",
                    "relation": "has-component",
                }
            ],
        )

        self.assertTrue(live_card_rotation_eligible(direct))
        self.assertTrue(live_card_rotation_eligible(root_centric))

    def test_random_acquisition_retries_a_stale_live_overlay(self) -> None:
        stale = _card(
            "stale",
            mode="word",
            nodes=[{"id": "term-stale", "type": "term", "label": "stale"}],
            edges=[],
        )
        healthy = _card(
            "healthy",
            mode="word",
            nodes=[
                {"id": "term-healthy", "type": "term", "label": "healthy"},
                {"id": "root-health", "type": "morpheme", "label": "health"},
            ],
            edges=[
                {
                    "source": "term-healthy",
                    "target": "root-health",
                    "relation": "has-component",
                }
            ],
        )

        class Store:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def random_accepted(self, _mode: str, excluded: tuple[str, ...]):
                self.calls.append(excluded)
                return healthy if "stale" in excluded else stale

        store = Store()
        rendered = acquire_random_renderable_card(
            store, "word", (), lambda candidate: candidate
        )

        self.assertEqual(rendered["card_id"], "healthy")
        self.assertEqual(store.calls, [(), ("stale",)])

    def test_random_acquisition_stops_retrying_after_server_deadline(self) -> None:
        stale = _card(
            "stale",
            mode="word",
            nodes=[{"id": "term-stale", "type": "term", "label": "stale"}],
            edges=[],
        )

        class Store:
            def __init__(self) -> None:
                self.calls = 0

            def random_accepted(self, _mode: str, _excluded: tuple[str, ...]):
                self.calls += 1
                return {**stale, "card_id": f"stale-{self.calls}"}

        store = Store()
        with patch("lkt.web.monotonic", side_effect=(0.0, 20.0)):
            rendered = acquire_random_renderable_card(
                store,
                "word",
                (),
                lambda candidate: candidate,
                max_attempts=8,
                deadline_seconds=10,
            )
        self.assertIsNone(rendered)
        self.assertEqual(store.calls, 1)

    def test_stale_serialized_evidence_uses_composition_relevance_policy(self) -> None:
        card = {
            "mode": "word",
            "query": "martial",
            "evidence": [
                {
                    "evidence_id": "exact",
                    "headword": "martial",
                    "kind": "entry",
                    "corpus_id": "word-origins",
                    "raw_excerpt": "Latin martialis denoted of Mars.",
                },
                {
                    "evidence_id": "loose",
                    "headword": "plagiarize",
                    "kind": "entry",
                    "corpus_id": "word-origins",
                    "raw_excerpt": "The epigrammatist Martial used it for literary thief.",
                },
                {
                    "evidence_id": "structured",
                    "headword": "Mars",
                    "kind": "entry",
                    "corpus_id": "word-origins",
                    "raw_excerpt": "Latin martialis is the source of English martial.",
                },
            ],
        }

        filtered = filter_card_evidence(card, support_forms=("martialis", "mar"))

        self.assertEqual(
            [item["evidence_id"] for item in filtered["evidence"]],
            ["exact", "structured"],
        )


if __name__ == "__main__":
    unittest.main()
