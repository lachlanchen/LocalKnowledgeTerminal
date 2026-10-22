from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from examples.pocketpolyglot_passage_graph import (
    DEFAULT_FIXTURE,
    DEFAULT_OUTPUT,
    LOCATOR_PREFIX,
    PARAGRAPH_ID,
    PARAGRAPH_TEXT,
    SOURCE_NOTE,
    SOURCE_SHA256,
    UNIT_TEXTS,
    build_proof,
    render_proof,
    validate_proof,
)


class PassageGraphExampleTests(unittest.TestCase):
    def test_fixture_is_the_exact_project_authored_sample(self) -> None:
        raw = DEFAULT_FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SOURCE_SHA256)
        proof = build_proof()
        self.assertEqual(proof["source"]["note"], SOURCE_NOTE)
        self.assertEqual(proof["source"]["paragraph_id"], PARAGRAPH_ID)
        self.assertEqual(proof["source"]["paragraph_text"], PARAGRAPH_TEXT)
        self.assertEqual("".join(UNIT_TEXTS), PARAGRAPH_TEXT)

    def test_reviewed_graph_edges_resolve_to_exact_unit_evidence(self) -> None:
        proof = build_proof()
        validate_proof(proof)
        graph = proof["graph"]
        self.assertEqual(len(graph["nodes"]), 8)
        self.assertEqual(len(graph["edges"]), 8)
        self.assertEqual(len(proof["evidence"]), 3)

        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        relations = {
            (labels[edge["source"]], edge["relation"], labels[edge["target"]])
            for edge in graph["edges"]
        }
        self.assertEqual(
            relations,
            {
                (PARAGRAPH_TEXT, "mentions-concept", "春天"),
                (PARAGRAPH_TEXT, "mentions-concept", "风"),
                ("风", "has-quality", "轻"),
                (PARAGRAPH_TEXT, "mentions-concept", "我"),
                (PARAGRAPH_TEXT, "mentions-concept", "小径"),
                ("我", "walks-on", "小径"),
                (PARAGRAPH_TEXT, "mentions-concept", "花"),
                ("花", "blooms-with-quality", "安静"),
            },
        )

        evidence = {item["evidence_id"]: item for item in proof["evidence"]}
        for edge in graph["edges"]:
            self.assertEqual(edge["basis"], "reviewed")
            self.assertTrue(edge["evidence_ids"])
            for evidence_id in edge["evidence_ids"]:
                self.assertIn(evidence_id, evidence)
                self.assertEqual(evidence[evidence_id]["source_hash"], SOURCE_SHA256)
        self.assertEqual(
            {item["locator"]: item["excerpt"] for item in evidence.values()},
            {
                f"{LOCATOR_PREFIX}/units/{index}": excerpt
                for index, excerpt in enumerate(UNIT_TEXTS)
            },
        )

    def test_independent_builds_are_byte_stable_and_artifact_is_current(self) -> None:
        first = render_proof()
        second = render_proof()
        self.assertEqual(first, second)
        self.assertEqual(DEFAULT_OUTPUT.read_bytes(), first)

        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first.json"
            second_path = Path(temporary) / "second.json"
            first_path.write_bytes(first)
            second_path.write_bytes(second)
            self.assertEqual(
                hashlib.sha256(first_path.read_bytes()).hexdigest(),
                hashlib.sha256(second_path.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
