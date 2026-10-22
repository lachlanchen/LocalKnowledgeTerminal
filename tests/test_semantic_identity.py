from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.atomic import (
    _canonical_component_projection_parts,
)
from lkt.knowledge import KnowledgeStore, _projection_edge_identity
from lkt.morphology_contract import canonical_lexical_node_identity
from lkt.morphology_ingest_v3 import SCHEMA as MORPHOLOGY_V3_SCHEMA
from lkt.morphology_ingest_v3 import _ensure_term_morpheme


NOW = "2026-09-05T00:00:00Z"


class SemanticIdentityTests(unittest.TestCase):
    def test_subject_role_and_language_are_identity_boundaries(self) -> None:
        suffix = canonical_lexical_node_identity(
            "term-clairvoyant", "morpheme", "en", "-ant", role="suffix"
        )
        self.assertEqual(
            suffix,
            canonical_lexical_node_identity(
                "term-clairvoyant", "morpheme", "en", "ant", role="suffix"
            ),
        )
        self.assertNotEqual(
            suffix,
            canonical_lexical_node_identity(
                "term-clairvoyant", "morpheme", "en", "ant", role="root"
            ),
        )
        latin = canonical_lexical_node_identity(
            "term-stricture",
            "historical-form",
            "la",
            "strictus",
            role="origin:root:strict",
        )
        french = canonical_lexical_node_identity(
            "term-stricture",
            "historical-form",
            "fr",
            "strictus",
            role="origin:root:strict",
        )
        self.assertNotEqual(latin, french)
        self.assertNotEqual(
            latin,
            canonical_lexical_node_identity(
                "term-stricture",
                "historical-form",
                "la",
                "strictus",
                role="origin:root:strict",
                period_label="Late Latin",
            ),
        )

    def test_projection_rekeys_a_supported_legacy_suffix(self) -> None:
        evidence = ["evidence-ant"]
        artifact = [{
            "morpheme_id": "morpheme-legacy-ant",
            "canonical_form": "-ant",
            "surface": "ant",
            "kind": "suffix",
            "language": "en",
            "meaning": "having the quality of",
            "evidence_ids": evidence,
            "ordinal": 2,
        }]
        accepted = [{
            "morpheme_id": "morpheme-canonical-ant",
            "canonical_form": "-ant",
            "surface": "ant",
            "kind": "suffix",
            "language": "en",
            "meaning": "having the quality of",
            "evidence_ids": evidence,
            "ordinal": 3,
            "basis": "book",
            "confidence": 1.0,
        }]
        parts, aliases = _canonical_component_projection_parts(
            "term-clairvoyant",
            artifact,
            accepted,
            subject_form="clairvoyant",
        )
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["morpheme_id"], "morpheme-canonical-ant")
        self.assertEqual(
            aliases["morpheme-legacy-ant"], "morpheme-canonical-ant"
        )

    def test_equal_meaning_without_shared_evidence_does_not_rebind(self) -> None:
        artifact = [{
            "morpheme_id": "morpheme-homograph-a",
            "canonical_form": "-ant",
            "kind": "suffix",
            "language": "en",
            "meaning": "having the quality of",
            "evidence_ids": ["evidence-a"],
        }]
        accepted = [{
            "morpheme_id": "morpheme-homograph-b",
            "canonical_form": "-ant",
            "kind": "suffix",
            "language": "en",
            "meaning": "having the quality of",
            "evidence_ids": ["evidence-b"],
        }]
        _parts, aliases = _canonical_component_projection_parts(
            "term-clairvoyant",
            artifact,
            accepted,
            subject_form="clairvoyant",
        )
        self.assertNotIn("morpheme-homograph-a", aliases)

    def test_edge_bundle_identity_keeps_relation_authority_separate(self) -> None:
        self.assertNotEqual(
            _projection_edge_identity("source", "has-component", "target"),
            _projection_edge_identity("source", "developed-into", "target"),
        )

    def test_word_projection_coalesces_only_evidenced_component_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "clairvoyant")
            canonical_id = store.upsert_morpheme(
                "en", "-ant", "suffix", "having the quality of"
            )
            evidence_id = store.add_evidence(
                "affix-book",
                "p0576-t003-r005",
                source_hash="source-hash",
                locator="page 576",
                excerpt="-ant having the quality of",
                payload={},
            )
            legacy_id = "morpheme-legacy-projection-ant"
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES (?,'morpheme','legacy:morpheme:en:suffix:-ant','-ant',
                               'accepted',
                               '{"kind":"suffix","language":"en","meaning":"having the quality of"}',
                               ?,?)""",
                    (legacy_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphemes(
                           entity_id,language,form,normalized,kind,meaning)
                       VALUES (?,'en','-ant','-ant','suffix',
                               'having the quality of')""",
                    (legacy_id,),
                )
                connection.commit()
            for target_id, ordinal in ((legacy_id, 2), (canonical_id, 3)):
                store.accept_relation_assertion(
                    term_id,
                    term_id,
                    target_id,
                    "has-component",
                    basis="book",
                    evidence_ids=(evidence_id,),
                    properties={"ordinal": ordinal, "surface": "ant"},
                )
            # Simulate the pre-finalizer production duplicate: the public API
            # correctly retires the incumbent exclusive slot, while the legacy
            # database contains both projections as accepted rows.
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE relation_assertions SET status='accepted'
                       WHERE subject_entity_id=? AND source_entity_id=?
                         AND relation='has-component'
                         AND target_entity_id IN (?,?)""",
                    (term_id, term_id, legacy_id, canonical_id),
                )
                connection.execute(
                    """UPDATE entity_edges SET status='accepted'
                       WHERE source_entity_id=? AND relation='has-component'
                         AND target_entity_id IN (?,?)""",
                    (term_id, legacy_id, canonical_id),
                )
                connection.execute(
                    """UPDATE relation_assertion_supports SET status='accepted'
                       WHERE assertion_id IN (
                           SELECT assertion_id FROM relation_assertions
                           WHERE subject_entity_id=? AND source_entity_id=?
                             AND relation='has-component'
                             AND target_entity_id IN (?,?)
                       )""",
                    (term_id, term_id, legacy_id, canonical_id),
                )
                connection.commit()
                self.assertEqual(
                    connection.execute(
                        """SELECT COUNT(*) FROM relation_assertions
                           WHERE subject_entity_id=? AND source_entity_id=?
                             AND relation='has-component' AND status='accepted'
                             AND target_entity_id IN (?,?)""",
                        (term_id, term_id, legacy_id, canonical_id),
                    ).fetchone()[0],
                    2,
                )

            graph = store.lexical_subgraph(
                term_id,
                "word",
                {"nodes": 16, "edges": 24, "depth": 3},
            )

            suffixes = [
                node
                for node in graph["nodes"]
                if node["type"] == "morpheme"
                and str(node.get("payload", {}).get("kind", "")) == "suffix"
                and str(node.get("label", "")).strip("-").casefold() == "ant"
            ]
            self.assertEqual(len(suffixes), 1)
            self.assertEqual(suffixes[0]["id"], canonical_id)
            self.assertIn(
                legacy_id,
                {
                    item["canonical_entity_id"]
                    for item in suffixes[0]["payload"][
                        "projection_display_aliases"
                    ]
                },
            )
            component_edges = [
                edge
                for edge in graph["edges"]
                if edge["source"] == term_id
                and edge["target"] == canonical_id
                and edge["relation"] == "has-component"
            ]
            self.assertEqual(len(component_edges), 1)
            self.assertEqual(component_edges[0]["evidence_ids"], [evidence_id])
            self.assertEqual(
                len(
                    {
                        (edge["source"], edge["target"])
                        for edge in graph["edges"]
                    }
                ),
                len(graph["edges"]),
            )

    def test_ingest_reuses_evidenced_legacy_term_slot_for_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id = store.upsert_term("en", "clairvoyant")
            canonical_id = store.upsert_morpheme(
                "en", "-ant", "suffix", "having the quality of"
            )
            evidence_id = store.add_evidence(
                "affix-book",
                "p0576-t003-r005",
                source_hash="source-hash",
                locator="page 576",
                excerpt="-ant having the quality of",
                payload={},
            )
            legacy_id = "morpheme-legacy-ant"
            with closing(store._connect()) as connection:
                connection.executescript(MORPHOLOGY_V3_SCHEMA)
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES (?,'morpheme','morpheme:en:suffix:-ant','-ant',
                               'accepted','{}',?,?)""",
                    (legacy_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphemes(
                           entity_id,language,form,normalized,kind,meaning)
                       VALUES (?,'en','-ant','-ant','suffix',
                               'having the quality of')""",
                    (legacy_id,),
                )
                for entity_id in (legacy_id, canonical_id):
                    connection.execute(
                        """INSERT INTO entity_evidence(
                               entity_id,evidence_id,claim,confidence)
                           VALUES (?,?,?,.95)""",
                        (entity_id, evidence_id, "-ant suffix"),
                    )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,2,'ant',.95,'book')""",
                    (term_id, legacy_id),
                )
                changed, ordinal = _ensure_term_morpheme(
                    connection,
                    term_id,
                    canonical_id,
                    "ant",
                    evidence_id,
                    source_ordinal=2,
                    source_occurrence_id="formula-clairvoyant-ant",
                )
                rows = connection.execute(
                    """SELECT morpheme_id,ordinal FROM term_morphemes
                       WHERE term_id=? ORDER BY ordinal""",
                    (term_id,),
                ).fetchall()
            self.assertTrue(changed)
            self.assertEqual(ordinal, 2)
            self.assertEqual([tuple(row) for row in rows], [(canonical_id, 2)])

            repeated_id = store.upsert_term("en", "antant")
            repeated_morpheme_id = store.upsert_morpheme(
                "en", "ant", "root", "repeated root"
            )
            occupied_morpheme_id = store.upsert_morpheme(
                "en", "tan", "root", "independent occupied slot"
            )
            with closing(store._connect()) as connection:
                connection.executemany(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,'ant',.95,'book')""",
                    (
                        (repeated_id, repeated_morpheme_id, 0),
                        (repeated_id, occupied_morpheme_id, 1),
                    ),
                )
                changed, ordinal = _ensure_term_morpheme(
                    connection,
                    repeated_id,
                    repeated_morpheme_id,
                    "ant",
                    evidence_id,
                    source_ordinal=1,
                    source_occurrence_id="formula-antant-root-1",
                )
                count = connection.execute(
                    """SELECT COUNT(*) FROM term_morphemes
                       WHERE term_id=? AND morpheme_id=?""",
                    (repeated_id, repeated_morpheme_id),
                ).fetchone()[0]
                replay_changed, replay_ordinal = _ensure_term_morpheme(
                    connection,
                    repeated_id,
                    repeated_morpheme_id,
                    "ant",
                    evidence_id,
                    source_ordinal=1,
                    source_occurrence_id="formula-antant-root-1",
                )
                replay_count = connection.execute(
                    """SELECT COUNT(*) FROM term_morphemes
                       WHERE term_id=? AND morpheme_id=?""",
                    (repeated_id, repeated_morpheme_id),
                ).fetchone()[0]
            self.assertTrue(changed)
            self.assertEqual(ordinal, 2)
            self.assertEqual(count, 2)
            self.assertFalse(replay_changed)
            self.assertEqual(replay_ordinal, 2)
            self.assertEqual(replay_count, 2)

    def test_component_projection_preserves_repeated_artifact_occurrences(self) -> None:
        evidence = ["evidence-repeat"]
        artifact = [
            {
                "morpheme_id": "artifact-root-0",
                "canonical_form": "-ant",
                "surface": "ant",
                "kind": "suffix",
                "language": "en",
                "meaning": "root",
                "evidence_ids": evidence,
                "ordinal": 0,
            },
            {
                "morpheme_id": "artifact-root-1",
                "canonical_form": "-ant",
                "surface": "ant",
                "kind": "suffix",
                "language": "en",
                "meaning": "root",
                "evidence_ids": evidence,
                "ordinal": 1,
            },
        ]
        accepted = [
            {
                "morpheme_id": "morpheme-root",
                "canonical_form": "-ant",
                "surface": "ant",
                "kind": "suffix",
                "language": "en",
                "meaning": "root",
                "evidence_ids": evidence,
                "ordinal": 0,
                "basis": "book",
                "confidence": 1.0,
            },
            {
                "morpheme_id": "morpheme-root",
                "canonical_form": "-ant",
                "surface": "ant",
                "kind": "suffix",
                "language": "en",
                "meaning": "root",
                "evidence_ids": evidence,
                "ordinal": 1,
                "basis": "book",
                "confidence": 1.0,
            },
        ]

        parts, aliases = _canonical_component_projection_parts(
            "term-clairvoyant",
            artifact,
            accepted,
            subject_form="clairvoyant",
        )

        self.assertEqual([part["ordinal"] for part in parts], [0, 1])
        self.assertEqual(
            [part["morpheme_id"] for part in parts],
            ["morpheme-root", "morpheme-root"],
        )
        self.assertEqual(aliases["artifact-root-0"], "morpheme-root")
        self.assertEqual(aliases["artifact-root-1"], "morpheme-root")


if __name__ == "__main__":
    unittest.main()
