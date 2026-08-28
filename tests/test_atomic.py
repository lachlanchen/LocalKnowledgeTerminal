from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.atomic import (
    PreparationWorker,
    _artifact_quality,
    _book_anchored_shape,
    _book_decomposition_shape,
    _book_origin_steps,
    _clean_usage_note,
    _collapse_repeated_arabic_alternative,
    _explicit_form_evidence_ids,
    _has_repeated_arabic_content_word,
    _lexically_related,
    _clean_morpheme_meaning,
    _morpheme_display_form,
    _plain_letter_key,
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

    def component_evidence(self, form: str, kind: str) -> list[dict[str, Any]]:
        if form.strip("-").casefold() != "spect" or kind != "root":
            return []
        return [
            {
                "entry_id": "root-spect-1",
                "corpus_id": "test-roots:1.0",
                "source_title": "Test Root Dictionary",
                "headword": "SPECT",
                "excerpt": "SPECT comes from Latin and means to look or see.",
                "source_hash": "roots123",
                "locator": "root SPECT",
                "kind": "morphology-root",
            }
        ]

    def origin_evidence(self, form: str) -> list[dict[str, Any]]:
        if form.strip("-").casefold() != "spect":
            return []
        return [
            {
                "entry_id": "origin-spect-1",
                "corpus_id": "test-word-origins:1.0",
                "source_title": "Test Word Origins",
                "headword": "spectacle",
                "excerpt": "Latin specere descends from Indo-European *spek-, to look.",
                "source_hash": "origins123",
                "locator": "spectacle entry",
                "kind": "entry",
            }
        ]


class FakeAtomicModel:
    model_name = "test-qwen-8b"

    def complete_json(
        self, _system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]:
        match = re.search(r'"(evidence-[^"]+)"', prompt)
        if "ONE ORIGIN BRANCH" in prompt:
            assert match is not None
            component_ids = re.findall(r'"component_id": "([^"]+)"', prompt)
            evidence_ids = re.findall(r'"evidence_id": "(evidence-[^"]+)"', prompt)
            assert len(component_ids) == 1
            return {
                "value": {
                    "component_id": component_ids[0],
                    "steps": [
                        {
                            "form": "*spek-",
                            "language": "ine-pro",
                            "period": "Proto-Indo-European",
                            "meaning": ["look"],
                            "confidence": 0.9,
                            "evidence_ids": [evidence_ids[-1]],
                        },
                        {
                            "form": "specere",
                            "language": "la",
                            "period": "Latin",
                            "meaning": "look at",
                            "confidence": 0.9,
                            "evidence_ids": [evidence_ids[-1]],
                        },
                    ],
                },
                "model": self.model_name,
                "metrics": {"completion_tokens": 120},
            }
        if "MORPHEME SPLIT" in prompt:
            assert match is not None
            return {
                "value": {
                    "parts": [
                        {
                            "surface": "in",
                            "canonical_form": "in-",
                            "kind": "prefix",
                            "language": "la",
                            "meaning": "in or into",
                            "confidence": 0.9,
                            "evidence_ids": [match.group(1)],
                        },
                        {
                            "surface": "spect",
                            "canonical_form": "spect",
                            "kind": "root",
                            "language": "la",
                            "meaning": "look or see",
                            "confidence": 0.9,
                            "evidence_ids": [],
                        },
                        {
                            "surface": "ion",
                            "canonical_form": "-ion",
                            "kind": "suffix",
                            "language": "en",
                            "meaning": "action or result",
                            "confidence": 0.8,
                            "evidence_ids": [],
                        },
                    ]
                },
                "model": self.model_name,
                "metrics": {"completion_tokens": 90},
            }
        if "TARGET LANGUAGE: Japanese" in prompt:
            return {
                "value": {
                    "term": "\u691c\u67fb",
                    "meaning": "\u72b6\u614b\u3084\u54c1\u8cea\u3092\u78ba\u304b\u3081\u308b\u305f\u3081\u306e\u516c\u5f0f\u306a\u8abf\u67fb\u3002",
                    "reading": "\u3051\u3093\u3055",
                    "usage_note": "standard formal examination sense",
                    "confidence": 0.9,
                },
                "model": self.model_name,
                "metrics": {"completion_tokens": 48},
            }
        return {
            # Meaning prompts expose fixed evidence IDs; translation prompts do not.
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
    @staticmethod
    def _question_card() -> dict[str, Any]:
        return {
            "card_id": "question-card-100",
            "mode": "question",
            "english": {
                "term": "Would a technological breakthrough justify an enormous cost for people?"
            },
            "japanese": {"term": "技術的進歩は大きな代償を正当化しますか？"},
            "chinese": {"simplified": "技术突破能证明巨大代价是合理的吗？"},
            "evidence": [
                {
                    "corpus_id": "book-of-questions",
                    "entry_id": "question-100",
                    "locator": "questions.xhtml",
                    "excerpt": "Would a technological breakthrough justify an enormous cost for people?",
                }
            ],
        }

    def test_investigation_terms_are_bounded_to_reviewed_source_words(self) -> None:
        class InvestigationModel:
            model_name = "test-qwen-4b"

            def complete_json(
                self, _system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                self.last_prompt = prompt
                return {
                    "value": {
                        "terms": [
                            {
                                "surface": "technological",
                                "note": "Connects technology with ethical consequences",
                                "confidence": 0.9,
                            },
                            {
                                "surface": "breakthrough",
                                "note": "A vivid compound for major discovery",
                                "confidence": 0.8,
                            },
                            {
                                "surface": "people",
                                "note": "A generic word that should be filtered",
                                "confidence": 0.9,
                            },
                        ]
                    },
                    "model": self.model_name,
                    "metrics": {"completion_tokens": 42},
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            acquired = store.acquire_card_book_card(self._question_card())
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_card_investigations(
                "question-card-100"
            )
            result = PreparationWorker(
                store, FakeRetriever(), InvestigationModel()
            ).run_once()

            self.assertIsNotNone(result)
            self.assertEqual(result.job_type, "extract-investigation-terms")
            self.assertEqual(result.status, "complete")
            terms = store.investigation_terms(acquired["source_entity_id"])
            self.assertEqual(
                [item["term"] for item in terms],
                ["technological", "breakthrough"],
            )
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-investigation-terms",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["quality_score"], 0.75)
            self.assertEqual(
                artifact["payload"]["rejected_terms"],
                [{"surface": "people", "reason": "too generic"}],
            )
            self.assertTrue(all(item["confidence"] <= 0.75 for item in terms))

    def test_investigation_term_absent_from_source_is_rejected(self) -> None:
        class HallucinatingModel:
            model_name = "test-qwen-4b"

            def complete_json(
                self, _system: str, _prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                return {
                    "value": {
                        "terms": [
                            {
                                "surface": "compromise",
                                "note": "A useful ethical decision word",
                                "confidence": 0.9,
                            }
                        ]
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            acquired = store.acquire_card_book_card(self._question_card())
            PreparationPlanner(store, model="test-qwen-4b").plan_card_investigations(
                "question-card-100"
            )
            result = PreparationWorker(
                store, FakeRetriever(), HallucinatingModel()
            ).run_once()

            self.assertIsNotNone(result)
            self.assertEqual(result.status, "retry")
            self.assertEqual(
                store.investigation_terms(acquired["source_entity_id"]), []
            )

    def test_artifact_quality_uses_payload_confidence_only_when_metadata_is_missing(
        self,
    ) -> None:
        self.assertEqual(
            _artifact_quality({"quality_score": None, "payload": {"confidence": 0.95}}),
            0.95,
        )
        self.assertEqual(
            _artifact_quality({"quality_score": 0.0, "payload": {"confidence": 0.95}}),
            0.0,
        )

    def test_exact_book_root_anchors_the_surface_split(self) -> None:
        records = [
            {
                "headword": "SPECT",
                "component_hint": "root",
                "component_surface": "spect",
            },
            {
                "headword": "SPEC",
                "component_hint": "root",
                "component_surface": "spec",
            },
        ]
        self.assertEqual(
            _book_anchored_shape("inspection", records),
            [
                {"surface": "in", "kind": "prefix"},
                {"surface": "spect", "kind": "root"},
                {"surface": "ion", "kind": "suffix"},
            ],
        )

    def test_exact_book_formula_beats_incidental_substring_roots(self) -> None:
        records = [
            {
                "headword": "predecessor",
                "kind": "morphology-root",
                "knowledge_evidence_id": "evidence-predecessor",
                "excerpt": (
                    "predecessor [pre(=before)＋de(=down)＋cess(=go)] "
                    "former holder of an office"
                ),
            },
            {
                "headword": "PRED",
                "kind": "morphology-root",
                "component_hint": "root",
                "component_surface": "pred",
                "knowledge_evidence_id": "evidence-pred",
                "excerpt": "PRED means to plunder",
            },
        ]
        self.assertEqual(
            _book_decomposition_shape("predecessor", records),
            [
                {
                    "surface": "pre",
                    "kind": "",
                    "evidence_ids": ["evidence-predecessor"],
                },
                {
                    "surface": "de",
                    "kind": "",
                    "evidence_ids": ["evidence-predecessor"],
                },
                {
                    "surface": "cess",
                    "kind": "root",
                    "evidence_ids": ["evidence-predecessor"],
                },
                {"surface": "or", "kind": "suffix", "evidence_ids": []},
            ],
        )

    def test_morpheme_display_notation_is_deterministic(self) -> None:
        self.assertEqual(_morpheme_display_form("in", "prefix"), "in-")
        self.assertEqual(_morpheme_display_form("spect", "root"), "spect")
        self.assertEqual(_morpheme_display_form("ion", "suffix"), "-ion")
        self.assertEqual(
            _clean_morpheme_meaning("to look, to see"), "to look or to see"
        )
        self.assertEqual(_plain_letter_key("dēcēdere"), "decedere")
        self.assertEqual(
            _explicit_form_evidence_ids(
                "dēcēdere",
                [
                    {
                        "evidence_id": "book-entry",
                        "excerpt": "Latin dÄ“cÄ“dere â€˜go awayâ€™",
                    }
                ],
            ),
            ["book-entry"],
        )

    def test_origin_rejects_modern_word_as_a_historical_step(self) -> None:
        class RepeatingOriginModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ONE ORIGIN BRANCH" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                component_id = re.findall(r'"component_id": "([^"]+)"', prompt)[0]
                return {
                    "value": {
                        "component_id": component_id,
                        "steps": [
                            {
                                "form": "inspection",
                                "language": "en",
                                "period": "Modern English",
                                "meaning": "the modern word",
                                "confidence": 0.9,
                                "evidence_ids": [],
                            }
                        ],
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en",)
            )
            results = PreparationWorker(
                store, FakeRetriever(), RepeatingOriginModel()
            ).run(4)
            self.assertEqual(results[-1].job_type, "expand-origin-branches")
            self.assertEqual(results[-1].status, "retry")
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-origin-branches",
                    validation_state="accepted",
                ),
                [],
            )

    def test_explicit_book_chain_is_extracted_without_model_inference(self) -> None:
        steps = _book_origin_steps(
            [
                {
                    "evidence_id": "evidence-spectacle",
                    "excerpt": (
                        "Latin specere ‘look’ (a descendant of the "
                        "Indo-European base *spek- ‘look’)"
                    ),
                }
            ]
        )
        self.assertEqual([step["form"] for step in steps], ["*spek-", "specere"])
        self.assertEqual([step["language"] for step in steps], ["ine-pro", "la"])
        self.assertTrue(all(step["evidence_ids"] == ["evidence-spectacle"] for step in steps))

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
            results = worker.run(5)
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
                return {
                    "value": {
                        "term": "inspection",
                        "meaning": "examen officiel ou formel",
                        "reading": "inspektion",
                        "usage_note": "sens standard et officiel",
                        "confidence": 0.9,
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "fr")
            )
            PreparationWorker(store, FakeRetriever(), FrenchModel()).run(5)
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
                return {
                    "value": {
                        "term": "\u0645\u0639\u0627\u064a\u0646\u0629",
                        "meaning": "\u0641\u062d\u0635 \u0631\u0633\u0645\u064a \u0623\u0648 \u0631\u0633\u0645\u064a \u0644\u0634\u064a\u0621 \u0645\u0639\u064a\u0646",
                        "reading": "mu'ayana",
                        "usage_note": "official inspection",
                        "confidence": 0.9,
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ar")
            )
            results = PreparationWorker(store, FakeRetriever(), RepetitiveArabicModel()).run(5)
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

    def test_mixed_script_arabic_translation_is_rejected(self) -> None:
        class MixedArabicModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "TARGET LANGUAGE: Arabic" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                return {
                    "value": {
                        "term": "انBREAKTHROUGH",
                        "meaning": "إنجاز مهم",
                        "reading": "breakthrough",
                        "usage_note": "",
                        "confidence": 0.9,
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "inspection", display_languages=("en", "ar")
            )
            results = PreparationWorker(store, FakeRetriever(), MixedArabicModel()).run(3)
            self.assertEqual(results[-1].status, "retry")
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-translation",
                    validation_state="accepted",
                ),
                [],
            )

    def test_mixed_arabic_draft_gets_one_bounded_script_repair(self) -> None:
        class RepairingArabicModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ARABIC SCRIPT REPAIR" in prompt:
                    return {
                        "value": {
                            "term": "اختراق",
                            "meaning": "اكتشاف مهم يؤدي إلى تقدم جديد",
                            "reading": "ikhtiraq",
                            "usage_note": "major advance sense",
                            "confidence": 0.86,
                        },
                        "model": self.model_name,
                    }
                if "TARGET LANGUAGE: Arabic" in prompt:
                    return {
                        "value": {
                            "term": "انBREAKTHROUGH",
                            "meaning": "إنجاز مهم",
                            "reading": "breakthrough",
                            "usage_note": "",
                            "confidence": 0.7,
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "inspection", display_languages=("en", "ar")
            )
            results = PreparationWorker(
                store, FakeRetriever(), RepairingArabicModel()
            ).run(3)
            self.assertEqual(results[-1].status, "complete")
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["payload"]["term"], "اختراق")
            self.assertEqual(
                artifact["payload"]["normalizations"],
                ["repaired-arabic-script"],
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

    def test_origin_expansion_is_atomic_and_later_composition_stays_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en",)
            )
            worker = PreparationWorker(
                store, FakeRetriever(), FakeAtomicModel(), FakePronouncer()
            )
            results = worker.run(10)
            self.assertEqual(len(results), 6)
            self.assertEqual(results[-1].job_type, "prepare-grammar-properties")
            grammar = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-grammar-properties",
                validation_state="accepted",
            )[0]
            self.assertEqual(grammar["payload"]["part_of_speech"], "noun")
            split = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-morpheme-split",
                validation_state="accepted",
            )[0]["payload"]["parts"]
            self.assertEqual([part["surface"] for part in split], ["in", "spect", "ion"])
            self.assertEqual([part["basis"] for part in split], ["model", "book", "model"])
            draft = store.artifacts_for_subject(
                plan.subject_key,
                stage="model-morpheme-draft",
                validation_state="candidate",
            )
            self.assertEqual(len(draft), 1)
            origin = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-origin-branches",
                validation_state="accepted",
            )
            self.assertEqual(len(origin), 1)
            root_branch = next(
                branch
                for branch in origin[0]["payload"]["branches"]
                if branch["component_kind"] == "root"
            )
            self.assertEqual(
                [step["form"] for step in root_branch["steps"]],
                ["*spek-", "specere"],
            )
            self.assertTrue(all(step["basis"] == "book" for step in root_branch["steps"]))
            self.assertEqual(store.status()["counts"]["historical_forms"], 2)
            queued_types = {
                job["job_type"]
                for job in store.jobs_for_subject(plan.subject_key)
                if job["status"] == "queued"
            }
            self.assertNotIn("expand-origin-branches", queued_types)
            self.assertIn("compose-origin-card", queued_types)
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
            store.finish_job(plan.jobs["split-morphemes"])
            store.finish_job(plan.jobs["expand-origin-branches"])
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
            split_parts = [
                {
                    "morpheme_id": "m-in",
                    "surface": "in",
                    "canonical_form": "in-",
                    "kind": "prefix",
                    "language": "en",
                    "meaning": "into",
                    "basis": "model",
                    "confidence": 0.8,
                    "evidence_ids": [],
                },
                {
                    "morpheme_id": "m-spect",
                    "surface": "spect",
                    "canonical_form": "spect",
                    "kind": "root",
                    "language": "la",
                    "meaning": "look",
                    "basis": "book",
                    "confidence": 0.95,
                    "evidence_ids": [evidence_id],
                },
                {
                    "morpheme_id": "m-ion",
                    "surface": "ion",
                    "canonical_form": "-ion",
                    "kind": "suffix",
                    "language": "en",
                    "meaning": "process",
                    "basis": "model",
                    "confidence": 0.8,
                    "evidence_ids": [],
                },
            ]
            store.save_job_artifact(
                plan.jobs["split-morphemes"],
                "accepted-morpheme-split",
                {"term": "inspection", "parts": split_parts},
                language="en",
                validation_state="accepted",
                quality_score=0.8,
            )
            store.finish_job(plan.jobs["split-morphemes"])
            store.save_job_artifact(
                plan.jobs["expand-origin-branches"],
                "accepted-origin-branches",
                {
                    "term": "inspection",
                    "branches": [
                        {
                            "component_id": "m-in",
                            "component_form": "in-",
                            "component_kind": "prefix",
                            "steps": [],
                        },
                        {
                            "component_id": "m-spect",
                            "component_form": "spect",
                            "component_kind": "root",
                            "steps": [
                                {
                                    "historical_form_id": "h-pie",
                                    "form": "*spek-",
                                    "language": "ine-pro",
                                    "period": "Proto-Indo-European",
                                    "meaning": "look",
                                    "basis": "book",
                                    "confidence": 0.95,
                                    "evidence_ids": [evidence_id],
                                },
                                {
                                    "historical_form_id": "h-latin",
                                    "form": "specere",
                                    "language": "la",
                                    "period": "Latin",
                                    "meaning": "look",
                                    "basis": "book",
                                    "confidence": 0.95,
                                    "evidence_ids": [evidence_id],
                                },
                            ],
                        },
                        {
                            "component_id": "m-ion",
                            "component_form": "-ion",
                            "component_kind": "suffix",
                            "steps": [],
                        },
                    ],
                },
                language="en",
                validation_state="accepted",
                quality_score=0.95,
            )
            store.finish_job(plan.jobs["expand-origin-branches"])

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
            origin_result = worker.run_once()
            self.assertEqual(origin_result.job_type, "compose-origin-card")
            cards_by_mode = {card["mode"]: card for card in cards.recent(10)}
            self.assertEqual(
                set(cards_by_mode), {"knowledge", "word", "root", "affix"}
            )
            origin_card = cards_by_mode["word"]
            graph = origin_card["extensions"]["morphology_graph"]
            self.assertEqual(len(graph["nodes"]), 6)
            self.assertEqual(len(graph["edges"]), 5)
            self.assertEqual(
                [area["kind"] for area in graph["focus_areas"]],
                ["overview", "overview", "root", "prefix", "suffix"],
            )
            root_graph = cards_by_mode["root"]["extensions"]["morphology_graph"]
            self.assertEqual(root_graph["focus_areas"][0]["kind"], "root")
            self.assertEqual(root_graph["center_id"], "m-spect")
            affix_graph = cards_by_mode["affix"]["extensions"]["morphology_graph"]
            self.assertEqual(
                [area["kind"] for area in affix_graph["focus_areas"][:2]],
                ["prefix", "suffix"],
            )
            self.assertEqual(affix_graph["center_id"], "m-in")


if __name__ == "__main__":
    unittest.main()
