from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.growth import (
    _PREFIX,
    _finish_history_epochs,
    _plan_history,
    _recover_annotation_publications,
    claim_growth_work,
    growth_status,
)
from lkt.knowledge import KnowledgeStore
from lkt.morphology_contract import CURRENT_MORPHOLOGY_PARSER_VERSION
from lkt.store import card_annotation_warnings, card_validation_errors


class ContinuousGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.sqlite3")

    def subject(self, word="inspection", *, checkpoints=True):
        store = self.store
        term = store.upsert_term("en", word, status="accepted")
        root = store.upsert_morpheme("en", "spec", "root", "look")
        evidence = store.add_evidence(
            "test-corpus", "fixture-entry", locator="page 1",
            excerpt="Synthetic morphology test evidence, not a production claim.",
        )
        store.apply_relation_decision(
            term, term, root, "has-component", basis="book", confidence=0.9,
            properties={"ordinal": 0, "surface": "spec"}, evidence_ids=[evidence],
        )
        if checkpoints:
            job = store.enqueue_job("fixture", "term:" + term, subject_entity_id=term)
            for stage, payload in (
                ("accepted-meaning", {"definition": "an examination"}),
                ("accepted-morpheme-split", {"term": word, "parts": [{
                    "morpheme_id": root, "surface": "spec", "kind": "root",
                    "language": "en", "meaning": "look", "basis": "book", "evidence_ids": [evidence],
                }]}),
            ):
                store.save_job_artifact(job, stage, payload, language="en", validation_state="accepted")
            store.finish_job(job)
        return term

    @staticmethod
    def card():
        return {
            "card_id": "fixture-card", "mode": "word", "query": "inspection",
            "title": "Inspection", "grounded": True,
            "evidence": [{"corpus_id": "test-corpus", "entry_id": "fixture-entry"}],
            "languages": {
                "en": {"term": "inspection", "meaning": "an examination"},
                "ja": {"term": "\u691c\u67fb", "meaning": "examination", "ruby_tokens": []},
                "zh": {"term": "\u68c0\u67e5", "meaning": "examination", "ruby_tokens": []},
            },
            "extensions": {"morphology_graph": {
                "nodes": [{"id": "word"}, {"id": "root"}],
                "edges": [{"source": "word", "target": "root", "relation": "has-component"}],
            }},
        }

    def test_ruby_gaps_are_nonblocking_but_remain_diagnosable(self):
        card = self.card()
        self.assertEqual(card_validation_errors(card), [])
        self.assertEqual(len(card_annotation_warnings(card)), 2)
        self.assertEqual(len(card_validation_errors(card, strict_annotations=True)), 2)

    def test_manual_worker_does_not_create_autonomous_history_jobs(self):
        self.subject()
        worker = PreparationWorker(
            self.store, mock.Mock(), mock.Mock(model_name="local-test-qwen"),
        )
        self.assertIsNone(worker.run_once())
        self.assertEqual(growth_status(self.store)["worker_turns"], 0)
        self.assertEqual(growth_status(self.store)["jobs"], [])

    def test_continuous_worker_starts_a_bounded_history_job(self):
        self.subject()
        worker = PreparationWorker(
            self.store, mock.Mock(), mock.Mock(model_name="local-test-qwen"),
            continuous_growth=True,
        )
        worker._retrieve = mock.Mock(return_value="fixture:retrieval")
        result = worker.run_once()
        self.assertEqual(result.job_type, "retrieve-evidence")
        self.assertEqual(result.status, "complete")
        self.assertEqual(growth_status(self.store)["worker_turns"], 1)
        worker._retrieve.assert_called_once()

    def test_continuous_worker_preserves_explicit_type_restriction(self):
        self.subject()
        worker = PreparationWorker(
            self.store, mock.Mock(), mock.Mock(model_name="local-test-qwen"),
            continuous_growth=True,
        )
        self.assertIsNone(worker.run_once(("prepare-meaning",)))
        self.assertEqual(growth_status(self.store)["worker_turns"], 0)

    def test_worker_shutdown_grace_exceeds_default_model_request(self):
        service = (
            Path(__file__).resolve().parents[1] / "systemd" / "lkt-worker.service"
        ).read_text(encoding="utf-8")
        self.assertIn("TimeoutStopSec=15min\n", service)

    def test_provenance_and_graph_failures_still_block(self):
        card = self.card()
        card["evidence"] = []
        card["extensions"]["morphology_graph"]["edges"] = []
        errors = card_validation_errors(card)
        self.assertIn("missing RAG evidence", errors)
        self.assertIn("origin graph has no relationships", errors)

    def test_new_history_can_be_claimed_with_queued_translation(self):
        term = self.subject()
        self.store.enqueue_job("prepare-translation", "term:" + term, subject_entity_id=term, language="ja")
        claim = self.store.claim_lexical_revisit_candidate(
            prompt_prefix=_PREFIX, history_only=True,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim["term_id"], term)

    def test_default_revisit_still_respects_other_active_work(self):
        term = self.subject()
        self.store.enqueue_job("prepare-translation", "term:" + term, subject_entity_id=term, language="ja")
        self.assertIsNone(self.store.claim_lexical_revisit_candidate())

    def test_first_graph_only_subject_does_not_need_an_old_plan(self):
        term = self.subject(checkpoints=False)
        claim = self.store.claim_lexical_revisit_candidate(prompt_prefix=_PREFIX, history_only=True)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["term_id"], term)

    def test_history_plan_refreshes_rag_without_rebuilding_languages(self):
        term = self.subject()
        _plan_history(self.store, "test-local-qwen")
        jobs = [job for job in self.store.jobs_for_subject("term:" + term) if job["prompt_version"].startswith(_PREFIX)]
        self.assertEqual({job["job_type"] for job in jobs}, {
            "retrieve-evidence", "expand-origin-branches", "compose-origin-card",
        })
        self.assertTrue(all(job["model"] == "test-local-qwen" for job in jobs))
        self.assertTrue(all(len(job["source_fingerprint"]) == 64 for job in jobs))
        _plan_history(self.store, "test-local-qwen")
        self.assertEqual(len([j for j in self.store.jobs_for_subject("term:" + term) if j["prompt_version"].startswith(_PREFIX)]), 3)

    def test_expired_unbound_claim_does_not_stall_the_lane(self):
        self.subject()
        claim = self.store.claim_lexical_revisit_candidate(prompt_prefix=_PREFIX, history_only=True)
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE lexical_revisit_epochs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE epoch_id=?", (claim["epoch_id"],))
            connection.commit()
        self.assertFalse(_finish_history_epochs(self.store))
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT status FROM lexical_revisit_epochs WHERE epoch_id=?", (claim["epoch_id"],)).fetchone()
        self.assertEqual(row["status"], "failed")

    def test_unexpired_unbound_claim_is_not_stolen(self):
        self.subject()
        self.store.claim_lexical_revisit_candidate(prompt_prefix=_PREFIX, history_only=True)
        self.assertTrue(_finish_history_epochs(self.store))

    def test_completed_history_is_not_held_by_unrelated_translation(self):
        term = self.subject()
        translation = self.store.enqueue_job("prepare-translation", "term:" + term, subject_entity_id=term, language="ja")
        _plan_history(self.store, "test-local-qwen")
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE preparation_jobs SET status='complete' WHERE prompt_version LIKE ?", (_PREFIX + "%",))
            connection.commit()
        self.assertFalse(_finish_history_epochs(self.store))
        with closing(self.store._connect()) as connection:
            epoch = connection.execute("SELECT status FROM lexical_revisit_epochs WHERE prompt_version LIKE ?", (_PREFIX + "%",)).fetchone()
            job = connection.execute("SELECT status FROM preparation_jobs WHERE job_id=?", (translation,)).fetchone()
        self.assertEqual(epoch["status"], "complete")
        self.assertEqual(job["status"], "queued")

    def test_model_free_dispatch_does_not_plan_inference(self):
        self.subject()
        result = claim_growth_work(self.store, model="test-local-qwen", allowed_job_types=("compose-origin-card", "compose-word-card"))
        self.assertIsNone(result)
        self.assertEqual(growth_status(self.store)["jobs"], [])

    def failed_compose(self, term, version, error):
        job = self.store.enqueue_job("compose-origin-card", "term:" + term, subject_entity_id=term, prompt_version=version)
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE preparation_jobs SET status='failed',attempts=2,error=? WHERE job_id=?", (error, job))
            connection.commit()
        return job

    def test_recovery_is_latest_only_idempotent_and_annotation_specific(self):
        term = self.subject()
        annotation = "origin views failed prepublication validation: word:0: Japanese ruby does not cover every kanji; root:1: Chinese ruby does not cover every Han character"
        old = self.failed_compose(term, "older", annotation)
        current = self.failed_compose(term, "newer", annotation)
        broken_term = self.subject("spectator")
        broken = self.failed_compose(broken_term, "structural", annotation + "; root:2: missing RAG evidence")
        _recover_annotation_publications(self.store)
        _recover_annotation_publications(self.store)
        with closing(self.store._connect()) as connection:
            states = dict(connection.execute("SELECT job_id,status FROM preparation_jobs WHERE job_id IN (?,?,?)", (old, current, broken)).fetchall())
        self.assertEqual(states[old], "failed")
        self.assertEqual(states[current], "queued")
        self.assertEqual(states[broken], "failed")

    def morphology_job(self, label, kind, priority):
        now = datetime.now(UTC).isoformat()
        fingerprint = "f" * 64
        cluster = "cluster-" + label
        job = self.store.enqueue_job(
            "enrich-morphology-v3", "morphology-v3:" + cluster,
            priority=priority, source_fingerprint=fingerprint,
            prompt_version="fairness-" + label, model="test-local-qwen",
        )
        with closing(self.store._connect()) as connection:
            self.store._ensure_morphology_enrichment_schema(connection)
            connection.execute(
                """INSERT INTO morphology_enrichment_clusters_v3(
                    cluster_id,parser_version,cluster_key,cluster_kind,priority_class,
                    canonical_subject_key,evidence_fingerprint,input_fingerprint,
                    payload_json,status,last_job_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?)""",
                (cluster, CURRENT_MORPHOLOGY_PARSER_VERSION, cluster, kind, priority,
                 cluster, fingerprint, fingerprint, "{}", job, now, now),
            )
            connection.execute(
                """INSERT INTO morphology_enrichment_epochs_v3(
                    epoch_id,cluster_id,epoch_number,input_fingerprint,lease_token,
                    job_id,status,created_at,updated_at)
                    VALUES (?,?,1,?,?,?,'queued',?,?)""",
                ("epoch-" + label, cluster, fingerprint, "test-lease", job, now, now),
            )
            connection.commit()
        return job

    def test_morphology_kinds_get_turns_despite_declaration_priority(self):
        declaration = self.morphology_job("declaration-one", "declaration", 0)
        self.morphology_job("declaration-two", "declaration", 0)
        component = self.morphology_job("component", "component", 40)
        link = self.morphology_job("link", "link", 80)
        claimed = []
        for _ in range(3):
            job = self.store.claim_next_job(("enrich-morphology-v3",))
            self.assertIsNotNone(job)
            claimed.append(job["job_id"])
            self.store.finish_job(job["job_id"])
        self.assertEqual(claimed, [declaration, component, link])


if __name__ == "__main__":
    unittest.main()
