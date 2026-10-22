from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from lkt.atomic import PreparationWorker
from lkt.growth import _TURN_KEY, claim_growth_work
from lkt.knowledge import KnowledgeStore
from lkt.language_completion import (
    LANGUAGE_SLOTS, REFRESH_VERSION, claim_language_completion,
    language_completion_status, queue_language_card_refreshes,
)
from lkt.store import CardStore


class LanguageCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "knowledge.sqlite3"
        self.store = KnowledgeStore(self.path)
        self.sequence = 0

    def word(self, text):
        return self.store.upsert_term("en", text, status="accepted")

    def job(self, term, kind="prepare-translation", language="zh", **kwargs):
        self.sequence += 1
        return self.store.enqueue_job(
            kind, "term:" + term, subject_entity_id=term, language=language,
            prompt_version="fixture-" + str(self.sequence), **kwargs,
        )

    def artifact(self, job, stage, payload, language="en"):
        return self.store.save_job_artifact(
            job, stage, payload, language=language, validation_state="accepted",
        )

    def partial_card(self, term, inputs=()):
        job = self.job(term, "compose-word-card", "en")
        artifact = self.artifact(job, "accepted-word-card", {
            "knowledge_artifact_ids": list(inputs),
        })
        self.store.finish_job(job)
        return artifact

    def test_published_partial_card_does_not_hide_queued_translation(self):
        term = self.word("sample")
        self.partial_card(term)
        translation = self.job(term)
        self.assertIsNone(self.store.claim_word_card_progress())
        claimed = claim_language_completion(self.store, ("prepare-translation",))
        self.assertEqual(claimed["job_id"], translation)
        self.assertEqual(claimed["status"], "running")

    def test_missing_language_precedes_existing_checkpoint_rerun(self):
        old = self.word("older")
        completed = self.job(old)
        self.artifact(completed, "accepted-translation", {"term": "fixture"}, "zh")
        self.store.finish_job(completed)
        self.job(old, priority=1)
        missing = self.job(self.word("newer"), priority=1000)
        self.assertEqual(claim_language_completion(
            self.store, ("prepare-translation",),
        )["job_id"], missing)

    def test_existing_partial_card_gets_completion_before_unpublished_word(self):
        self.job(self.word("not-published"))
        term = self.word("published")
        self.partial_card(term)
        translation = self.job(term)
        self.assertEqual(claim_language_completion(
            self.store, ("prepare-translation",),
        )["job_id"], translation)

    def test_language_rotation_survives_restart_and_counts_failed_attempts(self):
        first = self.job(self.word("one"), language="zh")
        self.job(self.word("two"), language="zh")
        other = self.job(self.word("three"), language="fr")
        claimed = claim_language_completion(self.store, ("prepare-translation",))
        self.assertEqual(claimed["job_id"], first)
        self.store.finish_job(first, error="synthetic recoverable failure")
        restarted = KnowledgeStore(self.path)
        claimed = claim_language_completion(restarted, ("prepare-translation",))
        self.assertEqual(claimed["job_id"], other)
        self.assertEqual(language_completion_status(restarted)["last_dispatch"]["language"], "fr")

    def test_prerequisites_are_claimed_without_violating_type_filter(self):
        term = self.word("dependent")
        retrieval = self.job(term, "retrieve-evidence", "en")
        translation = self.job(term, depends_on=(retrieval,))
        self.assertIsNone(claim_language_completion(self.store, ("prepare-translation",)))
        claimed = claim_language_completion(
            self.store, ("prepare-translation", "retrieve-evidence"),
        )
        self.assertEqual(claimed["job_id"], retrieval)
        self.store.finish_job(retrieval)
        self.assertEqual(claim_language_completion(
            self.store, ("prepare-translation",),
        )["job_id"], translation)

    def test_exhausted_dependencies_do_not_get_retried_or_leased(self):
        term = self.word("blocked")
        retrieval = self.job(term, "retrieve-evidence", "en", max_attempts=1)
        translation = self.job(term, depends_on=(retrieval,))
        self.store.claim_next_job(("retrieve-evidence",))
        self.store.finish_job(retrieval, error="synthetic terminal failure")
        self.assertIsNone(claim_language_completion(
            self.store, ("prepare-translation", "retrieve-evidence"),
        ))
        jobs = {j["job_id"]: j for j in self.store.jobs_for_subject("term:" + term)}
        self.assertEqual(jobs[translation]["status"], "failed")
        self.assertEqual(jobs[retrieval]["attempts"], 1)

    def test_reserved_slots_survive_a_continuous_origin_backlog(self):
        allowed = ("prepare-translation", "prepare-meaning", "split-morphemes")
        term = self.word("waiting")
        for slot in LANGUAGE_SLOTS:
            translation = self.job(term, language="zh")
            with closing(self.store._connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
                    (_TURN_KEY, str(slot)),
                )
                connection.commit()
            with patch("lkt.growth.claim_publication_job", return_value={"job_id": "origin"}):
                claimed = claim_growth_work(self.store, model="local-test", allowed_job_types=allowed)
            self.assertEqual(claimed["job_id"], translation)
            self.store.finish_job(translation)

    def test_origin_slots_remain_protected_with_pending_languages(self):
        self.job(self.word("pending"))
        with patch("lkt.growth.claim_publication_job", return_value={"job_id": "origin"}):
            claimed = claim_growth_work(
                self.store, model="local-test",
                allowed_job_types=("prepare-translation", "split-morphemes"),
            )
        self.assertEqual(claimed["job_id"], "origin")

    def test_language_free_slot_is_lent_back_to_publication(self):
        with closing(self.store._connect()) as connection:
            connection.execute("INSERT INTO schema_meta(key,value) VALUES (?,?)", (_TURN_KEY, "1"))
            connection.commit()
        with patch("lkt.growth.claim_publication_job", return_value={"job_id": "origin"}):
            claimed = claim_growth_work(
                self.store, model="local-test",
                allowed_job_types=("prepare-translation", "split-morphemes"),
            )
        self.assertEqual(claimed["job_id"], "origin")

    def refresh_fixture(self):
        term = self.word("refresh")
        meaning_job = self.job(term, "prepare-meaning", "en")
        meaning = self.artifact(meaning_job, "accepted-meaning", {"definition": "fixture"})
        self.store.finish_job(meaning_job)
        self.partial_card(term, (meaning,))
        translation_job = self.job(term)
        translation = self.artifact(translation_job, "accepted-translation", {"term": "fixture"}, "zh")
        self.store.finish_job(translation_job)
        return term, meaning, translation

    def test_one_fingerprint_bound_refresh_does_not_wait_for_other_languages(self):
        term, _, _ = self.refresh_fixture()
        self.job(term, language="ja")
        self.job(term, "prepare-pronunciation", "zh")
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 1)
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 0)
        job = self.store.claim_next_job(("compose-word-card",))
        self.assertEqual(job["prompt_version"], REFRESH_VERSION)
        self.assertEqual(len(job["source_fingerprint"]), 64)

    def test_artifact_identity_detects_stale_snapshot_published_later(self):
        term, meaning, _ = self.refresh_fixture()
        self.partial_card(term, (meaning,))
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 1)

    def test_current_snapshot_does_not_requeue_composition(self):
        term, meaning, translation = self.refresh_fixture()
        self.partial_card(term, (meaning, translation))
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 0)

    def test_exhausted_refresh_waits_for_new_accepted_input(self):
        term, _, _ = self.refresh_fixture()
        queue_language_card_refreshes(self.store, model="local-test")
        for _ in range(2):
            job = self.store.claim_next_job(("compose-word-card",))
            self.store.finish_job(job["job_id"], error="synthetic missing evidence")
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 0)
        newer = self.job(term, language="fr")
        self.artifact(newer, "accepted-translation", {"term": "fixture"}, "fr")
        self.store.finish_job(newer)
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 1)

    def test_refresh_publishes_new_chinese_without_another_model_call(self):
        term = self.word("example")
        evidence = self.store.add_evidence(
            "fixture-book", "fixture-entry", excerpt="An illustrative case.",
            payload={"headword": "example", "source_title": "Fixture book", "pages": [3]},
        )
        sense = self.store.add_meaning(term, "en", "An illustrative case.", part_of_speech="noun")
        meaning_job = self.job(term, "prepare-meaning", "en")
        self.artifact(meaning_job, "accepted-meaning", {
            "meaning_id": sense, "definition": "An illustrative case.",
            "part_of_speech": "noun", "evidence_ids": [evidence],
        })
        self.store.finish_job(meaning_job)
        retrieval = self.job(term, "retrieve-evidence", "en")
        self.store.save_job_artifact(retrieval, "retrieved-evidence", {"records": [{
            "knowledge_evidence_id": evidence, "headword": "example",
            "entry_id": "fixture-entry", "corpus_id": "fixture-book",
            "source_title": "Fixture book", "excerpt": "An illustrative case.", "pages": [3],
        }]})
        self.store.finish_job(retrieval)
        self.job(term, "compose-word-card", "en")
        model = Mock(model_name="local-test")
        worker = PreparationWorker(
            self.store, Mock(), model, card_store=CardStore(self.directory / "cards.sqlite3"),
        )
        first = worker.run_once(("compose-word-card",))
        self.assertEqual(first.status, "complete")
        translated = "\u4f8b\u5b50"
        target = self.store.upsert_term("zh", translated)
        translation_id = self.store.add_translation(
            term, "zh", translated, source_meaning_id=sense, target_term_id=target,
            definition="\u7528\u4e8e\u8bf4\u660e\u7684\u60c5\u51b5", transliteration="li zi",
        )
        translated_job = self.job(term)
        self.artifact(translated_job, "accepted-translation", {
            "translation_id": translation_id, "target_term_id": target,
            "source_meaning_id": sense, "term": translated,
            "meaning": "\u7528\u4e8e\u8bf4\u660e\u7684\u60c5\u51b5", "reading": "li zi",
            "confidence": 0.7, "evidence_ids": [evidence],
        }, "zh")
        self.store.finish_job(translated_job)
        self.job(term, language="ja")
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 1)
        second = worker.run_once(("compose-word-card",))
        self.assertEqual(second.status, "complete")
        saved = self.store.artifacts_for_subject(
            "term:" + term, stage="accepted-word-card", validation_state="accepted",
        )[-1]["payload"]["card"]
        self.assertEqual(saved["chinese"]["simplified"], translated)
        self.assertEqual(saved["evidence"][0]["entry_id"], "fixture-entry")
        self.assertEqual(queue_language_card_refreshes(self.store, model="local-test"), 0)
        model.complete_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
