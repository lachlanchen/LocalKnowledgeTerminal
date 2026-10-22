from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lkt.cli import command_finalize_morphology_v7
from lkt.knowledge import KnowledgeStore
from lkt.morphology_contract import CURRENT_MORPHOLOGY_PARSER_VERSION
from lkt.morphology_ingest_v3 import SCHEMA as MORPHOLOGY_SCHEMA
from lkt.relation_support import (
    RelationSupportSpec,
    _complete_atomic_occurrence_projection,
    finalize_morphology_support_migration,
    morphology_v7_finalization_manifest,
    replace_relation_support_scope,
    upsert_relation_support,
)


NOW = "2026-09-05T00:00:00Z"


class MorphologyV7FinalizerTests(unittest.TestCase):
    def fixture(self, temp: str, *, records: int = 2):
        database = Path(temp) / "knowledge.sqlite3"
        store = KnowledgeStore(database)
        term_id = store.upsert_term("en", "preview")
        root_id = store.upsert_morpheme("en", "pre", "root")
        prefix_id = store.upsert_morpheme("en", "pre-", "prefix")
        expected = tuple(
            ("roots" if index == 0 else "affixes", f"r{index + 1}", f"hash-{index}")
            for index in range(records)
        )
        with closing(store._connect()) as connection:
            connection.executescript(MORPHOLOGY_SCHEMA)
            for index, (corpus_id, record_id, source_hash) in enumerate(expected):
                evidence_id = f"evidence-{index}"
                connection.execute(
                    """INSERT INTO evidence_records(
                           evidence_id, corpus_id, source_entry_id, source_hash,
                           locator, excerpt, payload) VALUES (?, ?, ?, ?, '', ?, '{}')""",
                    (evidence_id, corpus_id, record_id, source_hash, record_id),
                )
                connection.execute(
                    """INSERT INTO morphology_source_observations_v3(
                           source_observation_id, corpus_id, source_entry_id,
                           source_hash, parser_version, source_kind, evidence_id,
                           headword, headword_normalized, source_page, locator,
                           raw_excerpt, raw_fingerprint, parse_state,
                           materialization_state, eligibility_reason, term_count,
                           component_count, formula_link_count, declaration_count,
                           qwen_ready_count, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, 'parsed',
                               'ambiguous', '', 0, 0, 0, 0, 0, ?, ?)""",
                    (
                        f"v7-source-{index}", corpus_id, record_id, source_hash,
                        CURRENT_MORPHOLOGY_PARSER_VERSION,
                        "root" if index == 0 else "affix", evidence_id,
                        record_id, record_id, record_id, f"fp-{index}", NOW, NOW,
                    ),
                )
                replace_relation_support_scope(
                    connection, owner_type="morphology-parser",
                    owner_id=f"{corpus_id}:{record_id}",
                    owner_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    supports=(), timestamp=NOW,
                )
            manifest = [
                {"source_kind": "root" if index == 0 else "affix",
                 "corpus_id": corpus, "source_hash": source_hash,
                 "path": f"/{corpus}.sqlite3"}
                for index, (corpus, _, source_hash) in enumerate(expected)
            ]
            connection.execute(
                """INSERT INTO morphology_ingest_runs(
                       run_id, parser_version, source_manifest_json, state,
                       planned_records, processed_records, materialized_records,
                       ambiguous_records, invariant_report_json, error,
                       created_at, updated_at, finished_at)
                   VALUES ('run-v7', ?, ?, 'complete', ?, ?, 0, ?, '{}', '', ?, ?, ?)""",
                (
                    CURRENT_MORPHOLOGY_PARSER_VERSION, json.dumps(manifest),
                    records, records, records, NOW, NOW, NOW,
                ),
            )
            connection.execute(
                """INSERT INTO morphology_ingest_batches(
                       run_id, batch_index, source_kind, first_source_entry_id,
                       last_source_entry_id, record_count, state, counts_json,
                       error, created_at, updated_at)
                   VALUES ('run-v7', 0, 'mixed', 'r1', ?, ?, 'complete', '{}', '', ?, ?)""",
                (f"r{records}", records, NOW, NOW),
            )
            connection.commit()
        return store, expected, term_id, root_id, prefix_id

    @staticmethod
    def add_legacy_formula(
        store: KnowledgeStore, expected, term_id: str, morpheme_id: str,
        *, assertion_id: str = "legacy-pre-root",
    ) -> str:
        corpus_id, record_id, source_hash = expected[0]
        with closing(store._connect()) as connection:
            connection.execute(
                """INSERT INTO morphology_source_observations_v3(
                       source_observation_id, corpus_id, source_entry_id,
                       source_hash, parser_version, source_kind, evidence_id,
                       headword, headword_normalized, source_page, locator,
                       raw_excerpt, raw_fingerprint, parse_state,
                       materialization_state, eligibility_reason, term_count,
                       component_count, formula_link_count, declaration_count,
                       qwen_ready_count, created_at, updated_at)
                   VALUES ('legacy-source', ?, ?, ?, 'book-morphology-observation-v6',
                           'root', 'evidence-0', 'PRE', 'pre', 0, '', 'PRE',
                           'legacy-fp', 'parsed', 'materialized', '', 1, 1, 1, 0,
                           0, ?, ?)""",
                (corpus_id, record_id, source_hash, NOW, NOW),
            )
            connection.execute(
                """INSERT INTO morphology_term_observations_v3(
                       term_observation_id, source_observation_id, span_fingerprint,
                       clause_index, clause_text, span_start, span_end, raw_term,
                       normalized_term, language, confidence, state, evidence_id,
                       materialized_entity_id, properties_json, created_at, updated_at)
                   VALUES ('legacy-term', 'legacy-source', 'term-fp', 0, 'preview',
                           0, 7, 'preview', 'preview', 'en', .9, 'materialized',
                           'evidence-0', ?, '{}', ?, ?)""",
                (term_id, NOW, NOW),
            )
            connection.execute(
                """INSERT INTO morphology_component_observations_v3(
                       component_observation_id, source_observation_id,
                       span_fingerprint, clause_index, clause_text, span_start,
                       span_end, raw_form, normalized_form, gloss, proposed_kind,
                       kind_confidence, state, qwen_ready, evidence_id,
                       materialized_entity_id, properties_json, created_at, updated_at)
                   VALUES ('legacy-component', 'legacy-source', 'component-fp', 0,
                           'pre', 0, 3, 'pre', 'pre', 'before', 'root', .9,
                           'materialized', 0, 'evidence-0', ?, '{}', ?, ?)""",
                (morpheme_id, NOW, NOW),
            )
            connection.execute(
                """INSERT INTO morphology_formula_links_v3(
                       formula_link_id, source_observation_id, span_fingerprint,
                       clause_index, term_observation_id, component_observation_id,
                       surface_relation, confidence, state, qwen_ready, evidence_id,
                       properties_json, created_at, updated_at)
                   VALUES ('legacy-link', 'legacy-source', 'link-fp', 0,
                           'legacy-term', 'legacy-component', 'exact', .9,
                           'materialized', 0, 'evidence-0', '{}', ?, ?)""",
                (NOW, NOW),
            )
            connection.execute(
                """INSERT INTO relation_assertions(
                       assertion_id, subject_entity_id, source_entity_id,
                       target_entity_id, relation, basis, confidence, status,
                       properties_json, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'has-component', 'book', .9, 'accepted',
                           '{}', 1, ?, ?)""",
                (assertion_id, term_id, term_id, morpheme_id, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO assertion_evidence(assertion_id,evidence_id) VALUES (?, 'evidence-0')",
                (assertion_id,),
            )
            connection.execute(
                """INSERT INTO entity_edges(
                       edge_id, source_entity_id, target_entity_id, relation,
                       basis, confidence, status, properties, created_at, updated_at)
                   VALUES (?, ?, ?, 'has-component', 'book', .9, 'accepted', '{}', ?, ?)""",
                (f"edge-{assertion_id}", term_id, morpheme_id, NOW, NOW),
            )
            connection.execute(
                """INSERT OR REPLACE INTO term_morphemes(
                       term_id,morpheme_id,ordinal,surface,confidence,basis)
                   VALUES (?, ?, 0, 'pre', .9, 'book')""",
                (term_id, morpheme_id),
            )
            connection.commit()
        return assertion_id

    def test_dry_run_cleanup_alias_and_second_apply_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, prefix_id = self.fixture(temp)
            stale = self.add_legacy_formula(store, expected, term_id, root_id)
            duplicate_id = "legacy-prefix-alias"
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES (?, 'morpheme', 'legacy:prefix:pre', 'pre',
                               'accepted', '{}', ?, ?)""",
                    (duplicate_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphemes(entity_id,language,form,normalized,kind,meaning)
                       VALUES (?, 'en', 'pre', 'pre-', 'prefix', '')""",
                    (duplicate_id,),
                )
                review_assertion = "reviewed-prefix"
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES (?, ?, ?, ?, 'has-component', 'reviewed', .99,
                               'accepted', '{}', 1, ?, ?)""",
                    (review_assertion, term_id, term_id, prefix_id, NOW, NOW),
                )
                upsert_relation_support(
                    connection, assertion_id=review_assertion,
                    owner_type="relation-review", owner_id="curator",
                    owner_version="review-1", basis="reviewed", confidence=.99,
                    timestamp=NOW,
                )
                connection.execute(
                    """INSERT OR REPLACE INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,0,'pre',.99,'reviewed')""",
                    (term_id, prefix_id),
                )
                before = tuple(connection.execute(
                    "SELECT value FROM schema_meta WHERE key='relation_graph_revision'"
                ).fetchone())
                connection.commit()
            with closing(store._connect()) as connection:
                dry = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                )
                self.assertTrue(dry["destructive_ready"])
                self.assertIn(stale, dry["stale_parser_assertion_ids"])
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM relation_support_migrations"
                ).fetchone()[0], 0)
                applied = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True, timestamp="2026-09-05T00:01:00Z",
                )
                revision = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='relation_graph_revision'"
                ).fetchone()[0]
                completed_at = connection.execute(
                    "SELECT completed_at FROM relation_support_migrations"
                ).fetchone()[0]
                replay = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True, timestamp="2026-09-05T00:02:00Z",
                )
                self.assertTrue(applied["applied"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id=?", (stale,)
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id='reviewed-prefix'"
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (duplicate_id,)
                ).fetchone()[0], "archived")
                self.assertTrue(replay["already_complete"])
                self.assertFalse(replay["applied"])
                self.assertEqual(connection.execute(
                    "SELECT value FROM schema_meta WHERE key='relation_graph_revision'"
                ).fetchone()[0], revision)
                self.assertEqual(connection.execute(
                    "SELECT completed_at FROM relation_support_migrations"
                ).fetchone()[0], completed_at)
                self.assertNotEqual(before[0], revision)
                connection.execute(
                    "UPDATE entities SET status='archived' WHERE entity_id=?",
                    (term_id,),
                )
                connection.commit()
                drifted = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True, timestamp="2026-09-05T00:03:00Z",
                )
                self.assertTrue(drifted["already_complete"])
                self.assertFalse(drifted["destructive_ready"])
                self.assertFalse(drifted["applied"])
                self.assertIn(
                    "completed migration state drifted",
                    drifted["refusal_reasons"][0],
                )

    def test_equal_count_wrong_scope_and_unproved_claim_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, prefix_id = self.fixture(temp)
            wrong = (expected[0], (expected[1][0], "wrong", expected[1][2]))
            with closing(store._connect()) as connection:
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=wrong, expected_source_records=2,
                )
                self.assertFalse(report["destructive_ready"])
                self.assertTrue(report["missing_observations"])
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES ('unproved', ?, ?, ?, 'has-component', 'book', .8,
                               'accepted', '{}', 1, ?, ?)""",
                    (term_id, term_id, prefix_id, NOW, NOW),
                )
                connection.commit()
                blocked = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertFalse(blocked["applied"])
                self.assertIn("unproved", blocked["unknown_accepted_morphology_assertion_ids"])

    def test_completed_alias_migration_is_read_only_and_resolves_immutable_atomic_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, prefix_id = self.fixture(temp)
            database = Path(temp) / "knowledge.sqlite3"
            alias_id = "artifact-prefix-alias"
            view_id = store.upsert_morpheme(
                "en", "view", "free", "look", status="accepted"
            )
            text_id = store.upsert_morpheme(
                "en", "text", "free", "written words", status="accepted"
            )
            repeat_term_id = store.upsert_term("en", "prepre", status="accepted")
            conflict_term_id = store.upsert_term("en", "pretext", status="accepted")
            payload = {"term_id": term_id, "term": "preview", "parts": [
                {
                    "morpheme_id": alias_id, "ordinal": 0, "surface": "pre",
                    "canonical_form": "pre", "kind": "prefix", "language": "en",
                    "meaning": "before", "basis": "reviewed", "confidence": .9,
                    "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
                },
                {
                    "morpheme_id": view_id, "ordinal": 1, "surface": "view",
                    "canonical_form": "view", "kind": "free", "language": "en",
                    "meaning": "look", "basis": "reviewed", "confidence": .9,
                    "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
                },
            ]}
            repeat_payload = {
                "term_id": repeat_term_id,
                "term": "prepre",
                "parts": [
                    {
                        "morpheme_id": prefix_id, "ordinal": ordinal,
                        "surface": "pre", "canonical_form": "pre-",
                        "kind": "prefix", "language": "en", "meaning": "before",
                        "basis": "reviewed", "confidence": .9,
                        "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
                    }
                    for ordinal in (0, 1)
                ],
            }
            conflict_payload = {
                "term_id": conflict_term_id,
                "term": "pretext",
                "parts": [
                    {
                        "morpheme_id": prefix_id, "ordinal": 0,
                        "surface": "pre", "canonical_form": "pre-",
                        "kind": "prefix", "language": "en", "meaning": "before",
                        "basis": "reviewed", "confidence": .9,
                        "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
                    },
                    {
                        "morpheme_id": text_id, "ordinal": 1,
                        "surface": "text", "canonical_form": "text",
                        "kind": "free", "language": "en", "meaning": "written words",
                        "basis": "reviewed", "confidence": .9,
                        "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
                    },
                ],
            }
            encoded = json.dumps(payload)
            repeat_encoded = json.dumps(repeat_payload)
            conflict_encoded = json.dumps(conflict_payload)
            with closing(store._connect()) as connection:
                self.assertFalse(_complete_atomic_occurrence_projection(
                    connection,
                    {"term_id": term_id, "parts": payload["parts"][:1]},
                ))
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES (?,'morpheme','legacy:prefix:pre','pre','accepted','{}',?,?)""",
                    (alias_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphemes(entity_id,language,form,normalized,kind,meaning)
                       VALUES (?,'en','pre','pre-','prefix','before')""", (alias_id,),
                )
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES ('artifact-alias-claim',?,?,?,'has-component','model',.8,
                               'accepted',?,1,?,?)""",
                    (
                        term_id, term_id, alias_id,
                        json.dumps({"ordinal": 0, "surface": "pre"}), NOW, NOW,
                    ),
                )
                upsert_relation_support(
                    connection, assertion_id="artifact-alias-claim",
                    owner_type="relation-review", owner_id="alias-curator",
                    owner_version="review-1", basis="reviewed", confidence=.9,
                    evidence_ids=("evidence-0",),
                    timestamp=NOW,
                )
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,
                           target_entity_id,relation,basis,confidence,status,
                           properties_json,revision,created_at,updated_at)
                       VALUES ('canonical-prefix-claim',?,?,?,'has-component',
                               'book',.9,'accepted','{}',1,?,?)""",
                    (term_id, term_id, prefix_id, NOW, NOW),
                )
                upsert_relation_support(
                    connection, assertion_id="canonical-prefix-claim",
                    owner_type="relation-review", owner_id="canonical-prefix",
                    owner_version="review-1",
                    basis="book", confidence=.9,
                    evidence_ids=("evidence-0",), timestamp=NOW,
                )
                connection.executemany(
                    """INSERT INTO relation_assertion_slots(
                           assertion_id,slot_key,slot_mode,policy_version,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        (
                            "artifact-alias-claim",
                            "relation-slot:component:preview-ordinal-0",
                            "exclusive", "relation-slot-v2", NOW,
                        ),
                        (
                            "canonical-prefix-claim",
                            "relation-slot:unclassified:canonical-preview",
                            "none", "relation-slot-v2", NOW,
                        ),
                    ),
                )
                for assertion_id, subject_id, target_id, ordinal, surface in (
                    ("artifact-view-claim", term_id, view_id, 1, "view"),
                    ("artifact-repeat-claim", repeat_term_id, prefix_id, 0, "pre"),
                    ("artifact-conflict-prefix", conflict_term_id, prefix_id, 0, "pre"),
                    ("artifact-conflict-text", conflict_term_id, text_id, 1, "text"),
                ):
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,'has-component','reviewed',.9,'accepted',
                                   ?,1,?,?)""",
                        (
                            assertion_id, subject_id, subject_id, target_id,
                            json.dumps({"ordinal": ordinal, "surface": surface}),
                            NOW, NOW,
                        ),
                    )
                    upsert_relation_support(
                        connection, assertion_id=assertion_id,
                        owner_type="relation-review", owner_id=assertion_id,
                        owner_version="review-1", basis="reviewed", confidence=.9,
                        evidence_ids=("evidence-0",), timestamp=NOW,
                    )
                connection.executemany(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,?,?,'reviewed')""",
                    (
                        (term_id, alias_id, 0, "pre", .9),
                        (term_id, view_id, 1, "view", .9),
                        (term_id, prefix_id, 2, "pre", .95),
                        (repeat_term_id, prefix_id, 0, "pre", .9),
                        (repeat_term_id, prefix_id, 1, "pre", .9),
                        (conflict_term_id, prefix_id, 0, "pre", .9),
                        (conflict_term_id, text_id, 1, "text", .9),
                        (conflict_term_id, prefix_id, 2, "pro", .9),
                    ),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('alias-artifact-job','alias-artifact-job','split-morphemes',
                               ?,?,'complete',?,?)""",
                    (term_id, f"term:{term_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('alias-artifact','alias-artifact-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('alias-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (term_id, encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('repeat-artifact-job','repeat-artifact-job',
                               'split-morphemes',?,?,'complete',?,?)""",
                    (repeat_term_id, f"term:{repeat_term_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('repeat-artifact','repeat-artifact-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (repeat_encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('repeat-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (repeat_term_id, repeat_encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('conflict-artifact-job','conflict-artifact-job',
                               'split-morphemes',?,?,'complete',?,?)""",
                    (conflict_term_id, f"term:{conflict_term_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('conflict-artifact','conflict-artifact-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (conflict_encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('conflict-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (conflict_term_id, conflict_encoded, NOW),
                )
                connection.commit()
                applied = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(applied["applied"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (alias_id,),
                ).fetchone()[0], "archived")
                self.assertEqual(tuple(connection.execute(
                    """SELECT target_entity_id,status FROM relation_assertions
                       WHERE assertion_id='artifact-alias-claim'"""
                ).fetchone()), (alias_id, "archived"))
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_supports
                       WHERE assertion_id='canonical-prefix-claim'
                         AND owner_type='relation-review'
                         AND owner_id='alias-curator' AND status='accepted'"""
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    """SELECT slot_mode FROM relation_assertion_slots
                       WHERE assertion_id='artifact-alias-claim'"""
                ).fetchone()[0], "none")
                canonical_slot = connection.execute(
                    """SELECT slot.slot_key,slot.slot_mode,slot.policy_version,
                              a.properties_json
                       FROM relation_assertion_slots slot
                       JOIN relation_assertions a
                         ON a.assertion_id=slot.assertion_id
                       WHERE slot.assertion_id='canonical-prefix-claim'"""
                ).fetchone()
                self.assertTrue(str(canonical_slot[0]).startswith(
                    "relation-slot:component:"
                ))
                self.assertEqual(tuple(canonical_slot[1:3]), (
                    "exclusive", "relation-slot-v2",
                ))
                canonical_properties = json.loads(canonical_slot[3])
                self.assertEqual(canonical_properties["ordinal"], 0)
                self.assertEqual(canonical_properties["surface"], "pre")
                self.assertEqual(canonical_properties["branch_id"], "primary")
                self.assertEqual(json.loads(connection.execute(
                    "SELECT payload FROM job_artifacts WHERE artifact_id='alias-artifact'"
                ).fetchone()[0])["parts"][0]["morpheme_id"], alias_id)
                self.assertEqual([tuple(row) for row in connection.execute(
                    """SELECT ordinal,morpheme_id,surface FROM term_morphemes
                       WHERE term_id=? ORDER BY ordinal""", (term_id,),
                ).fetchall()], [(0, prefix_id, "pre"), (1, view_id, "view")])
                self.assertEqual([tuple(row) for row in connection.execute(
                    """SELECT ordinal,morpheme_id,surface FROM term_morphemes
                       WHERE term_id=? ORDER BY ordinal""", (repeat_term_id,),
                ).fetchall()], [(0, prefix_id, "pre"), (1, prefix_id, "pre")])
                self.assertIn(
                    conflict_term_id,
                    applied["artifact_term_projection_deferred_term_ids"],
                )
                self.assertEqual([tuple(row) for row in connection.execute(
                    """SELECT ordinal,morpheme_id,surface FROM term_morphemes
                       WHERE term_id=? ORDER BY ordinal""", (conflict_term_id,),
                ).fetchall()], [
                    (0, prefix_id, "pre"),
                    (1, text_id, "text"),
                    (2, prefix_id, "pro"),
                ])

            with closing(sqlite3.connect(database)) as connection:
                slots_before_initialise = tuple(connection.execute(
                    """SELECT term_id,morpheme_id,ordinal,surface,confidence,basis
                       FROM term_morphemes WHERE term_id IN (?,?,?)
                       ORDER BY term_id,ordinal""",
                    (term_id, repeat_term_id, conflict_term_id),
                ))
                edges_before_initialise = tuple(connection.execute(
                    """SELECT edge_id,source_entity_id,target_entity_id,relation,
                              basis,confidence,status,properties
                       FROM entity_edges ORDER BY edge_id"""
                ))
            KnowledgeStore(database)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(tuple(connection.execute(
                    """SELECT term_id,morpheme_id,ordinal,surface,confidence,basis
                       FROM term_morphemes WHERE term_id IN (?,?,?)
                       ORDER BY term_id,ordinal""",
                    (term_id, repeat_term_id, conflict_term_id),
                )), slots_before_initialise)
                self.assertEqual(tuple(connection.execute(
                    """SELECT edge_id,source_entity_id,target_entity_id,relation,
                              basis,confidence,status,properties
                       FROM entity_edges ORDER BY edge_id"""
                )), edges_before_initialise)

            with closing(sqlite3.connect(database)) as observer:
                before_version = observer.execute("PRAGMA data_version").fetchone()[0]
                before_content = tuple(observer.iterdump())
                with closing(sqlite3.connect(database)) as connection:
                    post_alias = morphology_v7_finalization_manifest(
                        connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                        expected_sources=expected, expected_source_records=2,
                        migration_key="post-alias-audit",
                    )
                    self.assertEqual(post_alias["atomic_artifact_errors"], [])
                    replay = finalize_morphology_support_migration(
                        connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                        expected_sources=expected, expected_source_records=2,
                    )
                    self.assertEqual(connection.total_changes, 0)
                self.assertTrue(replay["already_complete"])
                self.assertTrue(replay["destructive_ready"])
                self.assertEqual(replay["changes"], {})
                self.assertEqual(observer.execute("PRAGMA data_version").fetchone()[0],
                                 before_version)
                self.assertEqual(tuple(observer.iterdump()), before_content)

                settings = SimpleNamespace(
                    knowledge_db=database,
                    roots_db=Path(temp) / "roots.sqlite3",
                    affixes_db=Path(temp) / "affixes.sqlite3",
                )
                args = SimpleNamespace(
                    knowledge_db=str(database), root_index="", affix_index="",
                    expected_source_records=2, allow_write=False,
                )
                with patch("lkt.cli._settings", return_value=settings), patch(
                    "lkt.relation_support.morphology_expected_sources",
                    return_value=expected,
                ), patch(
                    "lkt.knowledge.KnowledgeStore",
                    side_effect=AssertionError("completed audit initialized KnowledgeStore"),
                ), redirect_stdout(io.StringIO()):
                    self.assertEqual(command_finalize_morphology_v7(args), 0)
                self.assertEqual(observer.execute("PRAGMA data_version").fetchone()[0],
                                 before_version)
                self.assertEqual(tuple(observer.iterdump()), before_content)

            self.assertEqual(store.accept_relation_assertion(
                term_id,
                term_id,
                root_id,
                "has-component",
                basis="model",
                confidence=.99,
                properties={"ordinal": 0, "surface": "pre"},
                context_evidence_ids=("evidence-0",),
                prompt_version="conflict-regression-v1",
                epoch_fingerprint="conflict-regression-epoch",
                owner_type="atomic-split",
                owner_id="conflict-regression",
            ), "")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions a
                       JOIN relation_assertion_slots slot
                         ON slot.assertion_id=a.assertion_id
                       WHERE a.subject_entity_id=? AND a.source_entity_id=?
                         AND a.relation='has-component' AND a.status='accepted'
                         AND slot.slot_mode='exclusive'
                         AND slot.slot_key LIKE 'relation-slot:component:%'
                         AND json_extract(a.properties_json,'$.ordinal')=0""",
                    (term_id, term_id),
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE subject_entity_id=? AND target_entity_id=?
                         AND relation='has-component' AND status='accepted'""",
                    (term_id, root_id),
                ).fetchone()[0], 0)

            with closing(sqlite3.connect(database)) as connection:
                accepted_slot = connection.execute(
                    """SELECT assertion_id,slot_key,slot_mode,policy_version,created_at
                       FROM relation_assertion_slots
                       WHERE assertion_id='canonical-prefix-claim'"""
                ).fetchone()
                connection.execute(
                    """DELETE FROM relation_assertion_slots
                       WHERE assertion_id='canonical-prefix-claim'"""
                )
                connection.commit()
                slot_drifted = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(slot_drifted["already_complete"])
                self.assertFalse(slot_drifted["destructive_ready"])
                self.assertIn(
                    "complete atomic occurrence projection drifted",
                    slot_drifted["refusal_reasons"][0],
                )
                connection.execute(
                    """INSERT INTO relation_assertion_slots(
                           assertion_id,slot_key,slot_mode,policy_version,created_at)
                       VALUES (?,?,?,?,?)""",
                    tuple(accepted_slot),
                )
                connection.execute(
                    "DELETE FROM term_morphemes WHERE term_id=? AND ordinal=1",
                    (repeat_term_id,),
                )
                connection.commit()
                drifted = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(drifted["already_complete"])
                self.assertFalse(drifted["destructive_ready"])
                self.assertFalse(drifted["applied"])
                self.assertIn(
                    "complete atomic occurrence projection drifted",
                    drifted["refusal_reasons"][0],
                )

    def test_atomic_artifact_preserves_exact_legacy_formula_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, _ = self.fixture(temp)
            assertion_id = self.add_legacy_formula(store, expected, term_id, root_id)
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,
                           status,created_at,updated_at)
                       VALUES ('split-job','split-job','split-morphemes',?,?,'complete',?,?)""",
                    (term_id, f"term:{term_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('split-artifact','split-job','accepted-morpheme-split',?,
                               'accepted',?)""",
                    (json.dumps({"term_id": term_id, "parts": [
                        {"morpheme_id": root_id, "ordinal": 0, "surface": "pre",
                         "canonical_form": "pre", "kind": "root", "language": "en",
                         "meaning": "before", "basis": "book", "confidence": .9,
                         "evidence_ids": ["evidence-0"],
                         "context_evidence_ids": []}
                    ], "related_terms": [{"term_id": "draft-related",
                                            "component_ids": []}]}), NOW),
                )
                payload = connection.execute(
                    "SELECT payload FROM job_artifacts WHERE artifact_id='split-artifact'"
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,
                           prompt_version,reason,accepted,payload,created_at)
                       VALUES ('split-revision',?,NULL,'test-model','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (term_id, payload, NOW),
                )
                connection.commit()
                applied = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(applied["applied"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id=?",
                    (assertion_id,),
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_supports
                       WHERE assertion_id=? AND owner_type='atomic-split'
                         AND status='accepted'""", (assertion_id,),
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    """SELECT basis FROM relation_assertion_supports
                       WHERE assertion_id=? AND owner_type='atomic-split'""",
                    (assertion_id,),
                ).fetchone()[0], "model")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM assertion_evidence WHERE assertion_id=?",
                    (assertion_id,),
                ).fetchone()[0], 0)

    def test_observed_source_and_materializable_declaration_are_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE morphology_source_observations_v3
                       SET materialization_state='observed',declaration_count=1
                       WHERE source_observation_id='v7-source-0'"""
                )
                connection.execute(
                    """INSERT INTO morphology_declarations_v3(
                           declaration_id,source_observation_id,span_fingerprint,
                           declaration_type,subject_component_observation_id,
                           object_value,object_normalized_form,language,period_label,
                           confidence,state,qwen_ready,evidence_id,properties_json,
                           created_at,updated_at)
                       VALUES ('terminal-declaration','v7-source-0','decl-fp','source',
                               NULL,'Latin','latin','la','',.95,'materializable',0,
                               'evidence-0','{}',?,?)""",
                    (NOW, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                )
                self.assertTrue(report["destructive_ready"])
                self.assertEqual(report["nonterminal_observations"]["sources"], 0)
                self.assertEqual(report["nonterminal_observations"]["declarations"], 0)

    def test_revision_backed_artifact_restores_missing_claim_and_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, _ = self.fixture(temp)
            payload = {
                "term_id": term_id, "term": "pre", "model": "test-qwen",
                "metrics": {}, "parts": [{
                    "morpheme_id": root_id, "ordinal": 0, "surface": "pre",
                    "canonical_form": "pre", "kind": "root", "language": "en",
                    "meaning": "before", "basis": "model", "confidence": .8,
                    "evidence_ids": [], "context_evidence_ids": [],
                    "normalizations": [],
                }],
            }
            with closing(store._connect()) as connection:
                connection.execute(
                    "UPDATE terms SET text='pre',normalized='pre' WHERE entity_id=?",
                    (term_id,),
                )
                connection.execute(
                    """UPDATE entities SET canonical_key='term:en:word:pre',label='pre'
                       WHERE entity_id=?""",
                    (term_id,),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,
                           model,prompt_version,status,created_at,updated_at)
                       VALUES ('live-split-job','live-split-job','split-morphemes',?,?,
                               'test-qwen','atomic-v1','complete',?,?)""",
                    (term_id, f"term:{term_id}", NOW, NOW),
                )
                encoded = json.dumps(payload)
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('live-split-artifact','live-split-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,
                           prompt_version,reason,accepted,payload,created_at)
                       VALUES ('live-split-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (term_id, encoded, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertFalse(report["atomic_artifact_errors"])
                assertion_id = report["artifact_assertions_to_restore"][0]
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id=?",
                    (assertion_id,),
                ).fetchone()[0], "accepted")
                self.assertEqual(tuple(connection.execute(
                    """SELECT morpheme_id,ordinal,surface FROM term_morphemes
                       WHERE term_id=?""", (term_id,),
                ).fetchone()), (root_id, 0, "pre"))

    def test_revision_only_related_claim_retains_atomic_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, _ = self.fixture(temp)
            derivative_id = store.upsert_term("en", "prefigure")
            payload = {
                "term_id": term_id, "parts": [{
                    "morpheme_id": root_id, "ordinal": 0, "surface": "pre",
                    "canonical_form": "pre", "kind": "root", "language": "en",
                    "meaning": "before", "basis": "model", "confidence": .8,
                    "evidence_ids": [], "context_evidence_ids": [],
                }],
                "related_terms": [{"term_id": derivative_id, "term": "prefigure",
                                   "component_ids": [root_id],
                                   "component_forms": ["pre"],
                                   "component_kinds": ["root"], "note": "related"}],
            }
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES ('revision-share',?,?,?,'shares-component','model',.55,
                               'accepted','{}',1,?,?)""",
                    (term_id, derivative_id, root_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,
                           prompt_version,reason,accepted,payload,created_at)
                       VALUES ('related-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (term_id, json.dumps(payload), NOW),
                )
                newer_payload = {"term_id": term_id, "parts": payload["parts"]}
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,
                           prompt_version,reason,accepted,payload,created_at)
                       VALUES ('zz-related-revision',?,'related-revision','test-qwen',
                               'atomic-v2','atomic morpheme split',1,?,?)""",
                    (term_id, json.dumps(newer_payload), NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id='revision-share'"
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertion_supports
                       WHERE assertion_id='revision-share' AND owner_type='atomic-split'
                         AND status='accepted'"""
                ).fetchone()[0], 1)

    def test_automatic_events_allow_orphan_and_historical_parser_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, prefix_id = self.fixture(temp)
            derivative_id = store.upsert_term("en", "pretest")
            stale = self.add_legacy_formula(store, expected, term_id, root_id)
            with closing(store._connect()) as connection:
                claims = (
                    ("orphan-has", term_id, term_id, prefix_id, "has-component"),
                    ("orphan-share", term_id, derivative_id, prefix_id, "shares-component"),
                )
                for assertion_id, subject, source, target, relation in claims:
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,?,'model',.7,'accepted','{}',1,?,?)""",
                        (assertion_id, subject, source, target, relation, NOW, NOW),
                    )
                    snapshot = {
                        "assertion_id": assertion_id, "subject_entity_id": subject,
                        "source_entity_id": source, "target_entity_id": target,
                        "relation": relation, "basis": "model", "confidence": .7,
                        "status": "accepted", "properties": {}, "revision": 1,
                        "created_at": NOW, "updated_at": NOW,
                        "evidence_ids": [], "context_evidence_ids": [],
                    }
                    connection.execute(
                        """INSERT INTO relation_assertion_events(
                               event_id,decision_id,assertion_id,action,before_json,
                               after_json,evidence_ids_json,context_evidence_ids_json,
                               input_fingerprint,output_fingerprint,job_id,reason,
                               validator_version,created_at)
                           VALUES (?,?,?,'baseline','{}',?,'[]','[]','','',NULL,?,?,?)""",
                        (f"baseline-{assertion_id}", f"decision-{assertion_id}",
                         assertion_id, json.dumps(snapshot),
                         "schema-v8 current-state baseline; historical revisions are not reconstructed",
                         "relation-assertion-decision-v1", NOW),
                    )
                    if assertion_id == "orphan-has":
                        connection.execute(
                            """INSERT INTO relation_assertion_events(
                                   event_id,decision_id,assertion_id,action,before_json,
                                   after_json,evidence_ids_json,context_evidence_ids_json,
                                   input_fingerprint,output_fingerprint,job_id,reason,
                                   validator_version,created_at)
                               VALUES ('keep-orphan','keep-decision',?,'keep',?,?,'[]',
                                       '[]','','',NULL,?,?,?)""",
                            (assertion_id, json.dumps(snapshot), json.dumps(snapshot),
                             "proposal did not materially strengthen the incumbent",
                             "relation-assertion-decision-v1", NOW),
                        )
                before = {"assertion_id": stale, "subject_entity_id": term_id,
                          "source_entity_id": term_id, "target_entity_id": root_id,
                          "relation": "has-component", "status": "accepted"}
                after = {**before, "status": "archived"}
                connection.execute(
                    """INSERT INTO relation_assertion_events(
                           event_id,decision_id,assertion_id,action,before_json,
                           after_json,evidence_ids_json,context_evidence_ids_json,
                           input_fingerprint,output_fingerprint,job_id,reason,
                           validator_version,created_at)
                       VALUES ('archive-stale','archive-decision',?,'archive',?,?,'[]',
                               '[]','','',NULL,?,?,?)""",
                    (stale, json.dumps(before), json.dumps(after),
                     "accepted assertion archived by store reconciliation",
                     "relation-assertion-archive-v1", NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(set(report["orphan_model_assertion_ids"]),
                                 {"orphan-has", "orphan-share"})
                self.assertIn(stale, report["stale_parser_assertion_ids"])
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE assertion_id IN ('orphan-has','orphan-share',?)
                         AND status='archived'""", (stale,),
                ).fetchone()[0], 3)

    def test_alias_with_unproved_term_occurrence_is_deferred_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            canonical_id = store.upsert_morpheme(
                "en", "-zz", "suffix", "suffix marker", status="accepted"
            )
            term_id = store.upsert_term("en", "fizz", status="accepted")
            alias_id = "unproved-suffix-alias"
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES (?,'morpheme','legacy:suffix:zz','zz','accepted','{}',?,?)""",
                    (alias_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphemes(
                           entity_id,language,form,normalized,kind,meaning)
                       VALUES (?,'en','zz','-zz','suffix','suffix marker')""",
                    (alias_id,),
                )
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,
                           target_entity_id,relation,basis,confidence,status,
                           properties_json,revision,created_at,updated_at)
                       VALUES ('unproved-suffix-claim',?,?,?,'has-component',
                               'reviewed',.9,'accepted',?,1,?,?)""",
                    (
                        term_id, term_id, alias_id,
                        json.dumps({"ordinal": 1, "surface": "zz"}), NOW, NOW,
                    ),
                )
                upsert_relation_support(
                    connection, assertion_id="unproved-suffix-claim",
                    owner_type="relation-review", owner_id="unproved-review",
                    owner_version="review-1", basis="reviewed", confidence=.9,
                    evidence_ids=("evidence-0",), timestamp=NOW,
                )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,1,'zz',.9,'reviewed')""",
                    (term_id, alias_id),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                group = next(
                    item for item in report["alias_manifest"]["groups"]
                    if item["kind"] == "suffix" and item["base"] == "zz"
                )
                self.assertTrue(group["deferred"])
                self.assertEqual(
                    group["deferred_reason"],
                    "unproved-complete-term-occurrence",
                )
                self.assertEqual(group["unproved_term_morpheme_ids"], [term_id])
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (alias_id,),
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT morpheme_id FROM term_morphemes
                       WHERE term_id=? AND ordinal=1""", (term_id,),
                ).fetchone()[0], alias_id)
                self.assertNotEqual(alias_id, canonical_id)

    def test_safe_alias_rewires_canonical_pointers_while_active_job_defers_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            prefix_id = store.upsert_morpheme("en", "pre-", "prefix")
            root_id = store.upsert_morpheme("en", "spect", "root")
            blocked_term_id = store.upsert_term("en", "preform")
            with closing(store._connect()) as connection:
                for entity_id, key, form, normalized, kind in (
                    ("blocked-prefix-alias", "legacy:prefix:pre", "pre", "pre-", "prefix"),
                    ("safe-root-alias", "legacy:root:spect", "spect", "spect-", "root"),
                ):
                    connection.execute(
                        """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                               status,payload,created_at,updated_at)
                           VALUES (?,'morpheme',?,?,'accepted','{}',?,?)""",
                        (entity_id, key, form, NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO morphemes(entity_id,language,form,normalized,kind,meaning)
                           VALUES (?,'en',?,?,?,'')""",
                        (entity_id, form, normalized, kind),
                    )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,
                           status,created_at,updated_at)
                       VALUES ('active-alias-job','active-alias-job','split-morphemes',
                               'blocked-prefix-alias','morpheme:blocked','queued',?,?)""",
                    (NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES ('blocked-alias-claim',?,?,?,'has-component','model',.8,
                               'accepted','{}',1,?,?)""",
                    (blocked_term_id, blocked_term_id, "blocked-prefix-alias", NOW, NOW),
                )
                upsert_relation_support(
                    connection, assertion_id="blocked-alias-claim",
                    owner_type="relation-review", owner_id="blocked-curator",
                    owner_version="review-1", basis="reviewed", confidence=.9,
                    timestamp=NOW,
                )
                blocked_payload = json.dumps({
                    "term_id": blocked_term_id,
                    "parts": [{
                        "morpheme_id": "blocked-prefix-alias", "ordinal": 0,
                        "surface": "pre", "canonical_form": "pre", "kind": "prefix",
                        "language": "en", "meaning": "before", "basis": "model",
                        "confidence": .8, "evidence_ids": [],
                        "context_evidence_ids": [],
                    }],
                })
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('blocked-artifact-job','blocked-artifact-job',
                               'split-morphemes',?,?,'complete',?,?)""",
                    (blocked_term_id, f"term:{blocked_term_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('blocked-artifact','blocked-artifact-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (blocked_payload, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('blocked-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (blocked_term_id, blocked_payload, NOW),
                )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,'blocked-prefix-alias',0,'pre',.8,'model')""",
                    (blocked_term_id,),
                )
                connection.execute(
                    """INSERT INTO entities(entity_id,entity_type,canonical_key,label,
                           status,payload,created_at,updated_at)
                       VALUES ('alias-meaning','meaning','meaning:alias','alias meaning',
                               'accepted','{}',?,?)""", (NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO meanings(entity_id,subject_entity_id,term_id,language,
                           definition) VALUES ('alias-meaning','safe-root-alias',NULL,'en','legacy')"""
                )
                connection.execute(
                    """INSERT INTO morphology_source_observations_v3(
                           source_observation_id,corpus_id,source_entry_id,source_hash,
                           parser_version,source_kind,evidence_id,headword,
                           headword_normalized,source_page,locator,raw_excerpt,
                           raw_fingerprint,parse_state,materialization_state,
                           eligibility_reason,term_count,component_count,
                           formula_link_count,declaration_count,qwen_ready_count,
                           created_at,updated_at)
                       VALUES ('legacy-alias-source','legacy','alias','legacy-hash','v6',
                               'root','evidence-0','SPECT','spect',0,'','','alias-fp',
                               'parsed','materialized','',0,1,0,0,0,?,?)""",
                    (NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_component_observations_v3(
                           component_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_form,
                           normalized_form,gloss,proposed_kind,kind_confidence,state,
                           qwen_ready,evidence_id,materialized_entity_id,properties_json,
                           created_at,updated_at)
                       VALUES ('alias-component','legacy-alias-source','alias-component-fp',
                               0,'spect',0,5,'spect','spect','','root',.9,'materialized',
                               0,'evidence-0','safe-root-alias','{}',?,?)""",
                    (NOW, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(report["changes"]["alias_groups_deferred"], 1)
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id='blocked-prefix-alias'"
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT target_entity_id FROM relation_assertions
                       WHERE assertion_id='blocked-alias-claim'"""
                ).fetchone()[0], "blocked-prefix-alias")
                self.assertEqual(connection.execute(
                    """SELECT morpheme_id FROM term_morphemes
                       WHERE term_id=? AND ordinal=0""", (blocked_term_id,),
                ).fetchone()[0], "blocked-prefix-alias")
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id='safe-root-alias'"
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    "SELECT subject_entity_id FROM meanings WHERE entity_id='alias-meaning'"
                ).fetchone()[0], root_id)
                self.assertEqual(connection.execute(
                    """SELECT materialized_entity_id
                       FROM morphology_component_observations_v3
                       WHERE component_observation_id='alias-component'"""
                ).fetchone()[0], root_id)
                self.assertNotEqual(prefix_id, "blocked-prefix-alias")

    def test_related_term_projection_does_not_promote_or_preserve_draft_decomposition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, host_id, _, prefix_id = self.fixture(temp)
            draft_id = store.upsert_term("en", "preform", status="draft")
            payload = {
                "term_id": host_id, "term": "preview",
                "parts": [{
                    "morpheme_id": prefix_id, "ordinal": 0, "surface": "pre",
                    "canonical_form": "pre-", "kind": "prefix", "language": "en",
                    "meaning": "before", "basis": "model", "confidence": .8,
                    "evidence_ids": [], "context_evidence_ids": [],
                }],
                "related_terms": [{
                    "term_id": draft_id, "term": "preform",
                    "component_ids": [prefix_id], "note": "suggested derivative",
                }],
            }
            encoded = json.dumps(payload)
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO relation_assertions(
                           assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                           relation,basis,confidence,status,properties_json,revision,
                           created_at,updated_at)
                       VALUES ('draft-related-claim',?,?,?,'has-component','model',.7,
                               'accepted','{}',1,?,?)""",
                    (draft_id, draft_id, prefix_id, NOW, NOW),
                )
                upsert_relation_support(
                    connection, assertion_id="draft-related-claim",
                    owner_type="atomic-split", owner_id=draft_id,
                    owner_version="legacy-related", basis="model", confidence=.7,
                    timestamp=NOW,
                )
                connection.execute(
                    """INSERT INTO entity_edges(
                           edge_id,source_entity_id,target_entity_id,relation,basis,
                           confidence,status,properties,created_at,updated_at)
                       VALUES ('draft-related-edge',?,?,'has-component','model',.7,
                               'accepted','{}',?,?)""",
                    (draft_id, prefix_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,0,'pre',.7,'model')""", (draft_id, prefix_id),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('related-host-job','related-host-job','split-morphemes',
                               ?,?,'complete',?,?)""",
                    (host_id, f"term:{host_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('related-host-artifact','related-host-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('related-host-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (host_id, encoded, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertIn("draft-related-claim",
                              report["invalid_endpoint_assertion_ids"])
                self.assertNotIn(draft_id, report["draft_term_promotions"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id='draft-related-claim'"
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    "SELECT status FROM entity_edges WHERE edge_id='draft-related-edge'"
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM term_morphemes WHERE term_id=?", (draft_id,),
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (draft_id,),
                ).fetchone()[0], "archived")
                self.assertEqual(json.loads(connection.execute(
                    "SELECT payload FROM job_artifacts WHERE artifact_id='related-host-artifact'"
                ).fetchone()[0])["related_terms"][0]["term_id"], draft_id)

    def test_direct_accepted_split_promotes_exact_draft_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, prefix_id = self.fixture(temp)
            draft_id = store.upsert_term("en", "preform", status="draft")
            form_id = store.upsert_morpheme(
                "en", "form", "free", "shape", status="accepted"
            )
            payload = {
                "term_id": draft_id, "term": "preform",
                "parts": [
                    {
                        "morpheme_id": prefix_id, "ordinal": 0, "surface": "pre",
                        "canonical_form": "pre-", "kind": "prefix", "language": "en",
                        "meaning": "before", "basis": "model", "confidence": .8,
                        "evidence_ids": [], "context_evidence_ids": [],
                    },
                    {
                        "morpheme_id": form_id, "ordinal": 1, "surface": "form",
                        "canonical_form": "form", "kind": "free", "language": "en",
                        "meaning": "shape", "basis": "model", "confidence": .8,
                        "evidence_ids": [], "context_evidence_ids": [],
                    },
                ],
            }
            encoded = json.dumps(payload)
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,status,
                           created_at,updated_at)
                       VALUES ('direct-draft-job','direct-draft-job','split-morphemes',
                               ?,?,'complete',?,?)""",
                    (draft_id, f"term:{draft_id}", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('direct-draft-artifact','direct-draft-job',
                               'accepted-morpheme-split',?,'accepted',?)""",
                    (encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('direct-draft-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (draft_id, encoded, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertIn(draft_id, report["draft_term_promotions"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM entities WHERE entity_id=?", (draft_id,),
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE subject_entity_id=? AND source_entity_id=?
                         AND target_entity_id=? AND relation='has-component'
                         AND status='accepted'""", (draft_id, draft_id, prefix_id),
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    """SELECT morpheme_id FROM term_morphemes
                       WHERE term_id=? AND ordinal=0""", (draft_id,),
                ).fetchone()[0], prefix_id)

    def test_current_parser_same_form_role_conflict_archives_legacy_atomic_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, prefix_id = self.fixture(temp)
            payload = {"term_id": term_id, "parts": [{
                "morpheme_id": prefix_id, "ordinal": 0, "surface": "pre",
                "canonical_form": "pre-", "kind": "prefix", "language": "en",
                "meaning": "before", "basis": "book", "confidence": .9,
                "evidence_ids": ["evidence-0"], "context_evidence_ids": [],
            }]}
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE morphology_source_observations_v3
                       SET materialization_state='materialized',term_count=1,
                           component_count=1,formula_link_count=1
                       WHERE source_observation_id='v7-source-0'"""
                )
                connection.execute(
                    """INSERT INTO morphology_term_observations_v3(
                           term_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_term,
                           normalized_term,language,confidence,state,evidence_id,
                           materialized_entity_id,properties_json,created_at,updated_at)
                       VALUES ('v7-term','v7-source-0','v7-term-fp',0,'preview',0,7,
                               'preview','preview','en',.9,'materialized','evidence-0',
                               ?,'{}',?,?)""", (term_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_component_observations_v3(
                           component_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_form,
                           normalized_form,gloss,proposed_kind,kind_confidence,state,
                           qwen_ready,evidence_id,materialized_entity_id,properties_json,
                           created_at,updated_at)
                       VALUES ('v7-root','v7-source-0','v7-root-fp',0,'pre',0,3,
                               'pre','pre','before','root',.95,'materialized',0,
                               'evidence-0',?,'{}',?,?)""", (root_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_formula_links_v3(
                           formula_link_id,source_observation_id,span_fingerprint,
                           clause_index,term_observation_id,component_observation_id,
                           surface_relation,confidence,state,qwen_ready,evidence_id,
                           properties_json,created_at,updated_at)
                       VALUES ('v7-link','v7-source-0','v7-link-fp',0,'v7-term','v7-root',
                               'exact',.9,'materialized',0,'evidence-0','{}',?,?)""",
                    (NOW, NOW),
                )
                for assertion_id, target, basis in (
                    ("current-root", root_id, "book"),
                    ("legacy-atomic-prefix", prefix_id, "book"),
                ):
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,target_entity_id,
                               relation,basis,confidence,status,properties_json,revision,
                               created_at,updated_at)
                           VALUES (?,?,?,?,'has-component',?,.9,'accepted','{}',1,?,?)""",
                        (assertion_id, term_id, term_id, target, basis, NOW, NOW),
                    )
                    connection.execute(
                        "INSERT INTO assertion_evidence(assertion_id,evidence_id) VALUES (?,'evidence-0')",
                        (assertion_id,),
                    )
                replace_relation_support_scope(
                    connection, owner_type="morphology-parser", owner_id="roots:r1",
                    owner_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    supports=(RelationSupportSpec(
                        "current-root", "book", .9, {}, ("evidence-0",)
                    ),), timestamp=NOW,
                )
                connection.execute(
                    """INSERT OR REPLACE INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,0,'pre',.9,'book')""",
                    (term_id, root_id),
                )
                connection.execute(
                    """INSERT INTO preparation_jobs(
                           job_id,job_key,job_type,subject_entity_id,subject_key,
                           status,created_at,updated_at)
                       VALUES ('conflict-job','conflict-job','split-morphemes',?,?,
                               'complete',?,?)""",
                    (term_id, f"term:{term_id}", NOW, NOW),
                )
                encoded = json.dumps(payload)
                connection.execute(
                    """INSERT INTO job_artifacts(
                           artifact_id,job_id,stage,payload,validation_state,created_at)
                       VALUES ('conflict-artifact','conflict-job','accepted-morpheme-split',
                               ?,'accepted',?)""", (encoded, NOW),
                )
                connection.execute(
                    """INSERT INTO entity_revisions(
                           revision_id,entity_id,previous_revision_id,model,prompt_version,
                           reason,accepted,payload,created_at)
                       VALUES ('conflict-revision',?,NULL,'test-qwen','atomic-v1',
                               'atomic morpheme split',1,?,?)""",
                    (term_id, encoded, NOW),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(report["atomic_role_conflict_assertion_ids"],
                                 ["legacy-atomic-prefix"])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id='legacy-atomic-prefix'"
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id='current-root'"
                ).fetchone()[0], "accepted")

    def test_exact_v7_book_occurrence_retires_only_model_role_and_projects_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            term_id = store.upsert_term("en", "resolve")
            prefix_id = store.upsert_morpheme("en", "re-", "prefix")
            suffix_id = store.upsert_morpheme("en", "-re", "suffix")
            prefix_properties = json.dumps({"ordinal": 1, "surface": "re"})
            suffix_properties = json.dumps({"ordinal": 0, "surface": "re"})
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE morphology_source_observations_v3
                       SET materialization_state='materialized',term_count=1,
                           component_count=1,formula_link_count=1
                       WHERE source_observation_id='v7-source-0'"""
                )
                connection.execute(
                    """INSERT INTO morphology_term_observations_v3(
                           term_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_term,
                           normalized_term,language,confidence,state,evidence_id,
                           materialized_entity_id,properties_json,created_at,updated_at)
                       VALUES ('resolve-term','v7-source-0','resolve-term-fp',0,'resolve',
                               0,7,'resolve','resolve','en',.95,'materialized',
                               'evidence-0',?,'{}',?,?)""", (term_id, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_component_observations_v3(
                           component_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_form,
                           normalized_form,gloss,proposed_kind,kind_confidence,state,
                           qwen_ready,evidence_id,materialized_entity_id,properties_json,
                           created_at,updated_at)
                       VALUES ('resolve-prefix','v7-source-0','resolve-prefix-fp',0,'re',
                               0,2,'re-','re','again','prefix',.95,'materialized',0,
                               'evidence-0',?,?,?,?)""",
                    (prefix_id, prefix_properties, NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_formula_links_v3(
                           formula_link_id,source_observation_id,span_fingerprint,
                           clause_index,term_observation_id,component_observation_id,
                           surface_relation,confidence,state,qwen_ready,evidence_id,
                           properties_json,created_at,updated_at)
                       VALUES ('resolve-link','v7-source-0','resolve-link-fp',0,
                               'resolve-term','resolve-prefix','exact',.95,'materialized',0,
                               'evidence-0',?,?,?)""", (prefix_properties, NOW, NOW),
                )
                for assertion_id, target_id, basis, assertion_properties in (
                    ("resolve-prefix-book", prefix_id, "book", prefix_properties),
                    ("resolve-suffix-model", suffix_id, "model", suffix_properties),
                ):
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,'has-component',?,.9,'accepted',?,1,?,?)""",
                        (assertion_id, term_id, term_id, target_id, basis,
                         assertion_properties, NOW, NOW),
                    )
                connection.execute(
                    """INSERT INTO assertion_evidence(assertion_id,evidence_id)
                       VALUES ('resolve-prefix-book','evidence-0')"""
                )
                replace_relation_support_scope(
                    connection, owner_type="morphology-parser", owner_id="roots:r1",
                    owner_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    supports=(RelationSupportSpec(
                        "resolve-prefix-book", "book", .95,
                        {"ordinal": 1, "surface": "re"}, ("evidence-0",),
                    ),), timestamp=NOW,
                )
                upsert_relation_support(
                    connection, assertion_id="resolve-suffix-model",
                    owner_type="atomic-split", owner_id=term_id,
                    owner_version="legacy-model", basis="model", confidence=.9,
                    properties={"ordinal": 0, "surface": "re"}, timestamp=NOW,
                )
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,0,'re',.9,'model')""", (term_id, suffix_id),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(report["role_arbitration_retired_assertion_ids"],
                                 ["resolve-suffix-model"])
                self.assertEqual(report["role_arbitration_deferred"], [])
                self.assertEqual(connection.execute(
                    """SELECT status FROM relation_assertions
                       WHERE assertion_id='resolve-suffix-model'"""
                ).fetchone()[0], "archived")
                self.assertEqual(connection.execute(
                    """SELECT status FROM relation_assertions
                       WHERE assertion_id='resolve-prefix-book'"""
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    """SELECT morpheme_id FROM term_morphemes
                       WHERE term_id=? AND ordinal=1""", (term_id,),
                ).fetchone()[0], prefix_id)

    def test_v7_allomorph_surfaces_align_model_roles_to_exact_word_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            cases = (
                ("avert", "ab-", "-ab", "a"),
                ("append", "ad-", "-ad", "ap"),
                ("arrogant", "ad-", "-ad", "ar"),
            )
            specs = []
            model_assertions = []
            expected_prefixes = {}
            prepared = []
            for term, prefix, suffix, surface in cases:
                term_id = store.upsert_term("en", term)
                prefix_id = store.upsert_morpheme("en", prefix, "prefix")
                suffix_id = store.upsert_morpheme("en", suffix, "suffix")
                prepared.append((term, surface, term_id, prefix_id, suffix_id, prefix))
            with closing(store._connect()) as connection:
                for index, (
                    term, surface, term_id, prefix_id, suffix_id, prefix
                ) in enumerate(prepared):
                    expected_prefixes[term_id] = prefix_id
                    typed_id = f"allomorph-book-{index}"
                    model_id = f"allomorph-model-{index}"
                    model_assertions.append(model_id)
                    typed_properties = {"ordinal": 1, "surface": surface}
                    model_properties = {"ordinal": 0, "surface": prefix.strip("-")}
                    connection.execute(
                        """INSERT INTO morphology_term_observations_v3(
                               term_observation_id,source_observation_id,span_fingerprint,
                               clause_index,clause_text,span_start,span_end,raw_term,
                               normalized_term,language,confidence,state,evidence_id,
                               materialized_entity_id,properties_json,created_at,updated_at)
                           VALUES (?,?,?, ?,?,0,?, ?,?,'en',.95,'materialized',
                                   'evidence-0',?,'{}',?,?)""",
                        (f"allomorph-term-{index}", "v7-source-0", f"term-fp-{index}",
                         index, term, len(term), term, term, term_id, NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO morphology_component_observations_v3(
                               component_observation_id,source_observation_id,
                               span_fingerprint,clause_index,clause_text,span_start,
                               span_end,raw_form,normalized_form,gloss,proposed_kind,
                               kind_confidence,state,qwen_ready,evidence_id,
                               materialized_entity_id,properties_json,created_at,updated_at)
                           VALUES (?,?,?, ?,?,0,?, ?,?,'','prefix',.95,'materialized',0,
                                   'evidence-0',?,?,?,?)""",
                        (f"allomorph-component-{index}", "v7-source-0",
                         f"component-fp-{index}", index, surface, len(surface), surface,
                         prefix, prefix_id, json.dumps(typed_properties), NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO morphology_formula_links_v3(
                               formula_link_id,source_observation_id,span_fingerprint,
                               clause_index,term_observation_id,component_observation_id,
                               surface_relation,confidence,state,qwen_ready,evidence_id,
                               properties_json,created_at,updated_at)
                           VALUES (?,?,?, ?,?,?,'exact',.95,'materialized',0,
                                   'evidence-0',?,?,?)""",
                        (f"allomorph-link-{index}", "v7-source-0", f"link-fp-{index}",
                         index, f"allomorph-term-{index}",
                         f"allomorph-component-{index}",
                         json.dumps({**typed_properties, "matching_parts": [surface]}),
                         NOW, NOW),
                    )
                    for assertion_id, target, basis, properties in (
                        (typed_id, prefix_id, "book", typed_properties),
                        (model_id, suffix_id, "model", model_properties),
                    ):
                        connection.execute(
                            """INSERT INTO relation_assertions(
                                   assertion_id,subject_entity_id,source_entity_id,
                                   target_entity_id,relation,basis,confidence,status,
                                   properties_json,revision,created_at,updated_at)
                               VALUES (?,?,?,?,'has-component',?,.9,'accepted',?,1,?,?)""",
                            (assertion_id, term_id, term_id, target, basis,
                             json.dumps(properties), NOW, NOW),
                        )
                    connection.execute(
                        "INSERT INTO assertion_evidence VALUES (?,'evidence-0')", (typed_id,),
                    )
                    specs.append(RelationSupportSpec(
                        typed_id, "book", .95, typed_properties, ("evidence-0",)
                    ))
                    upsert_relation_support(
                        connection, assertion_id=model_id, owner_type="atomic-split",
                        owner_id=term_id, owner_version="legacy-model", basis="model",
                        confidence=.9, properties=model_properties, timestamp=NOW,
                    )
                    connection.execute(
                        """INSERT INTO term_morphemes(
                               term_id,morpheme_id,ordinal,surface,confidence,basis)
                           VALUES (?,?,0,?,.9,'model')""", (term_id, suffix_id, surface),
                    )
                connection.execute(
                    """UPDATE morphology_source_observations_v3
                       SET materialization_state='materialized',term_count=3,
                           component_count=3,formula_link_count=3
                       WHERE source_observation_id='v7-source-0'"""
                )
                replace_relation_support_scope(
                    connection, owner_type="morphology-parser", owner_id="roots:r1",
                    owner_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    supports=tuple(specs), timestamp=NOW,
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(report["role_arbitration_retired_assertion_ids"],
                                 sorted(model_assertions))
                self.assertEqual(report["role_arbitration_blocking"], [])
                for term_id, prefix_id in expected_prefixes.items():
                    self.assertEqual(connection.execute(
                        """SELECT morpheme_id FROM term_morphemes
                           WHERE term_id=? AND ordinal=1""", (term_id,),
                    ).fetchone()[0], prefix_id)

    def test_mixed_role_review_deferrals_are_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, _, _, _ = self.fixture(temp)
            term_id = store.upsert_term("en", "adad")
            prefix_id = store.upsert_morpheme("en", "ad-", "prefix")
            suffix_id = store.upsert_morpheme("en", "-ad", "suffix")
            abrasive_id = store.upsert_term("en", "abrasive")
            rad_prefix_id = store.upsert_morpheme("en", "rad-", "prefix")
            rad_suffix_id = store.upsert_morpheme("en", "-rad", "suffix")
            unmapped = tuple(
                (
                    term,
                    form,
                    store.upsert_term("en", term),
                    store.upsert_morpheme("en", form, "prefix"),
                )
                for term, form in (
                    ("banana", "pro-"),
                    ("cobalt", "trans-"),
                    ("yellow", "sub-"),
                )
            )
            with closing(store._connect()) as connection:
                for ordinal, assertion_id, target, kind in (
                    (0, "repeat-prefix-model", prefix_id, "prefix"),
                    (1, "repeat-suffix-model", suffix_id, "suffix"),
                ):
                    properties = {"ordinal": ordinal, "surface": "ad"}
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,'has-component','model',.7,'accepted',?,1,?,?)""",
                        (assertion_id, term_id, term_id, target,
                         json.dumps(properties), NOW, NOW),
                    )
                    upsert_relation_support(
                        connection, assertion_id=assertion_id,
                        owner_type="atomic-split", owner_id=f"{term_id}:{kind}",
                        owner_version="model-v1", basis="model", confidence=.7,
                        properties=properties, timestamp=NOW,
                    )
                    connection.execute(
                        """INSERT INTO term_morphemes(
                               term_id,morpheme_id,ordinal,surface,confidence,basis)
                           VALUES (?,?,?,'ad',.7,'model')""", (term_id, target, ordinal),
                    )
                connection.execute(
                    """UPDATE morphology_source_observations_v3
                       SET materialization_state='materialized',term_count=4,
                           component_count=4,formula_link_count=4
                       WHERE source_observation_id='v7-source-0'"""
                )
                connection.execute(
                    """INSERT INTO morphology_term_observations_v3(
                           term_observation_id,source_observation_id,span_fingerprint,
                           clause_index,clause_text,span_start,span_end,raw_term,
                           normalized_term,language,confidence,state,evidence_id,
                           materialized_entity_id,properties_json,created_at,updated_at)
                       VALUES ('abrasive-term','v7-source-0','abrasive-term-fp',0,
                               'abrasive',0,8,'abrasive','abrasive','en',.95,
                               'materialized','evidence-0',?,'{}',?,?)""",
                    (abrasive_id, NOW, NOW),
                )
                typed_properties = {"ordinal": 1, "surface": "rad"}
                connection.execute(
                    """INSERT INTO morphology_component_observations_v3(
                           component_observation_id,source_observation_id,
                           span_fingerprint,clause_index,clause_text,span_start,
                           span_end,raw_form,normalized_form,gloss,proposed_kind,
                           kind_confidence,state,qwen_ready,evidence_id,
                           materialized_entity_id,properties_json,created_at,updated_at)
                       VALUES ('abrasive-rad','v7-source-0','abrasive-rad-fp',0,
                               'rad',0,3,'rad-','rad','','prefix',.95,
                               'materialized',0,'evidence-0',?,?,?,?)""",
                    (rad_prefix_id, json.dumps(typed_properties), NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO morphology_formula_links_v3(
                           formula_link_id,source_observation_id,span_fingerprint,
                           clause_index,term_observation_id,component_observation_id,
                           surface_relation,confidence,state,qwen_ready,evidence_id,
                           properties_json,created_at,updated_at)
                       VALUES ('abrasive-link','v7-source-0','abrasive-link-fp',0,
                               'abrasive-term','abrasive-rad','exact',.95,
                               'materialized',0,'evidence-0',?,?,?)""",
                    (json.dumps(typed_properties), NOW, NOW),
                )
                for assertion_id, target, basis, properties in (
                    ("abrasive-prefix-book", rad_prefix_id, "book", typed_properties),
                    ("abrasive-suffix-model", rad_suffix_id, "model",
                     {"ordinal": 0, "surface": "rad"}),
                ):
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,'has-component',?,.9,'accepted',?,1,?,?)""",
                        (assertion_id, abrasive_id, abrasive_id, target, basis,
                         json.dumps(properties), NOW, NOW),
                    )
                connection.execute(
                    "INSERT INTO assertion_evidence VALUES "
                    "('abrasive-prefix-book','evidence-0')"
                )
                parser_specs = [RelationSupportSpec(
                    "abrasive-prefix-book", "book", .95, typed_properties,
                    ("evidence-0",),
                )]
                for index, (term, form, term_entity, morpheme_entity) in enumerate(
                    unmapped, start=1
                ):
                    base = form.strip("-")
                    assertion_id = f"unmapped-book-{index}"
                    properties = {"ordinal": 1, "surface": base}
                    connection.execute(
                        """INSERT INTO morphology_term_observations_v3(
                               term_observation_id,source_observation_id,span_fingerprint,
                               clause_index,clause_text,span_start,span_end,raw_term,
                               normalized_term,language,confidence,state,evidence_id,
                               materialized_entity_id,properties_json,created_at,updated_at)
                           VALUES (?, 'v7-source-0', ?, ?, ?,0,?,?,?,'en',.95,
                                   'materialized','evidence-0',?,'{}',?,?)""",
                        (f"unmapped-term-{index}", f"unmapped-term-fp-{index}", index,
                         term, len(term), term, term, term_entity, NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO morphology_component_observations_v3(
                               component_observation_id,source_observation_id,
                               span_fingerprint,clause_index,clause_text,span_start,
                               span_end,raw_form,normalized_form,gloss,proposed_kind,
                               kind_confidence,state,qwen_ready,evidence_id,
                               materialized_entity_id,properties_json,created_at,updated_at)
                           VALUES (?, 'v7-source-0', ?, ?, ?,0,?,?,?,'','prefix',.95,
                                   'materialized',0,'evidence-0',?,?,?,?)""",
                        (f"unmapped-component-{index}",
                         f"unmapped-component-fp-{index}", index, base, len(base),
                         form, base, morpheme_entity, json.dumps(properties), NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO morphology_formula_links_v3(
                               formula_link_id,source_observation_id,span_fingerprint,
                               clause_index,term_observation_id,component_observation_id,
                               surface_relation,confidence,state,qwen_ready,evidence_id,
                               properties_json,created_at,updated_at)
                           VALUES (?, 'v7-source-0', ?, ?, ?,?,'exact',.95,
                                   'materialized',0,'evidence-0',?,?,?)""",
                        (f"unmapped-link-{index}", f"unmapped-link-fp-{index}", index,
                         f"unmapped-term-{index}", f"unmapped-component-{index}",
                         json.dumps(properties), NOW, NOW),
                    )
                    connection.execute(
                        """INSERT INTO relation_assertions(
                               assertion_id,subject_entity_id,source_entity_id,
                               target_entity_id,relation,basis,confidence,status,
                               properties_json,revision,created_at,updated_at)
                           VALUES (?,?,?,?,'has-component','book',.95,'accepted',?,1,?,?)""",
                        (assertion_id, term_entity, term_entity, morpheme_entity,
                         json.dumps(properties), NOW, NOW),
                    )
                    connection.execute(
                        "INSERT INTO assertion_evidence VALUES (?,'evidence-0')",
                        (assertion_id,),
                    )
                    connection.execute(
                        """INSERT INTO term_morphemes(
                               term_id,morpheme_id,ordinal,surface,confidence,basis)
                           VALUES (?,?,1,?,.95,'book')""",
                        (term_entity, morpheme_entity, base),
                    )
                    parser_specs.append(RelationSupportSpec(
                        assertion_id, "book", .95, properties, ("evidence-0",)
                    ))
                replace_relation_support_scope(
                    connection, owner_type="morphology-parser", owner_id="roots:r1",
                    owner_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    supports=tuple(parser_specs), timestamp=NOW,
                )
                upsert_relation_support(
                    connection, assertion_id="abrasive-suffix-model",
                    owner_type="atomic-split", owner_id=abrasive_id,
                    owner_version="legacy-model", basis="model", confidence=.7,
                    properties={"ordinal": 0, "surface": "rad"}, timestamp=NOW,
                )
                connection.executemany(
                    """INSERT INTO term_morphemes(
                           term_id,morpheme_id,ordinal,surface,confidence,basis)
                       VALUES (?,?,?,'rad',.9,?)""",
                    (
                        (abrasive_id, rad_suffix_id, 0, "model"),
                        (abrasive_id, rad_prefix_id, 1, "book"),
                    ),
                )
                connection.commit()
                report = finalize_morphology_support_migration(
                    connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                    expected_sources=expected, expected_source_records=2,
                    allow_write=True,
                )
                self.assertTrue(report["applied"])
                self.assertEqual(report["role_arbitration_retired_assertion_ids"], [])
                self.assertEqual(report["role_arbitration_blocking"], [])
                self.assertEqual(len(report["role_arbitration_deferred"]), 5)
                self.assertEqual(
                    {item["reason"] for item in report["role_arbitration_deferred"]},
                    {
                        "ambiguous-or-inconsistent-occurrence-slot",
                        "typed-v7-winner-has-no-unique-compatible-surface-span",
                    },
                )
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions
                       WHERE assertion_id IN (
                           'repeat-prefix-model','repeat-suffix-model',
                           'abrasive-prefix-book','abrasive-suffix-model')
                         AND status='accepted'"""
                ).fetchone()[0], 4)

    def test_transaction_failure_rolls_back_support_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, expected, term_id, root_id, _ = self.fixture(temp)
            assertion_id = self.add_legacy_formula(store, expected, term_id, root_id)
            with closing(store._connect()) as connection:
                revision = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='relation_graph_revision'"
                ).fetchone()[0]
                connection.execute(
                    """CREATE TEMP TRIGGER fail_support BEFORE INSERT
                       ON relation_assertion_supports BEGIN
                         SELECT RAISE(ABORT, 'forced migration failure');
                       END"""
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    finalize_morphology_support_migration(
                        connection, parser_version=CURRENT_MORPHOLOGY_PARSER_VERSION,
                        expected_sources=expected, expected_source_records=2,
                        allow_write=True,
                    )
                self.assertEqual(connection.execute(
                    "SELECT status FROM relation_assertions WHERE assertion_id=?",
                    (assertion_id,),
                ).fetchone()[0], "accepted")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM relation_support_migrations"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT value FROM schema_meta WHERE key='relation_graph_revision'"
                ).fetchone()[0], revision)


if __name__ == "__main__":
    unittest.main()
