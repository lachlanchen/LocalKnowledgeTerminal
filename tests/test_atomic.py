from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lkt.atomic import (
    PreparationWorker,
    WordEvidenceRetriever,
    _affix_origin_story,
    _artifact_quality,
    _attach_verbatim_origin_evidence,
    _book_anchored_shape,
    _book_decomposition_shape,
    _book_origin_steps,
    _clean_usage_note,
    _collapse_repeated_arabic_alternative,
    _explicit_form_evidence_ids,
    _origin_draft_review_reason,
    _origin_cross_reference_targets,
    _origin_history_headline,
    _normalize_origin_draft,
    _normalize_dictionary_candidate,
    _origin_source_record_matches,
    _origin_source_evidence_supported,
    _has_repeated_arabic_content_word,
    _align_grammar_draft,
    _grammar_role_matches,
    _lexically_related,
    _clean_morpheme_meaning,
    _align_grammar_parts,
    _morpheme_display_form,
    _normalise_grammar_labels,
    _normalise_grammar_role,
    _plain_letter_key,
    _publication_provenance,
    _retrieved_component_evidence_ids,
)
from lkt.knowledge import KnowledgeStore
from lkt.jmdict import JapaneseReadingIndex, build_jmdict_index
from lkt.models import Card, Evidence
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
                "excerpt": "SPECT comes from Latin specere and means to look or see.",
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
                "excerpt": "SPECT: Latin specere descends from Indo-European *spek-, to look.",
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

    def test_reviewed_sentence_grammar_is_exact_evidence_linked_knowledge(self) -> None:
        class GrammarModel:
            model_name = "test-qwen-4b"

            def complete_json(
                self, _system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                self.last_prompt = prompt
                return {
                    "value": {
                        "summary": "modal question with subject, predicate, object, and modifier",
                        "parts": [
                            {
                                "surface": "Would",
                                "lemma": "would",
                                "role": "modifier",
                                "part_of_speech": "auxiliary",
                                "confidence": 0.92,
                            },
                            {
                                "surface": "a technological breakthrough",
                                "lemma": "technological breakthrough",
                                "role": "subject",
                                "part_of_speech": "phrase",
                                "confidence": 0.9,
                            },
                            {
                                "surface": "justify",
                                "lemma": "justify",
                                "role": "predicate",
                                "part_of_speech": "verb",
                                "confidence": 0.95,
                            },
                            {
                                "surface": "an enormous cost",
                                "lemma": "enormous cost",
                                "role": "object",
                                "part_of_speech": "phrase",
                                "confidence": 0.91,
                            },
                            {
                                "surface": "for people?",
                                "lemma": "for people",
                                "role": "modifier",
                                "part_of_speech": "phrase",
                                "confidence": 0.88,
                            },
                        ],
                    },
                    "model": self.model_name,
                    "metrics": {"completion_tokens": 92},
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            acquired = store.acquire_card_book_card(self._question_card())
            source_id = acquired["language_entity_ids"]["en"]
            subject_key = f"content:{source_id}"
            store.enqueue_job(
                "prepare-grammar-parts",
                subject_key,
                subject_entity_id=source_id,
                language="en",
                model="test-qwen-4b",
                prompt_version="grammar-test-v1",
            )
            model = GrammarModel()
            result = PreparationWorker(store, FakeRetriever(), model).run_once()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.job_type, "prepare-grammar-parts")
            self.assertEqual(result.status, "complete")
            analysis = store.grammar_for_content(source_id)
            self.assertIsNotNone(analysis)
            assert analysis is not None
            self.assertEqual(
                "".join(part["surface"] for part in analysis["parts"]),
                self._question_card()["english"]["term"],
            )
            self.assertEqual(
                [part["color_key"] for part in analysis["parts"]],
                [
                    "grammar-modifier",
                    "grammar-subject",
                    "grammar-predicate",
                    "grammar-object",
                    "grammar-modifier",
                ],
            )
            self.assertTrue(store.evidence_for_entity(analysis["entity_id"]))
            artifact = store.artifacts_for_subject(
                subject_key,
                stage="accepted-grammar-parts",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["payload"]["source_entity_id"], source_id)
            self.assertIn("REVIEWED QUESTION TEXT", model.last_prompt)

    def test_grammar_alignment_rejects_missing_reviewed_words(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not reach the end"):
            _align_grammar_parts(
                "A complete reviewed sentence.",
                [{"surface": "A complete", "role": "subject"}],
            )

    def test_grammar_draft_keeps_exact_prefix_and_discards_prompt_leakage(self) -> None:
        source = "一步之遥"
        valid = {
            "surface": source,
            "lemma": source,
            "role": "noun",
            "part_of_speech": "noun",
            "confidence": 1,
        }
        self.assertEqual(_align_grammar_draft(source, valid), [valid])
        leaked = {
            "parts": [
                valid,
                {
                    "surface": "Return exactly one JSON object",
                    "role": "other",
                    "part_of_speech": "phrase",
                    "confidence": 1,
                },
            ]
        }
        self.assertEqual(_align_grammar_draft(source, leaked), [valid])

    def test_bare_exact_grammar_part_is_normalized_without_another_model_call(self) -> None:
        reviewed = self._question_card()["english"]["term"]

        class BarePartModel:
            model_name = "test-qwen-4b"

            def complete_json(
                self, _system: str, _prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                return {
                    "value": {
                        "surface": reviewed,
                        "lemma": "",
                        "role": "noun",
                        "part_of_speech": "phrase",
                        "confidence": 0.9,
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            acquired = store.acquire_card_book_card(self._question_card())
            source_id = acquired["language_entity_ids"]["en"]
            store.enqueue_job(
                "prepare-grammar-parts",
                f"content:{source_id}",
                subject_entity_id=source_id,
                language="en",
            )
            result = PreparationWorker(
                store, FakeRetriever(), BarePartModel()
            ).run_once()

            self.assertEqual(result.status, "complete")
            analysis = store.grammar_for_content(source_id)
            self.assertEqual(analysis["summary"], "Single phrase expression")
            self.assertEqual(analysis["parts"][0]["role"], "clause")
            self.assertTrue(analysis["parts"][0]["features"]["role_normalized"])

    def test_grammar_roles_reject_semantically_contradictory_labels(self) -> None:
        self.assertTrue(_grammar_role_matches("subject", "phrase", "you"))
        self.assertTrue(_grammar_role_matches("predicate", "verb", "consider"))
        self.assertFalse(_grammar_role_matches("subject", "verb", "you consider"))
        self.assertFalse(
            _grammar_role_matches(
                "connector",
                "conjunction",
                "Would you do anything about it? If so, what?",
            )
        )
        self.assertEqual(
            _normalise_grammar_role("subject", "verb", "you are crossing"),
            "predicate",
        )
        self.assertEqual(
            _normalise_grammar_role(
                "connector",
                "interjection",
                "Would you do anything about it? If so, what?",
            ),
            "clause",
        )
        self.assertEqual(
            _normalise_grammar_labels(
                "connector",
                "interjection",
                "Would you do anything about it? If so, what?",
            ),
            ("clause", "clause"),
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
                    "meaning": "before",
                },
                {
                    "surface": "de",
                    "kind": "",
                    "evidence_ids": ["evidence-predecessor"],
                    "meaning": "down",
                },
                {
                    "surface": "cess",
                    "kind": "root",
                    "evidence_ids": ["evidence-predecessor"],
                    "meaning": "go",
                },
                {"surface": "or", "kind": "suffix", "evidence_ids": []},
            ],
        )

    def test_late_component_formula_requires_exact_full_word_ownership(self) -> None:
        records = [
            {
                "headword": "TAIN",
                "kind": "morphology-root",
                "knowledge_evidence_id": "evidence-tain",
                "component_hint": "root",
                "component_surface": "tain",
                "excerpt": "TAIN: abstain [abs (=away) + tain (=hold)]",
            }
        ]

        self.assertEqual(
            _book_decomposition_shape("abstain", records),
            [
                {
                    "surface": "abs",
                    "kind": "prefix",
                    "evidence_ids": ["evidence-tain"],
                    "meaning": "away",
                },
                {
                    "surface": "tain",
                    "kind": "root",
                    "evidence_ids": ["evidence-tain"],
                    "meaning": "hold",
                },
            ],
        )
        self.assertEqual(_book_decomposition_shape("abstainer", records), [])

    def test_late_exact_formula_augments_free_draft_and_keeps_derivatives(self) -> None:
        derivative_terms = [
            "contain",
            "retain",
            "detain",
            "sustain",
            "maintain",
            "obtain",
            "pertain",
            "entertain",
        ]

        class AbstainRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    {
                        "entry_id": "dictionary-abstain-1",
                        "corpus_id": "test-dictionary:1.0",
                        "source_title": "Test Dictionary",
                        "headword": term,
                        "part_of_speech": "verb",
                        "definition": "to choose not to do something",
                        "source_hash": "abstain-dictionary",
                        "locator": "sense 1",
                    }
                ]

            def component_evidence(
                self, form: str, kind: str
            ) -> list[dict[str, Any]]:
                if form.strip("-").casefold() != "tain" or kind != "root":
                    return []
                return [
                    {
                        "entry_id": "root-tain-abstain",
                        "corpus_id": "test-roots:1.0",
                        "source_title": "Test Root Dictionary",
                        "headword": "TAIN",
                        "excerpt": "TAIN: abstain [abs (=away) + tain (=hold)]",
                        "source_hash": "tain-root",
                        "locator": "root TAIN",
                        "kind": "morphology-root",
                    }
                ]

        class AbstainModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "LINGUISTIC REVIEW OF A LEXICAL STRUCTURE DRAFT" in prompt:
                    raise AssertionError("an exact formula should augment without rejection")
                if "MORPHEME SPLIT" in prompt:
                    return {
                        "value": {
                            "parts": [
                                {
                                    "surface": "abstain",
                                    "canonical_form": "abstain",
                                    "kind": "free",
                                    "language": "en",
                                    "meaning": "choose not to act",
                                    "confidence": 0.8,
                                    "evidence_ids": [],
                                }
                            ],
                            "derivatives": [
                                {
                                    "term": term,
                                    "note": "shares the tain root",
                                    "component_forms": ["tain"],
                                }
                                for term in derivative_terms
                            ],
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(
                store,
                model="test-local-qwen",
                prompt_version="interactive-origin-graph-v4",
            ).plan_word("abstain", display_languages=("en",))
            worker = PreparationWorker(
                store, AbstainRetriever(), AbstainModel(), FakePronouncer()
            )
            for job_type in ("retrieve-evidence", "prepare-meaning"):
                result = worker.run_once((job_type,))
                self.assertIsNotNone(result)
                self.assertEqual(result.status, "complete")

            old_free = store.upsert_morpheme(
                "en", "abstain", "free", "choose not to act", status="accepted"
            )
            old_derivative_term = store.upsert_term(
                "en", "forbear", status="accepted"
            )
            old_history = store.add_historical_form(
                "la", "abstinere", period_label="Latin", status="accepted"
            )
            store.link_morpheme(
                plan.subject_entity_id,
                old_free,
                0,
                "abstain",
                basis="model",
                confidence=0.8,
            )
            old_component_assertion = store.accept_relation_assertion(
                plan.subject_entity_id,
                plan.subject_entity_id,
                old_free,
                "has-component",
                basis="model",
                confidence=0.8,
                properties={"modes": ["knowledge", "word", "root"]},
            )
            old_derivative_assertion = store.accept_relation_assertion(
                plan.subject_entity_id,
                old_derivative_term,
                old_free,
                "shares-component",
                basis="model",
                properties={"modes": ["knowledge", "word", "root"]},
            )
            retained_history_assertion = store.accept_relation_assertion(
                plan.subject_entity_id,
                old_free,
                old_history,
                "developed-into",
                basis="model",
                properties={"modes": ["word", "root"]},
            )
            old_split_artifact = store.save_job_artifact(
                plan.jobs["split-morphemes"],
                "accepted-morpheme-split",
                {
                    "term_id": plan.subject_entity_id,
                    "term": "abstain",
                    "parts": [
                        {
                            "morpheme_id": old_free,
                            "surface": "abstain",
                            "canonical_form": "abstain",
                            "kind": "free",
                            "language": "en",
                            "meaning": "choose not to act",
                            "ordinal": 0,
                            "basis": "model",
                            "confidence": 0.8,
                            "evidence_ids": [],
                        }
                    ],
                    "related_terms": [
                        {
                            "term_id": old_derivative_term,
                            "term": "forbear",
                            "note": "old weak association",
                            "component_forms": ["abstain"],
                            "component_ids": [old_free],
                            "component_kinds": ["free"],
                        }
                    ],
                    "model": "old-qwen",
                    "metrics": {},
                },
                language="en",
                validation_state="accepted",
                quality_score=0.8,
            )
            old_graph_revision = store.lexical_subgraph(
                plan.subject_entity_id,
                "word",
                {"nodes": 16, "edges": 24, "depth": 4},
            )["graph_revision"]
            cards = CardStore(Path(temp) / "cards.sqlite3")
            old_card = Card(
                card_id="old-abstain-word",
                mode="word",
                query="abstain",
                title="abstain",
                subtitle="old free analysis",
                summary_en="to choose not to act",
                origin_story="",
                key_points=[],
                english={"term": "abstain", "pronunciation": "", "meaning": "to refrain"},
                japanese={"term": "", "reading": "", "meaning": "", "ruby_tokens": []},
                chinese={"simplified": "", "traditional": "", "pinyin": "", "meaning": "", "ruby_tokens": []},
                memory_hook="",
                related_terms=[{"term": "forbear", "note": "old weak association"}],
                evidence=[],
                model="old-qwen",
                created_at="2026-08-31T00:00:00+00:00",
                extensions={"lexical_view": {"graph_revision": old_graph_revision}},
            )
            cards.save(old_card)
            old_card_payload = cards.get(old_card.card_id)

            split_job = {
                "job_id": plan.jobs["split-morphemes"],
                "subject_entity_id": plan.subject_entity_id,
                "subject_key": plan.subject_key,
                "prompt_version": "interactive-origin-graph-v4",
            }
            first_split_artifact = worker._split_morphemes(split_job)
            first_graph_revision = store.lexical_subgraph(
                plan.subject_entity_id,
                "word",
                {"nodes": 32, "edges": 48, "depth": 4},
            )["graph_revision"]
            replay_split_artifact = worker._split_morphemes(split_job)
            replay_graph_revision = store.lexical_subgraph(
                plan.subject_entity_id,
                "word",
                {"nodes": 32, "edges": 48, "depth": 4},
            )["graph_revision"]
            self.assertEqual(replay_graph_revision, first_graph_revision)
            self.assertNotEqual(first_split_artifact, replay_split_artifact)
            self.assertEqual(cards.get(old_card.card_id), old_card_payload)
            split_artifacts = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-morpheme-split"
            )
            self.assertIn(
                old_split_artifact,
                {artifact["artifact_id"] for artifact in split_artifacts},
            )
            self.assertEqual(
                next(
                    artifact["validation_state"]
                    for artifact in split_artifacts
                    if artifact["artifact_id"] == old_split_artifact
                ),
                "superseded",
            )
            with store._connect() as connection:
                stale_statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE assertion_id IN (?, ?, ?)""",
                        (
                            old_component_assertion,
                            old_derivative_assertion,
                            retained_history_assertion,
                        ),
                    )
                }
            self.assertEqual(stale_statuses[old_component_assertion], "archived")
            self.assertEqual(stale_statuses[old_derivative_assertion], "archived")
            self.assertEqual(stale_statuses[retained_history_assertion], "accepted")

            draft = store.artifacts_for_subject(
                plan.subject_key,
                stage="model-morpheme-draft",
                validation_state="candidate",
            )[-1]["payload"]["value"]
            self.assertEqual(
                [(part["surface"], part["kind"]) for part in draft["parts"]],
                [("abstain", "free")],
            )
            accepted = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-morpheme-split",
                validation_state="accepted",
            )[-1]["payload"]
            self.assertEqual(
                [
                    (part["surface"], part["kind"], part["meaning"])
                    for part in accepted["parts"]
                ],
                [("abs", "prefix", "away"), ("tain", "root", "hold")],
            )
            evidence_sets = {
                tuple(part["evidence_ids"]) for part in accepted["parts"]
            }
            self.assertEqual(len(evidence_sets), 1)
            self.assertTrue(next(iter(evidence_sets)))
            self.assertEqual(
                [item["term"] for item in accepted["related_terms"]],
                derivative_terms,
            )
            view = store.lexical_subgraph(
                plan.subject_entity_id,
                "word",
                {"nodes": 32, "edges": 48, "depth": 4},
            )
            component_edges = [
                edge for edge in view["edges"] if edge["relation"] == "has-component"
            ]
            derivative_edges = [
                edge
                for edge in view["edges"]
                if edge["relation"] == "shares-component"
            ]
            self.assertEqual(len(component_edges), 2)
            self.assertTrue(
                all(edge["basis"] == "book" and edge["evidence_ids"] for edge in component_edges)
            )
            self.assertEqual(len(derivative_edges), 8)
            self.assertTrue(
                all(
                    edge["basis"] == "model" and edge["evidence_ids"] == []
                    for edge in derivative_edges
                )
            )
            self.assertEqual(
                {edge["source"] for edge in derivative_edges},
                {item["term_id"] for item in accepted["related_terms"]},
            )

    def test_live_shaped_records_supply_mode_specific_component_evidence(self) -> None:
        records = [
            {
                "knowledge_evidence_id": "root-p0036-t001-r001",
                "kind": "morphology-root",
                "headword": "adjacent",
                "excerpt": (
                    r"adjacent [\mathrm { ad } (=\) to, near \() + "
                    r"\mathrm { jac } (=\) throw \()]"
                ),
            },
            {
                "knowledge_evidence_id": "affix-p0034-t004-r005",
                "kind": "morphology-affix",
                "headword": "adjacent",
                "excerpt": "adjacent [ad (=to, near) + jac (=throw)]",
            },
        ]

        self.assertEqual(
            _retrieved_component_evidence_ids("jac", "root", records),
            ["root-p0036-t001-r001"],
        )
        self.assertEqual(
            _retrieved_component_evidence_ids("ad-", "prefix", records),
            ["affix-p0034-t004-r005"],
        )
        self.assertEqual(
            _retrieved_component_evidence_ids("-ent", "suffix", records), []
        )
        self.assertEqual(
            _retrieved_component_evidence_ids("jac", "free", records), []
        )

    def test_morpheme_display_notation_is_deterministic(self) -> None:
        self.assertEqual(_morpheme_display_form("in", "prefix"), "in-")
        self.assertEqual(_morpheme_display_form("spect", "root"), "spect")
        self.assertEqual(_morpheme_display_form("ion", "suffix"), "-ion")
        self.assertEqual(
            _clean_morpheme_meaning("to look, to see"), "to look or to see"
        )
        self.assertEqual(
            _clean_morpheme_meaning(
                ["pull", "lead", "carry", "draw", "give birth", "off-spring"]
            ),
            "pull or lead or carry or draw or give birth",
        )
        self.assertEqual(
            _clean_morpheme_meaning(
                "one two three four five six seven eight nine or more words"
            ),
            "one two three four five six seven eight nine",
        )
        self.assertEqual(_clean_morpheme_meaning([]), "")
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

    def test_origin_restores_owned_component_id_and_drops_modern_endpoint(self) -> None:
        class NoisyOriginModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ONE ORIGIN BRANCH REVIEW" in prompt:
                    raise AssertionError("a normalized valid draft must not be reviewed")
                if "ONE ORIGIN BRANCH" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                return {
                    "value": {
                        "component_id": "model-invented-id",
                        "steps": [
                            {
                                "form": "*spok-",
                                "language": "ine-pro",
                                "period": "Proto-Indo-European",
                                "meaning": "look",
                                "confidence": 0.9,
                                "evidence_ids": [],
                            },
                            {
                                "form": "inspection",
                                "language": "en",
                                "period": "Modern English",
                                "meaning": "modern word",
                                "confidence": 0.9,
                                "evidence_ids": [],
                            },
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
                store, FakeRetriever(), NoisyOriginModel()
            ).run(4)

            self.assertEqual(results[-1].job_type, "expand-origin-branches")
            self.assertEqual(results[-1].status, "complete")
            accepted = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-origin-branches",
                validation_state="accepted",
            )
            self.assertEqual(len(accepted), 1)
            hypothesis = accepted[0]["payload"]["branches"][1]["steps"][0]
            self.assertEqual(hypothesis["form"], "*spok-")
            self.assertEqual(hypothesis["basis"], "model")
            self.assertEqual(hypothesis["confidence"], 0.7)
            self.assertEqual(hypothesis["evidence_ids"], [])
            self.assertEqual(hypothesis["edge_basis"], "model")
            self.assertEqual(hypothesis["edge_evidence_ids"], [])
            normalized = store.artifacts_for_subject(
                plan.subject_key, stage="normalized-origin-draft"
            )
            self.assertEqual(
                set(normalized[-1]["payload"]["normalizations"]),
                {
                    "restored-system-component-id",
                    "removed-redundant-modern-endpoint",
                },
            )

    def test_low_confidence_origin_is_retained_without_weakening_shape_checks(self) -> None:
        class LowConfidenceOriginModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ONE ORIGIN BRANCH" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                component_id = re.findall(
                    r'"component_id": "([^"]+)"', prompt
                )[0]
                return {
                    "value": {
                        "component_id": component_id,
                        "steps": [
                            {
                                "form": "*spok-",
                                "language": "ine-pro",
                                "period": "Proto-Indo-European",
                                "meaning": "look",
                                "confidence": 0.2,
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
                store, FakeRetriever(), LowConfidenceOriginModel()
            ).run(4)

            self.assertEqual(results[-1].job_type, "expand-origin-branches")
            self.assertEqual(results[-1].status, "complete")
            accepted = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-origin-branches",
                validation_state="accepted",
            )
            self.assertEqual(len(accepted), 1)
            low_steps = [
                step
                for branch in accepted[0]["payload"]["branches"]
                for step in branch["steps"]
                if step["form"] == "*spok-"
            ]
            self.assertTrue(low_steps)
            self.assertTrue(
                all(
                    step["confidence"] == 0.2
                    and step["basis"] == "model"
                    and step["evidence_ids"] == []
                    for step in low_steps
                )
            )

    def test_origin_record_can_anchor_a_named_subentry(self) -> None:
        self.assertTrue(
            _origin_source_record_matches(
                "attention",
                {
                    "kind": "entry",
                    "headword": "attend",
                    "excerpt": "The noun derivative attention comes from Latin attentio.",
                },
            )
        )
        self.assertFalse(
            _origin_source_record_matches(
                "attention",
                {
                    "kind": "entry",
                    "headword": "gimmick",
                    "excerpt": "A device intended to attract notice.",
                },
            )
        )
        self.assertFalse(
            _origin_source_record_matches(
                "arrange",
                {
                    "kind": "entry",
                    "headword": "tactic",
                    "excerpt": (
                        "Greek tássein meant 'put in order', hence 'arrange in "
                        "battle formation'. From this was derived taktós."
                    ),
                },
            )
        )
        self.assertTrue(
            _origin_source_record_matches(
                "arrange",
                {
                    "kind": "entry",
                    "headword": "array",
                    "excerpt": "English arrange was borrowed from Old French arengier.",
                },
            )
        )
        self.assertFalse(
            _origin_source_record_matches(
                "aardvark",
                {
                    "kind": "entry",
                    "headword": "aardvark",
                    "excerpt": "see EARTH, FARROW",
                },
            )
        )

    def test_origin_cross_reference_targets_are_followed(self) -> None:
        self.assertEqual(
            _origin_cross_reference_targets("see EARTH, FARROW"),
            ("EARTH", "FARROW"),
        )

        class CrossReferenceCorpus:
            def metadata(self) -> dict[str, str]:
                return {"source_sha256": "origin-book"}

            def search(self, query: str, _limit: int) -> list[Evidence]:
                entries = {
                    "aardvark": Evidence(
                        entry_id="aardvark",
                        headword="aardvark",
                        section="A",
                        date_label="",
                        pages=(1,),
                        excerpt="see EARTH, FARROW",
                    ),
                    "earth": Evidence(
                        entry_id="earth",
                        headword="EARTH",
                        section="E",
                        date_label="Old English",
                        pages=(2,),
                        excerpt="Old English eorthe developed from a Germanic base.",
                    ),
                    "farrow": Evidence(
                        entry_id="farrow",
                        headword="FARROW",
                        section="F",
                        date_label="Old English",
                        pages=(3,),
                        excerpt="Old English fearh meant piglet.",
                    ),
                }
                item = entries.get(query.casefold())
                return [item] if item else []

        retriever = WordEvidenceRetriever(
            CrossReferenceCorpus(), None, None, None  # type: ignore[arg-type]
        )
        records = retriever.origin_evidence("aardvark")
        self.assertEqual(
            [record["entry_id"] for record in records],
            ["aardvark", "earth", "farrow"],
        )

        self.assertTrue(
            _origin_source_evidence_supported(
                "aardvark",
                [
                    {
                        "kind": "entry",
                        "headword": record["headword"],
                        "excerpt": record["excerpt"],
                    }
                    for record in records
                ],
            )
        )
        self.assertFalse(
            _origin_source_evidence_supported(
                "arrange",
                [
                    {
                        "kind": "entry",
                        "headword": "tactic",
                        "excerpt": (
                            "Greek tássein meant 'put in order', hence 'arrange in "
                            "battle formation'."
                        ),
                    }
                ],
            )
        )

    def test_affix_origin_story_is_mode_specific_and_provenance_cautious(self) -> None:
        self.assertEqual(
            _affix_origin_story(
                [
                    {
                        "canonical_form": "in-",
                        "kind": "prefix",
                        "meaning": "into",
                        "evidence_ids": [],
                    },
                    {
                        "canonical_form": "-ion",
                        "kind": "suffix",
                        "meaning": "process",
                        "evidence_ids": ["evidence-ion"],
                    },
                ]
            ),
            "Accepted affix analysis gives in- as “into”; -ion as “process”.",
        )
        self.assertEqual(
            _affix_origin_story(
                [
                    {
                        "canonical_form": "pre-",
                        "kind": "prefix",
                        "meaning": "before",
                        "evidence_ids": ["evidence-pre"],
                    },
                    {
                        "canonical_form": "-or",
                        "kind": "suffix",
                        "meaning": "one who",
                        "evidence_ids": ["evidence-or"],
                    },
                ]
            ),
            "Cited affix evidence supports pre- as “before”; -or as “one who”.",
        )

    def test_old_english_code_is_normalized_without_dropping_history(self) -> None:
        value, changes = _normalize_origin_draft(
            {
                "component_id": "wrong",
                "steps": [
                    {
                        "form": "eorthe",
                        "language": "en",
                        "period": "Old English",
                    },
                    {
                        "form": "earth",
                        "language": "en",
                        "period": "Modern English",
                    },
                ],
            },
            component_id="owned",
            modern_word="earth",
            base_form="earth",
        )
        self.assertEqual(value["steps"][0]["language"], "ang")
        self.assertEqual(len(value["steps"]), 1)
        self.assertIn("normalized-old-english-code", changes)
        self.assertIn("removed-redundant-modern-endpoint", changes)

    def test_word_origin_rag_repairs_a_forced_split_into_an_unsplit_base(self) -> None:
        class LecherRetriever:
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    {
                        "entry_id": "dictionary-lecher-1",
                        "corpus_id": "test-dictionary:1.0",
                        "source_title": "Test Dictionary",
                        "headword": term,
                        "part_of_speech": "noun",
                        "definition": "a person given to lewd behavior",
                        "source_hash": "dictionary123",
                        "locator": "sense 1",
                    },
                    {
                        "entry_id": "entry-3622",
                        "corpus_id": "test-word-origins:1.0",
                        "source_title": "Test Word Origins",
                        "headword": term,
                        "excerpt": (
                            "Old French lecheor was derived from lechier 'lick', "
                            "ultimately from Frankish likkon."
                        ),
                        "source_hash": "origins123",
                        "locator": "lecher entry",
                        "kind": "entry",
                    },
                ]

            def component_evidence(self, _form: str, _kind: str) -> list[dict[str, Any]]:
                return []

            def origin_evidence(self, _form: str) -> list[dict[str, Any]]:
                return []

        class ReviewingModel:
            model_name = "test-local-qwen"

            def complete_json(
                self, _system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "LINGUISTIC REVIEW" in prompt:
                    return {
                        "value": {
                            "parts": [
                                {
                                    "surface": "lecher",
                                    "canonical_form": "lecher",
                                    "kind": "free",
                                    "language": "en",
                                    "meaning": (
                                        "person given to lewd behavior in historical French "
                                        "and English usage"
                                    ),
                                    "confidence": 0.9,
                                    "evidence_ids": [],
                                }
                            ]
                        },
                        "model": self.model_name,
                    }
                if "ORIGIN BRANCH REVIEW" in prompt:
                    component_id = re.findall(
                        r'"component_id": "([^"]+)"', prompt
                    )[0]
                    return {
                        "value": {
                            "component_id": component_id,
                            "steps": [
                                {
                                    "form": "likkon",
                                    "language": "gem-pro",
                                    "period": "Frankish",
                                    "meaning": "lick",
                                    "confidence": 0.9,
                                    "evidence_ids": [],
                                },
                                {
                                    "form": "lechier",
                                    "language": "fro",
                                    "period": "Old French verb",
                                    "meaning": "lick or live dissolutely",
                                    "confidence": 0.92,
                                    "evidence_ids": [],
                                },
                                {
                                    "form": "lecheor",
                                    "language": "fro",
                                    "period": "Old French noun",
                                    "meaning": "debauched person",
                                    "confidence": 0.92,
                                    "evidence_ids": [],
                                },
                            ],
                        },
                        "model": self.model_name,
                    }
                if "MORPHEME SPLIT" in prompt:
                    return {
                        "value": {
                            "parts": [
                                {
                                    "surface": "lech",
                                    "canonical_form": "lech-",
                                    "kind": "prefix",
                                    "language": "en",
                                    "meaning": "lick",
                                    "confidence": 0.8,
                                    "evidence_ids": [],
                                },
                                {
                                    "surface": "er",
                                    "canonical_form": "-er",
                                    "kind": "suffix",
                                    "language": "en",
                                    "meaning": "person",
                                    "confidence": 0.8,
                                    "evidence_ids": [],
                                },
                            ]
                        },
                        "model": self.model_name,
                    }
                if "ONE ORIGIN BRANCH" in prompt:
                    component_id = re.findall(
                        r'"component_id": "([^"]+)"', prompt
                    )[0]
                    return {
                        "value": {
                            "component_id": component_id,
                            "steps": [
                                {
                                    "form": "lecheor",
                                    "language": "fro",
                                    "period": "Old French derivative",
                                    "meaning": "lewd person",
                                    "confidence": 0.92,
                                    "evidence_ids": [],
                                },
                                {
                                    "form": "lecher",
                                    "language": "en",
                                    "period": "Modern English",
                                    "meaning": "lewd person",
                                    "confidence": 0.9,
                                    "evidence_ids": [],
                                },
                            ],
                        },
                        "model": self.model_name,
                    }
                evidence_id = re.findall(r'"(evidence-[^"]+)"', prompt)[0]
                return {
                    "value": {
                        "definition": "A person given to lewd behavior.",
                        "part_of_speech": "noun",
                        "sense_note": "historical noun",
                        "confidence": 0.9,
                        "evidence_ids": [evidence_id],
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-local-qwen").plan_word(
                "lecher", display_languages=("en",)
            )
            results = PreparationWorker(
                store, LecherRetriever(), ReviewingModel(), FakePronouncer()
            ).run(4)

            self.assertTrue(all(result.status == "complete" for result in results))
            split = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-morpheme-split",
                validation_state="accepted",
            )[0]
            self.assertEqual(
                [(part["surface"], part["kind"]) for part in split["payload"]["parts"]],
                [("lecher", "free")],
            )
            self.assertEqual(
                len(
                    store.artifacts_for_subject(
                        plan.subject_key,
                        stage="model-morpheme-review-draft",
                        validation_state="candidate",
                    )
                ),
                1,
            )
            origin = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-origin-branches",
                validation_state="accepted",
            )[0]
            branch = origin["payload"]["branches"][0]
            self.assertEqual(branch["component_kind"], "free")
            self.assertEqual(
                [step["form"] for step in branch["steps"]],
                ["likkon", "lechier", "lecheor"],
            )
            self.assertTrue(all(step["basis"] == "book" for step in branch["steps"]))
            self.assertEqual(
                len(
                    store.artifacts_for_subject(
                        plan.subject_key,
                        stage="model-origin-review-draft",
                        validation_state="candidate",
                    )
                ),
                1,
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

    def test_exact_bilingual_candidate_repairs_a_missing_wordnet_translation(self) -> None:
        class ArabicFallbackRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "freedict-inspection",
                        "corpus_id": "freedict-eng-ara:0.6.3",
                        "source_title": "FreeDict English-Arabic 0.6.3",
                        "headword": term,
                        "definition": "",
                        "translations": {"ar": ["معاينة"]},
                        "source_hash": "freedict123",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class ArabicCandidateModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ARABIC TRANSLATION" not in prompt:
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                self.assert_candidate = 'DICTIONARY CANDIDATES: ["معاينة"]' in prompt
                return {
                    "value": {
                        "term": "معاينة",
                        "meaning": "فحص دقيق لتقييم الحالة أو الجودة",
                        "reading": "mu'ayana",
                        "confidence": 0.91,
                    },
                    "model": self.model_name,
                }

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ar")
            )
            model = ArabicCandidateModel()
            results = PreparationWorker(
                store, ArabicFallbackRetriever(), model
            ).run(5)
            self.assertEqual(results[-1].status, "complete")
            self.assertTrue(model.assert_candidate)
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["payload"]["term"], "معاينة")
            self.assertEqual(len(artifact["payload"]["dictionary_evidence_ids"]), 1)
            self.assertEqual(len(artifact["payload"]["evidence_ids"]), 2)

    def test_sole_arabic_candidate_is_selected_with_exact_dictionary_spelling(self) -> None:
        class SerendipityRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "freedict-serendipity",
                        "corpus_id": "freedict-eng-ara:0.6.3",
                        "source_title": "FreeDict English-Arabic 0.6.3",
                        "headword": term,
                        "definition": "",
                        "translations": {"ar": ["موهبة الإكتشاف"]},
                        "source_hash": "freedict-serendipity",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class SerendipityModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ARABIC TRANSLATION" in prompt:
                    self.assert_candidate = (
                        'DICTIONARY CANDIDATES: ["موهبة الإكتشاف"]' in prompt
                    )
                    return {
                        "value": {
                            "term": "موهبة الاكتشاف",
                            "meaning": "قدرة على اكتشاف أشياء قيمة بالمصادفة (serendipity)",
                            "reading": "mawhibat al-iktishaf",
                            "confidence": 0.91,
                        },
                        "model": self.model_name,
                    }
                completion = super().complete_json(
                    system, prompt, max_tokens=max_tokens
                )
                value = completion.get("value")
                if isinstance(value, dict) and "definition" in value:
                    value["definition"] = (
                        "The chance discovery of something valuable or interesting."
                    )
                return completion

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "serendipity", display_languages=("en", "ar")
            )
            model = SerendipityModel()
            worker = PreparationWorker(store, SerendipityRetriever(), model)
            self.assertTrue(all(result.status == "complete" for result in worker.run(2)))
            job = store.claim_next_job(("prepare-translation",))
            self.assertIsNotNone(job)
            worker._prepare_translation(job)
            self.assertTrue(model.assert_candidate)
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            self.assertEqual(artifact["payload"]["term"], "موهبة الإكتشاف")
            self.assertEqual(
                artifact["payload"]["meaning"],
                "قدرة على اكتشاف أشياء قيمة بالمصادفة",
            )
            self.assertEqual(
                artifact["payload"]["normalizations"],
                [
                    "selected-sole-arabic-dictionary-candidate",
                    "removed-source-headword-from-arabic-meaning",
                ],
            )
            self.assertEqual(len(artifact["payload"]["dictionary_evidence_ids"]), 1)
            self.assertEqual(len(artifact["payload"]["evidence_ids"]), 2)

    def test_local_rag_repairs_cyrillic_meaning_for_sole_freedict_candidate(self) -> None:
        class SerendipityRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "freedict-serendipity",
                        "corpus_id": "freedict-eng-ara:0.6.3",
                        "source_title": "FreeDict English-Arabic 0.6.3",
                        "headword": term,
                        "definition": "",
                        "translations": {"ar": ["موهبة الإكتشاف"]},
                        "source_hash": "freedict-serendipity",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class CyrillicMeaningModel(FakeAtomicModel):
            def __init__(self) -> None:
                self.arabic_calls = 0
                self.rag_repair_prompt = ""

            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "ARABIC RAG MEANING-ONLY REPAIR" in prompt:
                    self.arabic_calls += 1
                    self.rag_repair_prompt = prompt
                    return {
                        "value": {"meaning": "اكتشاف مفيد يحدث بالمصادفة"},
                        "model": self.model_name,
                    }
                if (
                    "TARGET LANGUAGE: Arabic" in prompt
                    or "ARABIC SCRIPT REPAIR" in prompt
                ):
                    self.arabic_calls += 1
                    return {
                        "value": {
                            "term": "موهبة الإكتشاف",
                            "meaning": "الظهور والظهور المفاجئ ل события مفيدة بشكل عشوائي",
                            "reading": "mawhibat al-iktishaf",
                            "confidence": 0.91,
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "serendipity", display_languages=("en", "ar")
            )
            model = CyrillicMeaningModel()
            results = PreparationWorker(store, SerendipityRetriever(), model).run(3)

            self.assertEqual(results[-1].status, "complete")
            self.assertEqual(model.arabic_calls, 3)
            for expected in (
                "ACCEPTED ENGLISH SENSE: A careful examination to assess condition or quality.",
                'EXACT RETRIEVED ARABIC CANDIDATE: "موهبة الإكتشاف"',
                "RETRIEVED CANDIDATE EVIDENCE IDS:",
                'OFFENDING NON-ARABIC TOKENS: ["события"]',
                'OFFENDING SCRIPTS: ["CYRILLIC"]',
            ):
                self.assertIn(expected, model.rag_repair_prompt)
            artifact = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-translation",
                validation_state="accepted",
            )[0]
            payload = artifact["payload"]
            self.assertEqual(payload["term"], "موهبة الإكتشاف")
            self.assertEqual(payload["meaning"], "اكتشاف مفيد يحدث بالمصادفة")
            self.assertEqual(
                payload["normalizations"],
                [
                    "repaired-arabic-script",
                    "repaired-mixed-script-arabic-meaning-with-local-rag",
                ],
            )
            self.assertEqual(len(payload["dictionary_evidence_ids"]), 1)
            self.assertIn(
                payload["dictionary_evidence_ids"][0], payload["evidence_ids"]
            )

    def test_local_rag_repair_rejects_repeated_cyrillic_meaning(self) -> None:
        class SerendipityRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "freedict-serendipity",
                        "corpus_id": "freedict-eng-ara:0.6.3",
                        "source_title": "FreeDict English-Arabic 0.6.3",
                        "headword": term,
                        "definition": "",
                        "translations": {"ar": ["موهبة الإكتشاف"]},
                        "source_hash": "freedict-serendipity",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class StillMixedMeaningModel(FakeAtomicModel):
            def __init__(self) -> None:
                self.saw_rag_repair_prompt = False

            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                mixed = "الظهور المفاجئ ل события مفيدة بشكل عشوائي"
                if "ARABIC RAG MEANING-ONLY REPAIR" in prompt:
                    self.saw_rag_repair_prompt = True
                    return {
                        "value": {"meaning": mixed},
                        "model": self.model_name,
                    }
                if (
                    "TARGET LANGUAGE: Arabic" in prompt
                    or "ARABIC SCRIPT REPAIR" in prompt
                ):
                    return {
                        "value": {
                            "term": "موهبة الإكتشاف",
                            "meaning": mixed,
                            "reading": "mawhibat al-iktishaf",
                            "confidence": 0.91,
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "serendipity", display_languages=("en", "ar")
            )
            model = StillMixedMeaningModel()
            results = PreparationWorker(store, SerendipityRetriever(), model).run(3)

            self.assertEqual(results[-1].status, "retry")
            self.assertTrue(model.saw_rag_repair_prompt)
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-translation",
                    validation_state="accepted",
                ),
                [],
            )
            rejected = store.artifacts_for_subject(
                plan.subject_key,
                stage="rejected-translation",
                validation_state="rejected",
            )
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0]["reusable"])
            self.assertIn("события", rejected[0]["payload"]["raw"])

    def test_mixed_arabic_meaning_does_not_fallback_to_non_freedict_candidate(self) -> None:
        class OtherDictionaryRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "other-serendipity",
                        "corpus_id": "other-eng-ara:1.0",
                        "source_title": "Other English-Arabic Dictionary",
                        "headword": term,
                        "definition": "",
                        "translations": {"ar": ["موهبة الإكتشاف"]},
                        "source_hash": "other-serendipity",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class MixedMeaningModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if (
                    "TARGET LANGUAGE: Arabic" in prompt
                    or "ARABIC SCRIPT REPAIR" in prompt
                ):
                    return {
                        "value": {
                            "term": "موهبة الإكتشاف",
                            "meaning": "اكتشاف события مفيدة بالمصادفة",
                            "reading": "mawhibat al-iktishaf",
                            "confidence": 0.91,
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "serendipity", display_languages=("en", "ar")
            )
            results = PreparationWorker(
                store, OtherDictionaryRetriever(), MixedMeaningModel()
            ).run(3)

            self.assertEqual(results[-1].status, "retry")
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-translation",
                    validation_state="accepted",
                ),
                [],
            )
            rejected = store.artifacts_for_subject(
                plan.subject_key,
                stage="rejected-translation",
                validation_state="rejected",
            )
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0]["reusable"])

    def test_mixed_arabic_meaning_does_not_fallback_with_multiple_candidates(self) -> None:
        class MultipleCandidateRetriever(FakeRetriever):
            def retrieve(self, term: str) -> list[dict[str, Any]]:
                return [
                    *super().retrieve(term),
                    {
                        "entry_id": "freedict-serendipity",
                        "corpus_id": "freedict-eng-ara:0.6.3",
                        "source_title": "FreeDict English-Arabic 0.6.3",
                        "headword": term,
                        "definition": "",
                        "translations": {
                            "ar": ["موهبة الإكتشاف", "مصادفة سعيدة"]
                        },
                        "source_hash": "freedict-serendipity",
                        "locator": f"headword {term}",
                        "kind": "bilingual-dictionary",
                        "translation_scope": "exact-headword",
                    },
                ]

        class MixedMeaningModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if (
                    "TARGET LANGUAGE: Arabic" in prompt
                    or "ARABIC SCRIPT REPAIR" in prompt
                ):
                    return {
                        "value": {
                            "term": "موهبة الإكتشاف",
                            "meaning": "اكتشاف события مفيدة بالمصادفة",
                            "reading": "mawhibat al-iktishaf",
                            "confidence": 0.91,
                        },
                        "model": self.model_name,
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word_card(
                "serendipity", display_languages=("en", "ar")
            )
            results = PreparationWorker(
                store, MultipleCandidateRetriever(), MixedMeaningModel()
            ).run(3)

            self.assertEqual(results[-1].status, "retry")
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-translation",
                    validation_state="accepted",
                ),
                [],
            )
            rejected = store.artifacts_for_subject(
                plan.subject_key,
                stage="rejected-translation",
                validation_state="rejected",
            )
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0]["reusable"])
            self.assertEqual(
                rejected[0]["payload"]["candidate"]["meaning"],
                "اكتشاف события مفيدة بالمصادفة",
            )

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
                if not (
                    "TARGET LANGUAGE: Arabic" in prompt
                    or "ARABIC SCRIPT REPAIR" in prompt
                ):
                    return super().complete_json(system, prompt, max_tokens=max_tokens)
                return {
                    "value": {
                        "term": "معاينة",
                        "meaning": "فحص inspection official دقيق",
                        "reading": "mu'ayana",
                        "usage_note": "",
                        "confidence": 0.9,
                    },
                    "model": self.model_name,
                    "raw": "r" * 5_000,
                    "metrics": {
                        "elapsed_seconds": 12.5,
                        "completion_tokens": 68,
                    },
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
            rejected = store.artifacts_for_subject(
                plan.subject_key,
                stage="rejected-translation",
                validation_state="rejected",
            )
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0]["reusable"])
            self.assertEqual(len(rejected[0]["payload"]["raw"]), 4_000)
            self.assertEqual(
                rejected[0]["payload"]["candidate"]["meaning"],
                "فحص official دقيق",
            )
            self.assertNotIn("evidence_ids", rejected[0]["payload"])

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
            self.assertTrue(
                all(step["edge_evidence_ids"] for step in root_branch["steps"])
            )
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

    def test_jmdict_repairs_an_incorrect_japanese_reading_without_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "jmdict.json"
            source.write_text(
                json.dumps(
                    {
                        "version": "3.6.2",
                        "dictDate": "2026-08-24",
                        "words": [
                            {
                                "id": "1600470",
                                "kanji": [{"common": True, "text": "風俗", "tags": []}],
                                "kana": [
                                    {
                                        "common": True,
                                        "text": "ふうぞく",
                                        "tags": [],
                                        "appliesToKanji": ["*"],
                                    }
                                ],
                                "sense": [
                                    {
                                        "partOfSpeech": ["n"],
                                        "appliesToKanji": ["*"],
                                        "appliesToKana": ["*"],
                                        "gloss": [
                                            {"lang": "eng", "text": "manners and customs"}
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            jmdict_database = root / "jmdict.sqlite3"
            build_jmdict_index(
                source, jmdict_database, release="3.6.2+20260824122934"
            )
            store = KnowledgeStore(root / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "lecher", display_languages=("en", "ja")
            )
            worker = PreparationWorker(
                store,
                FakeRetriever(),
                FakeAtomicModel(),
                FakePronouncer(),
                None,
                JapaneseReadingIndex(jmdict_database),
            )
            worker.run(2)
            store.finish_job(plan.jobs["split-morphemes"])
            store.finish_job(plan.jobs["expand-origin-branches"])
            meaning = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-meaning",
                validation_state="accepted",
            )[0]["payload"]
            target_id = store.upsert_term("ja", "風俗", status="accepted")
            translation_job = plan.jobs["translation:ja"]
            store.save_job_artifact(
                translation_job,
                "accepted-translation",
                {
                    "translation_id": "translation-ja",
                    "target_term_id": target_id,
                    "language": "ja",
                    "term": "風俗",
                    "meaning": "習慣",
                    "reading": "ふうしょく",
                    "confidence": 0.8,
                    "evidence_ids": meaning["evidence_ids"],
                },
                language="ja",
                validation_state="accepted",
                quality_score=0.8,
            )
            store.finish_job(translation_job)
            legacy_job = store.enqueue_job(
                "prepare-pronunciation",
                plan.subject_key,
                subject_entity_id=plan.subject_entity_id,
                language="ja",
                prompt_version="legacy-reading",
            )
            legacy_artifact = store.save_job_artifact(
                legacy_job,
                "accepted-pronunciation",
                {
                    "term": "風俗",
                    "reading": "ふうしょく",
                    "language": "ja",
                    "method": {"engine": "accepted translation"},
                },
                language="ja",
                validation_state="accepted",
                quality_score=0.7,
            )
            store.finish_job(legacy_job)

            worker.run(2)
            japanese = next(
                item
                for item in store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-pronunciation",
                    validation_state="accepted",
                )
                if item["language"] == "ja"
            )
            self.assertEqual(japanese["payload"]["reading"], "ふうぞく")
            self.assertEqual(japanese["payload"]["method"]["engine"], "JMdict")
            self.assertEqual(
                japanese["payload"]["method"]["selection"],
                "unique-dictionary-reading",
            )
            self.assertEqual(
                store.artifacts_for_subject(
                    plan.subject_key, stage="model-japanese-reading-review"
                ),
                [],
            )
            all_readings = store.artifacts_for_subject(
                plan.subject_key, stage="accepted-pronunciation"
            )
            self.assertEqual(
                next(
                    item for item in all_readings
                    if item["artifact_id"] == legacy_artifact
                )["validation_state"],
                "superseded",
            )

    def test_jmdict_uses_qwen_only_for_an_ambiguous_exact_form(self) -> None:
        class ReadingReviewModel(FakeAtomicModel):
            def __init__(self) -> None:
                self.reading_reviews = 0

            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                if "JAPANESE READING REVIEW" in prompt:
                    self.reading_reviews += 1
                    self.assert_review_prompt = prompt
                    return {
                        "value": {"reading": "けんさ"},
                        "model": self.model_name,
                        "metrics": {"completion_tokens": 8},
                    }
                return super().complete_json(system, prompt, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "jmdict.json"
            source.write_text(
                json.dumps(
                    {
                        "version": "3.6.2",
                        "dictDate": "2026-08-24",
                        "words": [
                            {
                                "id": "1259880",
                                "kanji": [{"common": True, "text": "検査"}],
                                "kana": [
                                    {"common": True, "text": "けんさ", "appliesToKanji": ["*"]},
                                    {"common": False, "text": "けんしゃ", "appliesToKanji": ["*"]},
                                ],
                                "sense": [
                                    {
                                        "partOfSpeech": ["n"],
                                        "appliesToKanji": ["*"],
                                        "appliesToKana": ["けんさ"],
                                        "gloss": [{"lang": "eng", "text": "inspection"}],
                                    },
                                    {
                                        "partOfSpeech": ["n"],
                                        "appliesToKanji": ["*"],
                                        "appliesToKana": ["けんしゃ"],
                                        "gloss": [{"lang": "eng", "text": "reviewer"}],
                                    },
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = root / "jmdict.sqlite3"
            build_jmdict_index(source, database, release="test-release")
            store = KnowledgeStore(root / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="test").plan_word(
                "inspection", display_languages=("en", "ja")
            )
            model = ReadingReviewModel()
            worker = PreparationWorker(
                store,
                FakeRetriever(),
                model,
                FakePronouncer(),
                None,
                JapaneseReadingIndex(database),
            )
            worker.run(2)
            store.finish_job(plan.jobs["split-morphemes"])
            store.finish_job(plan.jobs["expand-origin-branches"])
            meaning = store.artifacts_for_subject(
                plan.subject_key,
                stage="accepted-meaning",
                validation_state="accepted",
            )[0]["payload"]
            target_id = store.upsert_term("ja", "検査", status="accepted")
            translation_job = plan.jobs["translation:ja"]
            store.save_job_artifact(
                translation_job,
                "accepted-translation",
                {
                    "translation_id": "translation-ja",
                    "target_term_id": target_id,
                    "language": "ja",
                    "term": "検査",
                    "meaning": "状態を調べること",
                    "reading": "けんざ",
                    "confidence": 0.8,
                    "evidence_ids": meaning["evidence_ids"],
                },
                language="ja",
                validation_state="accepted",
                quality_score=0.8,
            )
            store.finish_job(translation_job)

            worker.run(2)

            japanese = next(
                item
                for item in store.artifacts_for_subject(
                    plan.subject_key,
                    stage="accepted-pronunciation",
                    validation_state="accepted",
                )
                if item["language"] == "ja"
            )
            self.assertEqual(japanese["payload"]["reading"], "けんさ")
            self.assertEqual(
                japanese["payload"]["method"]["selection"],
                "sense-aligned-local-model-selection",
            )
            self.assertEqual(model.reading_reviews, 1)
            self.assertIn('"けんさ"', model.assert_review_prompt)
            self.assertIn('"けんしゃ"', model.assert_review_prompt)

    def test_word_card_is_composed_only_from_accepted_atomic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = KnowledgeStore(root / "knowledge.sqlite3")
            cards = CardStore(root / "cards.sqlite3")
            plan = PreparationPlanner(store, model="test-qwen-4b").plan_word(
                "inspection", display_languages=("en", "ja", "zh", "fr", "ar")
            )
            accepted_subject_id = store.upsert_term(
                "en", "inspection", status="accepted", quality_score=0.95
            )
            self.assertEqual(accepted_subject_id, plan.subject_entity_id)
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
            origin_evidence_id = store.add_evidence(
                "test-roots:1.0",
                "root-spect-inspection",
                locator="root SPECT",
                excerpt=(
                    "SPECT is the root in inspection; Latin specere developed from "
                    "Proto-Indo-European *spek-."
                ),
                payload={
                    "entry_id": "test-roots:1.0:root-spect-inspection",
                    "headword": "SPECT",
                    "source_title": "Test Root Dictionary",
                    "kind": "morphology-root",
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
            wrong_japanese_translation_reading = "\u305b\u3093\u3048\u308b"
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
                        "reading": (
                            wrong_japanese_translation_reading
                            if language == "ja"
                            else reading if language in {"zh", "ar"} else ""
                        ),
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
                    "confidence": 0.4,
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
                    "evidence_ids": [origin_evidence_id],
                },
                {
                    "morpheme_id": "m-ion",
                    "surface": "ion",
                    "canonical_form": "-ion",
                    "kind": "suffix",
                    "language": "en",
                    "meaning": "process",
                    "basis": "model",
                    "confidence": 0.4,
                    "evidence_ids": [],
                },
            ]
            store.save_job_artifact(
                plan.jobs["split-morphemes"],
                "accepted-morpheme-split",
                {
                    "term": "inspection",
                    "parts": split_parts,
                    "related_terms": [
                        {
                            "term": "spectator",
                            "note": "shares the spect root",
                            "component_forms": ["spect"],
                        },
                        {
                            "term": "inward",
                            "note": "shares the in- prefix",
                            "component_forms": ["in-"],
                        },
                        {
                            "term": "decision",
                            "note": "shares the -ion suffix",
                            "component_forms": ["-ion"],
                        },
                        {
                            "term": "inspection",
                            "note": "source word must not repeat",
                            "component_forms": ["spect"],
                        },
                        {
                            "term": "spect",
                            "note": "bare component must not appear",
                            "component_forms": ["spect"],
                        },
                        {
                            "term": "<derivative word>",
                            "note": "placeholder must not appear",
                            "component_forms": ["spect"],
                        },
                    ],
                },
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
                                    "evidence_ids": [origin_evidence_id],
                                    "edge_evidence_ids": [origin_evidence_id],
                                },
                                {
                                    "historical_form_id": "h-latin",
                                    "form": "specere",
                                    "language": "la",
                                    "period": "Latin",
                                    "meaning": "look",
                                    "basis": "book",
                                    "confidence": 0.95,
                                    "evidence_ids": [origin_evidence_id],
                                    "edge_evidence_ids": [origin_evidence_id],
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
            self.assertEqual(result.status, "complete")
            card = cards.recent(1)[0]
            self.assertEqual(card["mode"], "knowledge")
            self.assertEqual(card["english"]["meaning"], meaning["definition"])
            self.assertEqual(card["japanese"]["reading"], "\u3057\u3093\u3055")
            self.assertEqual(
                card["japanese"]["ruby_tokens"],
                [{"t": "\u5be9\u67fb", "r": "\u3057\u3093\u3055"}],
            )
            self.assertNotIn(
                wrong_japanese_translation_reading,
                json.dumps(card, ensure_ascii=False),
            )
            self.assertEqual(card["extra_languages"]["french"]["term"], "inspection")
            self.assertEqual(card["evidence"][0]["evidence_id"], evidence_id)
            self.assertEqual(
                card["extensions"]["lexical_view"]["subject_entity_id"],
                plan.subject_entity_id,
            )
            self.assertNotIn("morphology_graph", card["extensions"])
            origin_result = worker.run_once()
            self.assertEqual(origin_result.job_type, "compose-origin-card")
            cards_by_mode = {card["mode"]: card for card in cards.recent(10)}
            self.assertEqual(
                set(cards_by_mode), {"knowledge", "word", "root", "affix"}
            )
            lexical_views = {
                card_mode: composed_card["extensions"]["lexical_view"]
                for card_mode, composed_card in cards_by_mode.items()
            }
            self.assertEqual(
                {view["subject_entity_id"] for view in lexical_views.values()},
                {plan.subject_entity_id},
            )
            self.assertEqual(
                {view["graph_revision"] for view in lexical_views.values()},
                {lexical_views["knowledge"]["graph_revision"]},
            )
            self.assertTrue(
                all(view["projection_hash"] for view in lexical_views.values())
            )
            self.assertEqual(
                {card_mode: view["mode"] for card_mode, view in lexical_views.items()},
                {card_mode: card_mode for card_mode in lexical_views},
            )
            self.assertEqual(
                lexical_views["word"]["focus_entity_ids"],
                [plan.subject_entity_id],
            )
            self.assertEqual(
                lexical_views["root"]["focus_entity_ids"], ["m-spect"]
            )
            self.assertEqual(
                lexical_views["affix"]["focus_entity_ids"], ["m-in", "m-ion"]
            )
            for composed_card in cards_by_mode.values():
                self.assertEqual(composed_card["japanese"]["reading"], "\u3057\u3093\u3055")
                self.assertEqual(
                    composed_card["japanese"]["ruby_tokens"],
                    [{"t": "\u5be9\u67fb", "r": "\u3057\u3093\u3055"}],
                )
                self.assertNotIn(
                    wrong_japanese_translation_reading,
                    json.dumps(composed_card, ensure_ascii=False),
                )
            origin_card = cards_by_mode["word"]
            self.assertEqual(
                origin_card["related_terms"],
                [
                    {"term": "spectator", "note": "shares the spect root"},
                    {"term": "inward", "note": "shares the in- prefix"},
                    {"term": "decision", "note": "shares the -ion suffix"},
                ],
            )
            graph = origin_card["extensions"]["morphology_graph"]
            self.assertEqual(len(graph["nodes"]), 6)
            self.assertEqual(len(graph["edges"]), 5)
            nodes_by_id = {node["id"]: node for node in graph["nodes"]}
            self.assertEqual(
                {
                    node_id: (
                        nodes_by_id[node_id]["form"],
                        nodes_by_id[node_id]["basis"],
                        nodes_by_id[node_id]["evidence_ids"],
                        nodes_by_id[node_id]["confidence"],
                    )
                    for node_id in ("m-in", "m-ion")
                },
                {
                    "m-in": ("in-", "model", [], "low"),
                    "m-ion": ("-ion", "model", [], "low"),
                },
            )
            self.assertEqual(nodes_by_id["m-spect"]["basis"], "book")
            self.assertEqual(
                nodes_by_id["m-spect"]["evidence_ids"], [origin_evidence_id]
            )
            model_edges = [
                edge for edge in graph["edges"] if edge["basis"] == "model"
            ]
            self.assertEqual(len(model_edges), 2)
            self.assertTrue(
                all(
                    edge["evidence_ids"] == [] and edge["confidence"] == "low"
                    for edge in model_edges
                )
            )
            book_edges = [
                edge for edge in graph["edges"] if edge["basis"] == "book"
            ]
            self.assertEqual(len(book_edges), 3)
            self.assertTrue(
                all(
                    edge["evidence_ids"] == [origin_evidence_id]
                    for edge in book_edges
                )
            )
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
            self.assertEqual(
                cards_by_mode["affix"]["origin_story"],
                "Accepted affix analysis gives in- as “into”; -ion as “process”.",
            )
            self.assertEqual(
                cards_by_mode["root"]["origin_story"],
                origin_card["origin_story"],
            )
            self.assertEqual(
                cards_by_mode["root"]["related_terms"],
                [{"term": "spectator", "note": "shares the spect root"}],
            )
            self.assertEqual(
                cards_by_mode["affix"]["related_terms"],
                [
                    {"term": "inward", "note": "shares the in- prefix"},
                    {"term": "decision", "note": "shares the -ion suffix"},
                ],
            )
            self.assertNotIn(
                "placeholder",
                json.dumps(
                    {
                        "root": cards_by_mode["root"]["related_terms"],
                        "affix": cards_by_mode["affix"]["related_terms"],
                    }
                ),
            )

    def test_split_and_origin_persist_canonical_scoped_assertions(self) -> None:
        class DerivativeModel(FakeAtomicModel):
            def complete_json(
                self, system: str, prompt: str, *, max_tokens: int = 256
            ) -> dict[str, Any]:
                result = super().complete_json(system, prompt, max_tokens=max_tokens)
                if "MORPHEME SPLIT" in prompt:
                    result["value"]["derivatives"] = [
                        {
                            "term": "spectator",
                            "note": "shares the spect root",
                            "component_forms": ["spect"],
                        }
                    ]
                return result

        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject_id = store.upsert_term(
                "en", "inspection", status="accepted", quality_score=0.95
            )
            subject_key = f"term:{subject_id}"
            retrieval_job = store.enqueue_job(
                "retrieve-evidence",
                subject_key,
                subject_entity_id=subject_id,
            )
            evidence_id = store.add_evidence(
                "test-dictionary:1.0",
                "dictionary-inspection-1",
                source_hash="abc123",
                locator="sense 1",
                excerpt="a careful examination of something",
                payload={
                    "entry_id": "dictionary-inspection-1",
                    "headword": "inspection",
                    "kind": "dictionary-sense",
                },
            )
            store.save_job_artifact(
                retrieval_job,
                "retrieved-evidence",
                {
                    "records": [
                        {
                            "entry_id": "dictionary-inspection-1",
                            "corpus_id": "test-dictionary:1.0",
                            "headword": "inspection",
                            "definition": "a careful examination of something",
                            "knowledge_evidence_id": evidence_id,
                        }
                    ]
                },
                validation_state="candidate",
            )
            split_job = store.enqueue_job(
                "split-morphemes", subject_key, subject_entity_id=subject_id
            )
            origin_job = store.enqueue_job(
                "expand-origin-branches", subject_key, subject_entity_id=subject_id
            )
            jobs = {
                job["job_id"]: job for job in store.jobs_for_subject(subject_key)
            }
            worker = PreparationWorker(
                store,
                FakeRetriever(),
                DerivativeModel(),
                FakePronouncer(),
            )

            split_job_record = {
                **jobs[split_job],
                "subject_key": subject_key,
                "subject_entity_id": subject_id,
            }
            origin_job_record = {
                **jobs[origin_job],
                "subject_key": subject_key,
                "subject_entity_id": subject_id,
            }

            worker._split_morphemes(split_job_record)
            worker._expand_origin_branches(origin_job_record)

            split = store.artifacts_for_subject(
                subject_key,
                stage="accepted-morpheme-split",
                validation_state="accepted",
            )[-1]["payload"]
            spectator = next(
                item for item in split["related_terms"] if item["term"] == "spectator"
            )
            self.assertEqual(
                spectator["term_id"], store.upsert_term("en", "Spectator")
            )
            root_part = next(
                part
                for part in split["parts"]
                if part["canonical_form"] == "spect"
            )
            root_id = root_part["morpheme_id"]
            self.assertTrue(root_part["evidence_ids"])
            self.assertEqual(
                {
                    record["evidence_id"]
                    for record in store.evidence_records(root_part["evidence_ids"])
                },
                set(root_part["evidence_ids"]),
            )
            view = store.lexical_subgraph(
                subject_id,
                "word",
                {"nodes": 32, "edges": 48, "depth": 4},
            )
            component = next(
                edge
                for edge in view["edges"]
                if edge["relation"] == "has-component" and edge["target"] == root_id
            )
            self.assertEqual(component["subject_entity_id"], subject_id)
            self.assertEqual(
                component["evidence_ids"], root_part["evidence_ids"]
            )
            derivative = next(
                edge
                for edge in view["edges"]
                if edge["relation"] == "shares-component"
                and edge["source"] == spectator["term_id"]
            )
            self.assertEqual(derivative["subject_entity_id"], subject_id)
            self.assertEqual(derivative["basis"], "model")
            self.assertEqual(derivative["evidence_ids"], [])
            historical = [
                edge for edge in view["edges"]
                if edge["relation"] == "developed-into"
            ]
            self.assertTrue(historical)
            self.assertTrue(
                all(edge["subject_entity_id"] == subject_id for edge in historical)
            )
            self.assertTrue(
                any(edge["evidence_ids"] for edge in historical)
            )

    def test_book_origin_metadata_does_not_require_a_model_completion(self) -> None:
        from lkt.atomic import _origin_generation_metadata

        model, metrics = _origin_generation_metadata(None, "unused-local-model")

        self.assertEqual(model, "retrieved book evidence")
        self.assertEqual(metrics, {})

    def test_publication_provenance_repairs_claims_without_rejecting_them(self) -> None:
        warnings: list[dict[str, str]] = []

        self.assertEqual(
            _publication_provenance(
                "book",
                ["stored", "invented"],
                {"stored"},
                claim="component",
                claim_id="m-root",
                warnings=warnings,
            ),
            ("book", ["stored"]),
        )
        self.assertEqual(
            _publication_provenance(
                "book",
                ["invented"],
                {"stored"},
                claim="historical-node",
                claim_id="h-latin",
                warnings=warnings,
            ),
            ("model", []),
        )
        self.assertEqual(
            _publication_provenance(
                "model",
                ["stored"],
                {"stored"},
                claim="historical-edge",
                claim_id="h-latin",
                warnings=warnings,
            ),
            ("model", []),
        )
        self.assertEqual(
            _publication_provenance(
                "uncertain",
                ["stored"],
                {"stored"},
                claim="component",
                claim_id="m-prefix",
                warnings=warnings,
            ),
            ("model", []),
        )
        self.assertEqual(
            [warning["action"] for warning in warnings],
            [
                "removed-invalid-citations",
                "downgraded-to-model",
                "removed-model-citations",
                "downgraded-to-model",
            ],
        )
        self.assertTrue(all(warning["claim_id"] for warning in warnings))

    def test_free_only_analysis_has_no_derived_root_view(self) -> None:
        from lkt.atomic import _derived_origin_view_specs

        specs = _derived_origin_view_specs(
            [{"canonical_form": "lecher", "kind": "free"}]
        )

        self.assertNotIn("root", {spec[0] for spec in specs})

    def test_origin_history_headline_deduplicates_only_adjacent_equal_forms(self) -> None:
        self.assertEqual(
            _origin_history_headline(
                ["Serendip", "serendipity", "SERENDIPITY"]
            ),
            "Serendip → serendipity",
        )
        self.assertEqual(
            _origin_history_headline(
                ["*spek-", "specere", "spect", "inspection"]
            ),
            "*spek- → specere → spect → inspection",
        )
        self.assertEqual(
            _origin_history_headline(
                ["com-", "ponere", "pon", "compound"]
            ),
            "com- → ponere → pon → compound",
        )

    def test_dictionary_join_markup_normalizes_without_term_hardcoding(self) -> None:
        self.assertEqual(_normalize_dictionary_candidate("活着+的", "zh"), "活着的")
        self.assertEqual(_normalize_dictionary_candidate("生き+ている", "ja"), "生きている")
        self.assertEqual(_normalize_dictionary_candidate("C++", "fr"), "C++")

    def test_origin_evidence_is_attached_only_for_verbatim_forms(self) -> None:
        draft = {
            "component_id": "free-alive",
            "steps": [
                {"form": "on life", "evidence_ids": ["invented"]},
                {"form": "not in source", "evidence_ids": ["entry-0171"]},
            ],
        }
        value, changes = _attach_verbatim_origin_evidence(
            draft,
            evidence=[
                {
                    "evidence_id": "entry-0171",
                    "headword": "alive",
                    "excerpt": "Alive developed from on life and Old English līf.",
                }
            ],
            allowed_ids={"entry-0171"},
        )
        self.assertEqual(value["steps"][0]["evidence_ids"], ["entry-0171"])
        self.assertEqual(value["steps"][1]["evidence_ids"], [])
        self.assertEqual(changes, ["attached-verbatim-origin-evidence"])

    def test_origin_review_allows_uncited_history_but_rejects_model_citations(self) -> None:
        evidence = [
            {
                "evidence_id": "entry-0171",
                "headword": "alive",
                "excerpt": "Alive developed from on life and Old English līf.",
            }
        ]
        draft = {
            "component_id": "free-alive",
            "steps": [
                {
                    "form": "*libjan",
                    "language": "gem-pro",
                    "period": "Proto-Germanic hypothesis",
                    "meaning": "remain alive",
                    "confidence": 0.9,
                    "evidence_ids": [],
                }
            ],
        }
        review_args = {
            "component_id": "free-alive",
            "modern_word": "alive",
            "base_form": "alive",
            "fixed_provenance_ids": set(),
            "evidence": evidence,
        }

        self.assertEqual(_origin_draft_review_reason(draft, **review_args), "")

        draft["steps"][0]["evidence_ids"] = ["entry-0171"]
        self.assertEqual(
            _origin_draft_review_reason(draft, **review_args),
            "a historical form is not visibly supported by the exact book entry",
        )


if __name__ == "__main__":
    unittest.main()
