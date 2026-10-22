from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.knowledge import KnowledgeStore
from lkt.morphology import build_morphology_index
from lkt.morphology_ingest_v3 import (
    DeclarationLexicon,
    SourceRow,
    build_declaration_lexicon,
    extract_declaration_seeds,
    parse_source_record,
    run,
)


def source(kind: str, record_id: str, headword: str, content: str) -> SourceRow:
    return SourceRow(
        kind,
        f"test-{kind}",
        "book-hash",
        f"Test {kind.title()} Dictionary",
        record_id,
        headword,
        1,
        content,
    )


def index(directory: Path, content: str) -> Path:
    source_path = directory / "parser-v7.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "id": "record-pre",
                "source_page": 1,
                "headword": "PRE",
                "cells": ["PRE", content],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    database = directory / "parser-v7.sqlite3"
    database.unlink(missing_ok=True)
    build_morphology_index(
        source_path,
        database,
        "test-parser-v7",
        "Test Parser V7",
        "root",
    )
    return database


class MorphologyRoleInvariantV7Tests(unittest.TestCase):
    def test_markers_survive_index_shape_without_header_leakage(self) -> None:
        for raw, expected in (("PRE", "pre-"), ("RE", "re-"), ("IN", "in-")):
            record = source(
                "root",
                f"index-{raw.casefold()}",
                raw,
                f"前缀 {raw} INDEX 11 12",
            )
            self.assertEqual(
                {(item.normalized_form, item.kind) for item in extract_declaration_seeds(record)},
                {(expected, "prefix")},
            )

        sinu = source(
            "root",
            "p0292-t073-r002",
            "SINU",
            "词根 SINU 表示弯曲 [在里边画曲线、弯曲 in (= in)+sinu (=curve)]",
        )
        self.assertEqual(
            {(item.normalized_form, item.kind) for item in extract_declaration_seeds(sinu)},
            {("sinu", "root")},
        )
        unmarked_affix = source(
            "affix",
            "unmarked-pre",
            "PRE",
            "PRE 来源于拉丁语，意为 before。",
        )
        self.assertEqual(extract_declaration_seeds(unmarked_affix), ())

    def test_affix_role_requires_word_position_and_conflicts_stay_ambiguous(self) -> None:
        misplaced = source(
            "affix",
            "p0060-t001-r001",
            "inspect",
            "inspect [verb] -in(=into)",
        )
        component = next(
            item
            for item in parse_source_record(misplaced, DeclarationLexicon(())).components
            if item.clause_index >= 0 and item.raw_form == "-in"
        )
        self.assertEqual(component.proposed_kind, "ambiguous")
        self.assertEqual(component.properties["basis"], "affix-position-conflict")
        self.assertTrue(component.qwen_ready)

        declared_in = source(
            "affix", "prefix-in", "IN", "前缀 IN 意为 into。"
        )
        conflict = next(
            item
            for item in parse_source_record(
                misplaced, build_declaration_lexicon((declared_in, misplaced))
            ).components
            if item.clause_index >= 0 and item.raw_form == "-in"
        )
        self.assertEqual(conflict.proposed_kind, "ambiguous")
        self.assertEqual(
            conflict.properties["basis"], "affix-position-conflict"
        )
        self.assertTrue(conflict.qwen_ready)

        rebuild = source(
            "root", "word-rebuild", "rebuild", "rebuild [verb] re(=again)"
        )
        declared_re = source(
            "affix", "prefix-re", "RE", "前缀 RE 意为 again。"
        )
        re_component = next(
            item
            for item in parse_source_record(
                rebuild, build_declaration_lexicon((declared_re, rebuild))
            ).components
            if item.clause_index >= 0
        )
        self.assertEqual(re_component.proposed_kind, "prefix")
        self.assertEqual(re_component.normalized_form, "re-")
        self.assertEqual(re_component.state, "materializable")

    def test_record_owner_replacement_preserves_independent_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            knowledge_db = directory / "knowledge.sqlite3"
            store = KnowledgeStore(knowledge_db)
            source_index = index(
                directory,
                "前缀 PRE 意为 before。 preview [verb] pre(=before)",
            )
            run(knowledge_db, (("root", source_index),))
            with closing(store._connect()) as connection:
                row = connection.execute(
                    """SELECT assertion.assertion_id, assertion.subject_entity_id,
                              assertion.target_entity_id, assertion.properties_json,
                              assertion.revision, morpheme.form, morpheme.normalized,
                              morpheme.kind, entity.canonical_key
                       FROM relation_assertions AS assertion
                       JOIN morphemes AS morpheme
                         ON morpheme.entity_id = assertion.target_entity_id
                       JOIN entities AS entity
                         ON entity.entity_id = morpheme.entity_id
                       WHERE assertion.relation = 'has-component'
                         AND assertion.status = 'accepted'"""
                ).fetchone()
                parser_support = connection.execute(
                    """SELECT owner_id, basis, status
                       FROM relation_assertion_supports
                       WHERE assertion_id = ? AND owner_type = 'morphology-parser'""",
                    (row["assertion_id"],),
                ).fetchone()
            self.assertEqual(
                (row["form"], row["normalized"], row["kind"], row["canonical_key"]),
                ("pre-", "pre", "prefix", "morpheme:en:prefix:pre"),
            )
            self.assertEqual(
                tuple(parser_support),
                ("test-parser-v7:record-pre", "book", "accepted"),
            )

            revision = int(row["revision"])
            run(knowledge_db, (("root", source_index),))
            with closing(store._connect()) as connection:
                replay_revision = int(
                    connection.execute(
                        "SELECT revision FROM relation_assertions WHERE assertion_id = ?",
                        (row["assertion_id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(replay_revision, revision)

            assertion_id = store.accept_relation_assertion(
                row["subject_entity_id"],
                row["subject_entity_id"],
                row["target_entity_id"],
                "has-component",
                basis="model",
                properties=json.loads(row["properties_json"]),
                owner_type="atomic-split",
                owner_id=row["subject_entity_id"],
                owner_version="draft-1",
            )
            self.assertEqual(assertion_id, row["assertion_id"])

            empty_index = index(directory, "preview [verb] no decomposition")
            run(knowledge_db, (("root", empty_index),))
            with closing(store._connect()) as connection:
                supports = {
                    str(item["owner_type"]): str(item["status"])
                    for item in connection.execute(
                        """SELECT owner_type, status
                           FROM relation_assertion_supports
                           WHERE assertion_id = ?""",
                        (row["assertion_id"],),
                    )
                }
                status = str(
                    connection.execute(
                        "SELECT status FROM relation_assertions WHERE assertion_id = ?",
                        (row["assertion_id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(supports["morphology-parser"], "retired")
            self.assertEqual(supports["atomic-split"], "accepted")
            self.assertEqual(status, "accepted")


if __name__ == "__main__":
    unittest.main()
