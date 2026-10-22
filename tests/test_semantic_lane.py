from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from lkt.atomic import (
    PreparationWorker,
    SUPPORTED_ATOMIC_JOBS,
    _semantic_enrichment_worker_slot_due,
)
from lkt.knowledge import KnowledgeStore
from lkt.preparation import SemanticEnrichmentSeeder


class _UnusedRetriever:
    def retrieve(self, _term: str, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("morpheme semantic enrichment must use persisted context")


class _SemanticModel:
    model_name = "local-qwen-test"

    def __init__(self, evidence_id: str, *, invent_citation: bool = False):
        self.evidence_id = evidence_id
        self.invent_citation = invent_citation
        self.prompts: list[str] = []

    def complete_json(
        self, _system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {
            "value": {
                "meaning": "to turn or direct toward another position",
                "confidence": 0.91,
                "evidence_ids": [
                    "invented-evidence" if self.invent_citation else self.evidence_id
                ],
            },
            "model": self.model_name,
            "metrics": {"max_tokens": max_tokens},
        }


def _morphology_fixture(
    store: KnowledgeStore,
    *,
    form: str = "vert",
    cached_meaning: str = "",
) -> tuple[str, str, str]:
    term_id = store.upsert_term("en", "revert", status="accepted")
    morpheme_id = store.upsert_morpheme(
        "en", form, "root", cached_meaning, status="accepted"
    )
    evidence_id = store.add_evidence(
        "root-dictionary:test",
        f"root-{form}",
        source_hash=f"hash-{form}",
        locator="root entry",
        excerpt=f"re (= back) + {form} (= turn)",
        payload={"headword": "revert", "kind": "root-dictionary"},
    )
    store.link_evidence(
        morpheme_id,
        evidence_id,
        claim=f"exact reviewed source for {form}",
        confidence=0.95,
    )
    if cached_meaning:
        store.add_entity_meaning(
            morpheme_id,
            cached_meaning,
            basis="book",
            confidence=0.95,
            evidence_ids=(evidence_id,),
        )
    store.accept_relation_assertion(
        term_id,
        term_id,
        morpheme_id,
        "has-component",
        basis="book",
        confidence=0.95,
        evidence_ids=(evidence_id,),
    )
    return term_id, morpheme_id, evidence_id


def _evidence_free_morphology_fixture(
    store: KnowledgeStore, *, form: str = "phan"
) -> tuple[str, str]:
    term_id = store.upsert_term("en", f"{form}atic", status="accepted")
    morpheme_id = store.upsert_morpheme(
        "en", form, "root", status="accepted"
    )
    store.accept_relation_assertion(
        term_id,
        term_id,
        morpheme_id,
        "has-component",
        basis="model",
        confidence=0.55,
    )
    return term_id, morpheme_id


class SemanticLaneTests(unittest.TestCase):
    @staticmethod
    def _scheduling_worker(store: KnowledgeStore, evidence_id: str) -> PreparationWorker:
        worker = PreparationWorker(
            store,
            _UnusedRetriever(),
            _SemanticModel(evidence_id),
            card_store=object(),
        )
        worker._compose_word_card = lambda job: f"artifact:{job['job_id']}"
        worker._compose_origin_card = lambda job: f"artifact:{job['job_id']}"
        worker._enrich_morphology_v3 = lambda job: f"artifact:{job['job_id']}"
        worker._retrieve = lambda job: f"artifact:{job['job_id']}"
        worker._prepare_meaning = lambda job: f"artifact:{job['job_id']}"
        worker._prepare_entity_meaning = lambda job: f"artifact:{job['job_id']}"
        return worker

    def test_morpheme_lane_uses_local_evidence_and_applies_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, morpheme_id, evidence_id = _morphology_fixture(store)
            seeded = SemanticEnrichmentSeeder(
                store,
                model="local-qwen-test",
                prompt_version="semantic-test-v1",
            ).run_once()

            self.assertTrue(seeded["selected"])
            self.assertEqual(seeded["entity_id"], morpheme_id)
            self.assertEqual(seeded["job_type"], "prepare-entity-meaning")
            self.assertIn("prepare-entity-meaning", SUPPORTED_ATOMIC_JOBS)
            model = _SemanticModel("E1")
            result = PreparationWorker(store, _UnusedRetriever(), model).run_once(
                ("prepare-entity-meaning",)
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.status, "complete")
            self.assertIn('"id": "E1"', model.prompts[0])
            self.assertNotIn(evidence_id, model.prompts[0])
            self.assertIn("re (= back) + vert (= turn)", model.prompts[0])
            draft = store.artifacts_for_job(
                seeded["job_id"], stage="entity-meaning-model-draft"
            )[-1]["payload"]
            accepted = store.artifacts_for_job(
                seeded["job_id"], stage="accepted-entity-meaning"
            )[-1]["payload"]
            trace = accepted["meaning_inference"]
            self.assertEqual(trace["evidence_aliases"], {"E1": evidence_id})
            self.assertEqual(trace["selected_evidence_ids"], [evidence_id])
            self.assertEqual(draft["meaning_inference"], trace)
            self.assertEqual(draft["completion"]["value"]["evidence_ids"], ["E1"])
            self.assertEqual(accepted["evidence_ids"], [evidence_id])
            context = store.semantic_enrichment_context(morpheme_id)
            self.assertEqual(
                context["accepted_meanings"][0]["definition"],
                "to turn or direct toward another position",
            )
            self.assertEqual(
                context["accepted_meanings"][0]["evidence_ids"], [evidence_id]
            )
            self.assertEqual(
                store.lexical_semantic_enrichment_state(morpheme_id)["status"],
                "applied",
            )

    def test_morpheme_lane_keeps_cautious_model_context_without_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, morpheme_id = _evidence_free_morphology_fixture(store)
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            self.assertEqual(seeded["entity_id"], morpheme_id)
            model = _SemanticModel("", invent_citation=True)

            retriever = mock.Mock()
            retriever.retrieve.return_value = []
            result = PreparationWorker(store, retriever, model).run_once(
                ("prepare-entity-meaning",)
            )

            self.assertEqual(result.status, "complete")
            self.assertIn("evidence_ids: exactly []", model.prompts[0])
            context = store.semantic_enrichment_context(morpheme_id)
            meaning = context["accepted_meanings"][0]
            self.assertEqual(meaning["evidence_ids"], [])
            self.assertLessEqual(float(meaning["confidence"]), 0.35)
            artifact = store.artifacts_for_job(
                seeded["job_id"], stage="accepted-entity-meaning"
            )[-1]["payload"]
            self.assertEqual(artifact["grounding"], "model-context-only")
            self.assertEqual(artifact["evidence_ids"], [])

    def test_old_exact_evidence_failure_is_requeued_for_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, morpheme_id = _evidence_free_morphology_fixture(
                store, form="vac"
            )
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            for _attempt in range(2):
                job = store.claim_next_job(("prepare-entity-meaning",))
                self.assertEqual(job["job_id"], seeded["job_id"])
                store.finish_job(
                    job["job_id"],
                    error="semantic meaning has no exact local evidence",
                )
            self.assertTrue(
                store.fail_lexical_semantic_enrichment(
                    morpheme_id,
                    str(job["source_fingerprint"]),
                    "semantic meaning has no exact local evidence",
                    enrichment_job_id=str(job["job_id"]),
                )
            )

            outcomes = store.reconcile_lexical_semantic_enrichments()

            self.assertIn(
                {"entity_id": morpheme_id, "outcome": "model-context-requeued"},
                outcomes,
            )
            jobs = {item["job_id"]: item for item in store.jobs_for_subject(
                f"semantic:{morpheme_id}:en"
            )}
            self.assertEqual(jobs[seeded["job_id"]]["status"], "queued")
            self.assertEqual(jobs[seeded["job_id"]]["attempts"], 0)
            self.assertEqual(
                store.lexical_semantic_enrichment_state(morpheme_id)["status"],
                "planned",
            )

    def test_invented_citation_is_dropped_without_rejecting_useful_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, morpheme_id, evidence_id = _morphology_fixture(
                store, form="spec"
            )
            seeded = SemanticEnrichmentSeeder(store, model="local-qwen-test").run_once()
            model = _SemanticModel(evidence_id, invent_citation=True)
            result = PreparationWorker(
                store, _UnusedRetriever(), model,
            ).run_once(("prepare-entity-meaning",))

            self.assertEqual(result.status, "complete")
            self.assertEqual(len(model.prompts), 1)
            context = store.semantic_enrichment_context(morpheme_id)
            meaning = context["accepted_meanings"][0]
            self.assertEqual(meaning["evidence_ids"], [])
            self.assertLessEqual(float(meaning["confidence"]), 0.35)
            state = store.lexical_semantic_enrichment_state(morpheme_id)
            self.assertEqual(state["status"], "applied")
            self.assertEqual(state["failure_count"], 0)
            artifact = store.artifacts_for_job(
                seeded["job_id"], stage="accepted-entity-meaning"
            )[-1]["payload"]
            self.assertEqual(artifact["grounding"], "model-context-only")
            self.assertEqual(artifact["evidence_ids"], [])
            self.assertEqual(
                artifact["meaning_inference"]["selected_evidence_ids"], [evidence_id]
            )

    def test_derivative_reuses_prepare_meaning_and_global_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id, _morpheme_id, evidence_id = _morphology_fixture(
                store, cached_meaning="turn"
            )
            store.link_evidence(
                term_id,
                evidence_id,
                claim="dictionary evidence for derivative",
                confidence=0.9,
            )
            seeder = SemanticEnrichmentSeeder(
                store,
                model="local-qwen-test",
                prompt_version="semantic-test-v2",
            )

            seeded = seeder.run_once()
            blocked = seeder.run_once()

            self.assertTrue(seeded["selected"])
            self.assertEqual(seeded["entity_id"], term_id)
            self.assertEqual(seeded["job_type"], "prepare-meaning")
            self.assertEqual(
                set(seeded["jobs"]), {"retrieve-evidence", "prepare-meaning"}
            )
            self.assertFalse(blocked["selected"])
            jobs = {
                job["job_type"]: job
                for job in store.jobs_for_subject(f"term:{term_id}")
            }
            self.assertEqual(jobs["retrieve-evidence"]["priority"], 6)
            self.assertEqual(jobs["prepare-meaning"]["priority"], 7)
            self.assertEqual(
                store.lexical_semantic_enrichment_state(term_id)[
                    "enrichment_job_id"
                ],
                seeded["job_id"],
            )

    def test_bound_semantic_job_runs_after_four_persisted_completions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, _morpheme_id, evidence_id = _morphology_fixture(store)
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            compositions = [
                store.enqueue_job(
                    "compose-word-card",
                    f"term:composition-{index}",
                    priority=90,
                )
                for index in range(6)
            ]

            results = [
                self._scheduling_worker(store, evidence_id).run_once()
                for _ in range(5)
            ]

            self.assertEqual(
                [result.job_id for result in results],
                [*compositions[:4], seeded["job_id"]],
            )
            self.assertTrue(all(result.status == "complete" for result in results))

    def test_semantic_cadence_yields_to_every_existing_worker_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id, _morpheme_id, evidence_id = _morphology_fixture(store)
            first = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            completed = PreparationWorker(
                store, _UnusedRetriever(), _SemanticModel(evidence_id)
            ).run_once(("prepare-entity-meaning",))
            self.assertEqual((completed.job_id, completed.status), (first["job_id"], "complete"))

            store.link_evidence(
                term_id,
                evidence_id,
                claim="dictionary evidence for derivative",
                confidence=0.9,
            )
            second = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            self.assertEqual(second["job_type"], "prepare-meaning")
            self.assertIn(
                second["job_id"],
                {
                    job["job_id"]
                    for job in store.jobs_for_subject(f"term:{term_id}")
                },
            )
            retrieval = second["jobs"]["retrieve-evidence"]
            projection = store.enqueue_job(
                "compose-origin-card", "term:projection-repair", priority=90
            )
            morphology = store.enqueue_job(
                "enrich-morphology-v3", "morphology:cadence", priority=50
            )
            ordinary = store.enqueue_job(
                "prepare-meaning", "term:ordinary", priority=20
            )

            results = [
                self._scheduling_worker(store, evidence_id).run_once()
                for _ in range(5)
            ]

            self.assertEqual(
                [result.job_id for result in results],
                [projection, morphology, retrieval, ordinary, second["job_id"]],
            )
            self.assertTrue(all(result.status == "complete" for result in results))

    def test_bind_failure_cancels_every_job_from_the_unbound_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, morpheme_id, _evidence_id = _morphology_fixture(store)
            seeder = SemanticEnrichmentSeeder(store, model="local-qwen-test")

            with mock.patch.object(
                store, "bind_lexical_semantic_enrichment", return_value=False
            ):
                result = seeder.run_once()

            self.assertFalse(result["selected"])
            jobs = store.jobs_for_subject(f"semantic:{morpheme_id}:en")
            self.assertTrue(jobs)
            self.assertEqual({job["status"] for job in jobs}, {"failed"})

    def test_stale_term_semantic_job_fails_before_model_or_accepted_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            term_id, _morpheme_id, evidence_id = _morphology_fixture(
                store, cached_meaning="turn"
            )
            store.link_evidence(term_id, evidence_id, claim="derivative evidence")
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            retrieval = store.claim_next_job(("retrieve-evidence",))
            self.assertEqual(retrieval["job_id"], seeded["jobs"]["retrieve-evidence"])
            store.finish_job(retrieval["job_id"])
            connection = store._connect()
            try:
                connection.execute(
                    """UPDATE lexical_semantic_enrichments
                       SET desired_fingerprint = ? WHERE entity_id = ?""",
                    ("f" * 64, term_id),
                )
                connection.commit()
            finally:
                connection.close()
            model = _SemanticModel(evidence_id)

            result = PreparationWorker(store, _UnusedRetriever(), model).run_once(
                ("prepare-meaning",)
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(model.prompts, [])
            self.assertEqual(
                store.artifacts_for_job(
                    seeded["job_id"], stage="accepted-meaning"
                ),
                [],
            )

    def test_failed_nonsemantic_attempts_advance_the_semantic_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, _morpheme_id, evidence_id = _morphology_fixture(store)
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            failures = [
                store.enqueue_job(
                    "split-morphemes",
                    f"term:failing-{index}",
                    priority=20,
                    max_attempts=1,
                )
                for index in range(4)
            ]

            results = []
            for _ in failures:
                worker = self._scheduling_worker(store, evidence_id)
                worker._split_morphemes = lambda _job: (_ for _ in ()).throw(
                    ValueError("terminal nonsemantic failure")
                )
                results.append(worker.run_once())
            results.append(self._scheduling_worker(store, evidence_id).run_once())

            self.assertEqual(
                [result.job_id for result in results],
                [*failures, seeded["job_id"]],
            )
            self.assertEqual([result.status for result in results[:4]], ["retry"] * 4)
            self.assertEqual(results[-1].status, "complete")

    def test_watermark_write_failure_is_nonfatal_and_self_heals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            _term_id, _morpheme_id, evidence_id = _morphology_fixture(store)
            seeded = SemanticEnrichmentSeeder(
                store, model="local-qwen-test"
            ).run_once()
            worker = self._scheduling_worker(store, evidence_id)

            with mock.patch(
                "lkt.atomic._record_semantic_worker_attempt",
                side_effect=sqlite3.OperationalError("transient lock"),
            ):
                result = worker.run_once(("prepare-entity-meaning",))

            self.assertEqual((result.job_id, result.status), (seeded["job_id"], "complete"))
            self.assertFalse(_semantic_enrichment_worker_slot_due(store))
            connection = store._connect()
            try:
                watermark = connection.execute(
                    """SELECT value FROM schema_meta
                       WHERE key = 'atomic_semantic_last_attempt_at'"""
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(watermark)


if __name__ == "__main__":
    unittest.main()
