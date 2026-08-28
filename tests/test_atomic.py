from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.atomic import (
    PreparationWorker,
    _clean_usage_note,
    _collapse_repeated_arabic_alternative,
    _has_repeated_arabic_content_word,
    _lexically_related,
)
from lkt.knowledge import KnowledgeStore
from lkt.models import Evidence
from lkt.preparation import PreparationPlanner
from lkt.store import CardStore


class FakeRetriever:
    def retrieve(self, term: str) -> list[dict[str, Any]]:
        return [
            {
                "entry_id": "dictionary-inspection-1",
                "corpus_id": "test-dictionary:1.0",
                "source_title": "Test Dictionary",
                "headword": term,
                "part_of_speech": "noun",
                "definition": "a careful examination of something",
                "translations": {"ja": ["\u691c\u67fb"]},
                "source_hash": "abc123",
                "locator": "sense 1",
            }
        ]


class FakeAtomicModel:
    model_name = "test-qwen-8b"

    def complete_json(
        self, _system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]:
        match = re.search(r'"(evidence-[^"]+)"', prompt)
        assert match is not None
        if "TARGET LANGUAGE: Japanese" in prompt:
            return {
                "value": {
                    "term": "\u691c\u67fb",
                    "meaning": "\u72b6\u614b\u3084\u54c1\u8cea\u3092\u78ba\u304b\u3081\u308b\u305f\u3081\u306e\u516c\u5f0f\u306a\u8abf\u67fb\u3002",
                    "reading": "\u3051\u3093\u3055",
                    "usage_note": "standard formal examination sense",
                    "confidence": 0.9,
                    "evidence_ids": [match.group(1)],
                },
                "model": self.model_name,
                "metrics": {"completion_tokens": 48},
            }
        return {
            "value": {
                "definition": "A careful examination to assess condition or quality.",
                "part_of_speech": "noun",
                "sense_note": "the core examination sense",
                "confidence": 0.92,
                "evidence_ids": [match.group(1)],
            },
            "model": self.model_name,
            "metrics": {"completion_tokens": 40},
        }


class FakePronouncer:
    def pronounce(self, text: str, language: str) -> dict[str, Any]:
        return {
            "reading": "\u026ansp\u02c8\u025bk\u0283\u0259n",
            "system": "ipa",
            "dialect": "en-us",
            "segments": [
                {"grapheme": text, "phoneme": "\u026ansp\u02c8\u025bk\u0283\u0259n", "color_key": "p0"}
            ],
            "source": {"engine": "fake-espeak", "version": "test", "voice": language},
        }


class AtomicWorkerTests(unittest.TestCase):
    def test_morphology_context_rejects_incidental_fts_hits(self) -> None:
        def item(headword: str, kind: str) -> Evidence:
            return Evidence("id", headword, "", "", (), "excerpt", kind=kind)

        self.assertTrue(
            _lexically_related("inspection", item("inspect", "morphology-root"))
        )
        self.assertTrue(
            _lexically_related("inspection", item("-ion", "morphology-affix"))
        )
        self.assertFalse(
            _lexically_related("inspection", item("injurious", "morphology-root"))
        )
        self.assertFalse(
            _lexically_related("inspection", item("autopsy", "morphology-affix"))
        )

    def test_evidence_and_meaning_run_as_two_reusable_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(
                store,
                model="test-qwen-8b",
                source_fingerprint="sources-v1",
            ).plan_word("inspection", display_languages=("en",))
            worker = PreparationWorker(store, FakeRetriever(), FakeAtomicModel())
            results = worker.run(2)
            self.assertEqual(
                [result.job_type for result in results],
                ["retrieve-evidence", "prepare-meaning"],
            )
            self.assertTrue(all(result.status == "complete" for result in results))
            term = store.term_record(plan.subject_entity_id)
            self.assertEqual(term["status"], "accepted")
            self.assertEqual(term["quality_score"], 0.92)
            meaning = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-meaning"
            )
            self.assertEqual(len(meaning), 1)
            self.assertEqual(meaning[0]["payload"]["part_of_speech"], "noun")
            self.assertEqual(meaning[0]["validation_state"], "accepted")
            self.assertEqual(meaning[0]["quality_score"], 0.92)
            self.assertEqual(store.status()["counts"]["meanings"], 1)

    def test_translation_is_a_separate_sense_aligned_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ja")
            )
            worker = PreparationWorker(store, FakeRetriever(), FakeAtomicModel())
            results = worker.run(3)
            self.assertEqual(results[-1].job_type, "prepare-translation")
            artifacts = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-translation"
            )
            self.assertEqual(artifacts[0]["language"], "ja")
            self.assertEqual(artifacts[0]["payload"]["term"], "\u691c\u67fb")
            self.assertEqual(artifacts[0]["payload"]["reading"], "\u3051\u3093\u3055")
            self.assertEqual(artifacts[0]["validation_state"], "accepted")
            self.assertEqual(store.status()["counts"]["translations"], 1)

    def test_translation_output_is_normalized_before_acceptance(self) -> None:
        class FrenchModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "TARGET LANGUAGE: French" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                evidence = re.search(r'"(evidence-[^"]+)"', prompt)
                assert evidence is not None
                return {
                    "value": {
                        "term": "inspection",
                        "meaning": "examen officiel ou formel",
                        "reading": "inspektion",
                        "usage_note": "sens standard et officiel",
                        "confidence": 0.9,
                        "evidence_ids": [evidence.group(1)],
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "fr")
            )
            PreparationWorker(store, FakeRetriever(), FrenchModel()).run(3)
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["payload"]["reading"], "")
            self.assertEqual(artifact["payload"]["usage_note"], "")

    def test_exact_arabic_repetition_is_normalized_before_acceptance(self) -> None:
        class RepetitiveArabicModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "TARGET LANGUAGE: Arabic" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                evidence = re.search(r'"(evidence-[^"]+)"', prompt)
                assert evidence is not None
                return {
                    "value": {
                        "term": "\u0645\u0639\u0627\u064a\u0646\u0629",
                        "meaning": "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0623\u0648 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646",
                        "reading": "mu'ayana",
                        "usage_note": "official inspection",
                        "confidence": 0.9,
                        "evidence_ids": [evidence.group(1)],
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ar")
            )
            results = PreparationWorker(store, FakeRetriever(), RepetitiveArabicModel()).run(3)
            self.assertEqual(results[-1].status, "complete")
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            self.assertEqual(
                artifact["payload"]["meaning"],
                "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646",
            )
            self.assertEqual(
                artifact["payload"]["normalizations"],
                ["collapsed-repeated-arabic-alternative"],
            )

    def test_small_output_quality_helpers_are_deliberately_restrained(self) -> None:
        self.assertEqual(_clean_usage_note("formal examination sense"), "formal examination sense")
        self.assertEqual(_clean_usage_note("\u516c\u5f0f\u306a\u691c\u67fb\u306e\u610f\u5473"), "")
        self.assertTrue(
            _has_repeated_arabic_content_word(
                "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0623\u0648 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646"
            )
        )
        self.assertEqual(
            _collapse_repeated_arabic_alternative(
                "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0623\u0648 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646"
            ),
            "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646",
        )

    def test_worker_does_not_claim_later_unsupported_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en",)
            )
            worker = PreparationWorker(
                store, FakeRetriever(), FakeAtomicModel(), FakePronouncer()
            )
            results = worker.run(10)
            self.assertEqual(len(results), 4)
            self.assertEqual(results[-1].job_type, "prepare-grammar-properties")
            grammar = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-grammar-properties",
                validation_state="accepted",
            )[0]
            self.assertEqual(grammar["payload"]["part_of_speech"], "noun")
            queued_types = {
                job["job_type"]
                for job in store.jobs_for_subject(plan.subject_key)
                if job["status"] == "queued"
            }
            self.assertIn("split-morphemes", queued_types)
            self.assertIn("compose-word-card", queued_types)

    def test_pronunciation_reuses_accepted_atoms_without_an_llm_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en", "zh")
            )
            worker = PreparationWorker(
                store, FakeRetriever(), FakeAtomicModel(), FakePronouncer()
            )
            worker.run(2)
            meaning = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-meaning",
                validation_state="accepted",
            )[0]["payload"]
            target_id = store.upsert_term("zh", "\u68c0\u67e5", status="accepted")
            translation_job = plan.jobs["translation:zh"]
            store.save_job_artifact(
                translation_job,
                "accepted-translation",
                {
                    "translation_id": "translation-zh",
                    "target_term_id": target_id,
                    "language": "zh",
                    "term": "\u68c0\u67e5",
                    "reading": "ji\u01cen ch\u00e1",
                    "confidence": 1.0,
                    "evidence_ids": meaning["evidence_ids"],
                },
                language="zh",
                validation_state="accepted",
                quality_score=1.0,
            )
            store.finish_job(translation_job)
            results = worker.run(2)
            self.assertEqual(
                [result.job_type for result in results],
                ["prepare-pronunciation", "prepare-pronunciation"],
            )
            artifacts = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-pronunciation",
                validation_state="accepted",
            )
            chinese = next(item for item in artifacts if item["language"] == "zh")
            self.assertEqual(chinese["payload"]["system"], "pinyin")
            self.assertEqual(len(chinese["payload"]["segments"]), 2)
            self.assertEqual(store.status()["counts"]["pronunciations"], 2)

    def test_word_card_is_composed_only_from_accepted_atomic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = KnowledgeStore(root / "knowledge.sqlite3")
            cards = CardStore(root / "cards.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ja", "zh", "fr", "ar")
            )
            evidence_id = store.add_evidence(
                "omw-en:2.0",
                "sense-inspection-1",
                locator="sense 1",
                excerpt="A formal or official examination",
                payload={
                    "entry_id": "omw-en:2.0:sense-inspection-1",
                    "headword": "inspection",
                    "source_title": "Open Multilingual Wordnet 2.0",
                    "kind": "dictionary-sense",
                },
            )
            store.finish_job(plan.jobs["retrieve-evidence"])
            meaning_id = "meaning-inspection"
            meaning = {
                "meaning_id": meaning_id,
                "definition": "A formal or official examination.",
                "part_of_speech": "noun",
                "confidence": 0.95,
                "evidence_ids": [evidence_id],
            }
            store.save_job_artifact(
                plan.jobs["meaning:en"],
                "accepted-meaning",
                meaning,
                language="en",
                validation_state="accepted",
                quality_score=0.95,
            )
            store.finish_job(plan.jobs["meaning:en"])
            language_values = {
                "ja": ("\u5be9\u67fb", "\u516c\u5f0f\u306a\u8abf\u67fb", "\u3057\u3093\u3055"),
                "zh": ("\u68c0\u67e5", "\u6b63\u5f0f\u7684\u68c0\u67e5", "ji\u01cen ch\u00e1"),
                "fr": ("inspection", "examen officiel ou formel", "\u025b\u0303sp\u025bksj\u02c8\u0254\u0303"),
                "ar": ("\u0645\u0639\u0627\u064a\u0646\u0629", "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646", "mu\u0295\u02c8a\u02d0jan\u02cca"),
            }
            for language, (term, translated_meaning, reading) in language_values.items():
                translation_job = plan.jobs[f"translation:{language}"]
                store.save_job_artifact(
                    translation_job,
                    "accepted-translation",
                    {
                        "translation_id": f"translation-{language}",
                        "target_term_id": f"term-{language}",
                        "term": term,
                        "meaning": translated_meaning,
                        "reading": reading if language in {"ja", "zh", "ar"} else "",
                        "confidence": 0.9,
                        "evidence_ids": [evidence_id],
                    },
                    language=language,
                    validation_state="accepted",
                    quality_score=0.9,
                )
                store.finish_job(translation_job)

            pronunciation_values = {
                "en": ("inspection", "\u026ansp\u02c8\u025bk\u0283\u0259n"),
                **{
                    language: (values[0], values[2])
                    for language, values in language_values.items()
                },
            }
            for language, (term, reading) in pronunciation_values.items():
                pronunciation_job = plan.jobs[f"pronunciation:{language}"]
                segments = (
                    [
                        {"grapheme": "\u68c0", "phoneme": "ji\u01cen"},
                        {"grapheme": "\u67e5", "phoneme": "ch\u00e1"},
                    ]
                    if language == "zh"
                    else [{"grapheme": term, "phoneme": reading}]
                )
                store.save_job_artifact(
                    pronunciation_job,
                    "accepted-pronunciation",
                    {
                        "target_term_id": f"term-{language}",
                        "language": language,
                        "term": term,
                        "reading": reading,
                        "segments": segments,
                    },
                    language=language,
                    validation_state="accepted",
                    quality_score=0.9,
                )
                store.finish_job(pronunciation_job)
            store.save_job_artifact(
                plan.jobs["grammar-properties"],
                "accepted-grammar-properties",
                {"part_of_speech": "noun", "confidence": 0.95},
                language="en",
                validation_state="accepted",
                quality_score=0.95,
            )
            store.finish_job(plan.jobs["grammar-properties"])

            worker = PreparationWorker(
                store, FakeRetriever(), FakeAtomicModel(), FakePronouncer(), cards
            )
            result = worker.run_once()
            self.assertIsNotNone(result)
            self.assertEqual(result.job_type, "compose-word-card")
            card = cards.recent(1)[0]
            self.assertEqual(card["mode"], "knowledge")
            self.assertEqual(card["english"]["meaning"], meaning["definition"])
            self.assertEqual(card["extra_languages"]["french"]["term"], "inspection")
            self.assertNotIn("morphology_graph", card["extensions"])


if __name__ == "__main__":
    unittest.main()
