from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lkt.cli import command_reprocess_morphology, parser
from lkt.knowledge import KnowledgeStore
from lkt.morphology import build_morphology_index
from lkt.morphology_contract import CURRENT_MORPHOLOGY_PARSER_VERSION
from lkt.morphology_ingest_v3 import PARSER_VERSION, run


def _index(directory: Path, kind: str) -> Path:
    source = directory / f"{kind}.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": f"{kind}-ambiguous-1",
                "source_page": 1,
                "headword": "example",
                "cells": [
                    "example",
                    "example [noun] ad (=to) + vers (=turn)",
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    database = directory / f"{kind}.sqlite3"
    build_morphology_index(
        source,
        database,
        f"test-{kind}-activation",
        f"Test {kind.title()} Activation",
        kind,
    )
    return database


def _role_correction_indexes(directory: Path) -> tuple[Path, Path]:
    records = {
        "root": {
            "id": "root-alliance",
            "source_page": 1,
            "headword": "alliance",
            "cells": [
                "alliance",
                "alliance [noun] [morphology] al<ad(=to,near)",
            ],
        },
        "affix": {
            "id": "affix-ad",
            "source_page": 1,
            "headword": "AD",
            "cells": ["AD-", "prefix AD- means to,near"],
        },
    }
    databases = []
    for kind in ("root", "affix"):
        source = directory / f"correction-{kind}.jsonl"
        source.write_text(json.dumps(records[kind]) + "\n", encoding="utf-8")
        database = directory / f"correction-{kind}.sqlite3"
        build_morphology_index(
            source,
            database,
            f"test-{kind}-correction",
            f"Test {kind.title()} Correction",
            kind,
        )
        databases.append(database)
    return databases[0], databases[1]


class MorphologyParserActivationTests(unittest.TestCase):
    def test_invariant_check_closes_each_source_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge_db = root / "knowledge.sqlite3"
            KnowledgeStore(knowledge_db)
            root_index = _index(root, "root")

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                run(knowledge_db, (("root", root_index),))
                gc.collect()

            leaked = [
                warning
                for warning in caught
                if "unclosed database" in str(warning.message)
            ]
            self.assertEqual(leaked, [])

    def test_current_ambiguous_parse_retracts_sole_source_legacy_component(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge_db = root / "knowledge.sqlite3"
            store = KnowledgeStore(knowledge_db)
            source = root / "removed-component.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "id": "root-removed-component",
                        "source_page": 1,
                        "headword": "example",
                        "cells": ["example", "example [noun] demonstration"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            root_index = root / "removed-component.sqlite3"
            build_morphology_index(
                source,
                root_index,
                "test-current-retraction",
                "Test Current Retraction",
                "root",
            )
            sources = (("root", root_index),)

            run(knowledge_db, sources)
            with closing(store._connect()) as connection:
                row = connection.execute(
                    """SELECT term.entity_id, source.corpus_id,
                              source.source_entry_id, source.source_hash,
                              source.evidence_id
                       FROM terms AS term
                       JOIN morphology_term_observations_v3 AS observation
                         ON observation.materialized_entity_id = term.entity_id
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id =
                            observation.source_observation_id
                       WHERE term.normalized = 'example'
                         AND source.parser_version = ?""",
                    (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                ).fetchone()
            self.assertIsNotNone(row)
            term_id = str(row["entity_id"])
            stale_root_id = store.upsert_morpheme(
                "en", "ad", "root", "toward"
            )
            store.apply_relation_decision(
                term_id,
                term_id,
                stale_root_id,
                "has-component",
                confidence=0.9,
                properties={
                    "acquisition": "book-morphology-v3",
                    "source_observation": True,
                    "source_kinds": ["root"],
                    "corpus_ids": [str(row["corpus_id"])],
                    "source_entry_ids": [str(row["source_entry_id"])],
                    "source_hashes": [str(row["source_hash"])],
                    "parser_versions": [
                        "book-morphology-observation-v4",
                        "book-morphology-observation-v5",
                    ],
                    "span_fingerprints": ["legacy-over-accepted-component"],
                    "evidence_ids": [str(row["evidence_id"])],
                    "surface_relation": "exact",
                },
                owner_type="morphology-parser",
                owner_id=(
                    f"{row['corpus_id']}:{row['source_entry_id']}"
                ),
                owner_version="book-morphology-observation-v5",
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    """INSERT INTO term_morphemes(
                           term_id, morpheme_id, ordinal, surface,
                           confidence, basis
                       ) VALUES (?, ?, 0, 'ad', 0.9, 'book')""",
                    (term_id, stale_root_id),
                )
                connection.commit()

            corrected = run(knowledge_db, sources)
            with closing(store._connect()) as connection:
                assertion_status = connection.execute(
                    """SELECT status FROM relation_assertions
                       WHERE subject_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (term_id, stale_root_id),
                ).fetchone()[0]
                edge_status = connection.execute(
                    """SELECT status FROM entity_edges
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (term_id, stale_root_id),
                ).fetchone()[0]
                normalized_links = connection.execute(
                    """SELECT COUNT(*) FROM term_morphemes
                       WHERE term_id = ? AND morpheme_id = ?""",
                    (term_id, stale_root_id),
                ).fetchone()[0]

            self.assertEqual(assertion_status, "archived")
            self.assertEqual(edge_status, "archived")
            self.assertEqual(normalized_links, 0)
            self.assertEqual(
                corrected["counts"]["superseded_role_assertions_archived"], 1
            )
            self.assertEqual(
                corrected["counts"]["superseded_normalized_links_removed"], 1
            )

    def test_materialized_assertion_keeps_normalized_clause_component_glosses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge_db = root / "knowledge.sqlite3"
            store = KnowledgeStore(knowledge_db)
            source = root / "clause-gloss.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "id": "root-lig-clause-gloss",
                        "source_page": 1,
                        "headword": "ligate",
                        "cells": [
                            "词根 LIG 意为 bind / fasten。",
                            "ligate［verb］lig(=bind / fasten)",
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            root_index = root / "clause-gloss.sqlite3"
            build_morphology_index(
                source,
                root_index,
                "test-clause-gloss",
                "Test Clause Gloss",
                "root",
            )

            run(knowledge_db, (("root", root_index),))
            with closing(store._connect()) as connection:
                row = connection.execute(
                    """SELECT assertion.properties_json
                       FROM relation_assertions AS assertion
                       JOIN terms AS term
                         ON term.entity_id = assertion.subject_entity_id
                       JOIN morphemes AS morpheme
                         ON morpheme.entity_id = assertion.target_entity_id
                       WHERE assertion.status = 'accepted'
                         AND assertion.relation = 'has-component'
                         AND term.normalized = 'ligate'
                         AND morpheme.normalized = 'lig'"""
                ).fetchone()

            self.assertIsNotNone(row)
            properties = json.loads(str(row["properties_json"]))
            self.assertEqual(properties["component_glosses"], ["bind", "fasten"])
            self.assertEqual(
                properties["parser_versions"],
                [CURRENT_MORPHOLOGY_PARSER_VERSION],
            )

    def test_role_correction_transactionally_invalidates_only_its_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge_db = root / "knowledge.sqlite3"
            store = KnowledgeStore(knowledge_db)
            root_index, affix_index = _role_correction_indexes(root)
            sources = (("root", root_index), ("affix", affix_index))

            run(knowledge_db, sources)
            with closing(store._connect()) as connection:
                row = connection.execute(
                    """SELECT term.entity_id, source.corpus_id,
                              source.source_entry_id,
                              source.source_hash, source.evidence_id
                       FROM terms AS term
                       JOIN morphology_term_observations_v3 AS observation
                         ON observation.materialized_entity_id = term.entity_id
                       JOIN morphology_source_observations_v3 AS source
                         ON source.source_observation_id =
                            observation.source_observation_id
                       WHERE term.normalized = 'alliance'
                         AND source.source_entry_id = 'root-alliance'"""
                ).fetchone()
                prefix_count = connection.execute(
                    """SELECT COUNT(*) FROM relation_assertions AS assertion
                       JOIN morphemes AS morpheme
                         ON morpheme.entity_id = assertion.target_entity_id
                       WHERE assertion.subject_entity_id = ?
                         AND assertion.relation = 'has-component'
                         AND assertion.status = 'accepted'
                         AND morpheme.kind = 'prefix'""",
                    (str(row["entity_id"]),),
                ).fetchone()[0]
            self.assertEqual(prefix_count, 1)
            term_id = str(row["entity_id"])
            stale_root_id = store.upsert_morpheme(
                "en", "ad", "root", "toward"
            )
            store.apply_relation_decision(
                term_id,
                term_id,
                stale_root_id,
                "has-component",
                confidence=0.9,
                properties={
                    "parser_versions": [PARSER_VERSION],
                    "source_entry_ids": [str(row["source_entry_id"])],
                    "source_hashes": [str(row["source_hash"])],
                    "evidence_ids": [str(row["evidence_id"])],
                },
                owner_type="morphology-parser",
                owner_id=(
                    f"{row['corpus_id']}:{row['source_entry_id']}"
                ),
                owner_version=PARSER_VERSION,
            )
            checkpoint_job = store.enqueue_job(
                "retrieve-evidence",
                f"term:{term_id}",
                subject_entity_id=term_id,
                language="en",
                model="test-local-qwen",
                prompt_version="morphology-role-invalidation",
            )
            for stage in (
                "accepted-meaning",
                "accepted-morpheme-split",
                "accepted-origin-branches",
            ):
                store.save_job_artifact(
                    checkpoint_job,
                    stage,
                    {"stage": stage},
                    validation_state="accepted",
                )
            claim = store.claim_lexical_projection_repair()
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            applied = store.lexical_projection_repair_state(term_id)

            corrected = run(knowledge_db, sources)
            invalidated = store.lexical_projection_repair_state(term_id)
            self.assertEqual(
                corrected["counts"]["projection_repairs_invalidated"], 1
            )
            self.assertEqual(invalidated["status"], "pending")
            self.assertEqual(invalidated["desired_fingerprint"], "")
            with closing(store._connect()) as connection:
                stale_status = connection.execute(
                    """SELECT status FROM relation_assertions
                       WHERE subject_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (term_id, stale_root_id),
                ).fetchone()[0]
            self.assertEqual(stale_status, "archived")

            refreshed = store.claim_lexical_projection_repair()
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed["term_entity_id"], term_id)
            self.assertEqual(refreshed["desired_modes"], ["affix"])
            self.assertNotEqual(
                refreshed["desired_fingerprint"],
                applied["applied_fingerprint"],
            )
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, refreshed["desired_fingerprint"]
                )
            )
            replay = run(knowledge_db, sources)
            self.assertEqual(
                replay["counts"].get("projection_repairs_invalidated", 0), 0
            )
            self.assertEqual(
                store.lexical_projection_repair_state(term_id)["status"],
                "applied",
            )

    def test_dual_index_reprocessing_is_current_scoped_and_idempotent(self) -> None:
        self.assertEqual(PARSER_VERSION, CURRENT_MORPHOLOGY_PARSER_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge_db = root / "knowledge.sqlite3"
            store = KnowledgeStore(knowledge_db)
            sources = (("root", _index(root, "root")), ("affix", _index(root, "affix")))

            with patch(
                "lkt.morphology_ingest_v3.PARSER_VERSION",
                "book-morphology-observation-v3",
            ):
                run(knowledge_db, sources)
            first = run(knowledge_db, sources)
            second = run(knowledge_db, sources)

            self.assertEqual(
                first["parser_version"], CURRENT_MORPHOLOGY_PARSER_VERSION
            )
            self.assertEqual(
                second["parser_version"], CURRENT_MORPHOLOGY_PARSER_VERSION
            )
            with closing(sqlite3.connect(knowledge_db)) as connection:
                connection.row_factory = sqlite3.Row
                versions = dict(
                    connection.execute(
                        """SELECT parser_version, COUNT(*)
                           FROM morphology_source_observations_v3
                           GROUP BY parser_version"""
                    ).fetchall()
                )
                current_ids = {
                    str(row[0])
                    for row in connection.execute(
                        """SELECT source_observation_id
                           FROM morphology_source_observations_v3
                           WHERE parser_version = ?""",
                        (CURRENT_MORPHOLOGY_PARSER_VERSION,),
                    )
                }
                candidates = store._morphology_enrichment_candidates(connection)

            self.assertEqual(versions["book-morphology-observation-v3"], 2)
            self.assertEqual(versions[CURRENT_MORPHOLOGY_PARSER_VERSION], 2)
            self.assertTrue(candidates)
            self.assertTrue(
                all(
                    item["source"]["source_observation_id"] in current_ids
                    and item["source"]["parser_version"]
                    == CURRENT_MORPHOLOGY_PARSER_VERSION
                    for item in candidates
                )
            )
            status = store.morphology_v3_status()
            self.assertTrue(status["ready"])
            self.assertEqual(
                status["parser_version"], CURRENT_MORPHOLOGY_PARSER_VERSION
            )
            self.assertEqual(status["observed"], 2)
            self.assertEqual(set(status["by_source_kind"]), {"root", "affix"})

            planned = store.plan_morphology_v3_enrichment(model="local-qwen")
            self.assertIsNotNone(planned)
            self.assertEqual(
                planned["parser_version"], CURRENT_MORPHOLOGY_PARSER_VERSION
            )
            with closing(sqlite3.connect(knowledge_db)) as connection:
                cluster_version = connection.execute(
                    """SELECT parser_version
                       FROM morphology_enrichment_clusters_v3
                       WHERE cluster_id = ?""",
                    (planned["cluster_id"],),
                ).fetchone()[0]
            self.assertEqual(
                cluster_version, CURRENT_MORPHOLOGY_PARSER_VERSION
            )

    def test_cli_reprocesses_both_configured_indexes_after_acknowledgement(self) -> None:
        settings = SimpleNamespace(
            knowledge_db=Path("knowledge.sqlite3"),
            roots_db=Path("roots.sqlite3"),
            affixes_db=Path("affixes.sqlite3"),
        )
        args = parser().parse_args(["reprocess-morphology", "--allow-write"])
        with (
            patch("lkt.cli._settings", return_value=settings),
            patch("lkt.morphology_ingest_v3.run", return_value={"ok": True}) as ingest,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(args.handler(args), 0)

        ingest.assert_called_once_with(
            Path("knowledge.sqlite3").resolve(),
            (
                ("root", Path("roots.sqlite3").resolve()),
                ("affix", Path("affixes.sqlite3").resolve()),
            ),
            batch_size=100,
        )
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})

    def test_cli_refuses_reprocessing_without_explicit_write_acknowledgement(self) -> None:
        args = parser().parse_args(["reprocess-morphology"])
        with redirect_stderr(StringIO()) as error:
            self.assertEqual(args.handler(args), 2)
        self.assertIn("requires --allow-write", error.getvalue())


if __name__ == "__main__":
    unittest.main()
