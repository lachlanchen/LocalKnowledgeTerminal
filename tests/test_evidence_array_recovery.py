"""Local-model formatting recovery must not alter evidence or invent content."""

import json
import unittest

from lkt.model_json import parse_model_json


class EvidenceArrayRecoveryTests(unittest.TestCase):
    def test_observed_unbracketed_ids_preserve_the_answer(self):
        reply = (
            '{"meaning": "A retrieved meaning", "confidence": 0.95, '
            '"evidence_ids": "entry-one", "entry-two"}'
        )
        value, method = parse_model_json(reply)
        self.assertEqual(value, {
            "meaning": "A retrieved meaning",
            "confidence": 0.95,
            "evidence_ids": ["entry-one", "entry-two"],
        })
        self.assertEqual(method, "evidence-array-repair")

    def test_following_fields_are_not_mistaken_for_ids(self):
        value, method = parse_model_json(
            '{"evidence_ids": "entry-one", "entry-two", "entry-three", '
            '"meaning": "Keep this field", "confidence": 0.8}'
        )
        self.assertEqual(value["evidence_ids"], ["entry-one", "entry-two", "entry-three"])
        self.assertEqual(value["meaning"], "Keep this field")
        self.assertEqual(value["confidence"], 0.8)
        self.assertEqual(method, "evidence-array-repair")

    def test_nested_fields_and_escaped_ids_are_preserved(self):
        ids = ['entry-"quoted"', "entry-\\backslash"]
        reply = '{"result": {"evidence_ids": ' + ", ".join(map(json.dumps, ids)) + '}}'
        value, method = parse_model_json(reply)
        self.assertEqual(value, {"result": {"evidence_ids": ids}})
        self.assertEqual(method, "evidence-array-repair")

    def test_valid_json_is_not_reinterpreted(self):
        expected = {
            "evidence_ids": ["entry-one", "entry-two"],
            "example": '"evidence_ids": "not-a-source", "also-not-a-source"',
        }
        value, method = parse_model_json(json.dumps(expected))
        self.assertEqual(value, expected)
        self.assertEqual(method, "none")

    def test_container_recovery_can_precede_list_recovery(self):
        value, method = parse_model_json('{"evidence_ids": "entry-one", "entry-two",')
        self.assertEqual(value, {"evidence_ids": ["entry-one", "entry-two"]})
        self.assertEqual(method, "evidence-array-repair")

    def test_other_orphan_values_and_incomplete_ids_remain_errors(self):
        for reply in (
            '{"meaning": "say", "speak"}',
            '{"evidence_ids": "entry-one", "incomplete',
            '{"evidence_ids": "entry-one", "entry-two", "confidence" 0.8}',
            '<think>{"evidence_ids": "entry-one", "entry-two"}',
        ):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                parse_model_json(reply)


if __name__ == "__main__":
    unittest.main()
