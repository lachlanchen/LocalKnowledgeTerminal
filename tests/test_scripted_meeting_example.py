from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from examples.scripted_bilingual_meeting_knowledge import (
    DEFAULT_FIXTURE,
    DEFAULT_OUTPUT,
    KNOWLEDGE_TYPES,
    NOT_CLAIMED,
    SOURCE_SHA256,
    build_example,
    render_example,
    validate_example,
)


class ScriptedMeetingExampleTests(unittest.TestCase):
    def test_fixture_is_project_owned_scripted_and_individually_timestamped(self) -> None:
        self.assertEqual(
            hashlib.sha256(DEFAULT_FIXTURE.read_bytes()).hexdigest(),
            SOURCE_SHA256,
        )
        example = build_example()
        self.assertTrue(example["example"]["scripted"])
        self.assertEqual(
            example["example"]["ownership"],
            "Local Knowledge Terminal project-owned fixture",
        )
        self.assertFalse(example["source"]["recording_exists"])
        self.assertEqual(example["source"]["utterance_count"], 10)
        observed_times = [
            (unit["source_span"]["start_ms"], unit["source_span"]["end_ms"])
            for unit in example["knowledge_units"]
        ]
        self.assertEqual(
            observed_times,
            [
                (0, 5200),
                (6000, 11000),
                (11800, 15700),
                (16500, 21300),
                (22200, 27500),
                (28400, 32900),
                (33700, 38200),
                (39000, 43500),
                (44300, 49100),
                (49900, 54800),
            ],
        )

    def test_ten_types_and_every_source_span_resolve_exactly(self) -> None:
        example = build_example()
        validate_example(example)
        transcript = example["source"]["transcript"]
        evidence = {
            record["evidence_id"]: record for record in example["evidence"]
        }
        self.assertEqual(
            [(unit["type"], unit["type_label"]) for unit in example["knowledge_units"]],
            list(KNOWLEDGE_TYPES),
        )
        self.assertEqual(len(evidence), 10)
        for unit in example["knowledge_units"]:
            span = unit["source_span"]
            exact_text = transcript[
                span["transcript_start_char"] : span["transcript_end_char"]
            ]
            self.assertEqual(exact_text, span["text"])
            self.assertEqual(evidence[unit["evidence_id"]]["excerpt"], exact_text)
            self.assertEqual(
                evidence[unit["evidence_id"]]["source_hash"], SOURCE_SHA256
            )
            self.assertEqual(span["source_hash"], SOURCE_SHA256)
            self.assertEqual(
                span["utterance_id"],
                evidence[unit["evidence_id"]]["source_entry_id"],
            )
            self.assertEqual(
                evidence[unit["evidence_id"]]["payload"]["speaker"],
                span["speaker"],
            )

    def test_reviewed_graph_resolves_every_typed_unit_to_evidence(self) -> None:
        example = build_example()
        units = {
            unit["entity_id"]: unit for unit in example["knowledge_units"]
        }
        self.assertEqual(len(example["graph"]["edges"]), 10)
        self.assertFalse(example["graph"]["truncated"])
        for edge in example["graph"]["edges"]:
            unit = units[edge["target"]]
            self.assertEqual(edge["basis"], "reviewed")
            self.assertEqual(edge["confidence"], 1.0)
            self.assertEqual(edge["relation"], f"has-{unit['type']}")
            self.assertEqual(edge["evidence_ids"], [unit["evidence_id"]])
            self.assertEqual(
                edge["properties"]["review_status"], "manually reviewed"
            )

    def test_manual_lifecycle_has_one_real_superseded_revision(self) -> None:
        example = build_example()
        lifecycle: dict[str, list[dict[str, object]]] = {}
        for record in example["review_lifecycle"]:
            lifecycle.setdefault(str(record["unit_id"]), []).append(record)
            self.assertEqual(record["review_method"], "manual")
        self.assertEqual(
            [
                (record["revision"], record["status"])
                for record in lifecycle["unit-03"]
            ],
            [(1, "superseded"), (2, "accepted")],
        )
        self.assertEqual(
            sum(
                record["status"] == "superseded"
                for records in lifecycle.values()
                for record in records
            ),
            1,
        )
        for unit in example["knowledge_units"]:
            self.assertEqual(lifecycle[unit["unit_id"]][-1]["status"], "accepted")
            self.assertEqual(
                lifecycle[unit["unit_id"]][-1]["summary"], unit["summary"]
            )

    def test_claim_boundaries_are_explicit(self) -> None:
        example = build_example()
        self.assertEqual(tuple(example["example"]["not_claimed"]), NOT_CLAIMED)
        combined = " ".join(example["example"]["not_claimed"])
        self.assertIn("automatic speech recognition", combined)
        self.assertIn("diarization", combined)
        self.assertIn("accuracy benchmark", combined)
        self.assertIn("customer deployment", combined)

    def test_independent_builds_are_byte_stable_and_artifact_is_current(self) -> None:
        first = render_example()
        second = render_example()
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
