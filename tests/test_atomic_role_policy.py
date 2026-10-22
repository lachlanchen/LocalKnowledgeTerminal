from __future__ import annotations

import unittest

from lkt.atomic import _apply_cited_component_roles, _retrieved_component_evidence_ids


class AtomicRolePolicyTests(unittest.TestCase):
    def test_raw_corpus_kind_cannot_launder_component_role_evidence(self) -> None:
        records = [{
            "kind": "morphology-root",
            "knowledge_evidence_id": "evidence-root-index",
            "headword": "pre",
            "excerpt": "pre",
        }]
        self.assertEqual(_retrieved_component_evidence_ids("pre", "root", records), [])

    def test_live_typed_book_support_overrides_model_role_on_same_span(self) -> None:
        parts = [{"surface": "ab", "canonical_form": "ab", "kind": "root",
                  "language": "en", "evidence_ids": []}]
        components = [{"ordinal": 0, "surface": "ab", "kind": "prefix",
                       "evidence_ids": ["book-evidence"]}]
        result = _apply_cited_component_roles(parts, components)
        self.assertEqual(result[0]["kind"], "prefix")
        self.assertEqual(result[0]["canonical_form"], "ab-")
        self.assertEqual(result[0]["evidence_ids"], ["book-evidence"])

    def test_conflicting_live_typed_roles_stay_ambiguous(self) -> None:
        parts = [{"surface": "in", "canonical_form": "in", "kind": "root",
                  "language": "en", "evidence_ids": []}]
        components = [
            {"ordinal": 0, "surface": "in", "kind": "prefix", "evidence_ids": ["e1"]},
            {"ordinal": 0, "surface": "in", "kind": "root", "evidence_ids": ["e2"]},
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous cited morphology roles"):
            _apply_cited_component_roles(parts, components)


if __name__ == "__main__":
    unittest.main()
