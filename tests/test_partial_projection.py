from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lkt.atomic import PreparationWorker, _canonical_component_projection_parts
from lkt.partial_projection import (
    component_projection_occurrence, merge_partial_component_projection,
)
from lkt.relation_support import reduce_relation_assertions, retire_relation_support
from lkt.store import CardStore
from tests import test_partial_morphology as fixtures


def component(identifier, form, kind, span=None, *, basis="model", language="und"):
    return {
        "morpheme_id": identifier, "canonical_form": form, "surface": form.strip("-"),
        "kind": kind, "language": language, "meaning": "fixture meaning",
        "source_span": span, "ordinal": 0, "confidence": 0.8,
        "basis": basis, "support_bases": [basis], "evidence_ids": [],
        "projection_relation": "has-component" if basis == "book" else "shares-component",
        "analysis_scope": "partial" if basis != "book" else "",
    }


class PartialProjectionTests(unittest.TestCase):
    def test_book_prefix_keeps_disjoint_partial_root_and_suffix(self):
        parts = [component("book", "re-", "prefix", basis="book", language="en"),
                 component("model", "re-", "prefix", [0, 2]),
                 component("root", "paint", "root", [2, 7]),
                 component("suffix", "-e", "suffix", [7, 8])]
        before = deepcopy(parts)
        result = merge_partial_component_projection("repainted", parts)
        self.assertEqual([p["morpheme_id"] for p in result], ["book", "root", "suffix"])
        self.assertEqual(result[0]["projection_alias_ids"], ["model"])
        self.assertEqual(parts, before)

    def test_book_role_wins_without_aliasing_a_different_role(self):
        parts = [component("book", "re-", "prefix", basis="book", language="en"),
                 component("wrong-role", "re", "root", [0, 2])]
        result = merge_partial_component_projection("repainted", parts)
        self.assertEqual([p["morpheme_id"] for p in result], ["book"])
        self.assertNotIn("projection_alias_ids", result[0])

    def test_partial_book_support_can_shadow_a_weaker_model_role_in_display(self):
        old = component("old", "re", "root", [0, 2])
        old["projection_relation"] = "has-component"
        book = component("book", "re-", "prefix", [0, 2], basis="book")
        book["projection_relation"] = "shares-component"
        result = merge_partial_component_projection("repainted", [old, book])
        self.assertEqual([p["morpheme_id"] for p in result], ["book"])
        self.assertNotIn("projection_alias_ids", result[0])

    def test_two_known_languages_do_not_inherit_each_others_history(self):
        parts = [component("book", "re-", "prefix", basis="book", language="en"),
                 component("latin", "re-", "prefix", [0, 2], language="la")]
        result = merge_partial_component_projection("repainted", parts)
        self.assertNotIn("projection_alias_ids", result[0])

    def test_internal_partial_suffix_uses_its_span_not_an_invented_slot(self):
        suffix = component("suffix", "-e", "suffix", [7, 8])
        self.assertEqual(component_projection_occurrence("repainted", suffix),
                         ("span", "7", "8"))
        suffix["source_span"] = [True, 8]
        self.assertEqual(component_projection_occurrence("repainted", suffix),
                         ("ordinal", "0"))

    def test_repeated_occurrences_are_not_folded_together(self):
        parts = [component("first", "can", "root", [0, 3]),
                 component("second", "can", "root", [3, 6])]
        self.assertEqual(len(merge_partial_component_projection("cancan", parts)), 2)


class MixedPartialPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PartialMorphologyPersistenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.worker = self.fixture.store, self.fixture.worker
        self.term = self.fixture.term
        excerpt = "Repainted means painted again; re- means again."
        self.evidence = self.store.add_evidence(
            "book-fixture", "repainted", excerpt=excerpt,
            payload={"corpus_id": "book-fixture", "entry_id": "repainted",
                     "headword": "repainted", "kind": "word-origin", "excerpt": excerpt},
        )
        self.prefix = self.store.upsert_morpheme(
            "en", "re-", "prefix", "again", status="accepted",
        )
        self.store.accept_relation_assertion(
            self.term, self.term, self.prefix, "has-component", basis="book",
            confidence=0.95, evidence_ids=[self.evidence],
            owner_type="morphology-parser", owner_id="book-fixture",
            properties={"ordinal": 0, "surface": "re", "component_kind": "prefix"},
        )
        self.store.link_morpheme(self.term, self.prefix, 0, "re", basis="book", confidence=0.95)
        value = {"parts": [fixtures.part("repainted", "free"),
                           fixtures.part("re", "prefix"), fixtures.part("paint"),
                           fixtures.part("e", "suffix")]}
        for part in value["parts"]:
            part["language"] = "und"
        self.job = self.fixture.job("mixed-partial")
        self.fixture.respond(value, value)
        self.worker._split_morphemes(self.job)
        self.accepted = self.fixture.accepted(self.job)

    def supported(self):
        return self.store.accepted_morphology_components(self.term, include_partial=True)

    def test_live_lookup_retains_partial_memberships_without_positional_slots(self):
        self.assertEqual(self.accepted["analysis_scope"], "partial")
        self.assertEqual([p["kind"] for p in self.supported()], ["prefix", "root", "suffix"])
        self.assertEqual(len(self.store.accepted_morphology_components(self.term)), 1)
        self.assertEqual(len(self.store.accepted_morphology_components(
            self.term, authoritative_only=True, include_partial=True,
        )), 1)
        with closing(self.store._connect()) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM term_morphemes WHERE term_id=?", (self.term,),
            ).fetchone()[0], 1)
        self.fixture.assert_invariants()

    def test_projection_aliases_the_book_prefix_and_keeps_partial_spans(self):
        result, aliases = _canonical_component_projection_parts(
            self.term, self.accepted["parts"], self.supported(), subject_form="repainted",
        )
        by_kind = {p["kind"]: p for p in result}
        self.assertEqual(len(result), 3)
        self.assertEqual(by_kind["prefix"]["morpheme_id"], self.prefix)
        model_prefix = next(p for p in self.accepted["parts"] if p["kind"] == "prefix")
        self.assertEqual(aliases[model_prefix["morpheme_id"]], self.prefix)
        self.assertEqual(by_kind["suffix"]["source_span"], [7, 8])

    def test_retired_support_is_not_resurrected_from_an_old_artifact(self):
        root = next(p for p in self.supported() if p["kind"] == "root")
        with closing(self.store._connect()) as connection, connection:
            owners = connection.execute(
                "SELECT owner_type,owner_id FROM relation_assertion_supports "
                "WHERE assertion_id=? AND status='accepted'", (root["assertion_id"],),
            ).fetchall()
            for owner in owners:
                retire_relation_support(connection, assertion_id=root["assertion_id"],
                                        owner_type=owner[0], owner_id=owner[1])
            reduce_relation_assertions(connection, [root["assertion_id"]])
            connection.commit()
        result, _ = _canonical_component_projection_parts(
            self.term, self.accepted["parts"], self.supported(), subject_form="repainted",
        )
        self.assertNotIn(root["morpheme_id"], {p["morpheme_id"] for p in result})
        self.fixture.assert_invariants()

    def test_generic_derivative_membership_is_not_a_source_partial_component(self):
        extra = self.store.upsert_morpheme("en", "other", "root", "other", status="accepted")
        self.store.accept_relation_assertion(
            self.term, self.term, extra, "shares-component", basis="model",
            confidence=0.7, owner_type="test-fixture", owner_id="generic-derivative",
            properties={"surface": "other"},
        )
        self.assertNotIn(extra, {p["morpheme_id"] for p in self.supported()})

    def test_real_composer_publishes_origin_root_and_both_affixes(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.worker.card_store = CardStore(Path(temporary.name) / "cards.sqlite3")
        self.worker._lexical_view = PreparationWorker._lexical_view.__get__(self.worker)
        for stage, payload in (
            ("accepted-meaning", {"definition": "Painted again.", "part_of_speech": "verb",
                                  "evidence_ids": [self.evidence]}),
            ("accepted-origin-branches", {"branches": [
                {"component_id": p["morpheme_id"], "component_kind": p["kind"], "steps": []}
                for p in self.accepted["parts"]
            ]}),
        ):
            self.store.save_job_artifact(self.job["job_id"], stage, payload,
                                         language="en", validation_state="accepted", quality_score=0.8)
        persisted = self.worker._persist_composed_origin_cards
        captured = []

        def capture(cards, **kwargs):
            captured.extend(card.to_dict() for card in cards)
            return persisted(cards, **kwargs)

        self.worker._persist_composed_origin_cards = Mock(side_effect=capture)
        self.worker._compose_origin_card(self.job)
        self.assertEqual(sorted(card["mode"] for card in captured), ["affix", "affix", "root", "word"])
        root_id = next(p["morpheme_id"] for p in self.supported() if p["kind"] == "root")
        for card in captured:
            view = card["extensions"]["lexical_view"]
            ids = [node["id"] for node in view["nodes"]]
            self.assertEqual(len(ids), len(set(ids)))
            if card["mode"] in {"word", "root"}:
                self.assertIn(root_id, ids)
            triples = [(edge["source"], edge.get("relation", edge.get("relationship")), edge["target"])
                       for edge in view["edges"]]
            self.assertEqual(len(triples), len(set(triples)))
        self.fixture.assert_invariants()
