from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore
from lkt.relation_support import (
    morphology_support_migration_report,
    replace_relation_support_scope,
)


class RelationSupportOwnerTests(unittest.TestCase):
    def test_model_morphology_defaults_to_atomic_subject_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term = store.upsert_term("en", "inspect")
            derivative = store.upsert_term("en", "inspection")
            component = store.upsert_morpheme("en", "spect", "root", "look")
            history = store.add_historical_form("la", "specere")
            component_assertion = store.accept_relation_assertion(
                term,
                term,
                component,
                "has-component",
                basis="model",
                properties={"ordinal": 0, "surface": "spect"},
            )
            derivative_assertion = store.accept_relation_assertion(
                term,
                derivative,
                component,
                "shares-component",
                basis="model",
            )
            history_assertion = store.accept_relation_assertion(
                term,
                component,
                history,
                "developed-into",
                basis="model",
            )

            with closing(store._connect()) as connection:
                owners = {
                    str(row["assertion_id"]): (
                        str(row["owner_type"]),
                        str(row["owner_id"]),
                    )
                    for row in connection.execute(
                        """SELECT assertion_id, owner_type, owner_id
                           FROM relation_assertion_supports
                           WHERE assertion_id IN (?, ?, ?)
                             AND status = 'accepted'""",
                        (
                            component_assertion,
                            derivative_assertion,
                            history_assertion,
                        ),
                    )
                }
            self.assertEqual(
                owners[component_assertion], ("atomic-split", term)
            )
            self.assertEqual(
                owners[derivative_assertion], ("atomic-split", term)
            )
            self.assertEqual(
                owners[history_assertion], ("relation-decision", term)
            )

    def test_exclusive_replacement_refuses_unowned_legacy_incumbent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term = store.upsert_term("en", "preview")
            legacy_root = store.upsert_morpheme("en", "pre", "root", "before")
            prefix = store.upsert_morpheme("en", "pre-", "prefix", "before")
            legacy_assertion = store.accept_relation_assertion(
                term,
                term,
                legacy_root,
                "has-component",
                basis="model",
                properties={"ordinal": 0, "surface": "pre"},
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    "DELETE FROM relation_assertion_supports WHERE assertion_id = ?",
                    (legacy_assertion,),
                )
                connection.commit()
            evidence_id = store.add_evidence("affixes", "prefix-pre")

            refused = store.accept_relation_assertion(
                term,
                term,
                prefix,
                "has-component",
                basis="book",
                evidence_ids=(evidence_id,),
                properties={"ordinal": 0, "surface": "pre"},
                owner_type="morphology-parser",
                owner_id="affixes:prefix-pre",
                owner_version="v7",
            )

            self.assertEqual(refused, "")
            with closing(store._connect()) as connection:
                accepted_targets = {
                    str(row[0])
                    for row in connection.execute(
                        """SELECT target_entity_id FROM relation_assertions
                           WHERE subject_entity_id = ?
                             AND relation = 'has-component'
                             AND status = 'accepted'""",
                        (term,),
                    )
                }
                observation = connection.execute(
                    """SELECT decision, reason FROM relation_claim_observations
                       WHERE decision = 'conflict'
                         AND reason LIKE '%no attributable support owner%'
                       ORDER BY created_at DESC LIMIT 1"""
                ).fetchone()
            self.assertEqual(accepted_targets, {legacy_root})
            self.assertEqual(str(observation["decision"]), "conflict")
            self.assertIn("no attributable support owner", str(observation["reason"]))

    def test_migration_readiness_scopes_unowned_morphology_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term = store.upsert_term("en", "inspect")
            peer = store.upsert_term("en", "observe")
            unrelated = store.accept_relation_assertion(
                term,
                term,
                peer,
                "associated-with",
                basis="model",
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    "DELETE FROM relation_assertion_supports WHERE assertion_id = ?",
                    (unrelated,),
                )
                replace_relation_support_scope(
                    connection,
                    owner_type="morphology-parser",
                    owner_id="record-one",
                    owner_version="v7",
                    supports=(),
                    timestamp="2026-09-05T00:00:00Z",
                )
                connection.commit()
                ready = morphology_support_migration_report(
                    connection,
                    parser_version="v7",
                    expected_source_records=1,
                )
            self.assertTrue(ready["destructive_ready"])
            self.assertEqual(
                ready["accepted_assertions_without_active_owner"], 0
            )

            component = store.upsert_morpheme("en", "spect", "root", "look")
            component_assertion = store.accept_relation_assertion(
                term,
                term,
                component,
                "has-component",
                basis="model",
                properties={"ordinal": 0, "surface": "spect"},
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    "DELETE FROM relation_assertion_supports WHERE assertion_id = ?",
                    (component_assertion,),
                )
                connection.commit()
                blocked = morphology_support_migration_report(
                    connection,
                    parser_version="v7",
                    expected_source_records=1,
                )
            self.assertFalse(blocked["destructive_ready"])
            self.assertEqual(
                blocked["accepted_assertions_without_active_owner"], 1
            )

    def test_schema_v13_and_morpheme_role_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")

            plain_prefix = store.upsert_morpheme("en", "pre", "prefix", "before")
            marked_prefix = store.upsert_morpheme("en", "PRE-", "prefix", "before")
            fullwidth_prefix = store.upsert_morpheme(
                "en", "pre\uff0d", "prefix", "before"
            )
            root = store.upsert_morpheme("en", "pre", "root", "before")

            self.assertEqual(plain_prefix, marked_prefix)
            self.assertEqual(marked_prefix, fullwidth_prefix)
            self.assertNotEqual(plain_prefix, root)
            with closing(store._connect()) as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type = 'table' AND name LIKE
                                 'relation_assertion_support%'"""
                    )
                }
                prefix = connection.execute(
                    """SELECT entity.canonical_key, entity.label,
                              morpheme.form, morpheme.normalized
                       FROM entities AS entity
                       JOIN morphemes AS morpheme USING(entity_id)
                       WHERE entity.entity_id = ?""",
                    (plain_prefix,),
                ).fetchone()
            self.assertEqual(version, "13")
            self.assertEqual(
                tables,
                {
                    "relation_assertion_supports",
                    "relation_assertion_support_evidence",
                },
            )
            self.assertEqual(
                tuple(prefix),
                ("morpheme:en:prefix:pre", "pre-", "pre-", "pre"),
            )

    def test_atomic_scope_retires_only_its_conflicting_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term = store.upsert_term("en", "inspect")
            prefix = store.upsert_morpheme("en", "in-", "prefix", "into")
            wrong_root = store.upsert_morpheme("en", "in", "root", "into")
            evidence_id = store.add_evidence("roots", "inspect-prefix")
            store.link_morpheme(term, prefix, 0, "in", basis="book")
            store.link_morpheme(term, wrong_root, 1, "in", basis="model")

            prefix_assertion = store.accept_relation_assertion(
                term,
                term,
                prefix,
                "has-component",
                basis="book",
                evidence_ids=(evidence_id,),
                properties={"ordinal": 0, "surface": "in"},
                owner_type="morphology-parser",
                owner_id="formula-prefix",
                owner_version="v7",
            )
            with closing(store._connect()) as connection:
                replay_revision_before = int(
                    connection.execute(
                        "SELECT revision FROM relation_assertions WHERE assertion_id = ?",
                        (prefix_assertion,),
                    ).fetchone()[0]
                )
            replay_assertion = store.accept_relation_assertion(
                term,
                term,
                prefix,
                "has-component",
                basis="book",
                evidence_ids=(evidence_id,),
                properties={"ordinal": 0, "surface": "in"},
                owner_type="morphology-parser",
                owner_id="formula-prefix",
                owner_version="v7",
            )
            with closing(store._connect()) as connection:
                replay_revision_after = int(
                    connection.execute(
                        "SELECT revision FROM relation_assertions WHERE assertion_id = ?",
                        (prefix_assertion,),
                    ).fetchone()[0]
                )
            self.assertEqual(replay_assertion, prefix_assertion)
            self.assertEqual(replay_revision_after, replay_revision_before)
            wrong_assertion = store.accept_relation_assertion(
                term,
                term,
                wrong_root,
                "has-component",
                basis="model",
                properties={"ordinal": 1, "surface": "in"},
                owner_type="atomic-split",
                owner_id=term,
                owner_version="draft-1",
            )
            store.accept_relation_assertion(
                term,
                term,
                prefix,
                "has-component",
                basis="model",
                properties={"ordinal": 0, "surface": "in"},
                owner_type="atomic-split",
                owner_id=term,
                owner_version="draft-1",
            )

            store.reconcile_accepted_lexical_split(
                term,
                active_assertion_ids=(prefix_assertion,),
                active_component_ids=(prefix,),
                component_count=1,
                owner_type="atomic-split",
                owner_id=term,
                owner_version="draft-2",
            )

            with closing(store._connect()) as connection:
                statuses = {
                    str(row["assertion_id"]): str(row["status"])
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE assertion_id IN (?, ?)""",
                        (prefix_assertion, wrong_assertion),
                    )
                }
                prefix_owners = {
                    str(row[0])
                    for row in connection.execute(
                        """SELECT owner_type FROM relation_assertion_supports
                           WHERE assertion_id = ? AND status = 'accepted'""",
                        (prefix_assertion,),
                    )
                }
                atomic_prefix_basis = connection.execute(
                    """SELECT basis FROM relation_assertion_supports
                       WHERE assertion_id = ? AND owner_type = 'atomic-split'
                         AND owner_id = ? AND status = 'accepted'""",
                    (prefix_assertion, term),
                ).fetchone()[0]
            self.assertEqual(statuses[prefix_assertion], "accepted")
            self.assertEqual(statuses[wrong_assertion], "archived")
            self.assertEqual(
                prefix_owners, {"morphology-parser", "atomic-split"}
            )
            self.assertEqual(atomic_prefix_basis, "model")
            authoritative = store.accepted_morphology_components(
                term, authoritative_only=True
            )
            self.assertEqual(
                [
                    (
                        item["base"],
                        item["canonical_form"],
                        item["kind"],
                        item["ordinal"],
                        item["surface"],
                    )
                    for item in authoritative
                ],
                [("in", "in-", "prefix", 0, "in")],
            )


if __name__ == "__main__":
    unittest.main()
