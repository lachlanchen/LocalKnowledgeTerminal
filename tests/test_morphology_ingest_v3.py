from __future__ import annotations

import json
import sqlite3
import unittest

from lkt.morphology_ingest_v3 import (
    DeclarationLexicon,
    SourceRow,
    _archive_superseded_component_roles,
    build_declaration_lexicon,
    extract_declaration_seeds,
    parse_source_record,
)


def source(
    kind: str,
    record_id: str,
    headword: str,
    content: str,
    *,
    source_hash: str = "book-hash",
) -> SourceRow:
    return SourceRow(
        kind,
        f"test-{kind}",
        source_hash,
        f"Test {kind.title()} Dictionary",
        record_id,
        headword,
        1,
        content,
    )


class MorphologyRoleClassificationTests(unittest.TestCase):
    def test_clause_gloss_blocks_unrelated_same_form_canonical_sense(self) -> None:
        canonical_al = source(
            "root", "root-al", "AL", "词根 AL 意为 to nourish。"
        )
        martial = source(
            "root",
            "word-martial",
            "martial",
            (
                "martial［adjective］【词根词缀】"
                "mar(=war)+ti(=connective)+al(=nature of)"
            ),
        )
        lexicon = build_declaration_lexicon((canonical_al, martial))

        parsed = parse_source_record(martial, lexicon)
        al = next(
            item
            for item in parsed.components
            if item.clause_index >= 0 and item.raw_form == "al"
        )
        link = next(
            item
            for item in parsed.links
            if item.component_observation_id == al.observation_id
        )

        self.assertEqual(al.proposed_kind, "ambiguous")
        self.assertEqual(al.state, "ambiguous")
        self.assertTrue(al.qwen_ready)
        self.assertEqual(
            al.properties["basis"], "declaration-gloss-mismatch"
        )
        self.assertEqual(al.properties["component_glosses"], ["nature of"])
        self.assertEqual(link.state, "ambiguous")
        self.assertTrue(link.qwen_ready)

        explicitly_typed = source(
            "root",
            "typed-martial",
            "martial",
            "martial［adjective］词根 al(=nature of)",
        )
        explicit = next(
            item
            for item in parse_source_record(explicitly_typed, lexicon).components
            if item.clause_index >= 0 and item.raw_form == "al"
        )
        self.assertEqual(explicit.proposed_kind, "root")
        self.assertEqual(explicit.state, "materializable")
        self.assertEqual(explicit.properties["basis"], "printed-declaration")

    def test_polysemous_in_and_re_require_exact_clause_local_sense(self) -> None:
        lexicon = build_declaration_lexicon(
            (
                source("affix", "in-not", "IN", "前缀 IN- 意为 not。"),
                source("affix", "in-inside", "IN", "前缀 IN- 意为 inside。"),
                source("affix", "re-again", "RE", "前缀 RE- 意为 again。"),
                source("root", "re-thing", "RE", "词根 RE 意为 thing。"),
            )
        )

        self.assertEqual(
            lexicon.resolve("in", "not"),
            ("prefix", 0.99, "declaration-form-and-gloss"),
        )
        self.assertEqual(
            lexicon.resolve("in", "toward"),
            ("ambiguous", 0.0, "conflicting-declaration-senses"),
        )
        self.assertEqual(
            lexicon.resolve("re", "again"),
            ("prefix", 0.99, "declaration-form-and-gloss"),
        )
        self.assertEqual(
            lexicon.resolve("re", "about"),
            ("ambiguous", 0.0, "conflicting-declaration-kinds"),
        )

        record = source(
            "affix",
            "polysemous-formula",
            "reinvent",
            "reinvent［verb］【词根词缀】re(=about)+in(=toward)",
        )
        components = [
            item
            for item in parse_source_record(record, lexicon).components
            if item.clause_index >= 0
        ]
        self.assertEqual({item.raw_form for item in components}, {"in", "re"})
        self.assertTrue(all(item.state == "ambiguous" for item in components))
        self.assertTrue(all(item.qwen_ready for item in components))

    def test_matching_declared_root_and_clause_gloss_remains_materializable(
        self,
    ) -> None:
        root_lig = source(
            "root", "root-lig", "LIG", "词根 LIG 意为 bind / fasten。"
        )
        word = source(
            "root",
            "word-ligate",
            "ligate",
            "ligate［verb］lig(=bind / fasten)",
        )

        component = next(
            item
            for item in parse_source_record(
                word, build_declaration_lexicon((root_lig, word))
            ).components
            if item.clause_index >= 0
        )

        self.assertEqual(component.proposed_kind, "root")
        self.assertEqual(component.state, "materializable")
        self.assertFalse(component.qwen_ready)
        self.assertEqual(
            component.properties["component_glosses"], ["bind", "fasten"]
        )

    def test_composite_heading_does_not_turn_every_formula_part_into_root(self) -> None:
        prefix_ad = source(
            "affix",
            "affix-ad",
            "AD",
            "前缀 AD- 来源于拉丁语，意为 to,near。",
        )
        root_record = source(
            "root",
            "root-lig-alliance",
            "alliance",
            (
                "词根 LIG 来源于拉丁语，意为 bind。 "
                "alliance［noun］【词根词缀】al<ad(=to,near)+li<lig(=bind)"
            ),
        )
        lexicon = build_declaration_lexicon((prefix_ad, root_record))

        parsed = parse_source_record(root_record, lexicon)
        formula_components = {
            item.normalized_form: item
            for item in parsed.components
            if item.clause_index >= 0
        }

        self.assertEqual(formula_components["ad-"].proposed_kind, "prefix")
        self.assertEqual(formula_components["ad-"].state, "materializable")
        self.assertEqual(formula_components["lig"].proposed_kind, "root")
        self.assertNotIn(
            ("ad-", "root"),
            {
                (item.normalized_form, item.kind)
                for item in extract_declaration_seeds(root_record)
            },
        )

    def test_generic_heading_without_declaration_remains_ambiguous(self) -> None:
        record = source(
            "root",
            "unknown-formula",
            "example",
            "example［noun］【词根词缀】ad(=to)+vers(=turn)",
        )

        parsed = parse_source_record(record, DeclarationLexicon(()))

        formula_components = [
            item for item in parsed.components if item.clause_index >= 0
        ]
        self.assertTrue(formula_components)
        self.assertTrue(
            all(item.proposed_kind == "ambiguous" for item in formula_components)
        )
        self.assertTrue(all(item.qwen_ready for item in formula_components))

    def test_allomorph_resolution_uses_canonical_declared_form(self) -> None:
        lexicon = build_declaration_lexicon(
            (
                source("root", "root-al", "AL", "词根 AL 意为 nourish。"),
                source("affix", "affix-ad", "AD", "前缀 AD- 意为 to,near。"),
            )
        )

        kind, confidence, basis = lexicon.resolve("al<ad", "to,near")

        self.assertEqual(kind, "prefix")
        self.assertGreaterEqual(confidence, 0.9)
        self.assertEqual(basis, "declaration-form-and-gloss")

    def test_declaration_list_stops_before_realistic_example_entries(self) -> None:
        prefix_ad = source(
            "affix",
            "affix-ad",
            "AD",
            "前缀 AD- 来源于拉丁语，意为 to,near。",
        )
        record = source(
            "root",
            "p0386-t003-r006",
            "lev",
            (
                "词根 -lev(=light,lift) -sid(=sit) -ten<tain(=hold) "
                "-vert(=turn) -juve(=young) "
                "assiduous［adjective］【词根词缀】as<ad(=to,near)+sid(=sit) "
                "inadvertency［noun］【词根词缀】in(=not)+ad(=to)+vert(=turn)"
            ),
        )
        lexicon = build_declaration_lexicon((prefix_ad, record))

        parsed = parse_source_record(record, lexicon)
        declared = [item for item in parsed.components if item.clause_index == -1]
        self.assertEqual(
            {item.normalized_form for item in declared},
            {"lev", "sid", "tain", "vert", "juve"},
        )
        self.assertNotIn(
            "ad", {_letters for item in declared if (_letters := item.normalized_form)}
        )
        self.assertFalse(
            any(
                item.properties.get("basis") == "co-declared-forms"
                for item in parsed.declarations
            )
        )
        allomorphs = [
            item
            for item in parsed.declarations
            if item.declaration_type == "allomorph"
        ]
        self.assertEqual(len(allomorphs), 1)
        self.assertEqual(allomorphs[0].object_normalized_form, "ten")
        self.assertEqual(
            allomorphs[0].properties.get("basis"),
            "inline-explicit-allomorph",
        )
        formula_ad = [
            item
            for item in parsed.components
            if item.clause_index >= 0 and "ad" in item.properties.get("form_parts", [])
        ]
        self.assertTrue(formula_ad)
        self.assertTrue(all(item.proposed_kind == "prefix" for item in formula_ad))

    def test_separate_root_list_items_are_not_allomorphs_of_first_root(self) -> None:
        prefix_ad = source(
            "affix", "affix-ad", "AD", "前缀 AD- 意为 to。"
        )
        record = source(
            "root",
            "p0546-t003-r006",
            "dur",
            (
                "词根 -dur(=last) -plaus(=strike) -serv(=serve) "
                "-ver(=true) -vers(=turn) "
                "applause［noun］【词根词缀】ap<ad(=to)+plaus(=strike)"
            ),
        )

        parsed = parse_source_record(
            record, build_declaration_lexicon((prefix_ad, record))
        )

        self.assertFalse(
            any(
                item.declaration_type == "allomorph"
                for item in parsed.declarations
            )
        )
        self.assertNotIn(
            "ad",
            {
                item.normalized_form
                for item in parsed.components
                if item.clause_index == -1
            },
        )
        applause_ad = [
            item
            for item in parsed.components
            if item.clause_index >= 0 and item.raw_form == "ap<ad"
        ]
        self.assertEqual(len(applause_ad), 1)
        self.assertEqual(applause_ad[0].proposed_kind, "prefix")

    def test_explicit_allomorph_role_is_indexed_but_conflicts_stay_ambiguous(self) -> None:
        inline_root = source(
            "root", "root-tain", "tain", "词根 -ten<tain(=hold)"
        )
        root_ad = source("root", "root-ad", "ad", "词根 AD 意为 to。")
        prefix_ad = source("affix", "prefix-ad", "AD", "前缀 AD- 意为 to。")
        lexicon = build_declaration_lexicon((inline_root, root_ad, prefix_ad))

        self.assertEqual(lexicon.resolve("ten", "hold")[:2], ("root", 0.99))
        self.assertEqual(lexicon.resolve("tain", "hold")[:2], ("root", 0.99))
        self.assertEqual(
            lexicon.resolve("ad", "to"),
            ("ambiguous", 0.0, "conflicting-declaration-kinds"),
        )


class SupersededMorphologyRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE morphemes(
                entity_id TEXT PRIMARY KEY, form TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE entity_edges(
                edge_id TEXT PRIMARY KEY, source_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL, relation TEXT NOT NULL,
                status TEXT NOT NULL, properties TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE relation_assertions(
                assertion_id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL,
                source_entity_id TEXT NOT NULL, target_entity_id TEXT NOT NULL,
                relation TEXT NOT NULL, status TEXT NOT NULL,
                properties_json TEXT NOT NULL, revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE assertion_evidence(
                assertion_id TEXT NOT NULL, evidence_id TEXT NOT NULL
            );
            CREATE TABLE term_morphemes(
                term_id TEXT NOT NULL, morpheme_id TEXT NOT NULL
            );
            """
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_same_record_correction_archives_only_unsupported_old_role(self) -> None:
        record = source(
            "root",
            "alliance-entry",
            "alliance",
            "alliance［noun］al<ad(=to,near)+li<lig(=bind)",
        )
        evidence_id = "evidence-alliance"
        old_properties = {
            "acquisition": "book-morphology-v3",
            "parser_versions": ["book-morphology-observation-v3"],
            "source_entry_ids": [record.record_id],
            "source_hashes": [record.source_hash],
            "evidence_ids": [evidence_id],
        }
        shared_properties = {
            **old_properties,
            "evidence_ids": [evidence_id, "independent-evidence"],
        }
        self.db.executemany(
            "INSERT INTO morphemes(entity_id, form, kind) VALUES (?, ?, ?)",
            (
                ("root-ad", "ad", "root"),
                ("prefix-ad", "ad-", "prefix"),
                ("suffix-ad", "-ad", "suffix"),
            ),
        )
        for edge_id, target, properties in (
            ("edge-old-root", "root-ad", old_properties),
            ("edge-independent", "suffix-ad", shared_properties),
        ):
            assertion_id = f"assertion-{target}"
            encoded = json.dumps(properties, sort_keys=True)
            self.db.execute(
                """INSERT INTO entity_edges VALUES
                   (?, 'term-alliance', ?, 'has-component', 'accepted', ?, '')""",
                (edge_id, target, encoded),
            )
            self.db.execute(
                """INSERT INTO relation_assertions VALUES
                   (?, 'term-alliance', 'term-alliance', ?, 'has-component',
                    'accepted', ?, 1, '')""",
                (assertion_id, target, encoded),
            )
            self.db.execute(
                "INSERT INTO assertion_evidence VALUES (?, ?)",
                (assertion_id, evidence_id),
            )
            self.db.execute(
                "INSERT INTO term_morphemes VALUES ('term-alliance', ?)",
                (target,),
            )
        self.db.execute(
            """INSERT INTO assertion_evidence VALUES
               ('assertion-suffix-ad', 'independent-evidence')"""
        )

        counts = _archive_superseded_component_roles(
            self.db,
            source=record,
            term_id="term-alliance",
            desired_morpheme_ids={"prefix-ad"},
            evidence_id=evidence_id,
            timestamp="2026-09-05T00:00:00+00:00",
        )

        statuses = dict(
            self.db.execute("SELECT edge_id, status FROM entity_edges").fetchall()
        )
        self.assertEqual(statuses["edge-old-root"], "archived")
        self.assertEqual(statuses["edge-independent"], "accepted")
        self.assertIsNone(
            self.db.execute(
                """SELECT 1 FROM term_morphemes
                   WHERE morpheme_id = 'root-ad'"""
            ).fetchone()
        )
        self.assertEqual(counts["superseded_role_edges_archived"], 1)


if __name__ == "__main__":
    unittest.main()
