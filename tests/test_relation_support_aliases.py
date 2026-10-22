from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

from lkt.relation_support import (
    ensure_relation_support_schema,
    morpheme_alias_migration_manifest,
    upsert_relation_support,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE entities(
            entity_id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL,
            status TEXT NOT NULL, quality_score REAL
        );
        CREATE TABLE morphemes(
            entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
            language TEXT, form TEXT, normalized TEXT, kind TEXT, meaning TEXT
        );
        CREATE TABLE evidence_records(evidence_id TEXT PRIMARY KEY);
        CREATE TABLE relation_assertions(
            assertion_id TEXT PRIMARY KEY, subject_entity_id TEXT,
            source_entity_id TEXT, target_entity_id TEXT, relation TEXT,
            basis TEXT, confidence REAL, status TEXT, properties_json TEXT,
            revision INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE assertion_evidence(
            assertion_id TEXT, evidence_id TEXT,
            PRIMARY KEY(assertion_id, evidence_id)
        );
        CREATE TABLE entity_edges(
            edge_id TEXT PRIMARY KEY, source_entity_id TEXT,
            target_entity_id TEXT, relation TEXT, basis TEXT,
            confidence REAL, status TEXT, properties TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE term_morphemes(
            term_id TEXT, morpheme_id TEXT, ordinal INTEGER, surface TEXT,
            PRIMARY KEY(term_id, morpheme_id)
        );
        CREATE TABLE entity_evidence(
            entity_id TEXT REFERENCES entities(entity_id), evidence_id TEXT,
            PRIMARY KEY(entity_id, evidence_id)
        );
        """
    )
    ensure_relation_support_schema(connection)
    connection.executemany(
        "INSERT INTO entities VALUES (?, ?, 'accepted', ?)",
        [
            ("term", "term:en:preview", 1.0),
            ("prefix-canonical", "morpheme:en:prefix:pre", 0.9),
            ("prefix-alias", "morpheme:en:prefix:pre-", 0.7),
            ("root-homograph", "morpheme:en:root:pre", 1.0),
        ],
    )
    connection.executemany(
        "INSERT INTO morphemes VALUES (?, 'en', ?, ?, ?, '')",
        [
            ("prefix-canonical", "pre-", "pre", "prefix"),
            ("prefix-alias", "pre", "pre", "prefix"),
            ("root-homograph", "pre", "pre", "root"),
        ],
    )
    for assertion_id, target in (("a-canonical", "prefix-canonical"), ("a-alias", "prefix-alias")):
        connection.execute(
            """INSERT INTO relation_assertions VALUES(
                   ?, 'term', 'term', ?, 'has-component', 'model', 0.7,
                   'accepted', '{}', 1, 't', 't')""",
            (assertion_id, target),
        )
        connection.execute(
            """INSERT INTO entity_edges VALUES(
                   ?, 'term', ?, 'has-component', 'model', 0.7,
                   'accepted', '{}', 't', 't')""",
            (f"edge-{assertion_id}", target),
        )
    connection.execute(
        "INSERT INTO term_morphemes VALUES('term', 'prefix-alias', 0, 'pre')"
    )
    return connection


class RelationSupportAliasManifestTests(unittest.TestCase):
    def test_plan_merges_same_role_alias_and_preserves_root_homograph(self) -> None:
        with closing(_connection()) as connection:
            for assertion_id in ("a-canonical", "a-alias"):
                upsert_relation_support(
                    connection,
                    assertion_id=assertion_id,
                    owner_type="atomic-split",
                    owner_id=f"owner-{assertion_id}",
                    basis="model",
                    confidence=0.7,
                    properties={},
                    timestamp="t",
                )
            manifest = morpheme_alias_migration_manifest(connection)
            self.assertTrue(manifest["destructive_ready"])
            self.assertEqual(manifest["group_count"], 1)
            group = manifest["groups"][0]
            self.assertEqual(group["kind"], "prefix")
            self.assertEqual(group["survivor_entity_id"], "prefix-canonical")
            self.assertEqual(group["archive_entity_ids"], ["prefix-alias"])
            alias_action = next(
                action for action in group["assertion_actions"]
                if action["assertion_id"] == "a-alias"
            )
            self.assertEqual(alias_action["merge_into_assertion_id"], "a-canonical")
            self.assertEqual(
                group["term_morpheme_actions"][0]["to_morpheme_id"],
                "prefix-canonical",
            )
            self.assertNotIn("root-homograph", group["archive_entity_ids"])

    def test_zero_ledger_and_unknown_reference_block_readiness(self) -> None:
        with closing(_connection()) as connection:
            connection.execute(
                """CREATE TABLE external_entity_use(
                       use_id TEXT PRIMARY KEY,
                       entity_id TEXT REFERENCES entities(entity_id))"""
            )
            connection.execute(
                "INSERT INTO external_entity_use VALUES('use', 'prefix-alias')"
            )
            manifest = morpheme_alias_migration_manifest(connection)
            self.assertFalse(manifest["destructive_ready"])
            self.assertEqual(
                manifest["zero_ledger_assertion_ids"],
                ["a-alias", "a-canonical"],
            )
            self.assertEqual(
                manifest["unknown_entity_references"],
                [
                    {
                        "table": "external_entity_use",
                        "column": "entity_id",
                        "row_count": 1,
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
