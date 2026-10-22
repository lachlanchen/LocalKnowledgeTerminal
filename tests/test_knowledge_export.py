from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lkt.knowledge_export import export_public_lexical_graph


class PublicKnowledgeExportTests(unittest.TestCase):
    def test_public_export_preserves_lexical_graph_and_quarantines_private_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private.sqlite3"
            self._create_source(source)
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

            manifest = export_public_lexical_graph(source, root / "public", "fixture-1")

            self.assertEqual(
                {"en", "ja", "zh", "fr", "ar", "de-DE", "la"},
                set(manifest.languages),
            )
            self.assertEqual({"unverified": 2}, dict(manifest.source_statuses))
            self.assertGreaterEqual(manifest.quarantine_counts["semantic_overlap_entities"], 1)
            self.assertEqual(source_digest, hashlib.sha256(source.read_bytes()).hexdigest())

            database_path = root / "public" / "data" / "lkt-lexical-knowledge.sqlite3"
            self.assertTrue(database_path.is_file())
            self.assertFalse((root / "public" / "lexical_graph.sqlite3").exists())
            checksum_text = (root / "public" / "SHA256SUMS").read_text(encoding="ascii")
            self.assertIn(
                "data/lkt-lexical-knowledge.sqlite3",
                checksum_text,
            )
            readme_path = root / "public" / "README.md"
            readme = readme_path.read_text(encoding="utf-8")
            self.assertTrue(readme.startswith("---\n"))
            self.assertIn(
                'pretty_name: "Local Knowledge Terminal Multilingual Lexical Knowledge Graph"',
                readme,
            )
            self.assertIn("license: other", readme)
            self.assertIn("- knowledge-graph", readme)
            self.assertIn("- retrieval-augmented-generation", readme)
            self.assertIn("All 2 included sources are recorded as `unverified`.", readme)
            self.assertIn("must remain private and gated", readme)
            public_tables = manifest.as_dict()["tables"]
            self.assertEqual(21, len(public_tables))
            self.assertEqual(21, readme.count("- config_name: "))
            self.assertEqual(21, readme.count("  - split: train"))
            for table, metadata in public_tables.items():
                self.assertIn(f"- config_name: {table}\n", readme)
                self.assertIn(f'    path: "{metadata["path"]}"\n', readme)
            checksum_rows = {
                path: digest
                for digest, path in (
                    line.split("  ", 1) for line in checksum_text.splitlines()
                )
            }
            self.assertEqual(
                hashlib.sha256(readme_path.read_bytes()).hexdigest(),
                checksum_rows["README.md"],
            )
            database = sqlite3.connect(database_path)
            try:
                self.assertEqual("ok", database.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual([], database.execute("PRAGMA foreign_key_check").fetchall())
                terms = dict(database.execute("SELECT text, language FROM terms"))
                self.assertEqual("ja", terms["焚き火"])
                self.assertEqual("zh", terms["篝火"])
                self.assertEqual("fr", terms["feu de joie"])
                self.assertEqual("ar", terms["نار المخيم"])
                self.assertEqual("de-DE", terms["Lagerfeuer"])
                self.assertEqual("en", terms["prompt"])
                self.assertEqual(
                    1,
                    database.execute(
                        "SELECT count(*) FROM historical_forms WHERE form = 'prompt'"
                    ).fetchone()[0],
                )
                self.assertEqual(2, database.execute("SELECT count(*) FROM term_morphemes").fetchone()[0])
                self.assertEqual(1, database.execute("SELECT count(*) FROM history_events").fetchone()[0])
                self.assertEqual(2, database.execute("SELECT count(*) FROM historical_forms").fetchone()[0])
                self.assertEqual(5, database.execute("SELECT count(*) FROM translations").fetchone()[0])
                self.assertEqual(1, database.execute("SELECT count(*) FROM phoneme_segments").fetchone()[0])
                self.assertEqual(1, database.execute("SELECT count(*) FROM grammar_parts").fetchone()[0])
                self.assertGreaterEqual(
                    database.execute("SELECT count(*) FROM entity_edges WHERE relation = 'derived-from'").fetchone()[0],
                    1,
                )
                roles = {
                    row[0]
                    for row in database.execute("SELECT support_role FROM assertion_citations")
                }
                self.assertEqual({"exact", "context"}, roles)
                self.assertEqual(
                    1,
                    database.execute(
                        """SELECT count(*) FROM relation_assertions assertion
                           LEFT JOIN assertion_citations citation
                             ON citation.assertion_id = assertion.assertion_id
                           WHERE assertion.basis = 'model'
                             AND assertion.relation = 'shares-component'
                             AND citation.assertion_id IS NULL"""
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    database.execute(
                        """SELECT count(*) FROM entity_edges edge
                           LEFT JOIN entities source ON source.entity_id = edge.source_entity_id
                           LEFT JOIN entities target ON target.entity_id = edge.target_entity_id
                           WHERE source.entity_id IS NULL OR target.entity_id IS NULL"""
                    ).fetchone()[0],
                )
                self.assertEqual(
                    database.execute("SELECT count(*) FROM relation_assertions").fetchone()[0],
                    database.execute("SELECT count(*) FROM relation_slot_winners").fetchone()[0],
                )
                self.assertEqual(
                    [],
                    database.execute(
                        """SELECT slot_id FROM relation_slot_winners
                           WHERE slot_mode = 'exclusive'
                           GROUP BY slot_id HAVING COUNT(*) > 1"""
                    ).fetchall(),
                )
                correction = database.execute(
                    """SELECT action, is_current, relation, exact_evidence_count,
                              context_evidence_count, validator_version
                       FROM relation_correction_events
                       WHERE action = 'baseline'"""
                ).fetchone()
                self.assertEqual(("baseline", 1, "derived-from", 1, 1, "validator-v1"), correction)
                archived = database.execute(
                    """SELECT action, is_current, relation, after_status
                       FROM relation_correction_events WHERE action = 'archive'"""
                ).fetchone()
                self.assertEqual(("archive", 0, "opposed-to", "archived"), archived)
                observation = database.execute(
                    """SELECT decision, relation, exact_evidence_count,
                              context_evidence_count, model_pass_version
                       FROM relation_claim_observations"""
                ).fetchone()
                self.assertEqual(("supersede", "opposed-to", 0, 1, "morphology-v3"), observation)
                self.assertEqual(
                    1,
                    database.execute(
                        "SELECT count(*) FROM relation_claim_observation_incumbents"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    database.execute(
                        "SELECT count(*) FROM entity_edges WHERE relation = 'opposed-to'"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    database.execute(
                        "SELECT count(*) FROM entity_edges WHERE relation = 'has-component'"
                    ).fetchone()[0],
                )
            finally:
                database.close()

            exported_bytes = b"".join(
                path.read_bytes() for path in sorted((root / "public").rglob("*")) if path.is_file()
            )
            for forbidden in (
                b"DRAFT-PRIVATE-TEXT",
                b"CONTENT-ITEM-PRIVATE",
                b"PRIVATE RAW PAYLOAD",
                b"PRIVATE EVIDENCE EXCERPT",
                b"PRIVATE CLAIM",
                b"PRIVATE EDGE PROPERTY",
                b"PRIVATE MODEL PROMPT",
                b"PRIVATE BEFORE PAYLOAD",
                b"PRIVATE AFTER PAYLOAD",
                b"PRIVATE CLAIM PROPOSAL",
                b"PRIVATE EVENT REASON",
                b"PRIVATE OBSERVATION REASON",
                b"PRIVATE PROMPT TEMPLATE",
                b"This private verbatim definition must never leave the source database.",
                b"C:\\Users\\private-person\\source.txt",
            ):
                self.assertNotIn(forbidden, exported_bytes)
            for forbidden_key in (
                b'"payload"',
                b'"excerpt"',
                b'"claim"',
                b'"properties"',
                b'"prompt":',
                b'"before_json"',
                b'"after_json"',
                b'"proposal_json"',
                b'"job_id"',
            ):
                self.assertNotIn(forbidden_key, exported_bytes)

    def test_logical_hashes_and_jsonl_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private.sqlite3"
            self._create_source(source)

            first = export_public_lexical_graph(source, root / "first", "fixture-1")
            second = export_public_lexical_graph(source, root / "second", "fixture-1")

            self.assertEqual(dict(first.table_sha256), dict(second.table_sha256))
            self.assertEqual(first.as_dict(), second.as_dict())
            first_files = sorted((root / "first" / "data").glob("*.jsonl"))
            second_files = sorted((root / "second" / "data").glob("*.jsonl"))
            self.assertEqual([path.name for path in first_files], [path.name for path in second_files])
            for first_path, second_path in zip(first_files, second_files):
                self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
                for line in first_path.read_text(encoding="utf-8").splitlines():
                    json.loads(line)

    def test_export_rejects_missing_slots_and_exclusive_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.sqlite3"
            self._create_source(missing)
            connection = sqlite3.connect(missing)
            try:
                connection.execute(
                    "DELETE FROM relation_assertion_slots WHERE assertion_id = 'assertion-derived'"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "without a complete correction slot"):
                export_public_lexical_graph(missing, root / "missing-export", "fixture-1")

            collision = root / "collision.sqlite3"
            self._create_source(collision)
            connection = sqlite3.connect(collision)
            try:
                connection.execute(
                    """UPDATE relation_assertion_slots
                       SET slot_key = 'slot-derived', slot_mode = 'exclusive'
                       WHERE assertion_id = 'assertion-model-only'"""
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "multiple winners"):
                export_public_lexical_graph(collision, root / "collision-export", "fixture-1")

    def _create_source(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(_SOURCE_DDL)
            connection.executemany(
                "INSERT INTO schema_meta(key,value) VALUES (?,?)",
                (("schema_version", "10"), ("relation_graph_revision", "42")),
            )
            entities = [
                ("term-en", "term", "accepted", 0.9),
                ("term-derived", "term", "accepted", 0.8),
                ("term-ja", "term", "accepted", 0.9),
                ("term-zh", "term", "accepted", 0.9),
                ("term-fr", "term", "accepted", 0.9),
                ("term-ar", "term", "accepted", 0.9),
                ("term-de", "term", "accepted", 0.9),
                ("term-prompt", "term", "accepted", 0.9),
                ("meaning-en", "meaning", "accepted", 0.9),
                ("meaning-derived", "meaning", "accepted", 0.8),
                ("meaning-unsafe", "meaning", "accepted", 0.7),
                ("root-fire", "morpheme", "accepted", 0.9),
                ("suffix-place", "morpheme", "accepted", 0.8),
                ("pron-en", "pronunciation", "accepted", 0.8),
                ("historical-la", "historical-form", "accepted", 0.8),
                ("historical-prompt", "historical-form", "accepted", 0.8),
                ("history-en", "history-event", "accepted", 0.8),
                ("grammar-ja", "grammar-analysis", "accepted", 0.8),
                *( (f"translation-{language}", "translation", "accepted", 0.8) for language in ("ja", "zh", "fr", "ar", "de") ),
                ("draft-term", "term", "draft", 0.1),
                ("content-private", "content-item", "accepted", 0.5),
            ]
            connection.executemany(
                "INSERT INTO entities(entity_id,entity_type,status,quality_score,payload,label,canonical_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        entity_id,
                        entity_type,
                        status,
                        score,
                        (
                            '{"prompt":"PRIVATE MODEL PROMPT","raw_content":"PRIVATE SOURCE BODY"}'
                            if entity_id == "term-prompt"
                            else '{"raw":"PRIVATE RAW PAYLOAD"}'
                        ),
                        entity_id,
                        entity_id,
                        "private-time",
                        "private-time",
                    )
                    for entity_id, entity_type, status, score in entities
                ],
            )
            connection.executemany(
                "INSERT INTO terms VALUES (?,?,?,?,?)",
                (
                    ("term-en", "en", "bonfire", "bonfire", "word"),
                    ("term-derived", "en", "bonfires", "bonfires", "word"),
                    ("term-ja", "ja", "焚き火", "焚き火", "word"),
                    ("term-zh", "zh", "篝火", "篝火", "word"),
                    ("term-fr", "fr", "feu de joie", "feu de joie", "phrase"),
                    ("term-ar", "ar", "نار المخيم", "نار المخيم", "phrase"),
                    ("term-de", "de-DE", "Lagerfeuer", "lagerfeuer", "word"),
                    ("term-prompt", "en", "prompt", "prompt", "word"),
                    ("draft-term", "en", "DRAFT-PRIVATE-TEXT", "draft-private-text", "word"),
                ),
            )
            connection.executemany(
                "INSERT INTO meanings VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ("meaning-en", "term-en", "term-en", "en", "a large outdoor fire", "noun", "", "", 1),
                    ("meaning-derived", "term-derived", "term-derived", "en", "plural outdoor fires", "noun", "", "", 1),
                    (
                        "meaning-unsafe",
                        "term-en",
                        "term-en",
                        "en",
                        "This private verbatim definition must never leave the source database.",
                        "noun",
                        "",
                        "",
                        2,
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO morphemes VALUES (?,?,?,?,?,?)",
                (
                    ("root-fire", "en", "fire", "fire", "root", "fire"),
                    ("suffix-place", "en", "-place", "place", "suffix", "a location"),
                ),
            )
            connection.executemany(
                "INSERT INTO term_morphemes VALUES (?,?,?,?,?,?)",
                (
                    ("term-en", "root-fire", 0, "fire", 0.9, "book"),
                    ("term-en", "suffix-place", 1, "", 0.7, "model"),
                ),
            )
            connection.execute("INSERT INTO pronunciations VALUES (?,?,?,?,?,?)", ("pron-en", "term-en", "en", "ipa", "ˈbɒn.faɪə", "",))
            connection.execute("INSERT INTO phoneme_segments VALUES (?,?,?,?,?,?,?,?)", ("segment-1", "pron-en", 0, "bon", "bɒn", "bon", "private-color", '{"private":"value"}'))
            connection.execute("INSERT INTO historical_forms VALUES (?,?,?,?,?,?,?,?)", ("historical-la", "la", "focus", "focus", "Classical", -100, 300, "hearth"))
            connection.execute("INSERT INTO historical_forms VALUES (?,?,?,?,?,?,?,?)", ("historical-prompt", "en", "prompt", "prompt", "Modern", 1900, None, "a cue for action"))
            connection.execute(
                "INSERT INTO history_events VALUES (?,?,?,?,?,?,?,?,?)",
                ("history-en", "term-en", "developed", "en", "Early Modern", 1400, 1700, "the form developed toward its modern use", '{"private":"history"}'),
            )
            translation_values = {
                "ja": ("term-ja", "焚き火", "takibi"),
                "zh": ("term-zh", "篝火", "gōuhuǒ"),
                "fr": ("term-fr", "feu de joie", ""),
                "ar": ("term-ar", "نار المخيم", "nar al-mukhayyam"),
                "de": ("term-de", "Lagerfeuer", ""),
            }
            connection.executemany(
                "INSERT INTO translations VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (f"translation-{language}", "term-en", "meaning-en", "de-DE" if language == "de" else language, term_id, text, text.casefold(), transliteration, "")
                    for language, (term_id, text, transliteration) in translation_values.items()
                ],
            )
            connection.execute("INSERT INTO grammar_analyses VALUES (?,?,?,?,?)", ("grammar-ja", "term-ja", "ja", "word", "Japanese noun phrase"))
            connection.execute("INSERT INTO grammar_parts VALUES (?,?,?,?,?,?,?,?,?,?)", ("grammar-part-1", "grammar-ja", 0, "焚き火", "焚き火", "head", "noun", "たきび", "private-color", '{"private":"grammar"}'))
            edges = (
                ("edge-derived", "term-derived", "term-en", "derived-from", "book", 0.9),
                ("edge-component-1", "term-en", "root-fire", "has-component", "book", 0.9),
                ("edge-component-2", "term-en", "suffix-place", "has-component", "model", 0.7),
                ("edge-history", "historical-la", "term-en", "developed-into", "book", 0.8),
                ("edge-unsafe", "term-en", "meaning-unsafe", "has-meaning", "book", 0.8),
                ("edge-archived-only", "term-en", "term-derived", "opposed-to", "model", 0.6),
            )
            connection.executemany(
                "INSERT INTO entity_edges VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (edge_id, source_id, target_id, relation, basis, confidence, "accepted", '{"note":"PRIVATE EDGE PROPERTY","evidence_ids":["evidence-exact"]}', "private-time", "private-time")
                    for edge_id, source_id, target_id, relation, basis, confidence in edges
                ],
            )
            assertions = (
                (
                    "assertion-derived",
                    "term-derived",
                    "term-derived",
                    "term-en",
                    "derived-from",
                    "book",
                    0.9,
                    '{"citation_evidence_ids":["evidence-exact"],"context_evidence_ids":["evidence-context"],"raw_form":"PRIVATE ASSERTION RAW"}',
                ),
                (
                    "assertion-model-only",
                    "term-en",
                    "root-fire",
                    "suffix-place",
                    "shares-component",
                    "model",
                    0.6,
                    "{}",
                ),
                (
                    "assertion-archived",
                    "term-en",
                    "term-en",
                    "term-derived",
                    "opposed-to",
                    "model",
                    0.5,
                    "{}",
                ),
            )
            connection.executemany(
                "INSERT INTO relation_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        *row[:7],
                        "archived" if row[0] == "assertion-archived" else "accepted",
                        row[7],
                        2 if row[0] == "assertion-archived" else 1,
                        "private-time",
                        "private-time",
                    )
                    for row in assertions
                ],
            )
            connection.executemany(
                "INSERT INTO relation_assertion_slots VALUES (?,?,?,?,?)",
                (
                    ("assertion-derived", "slot-derived", "exclusive", "relation-slot-v2", "private-time"),
                    ("assertion-model-only", "slot-model", "set-member", "relation-slot-v2", "private-time"),
                    ("assertion-archived", "slot-archived", "exclusive", "relation-slot-v2", "private-time"),
                ),
            )
            baseline_after = json.dumps(
                {
                    "assertion_id": "assertion-derived",
                    "subject_entity_id": "term-derived",
                    "source_entity_id": "term-derived",
                    "target_entity_id": "term-en",
                    "relation": "derived-from",
                    "basis": "book",
                    "confidence": 0.9,
                    "status": "accepted",
                    "properties": {"raw": "PRIVATE AFTER PAYLOAD"},
                    "revision": 1,
                }
            )
            archive_before = json.dumps(
                {
                    "assertion_id": "assertion-archived",
                    "subject_entity_id": "term-en",
                    "source_entity_id": "term-en",
                    "target_entity_id": "term-derived",
                    "relation": "opposed-to",
                    "basis": "model",
                    "confidence": 0.5,
                    "status": "accepted",
                    "properties": {"raw": "PRIVATE BEFORE PAYLOAD"},
                    "revision": 1,
                }
            )
            archive_after = json.dumps({**json.loads(archive_before), "status": "archived", "revision": 2})
            connection.executemany(
                "INSERT INTO relation_assertion_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        "event-baseline", "decision-baseline", "assertion-derived", "baseline",
                        "{}", baseline_after, '["evidence-exact"]', '["evidence-context"]',
                        "C:\\private\\input", "PRIVATE EVENT OUTPUT", "private-job-1",
                        "PRIVATE EVENT REASON", "validator-v1", "private-time",
                    ),
                    (
                        "event-archive", "decision-archive", "assertion-archived", "archive",
                        archive_before, archive_after, "[]", "[]", "private-input",
                        "private-output", "private-job-2", "PRIVATE EVENT REASON",
                        "validator-v1", "private-time-2",
                    ),
                ),
            )
            proposal = json.dumps(
                {
                    "assertion_id": "assertion-proposed",
                    "subject_entity_id": "term-en",
                    "source_entity_id": "term-en",
                    "target_entity_id": "term-derived",
                    "relation": "opposed-to",
                    "basis": "model",
                    "confidence": 0.7,
                    "properties": {
                        "prompt": "PRIVATE PROMPT TEMPLATE",
                        "raw": "PRIVATE CLAIM PROPOSAL",
                    },
                }
            )
            connection.execute(
                "INSERT INTO relation_claim_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "observation-1", "slot-archived", "exclusive", "assertion-proposed",
                    '["assertion-archived"]', proposal, "supersede", "[]",
                    '["evidence-context"]', "private-observation-input", "private-observation-output",
                    "private-epoch", "morphology-v3", "private-job-3",
                    "PRIVATE OBSERVATION REASON", "relation-slot-v2", "private-time",
                ),
            )
            connection.executemany(
                "INSERT INTO evidence_records VALUES (?,?,?,?,?,?,?)",
                (
                    (
                        "evidence-exact",
                        "C:\\Users\\private-person\\corpus",
                        "private-entry-1",
                        "private-source-hash",
                        "C:\\Users\\private-person\\source.txt",
                        "PRIVATE EVIDENCE EXCERPT",
                        '{"raw_content":"PRIVATE SOURCE BODY","source_title":"Private Book"}',
                    ),
                    (
                        "evidence-context",
                        "book-corpus",
                        "entry-2",
                        "context-hash",
                        "page 12",
                        "Context about outdoor fires without copying the generated definition.",
                        '{"source_title":"Word History"}',
                    ),
                    (
                        "evidence-unsafe",
                        "book-corpus",
                        "entry-3",
                        "unsafe-hash",
                        "page 13",
                        "This private verbatim definition must never leave the source database.",
                        '{"raw_content":"This private verbatim definition must never leave the source database."}',
                    ),
                ),
            )
            connection.execute("INSERT INTO assertion_evidence VALUES (?,?)", ("assertion-derived", "evidence-exact"))
            connection.executemany(
                "INSERT INTO entity_evidence VALUES (?,?,?,?)",
                (
                    ("term-en", "evidence-exact", "PRIVATE CLAIM", 0.9),
                    ("meaning-unsafe", "evidence-unsafe", "PRIVATE CLAIM", 0.9),
                ),
            )
            connection.execute("INSERT INTO entity_properties VALUES (?,?,?,?,?,?)", ("property-private", "term-en", "private", "PRIVATE PROPERTY", "private-time", "private-time"))
            connection.commit()
        finally:
            connection.close()


_SOURCE_DDL = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, status TEXT NOT NULL,
    quality_score REAL, payload TEXT NOT NULL, label TEXT NOT NULL,
    canonical_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE terms (entity_id TEXT PRIMARY KEY, language TEXT, text TEXT, normalized TEXT, kind TEXT);
CREATE TABLE meanings (
    entity_id TEXT PRIMARY KEY, subject_entity_id TEXT, term_id TEXT, language TEXT,
    definition TEXT, part_of_speech TEXT, register_label TEXT, domain_label TEXT, sense_order INTEGER
);
CREATE TABLE morphemes (entity_id TEXT PRIMARY KEY, language TEXT, form TEXT, normalized TEXT, kind TEXT, meaning TEXT);
CREATE TABLE term_morphemes (term_id TEXT, morpheme_id TEXT, ordinal INTEGER, surface TEXT, confidence REAL, basis TEXT);
CREATE TABLE pronunciations (entity_id TEXT PRIMARY KEY, term_id TEXT, language TEXT, system TEXT, reading TEXT, dialect TEXT);
CREATE TABLE phoneme_segments (segment_id TEXT PRIMARY KEY, pronunciation_id TEXT, ordinal INTEGER, grapheme TEXT, phoneme TEXT, syllable TEXT, color_key TEXT, features TEXT);
CREATE TABLE historical_forms (entity_id TEXT PRIMARY KEY, language TEXT, form TEXT, normalized TEXT, period_label TEXT, date_min INTEGER, date_max INTEGER, meaning TEXT);
CREATE TABLE history_events (entity_id TEXT PRIMARY KEY, subject_entity_id TEXT, event_type TEXT, language TEXT, period_label TEXT, date_min INTEGER, date_max INTEGER, description TEXT, properties TEXT);
CREATE TABLE translations (entity_id TEXT PRIMARY KEY, source_term_id TEXT, source_meaning_id TEXT, target_language TEXT, target_term_id TEXT, text TEXT, normalized TEXT, transliteration TEXT, usage_note TEXT);
CREATE TABLE grammar_analyses (entity_id TEXT PRIMARY KEY, subject_entity_id TEXT, language TEXT, analysis_type TEXT, summary TEXT);
CREATE TABLE grammar_parts (part_id TEXT PRIMARY KEY, analysis_id TEXT, ordinal INTEGER, surface TEXT, lemma TEXT, role TEXT, part_of_speech TEXT, reading TEXT, color_key TEXT, features TEXT);
CREATE TABLE entity_edges (edge_id TEXT PRIMARY KEY, source_entity_id TEXT, target_entity_id TEXT, relation TEXT, basis TEXT, confidence REAL, status TEXT, properties TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE relation_assertions (assertion_id TEXT PRIMARY KEY, subject_entity_id TEXT, source_entity_id TEXT, target_entity_id TEXT, relation TEXT, basis TEXT, confidence REAL, status TEXT, properties_json TEXT, revision INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE relation_assertion_slots (assertion_id TEXT PRIMARY KEY, slot_key TEXT, slot_mode TEXT, policy_version TEXT, created_at TEXT);
CREATE TABLE relation_assertion_events (event_id TEXT PRIMARY KEY, decision_id TEXT, assertion_id TEXT, action TEXT, before_json TEXT, after_json TEXT, evidence_ids_json TEXT, context_evidence_ids_json TEXT, input_fingerprint TEXT, output_fingerprint TEXT, job_id TEXT, reason TEXT, validator_version TEXT, created_at TEXT);
CREATE TABLE relation_claim_observations (observation_id TEXT PRIMARY KEY, slot_key TEXT, slot_mode TEXT, proposed_assertion_id TEXT, incumbent_assertion_ids_json TEXT, proposal_json TEXT, decision TEXT, exact_evidence_ids_json TEXT, context_evidence_ids_json TEXT, input_fingerprint TEXT, output_fingerprint TEXT, epoch_fingerprint TEXT, prompt_version TEXT, job_id TEXT, reason TEXT, policy_version TEXT, created_at TEXT);
CREATE TABLE assertion_evidence (assertion_id TEXT, evidence_id TEXT);
CREATE TABLE evidence_records (evidence_id TEXT PRIMARY KEY, corpus_id TEXT, source_entry_id TEXT, source_hash TEXT, locator TEXT, excerpt TEXT, payload TEXT);
CREATE TABLE entity_evidence (entity_id TEXT, evidence_id TEXT, claim TEXT, confidence REAL);
CREATE TABLE entity_properties (property_id TEXT PRIMARY KEY, entity_id TEXT, name TEXT, value TEXT, created_at TEXT, updated_at TEXT);
"""


if __name__ == "__main__":
    unittest.main()
