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

    def test_card_enrichment_state_is_bulk_missing_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            acquired = store.acquire_card_book_card(
                {
                    "card_id": "answer-bulk-state",
                    "mode": "answer",
                    "english": {"term": "Look again."},
                    "japanese": {"term": "もう一度見て。"},
                    "chinese": {"simplified": "再看一次。"},
                    "evidence": [
                        {
                            "corpus_id": "book-of-answers",
                            "entry_id": "answer-bulk-state",
                            "locator": "answers.xhtml",
                            "excerpt": "Look again.",
                        }
                    ],
                }
            )
            state = store.card_book_enrichment_state()
            self.assertEqual(state["reviewed"], {"answer-bulk-state"})
            self.assertEqual(state["needs_grammar"], {"answer-bulk-state"})

            for language, entity_id in acquired["language_entity_ids"].items():
                store.enqueue_job(
                    "prepare-grammar-parts",
                    f"content:{entity_id}",
                    subject_entity_id=entity_id,
                    language=language,
                )
            state = store.card_book_enrichment_state()
            self.assertEqual(state["reviewed"], {"answer-bulk-state"})
            self.assertEqual(state["needs_grammar"], set())

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
            current = store.grammar_for_content(japanese)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["summary"], "noun plus suru verb")
            self.assertEqual(
                [part["role"] for part in current["parts"]],
                ["object", "predicate"],
            )

            replacement = store.add_grammar_analysis(
                japanese,
                "ja",
                "one predicate phrase",
                [
                    {
                        "surface": "検査する",
                        "role": "predicate",
                        "part_of_speech": "phrase",
                    }
                ],
                basis="model",
            )
            current = store.grammar_for_content(japanese)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["entity_id"], replacement)
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM entities WHERE entity_id = ?", (analysis,)
                ).fetchone()[0],
                "archived",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT basis FROM entity_edges WHERE target_entity_id = ?",
                    (replacement,),
                ).fetchone()[0],
                "model",
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

    def test_replaced_origin_archives_only_its_old_historical_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            term = store.upsert_term("en", "predecessor")
            root = store.upsert_morpheme("la", "cess", "root", "go")
            store.link_morpheme(term, root, 0, "cess", basis="book")
            first = store.add_historical_form(
                "la", "cess", period_label="Classical Latin"
            )
            second = store.add_historical_form(
                "en", "predecessor", period_label="Modern English"
            )
            properties = {"component_id": root}
            store.add_edge(first, second, "developed-into", properties=properties)
            store.add_edge(second, root, "developed-into", properties=properties)
            other_term = store.upsert_term("en", "successor")
            store.link_morpheme(other_term, root, 0, "cess", basis="book")
            other_history = store.add_historical_form(
                "la", "succedere", period_label="Classical Latin"
            )
            other_edge = store.add_edge(
                other_history,
                root,
                "developed-into",
                properties={"component_id": root},
            )
            job = store.enqueue_job(
                "expand-origin-branches",
                f"term:{term}",
                subject_entity_id=term,
            )
            store.save_job_artifact(
                job,
                "accepted-origin-branches",
                {
                    "branches": [
                        {
                            "component_id": root,
                            "steps": [
                                {"historical_form_id": first},
                                {"historical_form_id": second},
                            ],
                        }
                    ]
                },
                validation_state="accepted",
            )

            result = store.retire_origin_analysis(term, "duplicate modern node")
            self.assertEqual(result["edges_archived"], 2)
            self.assertEqual(result["historical_forms_archived"], 2)
            self.assertEqual(
                store.artifacts_for_subject(
                    f"term:{term}", stage="accepted-origin-branches"
                )[0]["validation_state"],
                "rejected",
            )
            snapshot = store.graph_snapshot()
            self.assertIn(other_edge, {edge["id"] for edge in snapshot["edges"]})
            self.assertIn(
                "has-component", {edge["relation"] for edge in snapshot["edges"]}
            )

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

    def test_job_dependencies_are_committed_with_the_queued_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            first = store.enqueue_job("retrieve-evidence", "term:atomic")
            second = store.enqueue_job(
                "prepare-meaning", "term:atomic", depends_on=(first,)
            )
            self.assertEqual(store.claim_next_job()["job_id"], first)
            self.assertIsNone(store.claim_next_job())
            store.finish_job(first)
            self.assertEqual(store.claim_next_job()["job_id"], second)

    def test_lexical_jobs_are_claimed_before_optional_content_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            content = store.enqueue_job(
                "prepare-grammar-parts", "content:old-card", priority=1
            )
            lexical = store.enqueue_job(
                "prepare-translation", "term:new-word", priority=90
            )

            self.assertEqual(store.claim_next_job()["job_id"], lexical)
            store.finish_job(lexical)
            self.assertEqual(store.claim_next_job()["job_id"], content)

    def test_only_terms_with_a_real_atomic_plan_count_as_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            discovered = store.upsert_term("en", "discovered")
            planned = store.upsert_term("en", "planned")
            self.assertEqual(store.planned_term_keys("en"), set())

            store.enqueue_job(
                "retrieve-evidence",
                f"term:{planned}",
                subject_entity_id=planned,
            )

            self.assertEqual(store.planned_term_keys("en"), {"planned"})
            self.assertNotIn("discovered", store.planned_term_keys("en"))
            self.assertEqual(store.active_term_preparation_count(), 1)

    def test_discovery_claims_are_atomic_unique_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            first = store.claim_lexical_discovery_round(
                [
                    {"term": "Ａlpha", "source_kind": "qa-investigation"},
                    {
                        "term": "Beta",
                        "source_kind": "word-origins",
                        "source_entry_id": "origin-beta",
                    },
                ]
            )
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(
                store.discovered_or_planned_term_keys("en"), {"alpha", "beta"}
            )
            self.assertEqual(len(store.unplanned_lexical_discoveries()), 2)

            collision = store.claim_lexical_discovery_round(
                [
                    {"term": "gamma", "source_kind": "word-origins"},
                    {"term": "ALPHA", "source_kind": "word-origins"},
                ]
            )
            self.assertIsNone(collision)
            self.assertNotIn("gamma", store.discovered_or_planned_term_keys("en"))

            reopened = KnowledgeStore(database)
            pending = reopened.unplanned_lexical_discoveries()
            self.assertEqual([item["normalized"] for item in pending], ["alpha", "beta"])
            reopened.mark_lexical_discovery_planned(pending[0]["discovery_id"])
            self.assertEqual(
                [item["normalized"] for item in reopened.unplanned_lexical_discoveries()],
                ["beta"],
            )

    def test_investigation_groups_keep_unclaimed_terms_on_the_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            source_id = store.upsert_content_item(
                "question",
                "en",
                "Would a technological breakthrough justify compromise?",
                source_key="question-100",
                status="accepted",
            )
            terms = []
            for ordinal, text in enumerate(
                ("technological", "breakthrough", "compromise")
            ):
                term_id = store.upsert_term("en", text, status="accepted")
                store.add_edge(
                    source_id,
                    term_id,
                    "contains-investigation-term",
                    basis="model",
                    properties={"ordinal": ordinal},
                )
                terms.append({"term_id": term_id, "term": text, "ordinal": ordinal})
            job_id = store.enqueue_job(
                "extract-investigation-terms",
                f"content:{source_id}",
                subject_entity_id=source_id,
                language="en",
            )
            artifact_id = store.save_job_artifact(
                job_id,
                "accepted-investigation-terms",
                {"terms": terms},
                language="en",
                validation_state="accepted",
            )

            groups = store.investigation_suggestion_groups()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["source_artifact_id"], artifact_id)
            self.assertEqual(
                [item["term"] for item in groups[0]["terms"]],
                ["technological", "breakthrough", "compromise"],
            )
            remaining = store.investigation_suggestion_groups(
                {"technological", "breakthrough"}
            )
            self.assertEqual(
                [item["term"] for item in remaining[0]["terms"]], ["compromise"]
            )

    def test_terminal_failure_cascades_through_queued_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            first = store.enqueue_job(
                "retrieve-evidence", "term:fragile", max_attempts=1
            )
            second = store.enqueue_job("prepare-meaning", "term:fragile")
            third = store.enqueue_job("compose-word-card", "term:fragile")
            store.add_job_dependency(second, first)
            store.add_job_dependency(third, second)

            self.assertEqual(store.claim_next_job()["job_id"], first)
            store.finish_job(first, error="source unavailable")

            jobs = {job["job_id"]: job for job in store.jobs_for_subject("term:fragile")}
            self.assertEqual([jobs[job]["status"] for job in (first, second, third)], ["failed"] * 3)
            self.assertEqual(jobs[second]["attempts"], 0)
            self.assertIn("blocked by failed prerequisite", jobs[second]["error"])
            self.assertIn("blocked by failed prerequisite", jobs[third]["error"])

    def test_explicit_retry_requeues_only_failed_jobs_in_the_named_dag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            first = store.enqueue_job(
                "retrieve-evidence", "term:retry", max_attempts=1
            )
            second = store.enqueue_job(
                "prepare-meaning", "term:retry", depends_on=(first,)
            )
            outside = store.enqueue_job(
                "retrieve-evidence", "term:outside", max_attempts=1
            )
            store.save_job_artifact(first, "retrieved-evidence", {"kept": True})
            self.assertEqual(store.claim_next_job()["job_id"], first)
            store.finish_job(first, error="terminal plan failure")
            self.assertEqual(store.claim_next_job()["job_id"], outside)
            store.finish_job(outside, error="unrelated failure")

            self.assertEqual(store.requeue_failed_jobs((first, second)), 2)
            jobs = {
                job["job_id"]: job
                for subject in ("term:retry", "term:outside")
                for job in store.jobs_for_subject(subject)
            }
            self.assertEqual(jobs[first]["status"], "queued")
            self.assertEqual(jobs[first]["attempts"], 0)
            self.assertEqual(jobs[second]["status"], "queued")
            self.assertEqual(jobs[outside]["status"], "failed")
            self.assertEqual(
                store.artifacts_for_subject("term:retry")[0]["payload"],
                {"kept": True},
            )
            self.assertEqual(store.claim_next_job()["job_id"], first)

    def test_worker_heartbeat_separates_liveness_from_generation_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            self.assertFalse(store.worker_status()["ready"])

            memory = "background preparation paused: only 900 MiB memory is available"
            store.record_worker_heartbeat(memory)
            blocked = store.worker_status()
            self.assertTrue(blocked["ready"])
            self.assertFalse(blocked["generation_ready"])
            self.assertEqual(blocked["blocker"], memory)

            store.record_worker_heartbeat("")
            self.assertTrue(store.worker_status()["generation_ready"])
            store.record_worker_heartbeat("atomic worker stopped", status="stopped")
            self.assertFalse(store.worker_status()["ready"])

    def test_claim_cleans_legacy_job_blocked_by_a_failed_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            first = store.enqueue_job(
                "retrieve-evidence", "term:legacy", max_attempts=1
            )
            self.assertEqual(store.claim_next_job()["job_id"], first)
            store.finish_job(first, error="old terminal failure")
            second = store.enqueue_job("prepare-meaning", "term:legacy")
            store.add_job_dependency(second, first)

            self.assertIsNone(store.claim_next_job())
            jobs = {job["job_id"]: job for job in store.jobs_for_subject("term:legacy")}
            self.assertEqual(jobs[second]["status"], "failed")
            self.assertEqual(jobs[second]["attempts"], 0)

    def test_interrupted_worker_lease_is_safely_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            job_id = store.enqueue_job("prepare-meaning", "term:breakthrough")
            claimed = store.claim_next_job()
            self.assertEqual(claimed["job_id"], job_id)
            self.assertEqual(claimed["attempts"], 1)
            self.assertEqual(store.recover_running_jobs(), 1)
            recovered = store.claim_next_job()
            self.assertEqual(recovered["job_id"], job_id)
            self.assertEqual(recovered["attempts"], 1)

    def test_bad_language_atoms_are_quarantined_without_touching_other_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            source = store.upsert_term("en", "breakthrough")
            meaning = store.add_meaning(source, "en", "a productive insight")
            target = store.upsert_term("ar", "انBREAKTHROUGH")
            translation = store.add_translation(
                source,
                "ar",
                "انBREAKTHROUGH",
                source_meaning_id=meaning,
                target_term_id=target,
            )
            pronunciation = store.add_pronunciation(
                target,
                "ar",
                "ipa",
                "breakthrough",
                [{"grapheme": "انBREAKTHROUGH", "phoneme": "breakthrough"}],
            )
            subject_key = f"term:{source}"
            translation_job = store.enqueue_job(
                "prepare-translation", subject_key, subject_entity_id=source, language="ar"
            )
            store.save_job_artifact(
                translation_job,
                "accepted-translation",
                {
                    "translation_id": translation,
                    "target_term_id": target,
                    "term": "انBREAKTHROUGH",
                },
                language="ar",
                validation_state="accepted",
            )
            store.finish_job(translation_job)
            pronunciation_job = store.enqueue_job(
                "prepare-pronunciation",
                subject_key,
                subject_entity_id=source,
                language="ar",
            )
            store.save_job_artifact(
                pronunciation_job,
                "accepted-pronunciation",
                {
                    "pronunciation_id": pronunciation,
                    "target_term_id": target,
                },
                language="ar",
                validation_state="accepted",
            )
            store.finish_job(pronunciation_job)
            result = store.retire_language_analysis(
                source, "ar", "mixed Arabic and Latin script"
            )
            self.assertEqual(result["artifacts_rejected"], 2)
            self.assertEqual(result["entities_rejected"], 2)
            self.assertEqual(result["orphan_terms_rejected"], 1)
            labels = {node["label"] for node in store.graph_snapshot()["nodes"]}
            self.assertNotIn("انBREAKTHROUGH", labels)
            self.assertIn("breakthrough", labels)

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
            self.assertEqual(store.status()["schema_version"], "4")

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

    def test_contextual_assertions_share_topology_without_sharing_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            predecessor = store.upsert_term("en", "predecessor")
            successor = store.upsert_term("en", "successor")
            root = store.upsert_morpheme("la", "cedere", "root", "to go")
            ancestor = store.add_historical_form(
                "la", "cedere", period_label="Classical Latin"
            )
            evidence_a = store.add_evidence("roots", "entry-predecessor")
            evidence_b = store.add_evidence("roots", "entry-successor")

            store.accept_relation_assertion(
                predecessor, predecessor, root, "has-component", basis="model"
            )
            store.accept_relation_assertion(
                successor, successor, root, "has-component", basis="model"
            )
            assertion_a = store.accept_relation_assertion(
                predecessor,
                root,
                ancestor,
                "developed-from",
                basis="book",
                evidence_ids=(evidence_a, "missing-evidence"),
            )
            assertion_b = store.accept_relation_assertion(
                successor,
                root,
                ancestor,
                "developed-from",
                basis="book",
                evidence_ids=(evidence_b,),
            )
            model_assertion = store.accept_relation_assertion(
                predecessor,
                ancestor,
                root,
                "model-associated-with",
                basis="model",
                evidence_ids=(evidence_a,),
            )

            self.assertNotEqual(assertion_a, assertion_b)
            projected = store.lexical_subgraph(
                predecessor, "origin", {"nodes": 8, "edges": 8, "depth": 4}
            )
            by_id = {edge["assertion_id"]: edge for edge in projected["edges"]}
            self.assertEqual(by_id[assertion_a]["evidence_ids"], [evidence_a])
            self.assertEqual(by_id[model_assertion]["evidence_ids"], [])

            self.assertTrue(store.retire_relation_assertion(predecessor, assertion_a))
            self.assertFalse(store.retire_relation_assertion(predecessor, assertion_b))
            with closing(store._connect()) as connection:
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE assertion_id IN (?, ?)""",
                        (assertion_a, assertion_b),
                    )
                }
            self.assertEqual(statuses[assertion_a], "archived")
            self.assertEqual(statuses[assertion_b], "accepted")

    def test_accepted_split_reconciliation_is_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject = store.upsert_term("en", "inspection")
            old_free = store.upsert_morpheme("en", "inspection", "free", "examination")
            old_suffix = store.upsert_morpheme("en", "-old", "suffix", "stale")
            derivative = store.upsert_term("en", "inspector")
            history = store.add_historical_form(
                "la", "inspectio", period_label="Latin"
            )
            store.link_morpheme(subject, old_free, 0, "inspection", basis="model")
            store.link_morpheme(subject, old_suffix, 2, "old", basis="model")
            old_component = store.accept_relation_assertion(
                subject, subject, old_free, "has-component", basis="model"
            )
            old_derivative = store.accept_relation_assertion(
                subject,
                derivative,
                old_free,
                "shares-component",
                basis="model",
            )
            retained_history = store.accept_relation_assertion(
                subject,
                old_free,
                history,
                "developed-into",
                basis="model",
                properties={"modes": ["word"]},
            )

            prefix = store.upsert_morpheme("en", "in-", "prefix", "in")
            root = store.upsert_morpheme("la", "spect", "root", "look")
            store.link_morpheme(subject, prefix, 0, "in", basis="book")
            store.link_morpheme(subject, root, 1, "spect", basis="book")
            current_component_ids = [
                store.accept_relation_assertion(
                    subject, subject, prefix, "has-component", basis="book"
                ),
                store.accept_relation_assertion(
                    subject, subject, root, "has-component", basis="book"
                ),
                store.accept_relation_assertion(
                    subject, derivative, root, "shares-component", basis="model"
                ),
            ]

            first = store.reconcile_accepted_lexical_split(
                subject,
                active_assertion_ids=current_component_ids,
                active_component_ids=[prefix, root],
                component_count=2,
            )
            revision = store.lexical_subgraph(
                subject, "word", {"nodes": 16, "edges": 24, "depth": 4}
            )["graph_revision"]
            self.assertEqual(first["assertions_retired"], 2)
            self.assertEqual(first["trailing_ordinals_removed"], 1)
            with closing(store._connect()) as connection:
                statuses = {
                    row["assertion_id"]: row["status"]
                    for row in connection.execute(
                        """SELECT assertion_id, status FROM relation_assertions
                           WHERE assertion_id IN (?, ?, ?)""",
                        (old_component, old_derivative, retained_history),
                    )
                }
                ordinals = [
                    int(row["ordinal"])
                    for row in connection.execute(
                        """SELECT ordinal FROM term_morphemes
                           WHERE term_id = ? ORDER BY ordinal""",
                        (subject,),
                    )
                ]
            self.assertEqual(statuses[old_component], "archived")
            self.assertEqual(statuses[old_derivative], "archived")
            self.assertEqual(statuses[retained_history], "accepted")
            self.assertEqual(ordinals, [0, 1])

            replay = store.reconcile_accepted_lexical_split(
                subject,
                active_assertion_ids=current_component_ids,
                active_component_ids=[prefix, root],
                component_count=2,
            )
            self.assertEqual(
                replay,
                {
                    "assertions_retired": 0,
                    "legacy_edges_retired": 0,
                    "trailing_ordinals_removed": 0,
                },
            )
            self.assertEqual(
                store.lexical_subgraph(
                    subject, "word", {"nodes": 16, "edges": 24, "depth": 4}
                )["graph_revision"],
                revision,
            )

    def test_lexical_subgraph_is_connected_bounded_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject = store.upsert_term("en", "inspection")
            chain = [
                store.upsert_term("en", f"related-{ordinal}") for ordinal in range(5)
            ]
            source = subject
            for ordinal, target in enumerate(chain):
                store.accept_relation_assertion(
                    subject,
                    source,
                    target,
                    "leads-to",
                    properties={"mode": "origin", "ordinal": ordinal},
                )
                source = target

            limits = {"nodes": 3, "edges": 2, "depth": 8}
            first = store.lexical_subgraph(subject, "origin", limits)
            second = store.lexical_subgraph(subject, "origin", limits)
            self.assertEqual(first, second)
            self.assertEqual(len(first["nodes"]), 3)
            self.assertEqual(len(first["edges"]), 2)
            self.assertTrue(first["truncated"])
            self.assertEqual(len(first["projection_hash"]), 64)
            node_ids = {node["id"] for node in first["nodes"]}
            self.assertIn(subject, node_ids)
            for edge in first["edges"]:
                self.assertIn(edge["source"], node_ids)
                self.assertIn(edge["target"], node_ids)

    def test_relation_assertion_failure_rolls_back_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            subject = store.upsert_term("en", "inspection")
            source = store.upsert_morpheme("la", "specere", "root")
            evidence = store.add_evidence("roots", "entry-inspection")
            with self.assertRaisesRegex(ValueError, "unknown relation assertion entities"):
                store.accept_relation_assertion(
                    subject,
                    source,
                    "missing-target",
                    "derived-from",
                    basis="book",
                    evidence_ids=(evidence,),
                )
            with closing(store._connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM relation_assertions").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM assertion_evidence").fetchone()[0],
                    0,
                )

    def test_v3_relation_edge_backfill_is_idempotent_and_uncited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = KnowledgeStore(database)
            subject = store.upsert_term("en", "inspection")
            root = store.upsert_morpheme("la", "specere", "root")
            history = store.add_historical_form(
                "la", "inspectio", period_label="Late Latin"
            )
            store.add_edge(
                root,
                history,
                "developed-into",
                basis="book",
                properties={"term_id": subject},
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TABLE assertion_evidence")
                connection.execute("DROP TABLE relation_assertions")
                connection.execute(
                    "UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'"
                )
                connection.execute(
                    """UPDATE schema_meta SET value = '0'
                       WHERE key = 'relation_graph_revision'"""
                )
                connection.commit()

            migrated = KnowledgeStore(database)
            with closing(migrated._connect()) as connection:
                first_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertions"
                ).fetchone()[0]
                evidence_count = connection.execute(
                    "SELECT COUNT(*) FROM assertion_evidence"
                ).fetchone()[0]
                first_revision = connection.execute(
                    """SELECT value FROM schema_meta
                       WHERE key = 'relation_graph_revision'"""
                ).fetchone()[0]
            reopened = KnowledgeStore(database)
            with closing(reopened._connect()) as connection:
                second_count = connection.execute(
                    "SELECT COUNT(*) FROM relation_assertions"
                ).fetchone()[0]
                second_revision = connection.execute(
                    """SELECT value FROM schema_meta
                       WHERE key = 'relation_graph_revision'"""
                ).fetchone()[0]
            self.assertEqual((first_count, second_count), (1, 1))
            self.assertEqual(evidence_count, 0)
            self.assertEqual((first_revision, second_revision), ("1", "1"))
            self.assertEqual(reopened.status()["schema_version"], "4")


if __name__ == "__main__":
    unittest.main()
