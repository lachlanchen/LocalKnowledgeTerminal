from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lkt.atomic import PreparationWorker
from lkt.freedict import FREEDICT_CORPUS_ID
from lkt.knowledge import KnowledgeStore
from lkt.language_projection import language_card_atoms, project_language_atoms
from lkt.language_repair import (
    LanguageRepairSeeder, align_translation_reading, inspect_subject,
    restore_translation_checkpoint,
)


class LanguageRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(Path(temporary.name) / "knowledge.sqlite3")

    def word(self, text="resilient"):
        term = self.store.upsert_term("en", text)
        subject = "term:" + term
        evidence = self.store.add_evidence(
            "fixture-book", text, excerpt="able to withstand force",
            payload={"headword": text, "source_title": "Fixture book", "pages": [3]},
        )
        meaning = self.store.add_meaning(
            term, "en", "able to withstand force", part_of_speech="adjective",
        )
        job = self.store.enqueue_job("prepare-meaning", subject, subject_entity_id=term, language="en")
        self.store.save_job_artifact(job, "accepted-meaning", {
            "meaning_id": meaning, "definition": "able to withstand force",
            "part_of_speech": "adjective", "evidence_ids": [evidence],
        }, language="en", validation_state="accepted")
        self.store.finish_job(job)
        retrieval = self.store.enqueue_job("retrieve-evidence", subject, subject_entity_id=term)
        self.store.save_job_artifact(retrieval, "retrieved-evidence", {"records": [{
            "knowledge_evidence_id": evidence, "headword": text,
            "entry_id": text, "corpus_id": "fixture-book",
            "source_title": "Fixture book", "excerpt": "able to withstand force",
            "pages": [3],
        }]})
        self.store.finish_job(retrieval)
        return term, meaning

    def translation(self, source, sense, language="zh", text="\u575a\u97e7",
                    reading="jian ren", meaning="\u80fd\u591f\u627f\u53d7\u538b\u529b"):
        target = self.store.upsert_term(language, text)
        translation = self.store.add_translation(
            source, language, text, source_meaning_id=sense, target_term_id=target,
            definition=meaning, transliteration=reading,
        )
        job = self.store.enqueue_job(
            "prepare-translation", "term:" + source, subject_entity_id=source,
            language=language, prompt_version="fixture-" + text,
        )
        payload = {
            "translation_id": translation, "target_term_id": target,
            "source_meaning_id": sense, "term": text, "meaning": meaning,
            "reading": reading, "confidence": 0.7, "evidence_ids": [],
        }
        self.store.save_job_artifact(job, "accepted-translation", payload,
                                    language=language, validation_state="accepted")
        self.store.finish_job(job)
        return target, payload

    def pronunciation(self, source, target, language, text, reading, system):
        job = self.store.enqueue_job(
            "prepare-pronunciation", "term:" + source, subject_entity_id=source,
            language=language, prompt_version="fixture-" + reading,
        )
        payload = {"target_term_id": target, "language": language, "term": text,
                   "reading": reading, "system": system, "confidence": 0.9,
                   "segments": [{"grapheme": text, "phoneme": reading}]}
        self.store.save_job_artifact(job, "accepted-pronunciation", payload,
                                    language=language, validation_state="accepted")
        self.store.finish_job(job)

    def test_missing_language_detection_is_not_word_specific(self):
        for text in ("resilient", "persistent"):
            term, _ = self.word(text)
            observations = inspect_subject(self.store, term)
            self.assertEqual({x["language"] for x in observations}, {"ja", "zh", "fr", "ar"})
            self.assertTrue(all("missing-translation" in x["issues"] for x in observations))

    def test_one_language_repair_does_not_wait_for_every_other_language(self):
        term, _ = self.word()
        seeder = LanguageRepairSeeder(self.store, model="local-qwen", languages=("zh",))
        seeder.observe(term)
        plan = seeder.enqueue_next()
        self.assertTrue(plan["selected"])
        self.assertEqual(plan["language"], "zh")
        jobs = [j for j in self.store.jobs_for_subject("term:" + term)
                if j["prompt_version"].startswith("language-repair-v1:")]
        self.assertEqual({j["job_type"] for j in jobs}, {
            "retrieve-evidence", "prepare-translation", "prepare-pronunciation", "compose-word-card",
        })
        self.assertFalse(seeder.enqueue_next()["selected"])
        self.assertFalse(any(j["language"] in {"ja", "ar", "fr"} for j in jobs))

    def test_unsuccessful_repair_enters_durable_cooldown(self):
        term, _ = self.word()
        seeder = LanguageRepairSeeder(self.store, model="local-qwen", languages=("zh",))
        seeder.observe(term)
        seeder.enqueue_next()
        for job in self.store.jobs_for_subject("term:" + term):
            if job["prompt_version"].startswith("language-repair-v1:"):
                self.store.finish_job(job["job_id"])
        seeder.observe(term)
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT * FROM lexical_language_health").fetchone()
        self.assertEqual(row["status"], "cooldown")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(row["next_eligible_at"])
        restarted = LanguageRepairSeeder(self.store, model="local-qwen", languages=("zh",))
        restarted.observe(term)
        self.assertFalse(restarted.enqueue_next()["selected"])

    def test_missing_sense_link_is_detected_without_deleting_translation(self):
        source, sense = self.word()
        target, translation = self.translation(source, sense)
        self.pronunciation(source, target, "zh", translation["term"], translation["reading"], "pinyin")
        with closing(self.store._connect()) as connection:
            connection.execute("DELETE FROM entity_edges WHERE relation='translated-from'")
            connection.commit()
        result = inspect_subject(self.store, source, ("zh",))[0]
        self.assertIn("missing-target-sense-link", result["issues"])
        self.assertEqual(result["previous_translation"]["translation_id"], translation["translation_id"])

    def test_projection_keeps_all_languages_without_diagram_traversal(self):
        source, sense = self.word()
        for language, text, definition in (
            ("zh", "\u575a\u97e7", "\u80fd\u591f\u627f\u53d7\u538b\u529b"),
            ("ja", "\u5f37\u3044", "\u529b\u304c\u3042\u308b"),
            ("fr", "robuste", "qui resiste a la pression"),
            ("ar", "\u0642\u0648\u064a", "\u064a\u062a\u062d\u0645\u0644 \u0627\u0644\u0636\u063a\u0637"),
        ):
            self.translation(source, sense, language, text, "reading", definition)
        with patch.object(self.store, "lexical_subgraph", side_effect=AssertionError("bounded graph used")):
            atoms = project_language_atoms(self.store, source)
        self.assertEqual(set(atoms), {"en", "ja", "zh", "fr", "ar"})

    def test_dictionary_pronunciation_wins_over_conflicting_translation_reading(self):
        source, sense = self.word()
        target, translation = self.translation(
            source, sense, "ja", "\u5f37\u3044", "\u3064\u3088", "\u529b\u304c\u3042\u308b",
        )
        self.pronunciation(source, target, "ja", translation["term"], "\u3064\u3088\u3044", "kana")
        observations = inspect_subject(self.store, source, ("ja",))
        self.assertIn("reading-mismatch", observations[0]["issues"])
        atom = project_language_atoms(self.store, source)["ja"]
        self.assertEqual(atom["reading"], "\u3064\u3088\u3044")
        self.assertEqual(atom["ruby_tokens"][0]["r"], atom["reading"])

    def test_stale_pronunciation_cannot_overwrite_a_new_translation(self):
        atoms = language_card_atoms(
            {"entity_id": "source", "language": "en", "text": "word"},
            {"payload": {"definition": "a word"}},
            {"fr": {"payload": {"term": "nouveau", "meaning": "recent",
                                "target_term_id": "new", "reading": "new-reading"}}},
            {"fr": {"payload": {"term": "ancien", "target_term_id": "old",
                                "reading": "wrong-old-reading"}}},
        )
        self.assertEqual(atoms["fr"]["reading"], "new-reading")

    def test_reading_repair_converges_in_storage_and_observations(self):
        source, sense = self.word()
        target, translation = self.translation(
            source, sense, "ja", "\u5f37\u3044", "\u3064\u3088", "\u529b\u304c\u3042\u308b",
        )
        self.pronunciation(source, target, "ja", translation["term"], "\u3064\u3088\u3044", "kana")
        job_id = self.store.enqueue_job(
            "prepare-pronunciation", "term:" + source, subject_entity_id=source,
            language="ja", prompt_version="align-fixture",
        )
        align_translation_reading(
            self.store, {"job_id": job_id, "subject_entity_id": source, "language": "ja"},
            translation, "\u3064\u3088\u3044", {"engine": "JMdict"}, [],
        )
        self.assertNotIn("reading-mismatch", inspect_subject(self.store, source, ("ja",))[0]["issues"])
        with closing(self.store._connect()) as connection:
            stored = connection.execute(
                "SELECT transliteration FROM translations WHERE entity_id=?",
                (translation["translation_id"],),
            ).fetchone()[0]
        self.assertEqual(stored, "\u3064\u3088\u3044")

    def test_structural_repair_restores_links_without_model_inference(self):
        source, sense = self.word()
        target, translation = self.translation(source, sense)
        self.pronunciation(source, target, "zh", translation["term"], translation["reading"], "pinyin")
        with closing(self.store._connect()) as connection:
            connection.execute("DELETE FROM entity_edges WHERE relation='translated-from'")
            connection.commit()
        seeder = LanguageRepairSeeder(self.store, model="local-qwen", languages=("zh",))
        seeder.observe(source)
        plan = seeder.enqueue_next()
        self.assertEqual(plan["repair_mode"], "restore-checkpoint")
        worker = PreparationWorker(self.store, object(), object(), pronouncer=object())
        worker._prepare_translation({
            "job_id": plan["translation_job_id"], "subject_entity_id": source,
            "subject_key": "term:" + source, "language": "zh",
        })
        self.assertNotIn("missing-target-sense-link", inspect_subject(self.store, source, ("zh",))[0]["issues"])

    def test_restoration_does_not_reactivate_a_quarantined_target(self):
        source, sense = self.word()
        target, translation = self.translation(source, sense)
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE entities SET status='archived' WHERE entity_id=?", (target,))
            connection.commit()
        job = self.store.enqueue_job(
            "prepare-translation", "term:" + source, subject_entity_id=source,
            language="zh", prompt_version="restore-quarantine",
        )
        with self.assertRaisesRegex(ValueError, "quarantined"):
            restore_translation_checkpoint(
                self.store, {"job_id": job, "subject_entity_id": source, "language": "zh"}, translation,
            )

    def test_apparent_script_and_tautology_problems_are_observations(self):
        source, sense = self.word()
        self.translation(source, sense, "zh", "resilient", "resilient", "resilient")
        issues = inspect_subject(self.store, source, ("zh",))[0]["issues"]
        self.assertIn("unexpected-term-script", issues)
        self.assertIn("uninformative-definition", issues)
        self.assertIn("zh", project_language_atoms(self.store, source))

    def test_valid_arabic_model_answer_is_not_forced_to_another_dictionary_sense(self):
        source, sense = self.word()
        record = {
            "headword": "resilient", "kind": "bilingual-dictionary",
            "translation_scope": "exact-headword", "corpus_id": FREEDICT_CORPUS_ID,
            "translations": {"ar": ["\u0628\u064a\u0631\u0629"]},
        }
        evidence = self.store.add_evidence(FREEDICT_CORPUS_ID, "fixture-ar",
                                          excerpt="a different sense", payload=record)
        record["knowledge_evidence_id"] = evidence
        retrieval = self.store.enqueue_job("retrieve-evidence", "term:" + source,
                                           subject_entity_id=source, prompt_version="fresh")
        self.store.save_job_artifact(retrieval, "retrieved-evidence", {"records": [record]})
        self.store.finish_job(retrieval)
        job_id = self.store.enqueue_job("prepare-translation", "term:" + source,
                                        subject_entity_id=source, language="ar")
        calls = []

        class Model:
            model_name = "local-qwen-fixture"

            def complete_json(self, system, prompt, **kwargs):
                calls.append(prompt)
                return {"value": {
                    "term": "\u0642\u0648\u064a",
                    "meaning": "\u064a\u062a\u062d\u0645\u0644 \u0627\u0644\u0636\u063a\u0637",
                    "reading": "qawi", "confidence": 0.8,
                }, "model": self.model_name}

        worker = PreparationWorker(self.store, object(), Model(), pronouncer=object())
        worker._prepare_translation({
            "job_id": job_id, "subject_entity_id": source,
            "subject_key": "term:" + source, "language": "ar",
        })
        artifact = self.store.artifacts_for_subject(
            "term:" + source, stage="accepted-translation", validation_state="accepted",
        )[-1]
        self.assertEqual(artifact["payload"]["term"], "\u0642\u0648\u064a")
        self.assertEqual(artifact["payload"]["provenance"]["basis"], "model")
        self.assertEqual(artifact["payload"]["evidence_ids"], [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
