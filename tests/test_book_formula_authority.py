from __future__ import annotations

import unittest

from lkt.atomic import _book_decomposition_shape
from lkt.morpheme_prompting import pack_morpheme_context


def record(headword: str, formula: str, **extra):
    return {
        "headword": headword,
        "kind": "morphology-root",
        "excerpt": formula,
        "knowledge_evidence_id": "evidence-test-formula",
        "corpus_id": "test-book",
        "entry_id": headword,
        "source_title": "Synthetic regression material",
        **extra,
    }


class BookFormulaAuthorityTests(unittest.TestCase):
    def test_partial_formula_does_not_invent_trailing_suffix(self):
        self.assertEqual(
            _book_decomposition_shape(
                "surplus", [record("surplus", "sur(=over) + plu(=more)")]
            ),
            [],
        )

    def test_partial_formula_does_not_invent_leading_prefix(self):
        self.assertEqual(
            _book_decomposition_shape(
                "recomfort", [record("recomfort", "com(=with) + fort(=strong)")]
            ),
            [],
        )

    def test_partial_formula_does_not_freeze_both_unexplained_ends(self):
        self.assertEqual(
            _book_decomposition_shape(
                "recomforted", [record("recomforted", "com(=with) + fort(=strong)")]
            ),
            [],
        )

    def test_complete_formula_preserves_surfaces_meanings_and_provenance(self):
        shape = _book_decomposition_shape(
            "unkind", [record("unkind", "un(=not) + kind(=friendly)")]
        )
        self.assertEqual([p["surface"] for p in shape], ["un", "kind"])
        self.assertEqual([p["meaning"] for p in shape], ["not", "friendly"])
        self.assertTrue(all(p["evidence_ids"] == ["evidence-test-formula"] for p in shape))

    def test_root_lookup_does_not_assign_positional_roles(self):
        shape = _book_decomposition_shape(
            "unkind",
            [record(
                "kind", "un(=not) + kind(=friendly)",
                component_hint="root", component_surface="kind",
            )],
        )
        self.assertEqual([p["surface"] for p in shape], ["un", "kind"])
        self.assertEqual([p["kind"] for p in shape], ["", ""])

    def test_compound_formula_does_not_make_one_base_an_affix(self):
        shape = _book_decomposition_shape(
            "rainbow",
            [record(
                "bow", "rain(=water) + bow(=arc)",
                component_hint="root", component_surface="bow",
            )],
        )
        self.assertEqual([p["kind"] for p in shape], ["", ""])

    def test_complete_formula_can_be_owned_by_another_component_entry(self):
        shape = _book_decomposition_shape(
            "abstain", [record("tain", "abs(=away) + tain(=hold)")]
        )
        self.assertEqual([p["surface"] for p in shape], ["abs", "tain"])

    def test_partial_formula_stays_available_to_local_model_as_context(self):
        records = [record("surplus", "sur(=over) + plu(=more)")]
        shape = _book_decomposition_shape("surplus", records)
        context = pack_morpheme_context("surplus", records, shape, [], {}, "test-visit")
        self.assertEqual(context["required_parts"], [])
        self.assertIn("plu(=more)", context["evidence"][0]["excerpt"])
        self.assertIn("evidence-test-formula", context["diagnostics"]["selected_evidence_ids"])

    def test_incomplete_formula_does_not_displace_a_complete_one(self):
        shape = _book_decomposition_shape(
            "surplus",
            [record("surplus", "sur(=over) + plu(=more)"),
             record("plus", "sur(=over) + plus(=more)")],
        )
        self.assertEqual([p["surface"] for p in shape], ["sur", "plus"])


if __name__ == "__main__":
    unittest.main()
