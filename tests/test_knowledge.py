from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lkt.graph import rebuild_ladybug
from lkt.knowledge import KnowledgeStore
from lkt.lexicon import WordnetRag


class KnowledgeStoreTests(unittest.TestCase):
    def test_reviewed_card_book_languages_become_idempotent_content_atoms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            card = {
                "card_id": "question-card-100",
                "mode": "question",
                "english": {"term": "Would you accept the cost?"},
                "japanese": {"term": "その代償を受け入れますか？"},
                "chinese": {"simplified": "你会接受这个代价吗？"},
                "evidence": [
                    {
                        "corpus_id": "book-of-questions",
                        "entry_id": "question-100",
                        "locator": "questions.xhtml",
                        "excerpt": "Would you accept the cost?",
                    }
                ],
            }

            first = store.acquire_card_book_card(card)
            second = store.acquire_card_book_card(card)

            self.assertEqual(first, second)
            self.assertEqual(store.status()["counts"]["content_items"], 3)
            with closing(store._connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_edges WHERE relation = 'reviewed-translation'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_evidence"
                    ).fetchone()[0],
                    3,
                )

    def test_atomic_word_knowledge_is_reused_and_projected_as_a_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            inspection = store.upsert_term("en", "Inspection")
            self.assertEqual(inspection, store.upsert_term("en", "inspection"))
            prefix = store.upsert_morpheme("en", "in-", "prefix", "in or into")
            root = store.upsert_morpheme("la", "specere", "root", "to look")
            suffix = store.upsert_morpheme("en", "-ion", "suffix", "action or result")
            store.link_morpheme(inspection, prefix, 0, "in", basis="book", confidence=0.9)
            store.link_morpheme(inspection, root, 1, "spect", basis="book", confidence=0.95)
            store.link_morpheme(inspection, suffix, 2, "ion", basis="book", confidence=0.9)
            latin = store.add_historical_form(
                "la", "inspectio", period_label="Late Latin", meaning="examination"
            )
            store.add_edge(inspection, latin, "derived-from", basis="book", confidence=0.9)
            store.add_history_event(
                inspection,
                "semantic-shift",
                "The sense broadened from close looking to formal examination.",
                language="en",
                period_label="Modern English",
            )
            snapshot = store.graph_snapshot()
            self.assertEqual(len(snapshot["nodes"]), 6)
            relations = {edge["relation"] for edge in snapshot["edges"]}
            self.assertEqual(
                relations, {"has-component", "derived-from", "has-history"}
            )

    def test_language_pronunciation_translation_and_grammar_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            source = store.upsert_term("en", "inspect")
            meaning = store.add_meaning(
                source, "en", "look at closely", part_of_speech="verb"
            )
            japanese = store.upsert_term("ja", "検査する")
            store.add_translation(
                source,
                "ja",
                "検査する",
                transliteration="kensa suru",
                source_meaning_id=meaning,
                target_term_id=japanese,
            )
            store.add_translation(
                source,
                "zh",
                "检查",
                transliteration="jiǎnchá",
                source_meaning_id=meaning,
            )
            pronunciation = store.add_pronunciation(
                japanese,
                "ja",
                "kana",
                "けんさする",
                [
                    {"grapheme": "検", "phoneme": "けん", "color_key": "p0"},
                    {"grapheme": "査", "phoneme": "さ", "color_key": "p1"},
                    {"grapheme": "する", "phoneme": "する", "color_key": "p2"},
                ],
            )
            analysis = store.add_grammar_analysis(
                japanese,
                "ja",
                "noun plus suru verb",
                [
                    {"surface": "検査", "role": "object", "part_of_speech": "noun"},
                    {"surface": "する", "role": "predicate", "part_of_speech": "verb"},
                ],
            )
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM translations WHERE source_term_id = ?",
                    (source,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM phoneme_segments WHERE pronunciation_id = ?",
                    (pronunciation,),
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM grammar_parts WHERE analysis_id = ?",
                    (analysis,),
                ).fetchone()[0],
                2,
            )
            connection.close()

    def test_rejected_morpheme_split_is_quarantined_without_erasing_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            term = store.upsert_term("en", "inspection")
            wrong_root = store.upsert_morpheme("en", "pect", "root", "look")
            store.link_morpheme(term, wrong_root, 0, "pect", basis="model")
            job = store.enqueue_job(
                "split-morphemes", f"term:{term}", subject_entity_id=term
            )
            store.save_job_artifact(
                job,
                "accepted-morpheme-split",
                {"parts": [{"morpheme_id": wrong_root}]},
                language="en",
                validation_state="accepted",
                quality_score=0.8,
            )
            result = store.retire_morpheme_analysis(term, "root was not book grounded")
            self.assertEqual(result["components_removed"], 1)
            self.assertEqual(result["morphemes_archived"], 1)
            artifact = store.artifacts_for_subject(
                f"term:{term}", stage="accepted-morpheme-split"
            )[0]
            self.assertEqual(artifact["validation_state"], "rejected")
            self.assertNotIn("has-component", {
                edge["relation"] for edge in store.graph_snapshot()["edges"]
            })

    def test_jobs_checkpoint_artifacts_and_retry_only_the_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            first = store.enqueue_job(
                "translate",
                "term:inspection",
                language="ja",
                model="Qwen3-8B",
                prompt_version="translation-v1",
                source_fingerprint="books-v1",
            )
            self.assertEqual(
                first,
                store.enqueue_job(
                    "translate",
                    "term:inspection",
                    language="ja",
                    model="Qwen3-8B",
                    prompt_version="translation-v1",
                    source_fingerprint="books-v1",
                ),
            )
            claimed = store.claim_next_job()
            self.assertEqual(claimed["job_id"], first)
            store.save_job_artifact(
                first, "retrieved-evidence", {"source": "JMdict"}, language="ja"
            )
            store.finish_job(first, error="invalid reading")
            self.assertEqual(store.claim_next_job()["attempts"], 2)
            store.finish_job(first, error="invalid reading again")
            self.assertIsNone(store.claim_next_job())
            self.assertEqual(store.status()["counts"]["preparation_jobs"], 1)

    def test_artifact_validation_is_migrated_and_new_acceptance_supersedes_old(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """CREATE TABLE job_artifacts (
                       artifact_id TEXT PRIMARY KEY,
                       job_id TEXT NOT NULL,
                       stage TEXT NOT NULL,
                       language TEXT NOT NULL DEFAULT '',
                       payload TEXT NOT NULL,
                       reusable INTEGER NOT NULL DEFAULT 1,
                       created_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO job_artifacts(
                       artifact_id, job_id, stage, language, payload, created_at
                   ) VALUES ('legacy-accepted', 'missing-job', 'accepted-meaning',
                             'en', '{}', '2026-01-01T00:00:00Z')"""
            )
            connection.commit()
            connection.close()

            store = KnowledgeStore(database)
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT validation_state FROM job_artifacts WHERE artifact_id = ?",
                    ("legacy-accepted",),
                ).fetchone()[0],
                "accepted",
            )
            connection.close()

            first = store.enqueue_job(
                "prepare-translation", "term:inspection", language="fr", prompt_version="v1"
            )
            second = store.enqueue_job(
                "prepare-translation", "term:inspection", language="fr", prompt_version="v2"
            )
            store.save_job_artifact(
                first,
                "accepted-translation",
                {"term": "inspection"},
                language="fr",
                validation_state="accepted",
                quality_score=0.8,
            )
            store.save_job_artifact(
                second,
                "accepted-translation",
                {"term": "inspection"},
                language="fr",
                validation_state="accepted",
                quality_score=0.95,
            )
            artifacts = store.artifacts_for_subject(
                "term:inspection", stage="accepted-translation"
            )
            self.assertEqual(
                [artifact["validation_state"] for artifact in artifacts],
                ["superseded", "accepted"],
            )
            self.assertEqual(artifacts[-1]["quality_score"], 0.95)
            retrieval_v1 = store.enqueue_job(
                "retrieve-evidence", "term:inspection", prompt_version="retrieval-v1"
            )
            retrieval_v2 = store.enqueue_job(
                "retrieve-evidence", "term:inspection", prompt_version="retrieval-v2"
            )
            store.save_job_artifact(
                retrieval_v1, "retrieved-evidence", {"records": ["old"]}
            )
            store.save_job_artifact(
                retrieval_v2, "retrieved-evidence", {"records": ["polished"]}
            )
            retrievals = store.artifacts_for_subject(
                "term:inspection", stage="retrieved-evidence"
            )
            self.assertEqual(
                [artifact["validation_state"] for artifact in retrievals],
                ["superseded", "candidate"],
            )
            self.assertEqual(store.status()["schema_version"], "2")

    def test_inquiry_history_keeps_parent_child_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            source = store.upsert_content_item(
                "question", "en", "What deserves closer inspection?", source_key="q-1"
            )
            result = store.upsert_term("en", "inspection")
            thread = store.create_inquiry_thread("Inspect the question")
            self.assertTrue(store.has_inquiry_thread(thread))
            self.assertFalse(store.has_inquiry_thread("missing-thread"))
            parent = store.save_inquiry_event(
                thread,
                "Explain this question",
                source_entity_id=source,
                compact_summary="Meaning of the source question",
            )
            child = store.save_inquiry_event(
                thread,
                "Investigate inspection",
                parent_event_id=parent,
                source_entity_id=source,
                result_entity_id=result,
                selected_text="inspection",
            )
            other_thread = store.create_inquiry_thread("Other")
            with self.assertRaisesRegex(ValueError, "not in this thread"):
                store.save_inquiry_event(
                    other_thread,
                    "Invalid branch",
                    parent_event_id=parent,
                )
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT parent_event_id, result_entity_id FROM inquiry_events WHERE event_id = ?",
                (child,),
            ).fetchone()
            connection.close()
            self.assertEqual(row, (parent, result))

    def test_ladybug_projection_is_a_rebuildable_copy_of_accepted_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            store = KnowledgeStore(directory / "knowledge.sqlite3")
            child = store.upsert_term("en", "inspection")
            parent = store.add_historical_form("la", "inspectio")
            store.add_edge(child, parent, "derived-from", basis="book")
            calls: list[tuple[str, dict | None]] = []

            class FakeDatabase:
                def __init__(self, path: str, **_kwargs: object):
                    Path(path).mkdir(parents=True)

            class FakeConnection:
                def __init__(self, _database: object):
                    pass

                def execute(self, query: str, parameters: dict | None = None) -> None:
                    calls.append((query, parameters))

                def close(self) -> None:
                    pass

            fake_ladybug = types.SimpleNamespace(
                Database=FakeDatabase, Connection=FakeConnection
            )
            destination = directory / "graph.lbdb"
            with patch.dict(sys.modules, {"ladybug": fake_ladybug}):
                result = rebuild_ladybug(store, destination)
            self.assertTrue(destination.is_dir())
            self.assertEqual(result["nodes"], 2)
            self.assertEqual(result["edges"], 1)
            self.assertEqual(sum("CREATE (n:Entity" in query for query, _ in calls), 2)
            self.assertEqual(sum("KnowledgeEdge" in query for query, _ in calls), 2)

    def test_wordnet_rag_keeps_senses_and_languages_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            class FakeSynset:
                id = "synset-inspect"
                ili = "i-inspect"

                def definition(self) -> str:
                    return "look at closely"

            class FakeSense:
                id = "sense-inspect"

                def synset(self) -> FakeSynset:
                    return FakeSynset()

            class FakeWord:
                id = "word-inspect"
                pos = "v"

                def lemma(self) -> str:
                    return "inspect"

                def forms(self) -> list[str]:
                    return ["inspect", "inspects", "inspected"]

                def senses(self) -> list[FakeSense]:
                    return [FakeSense()]

            class FakeTranslationSynset:
                def __init__(self, specifier: str):
                    self.specifier = specifier

                def lemmas(self) -> list[str]:
                    return {
                        "omw-ja:2.0": ["検査する"],
                        "omw-cmn:2.0": ["检查"],
                    }.get(self.specifier, [])

            class FakeWordnet:
                def __init__(self, specifier: str, expand: str):
                    self.specifier = specifier
                    self.expand = expand

                def words(self, query: str) -> list[FakeWord]:
                    return [FakeWord()] if self.specifier == "omw-en:2.0" and query == "inspect" else []

                def synsets(self, *, ili: str) -> list[FakeTranslationSynset]:
                    self.assert_ili = ili
                    return [FakeTranslationSynset(self.specifier)]

            fake_wn = types.SimpleNamespace(
                config=types.SimpleNamespace(data_directory=None),
                Wordnet=FakeWordnet,
                lexicons=lambda: [],
            )
            with patch.dict(sys.modules, {"wn": fake_wn}):
                evidence = WordnetRag(Path(temp)).search(
                    "inspect", target_languages=("ja", "zh"), limit=2
                )
            self.assertEqual(evidence[0]["definition"], "look at closely")
            self.assertEqual(evidence[0]["translations"]["ja"], ["検査する"])
            self.assertEqual(evidence[0]["translations"]["zh"], ["检查"])


if __name__ == "__main__":
    unittest.main()
