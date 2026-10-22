from __future__ import annotations

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from lkt.atomic import PreparationWorker, _language_card_atoms
from lkt.deck import (
    AutonomousLexicalSeeder,
    _schedulable_lexical_language,
)
from lkt.knowledge import KnowledgeStore
from lkt.languages import DISPLAY_LANGUAGES, language_profile, normalize_language_code
from lkt.preparation import PreparationPlanner
from lkt.provenance_backfill import backfill_edge_evidence
from lkt.store import CardStore
from lkt.web import graph_language_fields


class MultilingualPipelineTests(unittest.TestCase):
    def test_deck_routes_frontier_and_revisit_without_crossing_morphology(self) -> None:
        class Corpus:
            def metadata(self):
                return {"source_sha256": "fixture-sources"}

            def lexical_headwords(self):
                return {"under", "averse", "fuseau"}

        class Cards:
            def accepted_for_modes(self, modes):
                return []

        class RevisitKnowledge:
            def __init__(self, store):
                self.store = store
                self.bound = ""
                self.completed = []

            def __getattr__(self, name):
                return getattr(self.store, name)

            def claim_lexical_revisit_candidate(
                self, *, prompt_prefix, language=None
            ):
                if language is not None:
                    raise AssertionError(
                        "autonomous multilingual revisit must not prefilter language"
                    )
                return {
                    "epoch_id": "epoch-fr-fuseau",
                    "epoch_number": 2,
                    "prompt_version": f"{prompt_prefix}-revisit-2",
                    "term": "fuseau",
                    "language": "fr",
                    "novelty_kind": "graph-changed",
                }

            def bind_lexical_revisit_epoch(self, epoch_id, *, job_subject_key):
                self.bound = job_subject_key
                return epoch_id == "epoch-fr-fuseau"

            def complete_lexical_revisit_epoch(self, epoch_id, **values):
                self.completed.append((epoch_id, values))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base.sqlite3"
            KnowledgeStore(base)

            recursive_path = root / "recursive.sqlite3"
            shutil.copy2(base, recursive_path)
            recursive_store = KnowledgeStore(recursive_path)
            recursive = AutonomousLexicalSeeder(
                Corpus(), Cards(), recursive_store, model="local-qwen"
            )
            progress = {"planned": 0, "total": 3}
            under = recursive._plan_recursive_word(
                "under",
                prompt_version="frontier-under-v1",
                status="frontier-queued",
                message="English frontier",
                progress=progress,
                language="en",
            )
            self.assertEqual(under.status, "frontier-queued")
            under_id = recursive_store.upsert_term("en", "under")
            under_jobs = recursive_store.jobs_for_subject(f"term:{under_id}")
            self.assertIn("split-morphemes", {job["job_type"] for job in under_jobs})

            frontier_path = root / "frontier.sqlite3"
            shutil.copy2(base, frontier_path)
            frontier_store = KnowledgeStore(frontier_path)
            frontier = AutonomousLexicalSeeder(
                Corpus(), Cards(), frontier_store, model="local-qwen"
            )
            claim = frontier_store.claim_lexical_discovery_round(
                [
                    {
                        "language": "en",
                        "term": "averse",
                        "source_kind": "graph-frontier",
                        "source_entity_id": "",
                        "source_artifact_id": "",
                        "source_entry_id": "",
                    }
                ]
            )
            self.assertIsNotNone(claim)
            result = frontier._plan_claimed_round(
                list(claim["discoveries"]),
                progress=progress,
                source_fingerprint="fixture-sources",
            )
            self.assertEqual(result.status, "queued")
            averse_id = frontier_store.upsert_term("en", "averse")
            averse_jobs = frontier_store.jobs_for_subject(f"term:{averse_id}")
            self.assertIn("compose-origin-card", {job["job_type"] for job in averse_jobs})

            revisit_path = root / "revisit.sqlite3"
            shutil.copy2(base, revisit_path)
            revisit_store = KnowledgeStore(revisit_path)
            revisit_knowledge = RevisitKnowledge(revisit_store)
            revisit = AutonomousLexicalSeeder(
                Corpus(), Cards(), revisit_knowledge, model="local-qwen"
            )
            revisit_result = revisit._claim_and_plan_revisit(progress)
            self.assertEqual(revisit_result.status, "revisit-queued")
            fuseau_id = revisit_store.upsert_term("fr", "fuseau")
            fuseau_jobs = revisit_store.jobs_for_subject(f"term:{fuseau_id}")
            fuseau_types = {job["job_type"] for job in fuseau_jobs}
            self.assertIn("compose-word-card", fuseau_types)
            self.assertNotIn("split-morphemes", fuseau_types)
            self.assertEqual(revisit_knowledge.bound, f"term:{fuseau_id}")
            self.assertFalse(revisit_knowledge.completed)

    def test_deck_language_filter_uses_registry_and_rejects_provenance_labels(self) -> None:
        self.assertEqual(_schedulable_lexical_language("fr_CA"), "fr-ca")
        self.assertEqual(_schedulable_lexical_language("x-lkt-test"), "x-lkt-test")
        self.assertEqual(_schedulable_lexical_language("Classical Latin"), "")
        self.assertEqual(_schedulable_lexical_language("English"), "")
        self.assertEqual(_schedulable_lexical_language("Old-English"), "")
        self.assertEqual(_schedulable_lexical_language("enm"), "enm")

    def test_french_source_runs_generic_semantics_to_an_english_graph_atom(self) -> None:
        class Retriever:
            def __init__(self) -> None:
                self.calls = []

            def retrieve(
                self,
                term: str,
                *,
                source_language: str = "en",
                target_languages=(),
            ):
                self.calls.append((term, source_language, tuple(target_languages)))
                return [
                    {
                        "entry_id": "fr-fuseau-1",
                        "corpus_id": "fixture-fr:1",
                        "source_title": "Local French fixture",
                        "headword": "fuseau",
                        "definition": "Petit instrument allongé servant à filer.",
                        "part_of_speech": "noun",
                        "kind": "dictionary-sense",
                        "source_language": "fr",
                    }
                ]

            def component_evidence(self, form: str, kind: str):
                raise AssertionError("French semantics entered English morphology")

            def origin_evidence(self, form: str):
                raise AssertionError("French semantics entered English origins")

        class Model:
            model_name = "local-qwen-fixture"

            def complete_json(self, system: str, prompt: str, *, max_tokens: int = 256):
                if "RETRIEVED EVIDENCE" in prompt:
                    import re

                    evidence_id = re.search(r'"id":\s*"([^"]+)"', prompt).group(1)
                    value = {
                        "definition": "Petit instrument allongé servant à filer.",
                        "part_of_speech": "noun",
                        "sense_note": "outil textile",
                        "confidence": 0.88,
                        "evidence_ids": [evidence_id],
                    }
                elif "TARGET LANGUAGE: English" in prompt:
                    value = {
                        "term": "spindle",
                        "meaning": "A slender tool used for spinning fibre.",
                        "reading": "",
                        "usage_note": "",
                        "confidence": 0.82,
                    }
                else:
                    raise AssertionError(prompt)
                return {"value": value, "model": self.model_name, "metrics": {}}

        class Pronouncer:
            def pronounce(self, text: str, language: str):
                return {
                    "reading": f"/{text}/",
                    "system": "ipa",
                    "dialect": language,
                    "segments": [{"grapheme": text, "phoneme": f"/{text}/"}],
                    "source": {"engine": "fixture", "version": "1"},
                }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge = KnowledgeStore(root / "knowledge.sqlite3")
            cards = CardStore(root / "cards.sqlite3")
            plan = PreparationPlanner(knowledge, model="local-qwen-fixture").plan_word_card(
                "fuseau", language="fr", display_languages=("fr", "en")
            )
            retriever = Retriever()
            results = PreparationWorker(
                knowledge,
                retriever,
                Model(),
                pronouncer=Pronouncer(),
                card_store=cards,
            ).run(20)
            self.assertTrue(results)
            self.assertTrue(
                all(result.status == "complete" for result in results),
                knowledge.jobs_for_subject(plan.subject_key),
            )
            self.assertEqual(retriever.calls[0][0:2], ("fuseau", "fr"))
            self.assertNotIn("split-morphemes", plan.jobs)
            card = cards.recent(1, "knowledge")[0]
            self.assertEqual(card["languages"]["fr"]["term"], "fuseau")
            self.assertEqual(card["languages"]["en"]["term"], "spindle")
            self.assertEqual(card["english"]["term"], "spindle")
            graph = knowledge.lexical_subgraph(
                plan.subject_entity_id,
                "knowledge",
                {"nodes": 32, "edges": 48, "depth": 3},
            )
            live = graph_language_fields(graph, plan.subject_entity_id)
            self.assertEqual(live["fr"]["term"], "fuseau")
            self.assertEqual(live["en"]["term"], "spindle")

    def test_core_registry_and_arbitrary_bcp47_fallback(self) -> None:
        self.assertEqual(DISPLAY_LANGUAGES, ("en", "ja", "zh", "fr", "ar"))
        self.assertEqual(normalize_language_code("de_CH"), "de-ch")
        extra = language_profile("de-CH")
        self.assertEqual(extra.code, "de-ch")
        self.assertEqual(extra.direction, "ltr")
        self.assertEqual(language_profile("ar").direction, "rtl")

    def test_non_english_semantics_do_not_queue_english_morphology(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(store, model="local-qwen").plan_word_card(
                "fuseau",
                language="fr",
                display_languages=(*DISPLAY_LANGUAGES, "de-CH"),
            )
            self.assertIn("meaning:fr", plan.jobs)
            self.assertIn("translation:en", plan.jobs)
            self.assertIn("translation:de-ch", plan.jobs)
            self.assertNotIn("split-morphemes", plan.jobs)
            with self.assertRaisesRegex(ValueError, "English"):
                PreparationPlanner(store, model="local-qwen").plan_word(
                    "fuseau", language="fr"
                )

    def test_partial_and_extra_language_atoms_remain_usable(self) -> None:
        for text in ("fuseau", "under", "averse"):
            source = {"entity_id": f"term-{text}", "language": "en", "text": text}
            meaning = {
                "artifact_id": f"meaning-{text}",
                "quality_score": 0.9,
                "payload": {"meaning_id": f"sense-{text}", "definition": f"meaning of {text}"},
            }
            translations = {
                code: {
                    "artifact_id": f"translation-{code}-{text}",
                    "quality_score": 0.7,
                    "payload": {
                        "target_term_id": f"term-{code}-{text}",
                        "translation_id": f"tr-{code}-{text}",
                        "term": f"{text}-{code}",
                        "meaning": f"{code} meaning",
                    },
                }
                for code in ("ja", "zh", "fr", "ar", "de-ch")
            }
            atoms = _language_card_atoms(source, meaning, translations, {})
            self.assertEqual(set(atoms), {"en", "ja", "zh", "fr", "ar", "de-ch"})
            self.assertEqual(atoms["de-ch"]["state"], "complete")
            self.assertEqual(atoms["ja"]["state"], "complete")

    def test_live_graph_supplies_term_meaning_and_pronunciation(self) -> None:
        graph = {
            "nodes": [
                {"id": "source", "type": "term", "label": "under", "payload": {"language": "en"}},
                {"id": "sense-en", "type": "meaning", "label": "below", "payload": {"language": "en"}},
                {"id": "term-fr", "type": "term", "label": "sous", "payload": {"language": "fr"}},
                {"id": "sense-fr", "type": "meaning", "label": "plus bas", "payload": {"language": "fr"}},
                {"id": "pron-fr", "type": "pronunciation", "label": "su", "payload": {"language": "fr"}},
            ],
            "edges": [
                {"source": "source", "target": "sense-en", "relation": "has-meaning"},
                {"source": "source", "target": "term-fr", "relation": "has-translation"},
                {"source": "term-fr", "target": "sense-fr", "relation": "has-meaning"},
                {"source": "term-fr", "target": "pron-fr", "relation": "has-pronunciation"},
            ],
        }
        atoms = graph_language_fields(graph, "source")
        self.assertEqual(atoms["fr"]["term"], "sous")
        self.assertEqual(atoms["fr"]["meaning"], "plus bas")
        self.assertEqual(atoms["fr"]["reading"], "su")

    def test_edge_evidence_backfill_is_dry_run_first_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original = Path(temp) / "original.sqlite3"
            store = KnowledgeStore(original)
            term = store.upsert_term("en", "averse")
            evidence = store.add_evidence("fixture", "averse-1", excerpt="averse evidence")
            store.add_meaning(term, "en", "strongly disinclined", basis="book", evidence_ids=[evidence])
            copied = Path(temp) / "copy.sqlite3"
            shutil.copy2(original, copied)
            dry = backfill_edge_evidence(copied)
            self.assertTrue(dry.dry_run)
            self.assertGreaterEqual(dry.assertions_to_create, 1)
            applied = backfill_edge_evidence(copied, dry_run=False)
            self.assertGreaterEqual(applied.evidence_links_created, 1)
            replay = backfill_edge_evidence(copied)
            self.assertEqual(replay.assertions_to_create, 0)
            self.assertEqual(replay.evidence_links_to_create, 0)

    def test_core_modules_do_not_import_output_adapters(self) -> None:
        root = Path(__file__).resolve().parents[1]
        forbidden = {"web", "static", "eink", "audio"}
        for name in (
            "languages.py", "retrieval.py", "lexicon.py", "preparation.py",
            "atomic.py", "provenance_backfill.py",
        ):
            tree = ast.parse((root / "lkt" / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertFalse(imported & forbidden, name)


if __name__ == "__main__":
    unittest.main()
