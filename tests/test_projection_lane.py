from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.deck import AutonomousLexicalSeeder
from lkt.knowledge import KnowledgeStore
from lkt.preparation import PreparationPlanner
from lkt.store import CardStore


class _IdleCorpus:
    def metadata(self) -> dict[str, str]:
        return {"source_sha256": "test-books"}

    def lexical_headwords(self) -> tuple[str, ...]:
        return ()

    def draw_unseen_word(self, _seed: str, _excluded: set[str]):
        return None


class ProjectionLaneTests(unittest.TestCase):
    def _stores(self, directory: str) -> tuple[KnowledgeStore, CardStore]:
        root = Path(directory)
        return (
            KnowledgeStore(root / "knowledge.sqlite3"),
            CardStore(root / "cards.sqlite3"),
        )

    def _subject(
        self,
        store: KnowledgeStore,
        word: str,
        *,
        language: str = "en",
        meaning: str = "a stable root meaning",
    ) -> tuple[str, str, str]:
        term_id = store.upsert_term(language, word, status="accepted")
        component_form = f"{word[:4]}-root"
        component_id = store.upsert_morpheme(
            language, component_form, "root", meaning
        )
        evidence_id = store.add_evidence(
            "projection-test-book",
            f"entry-{word}",
            source_hash=f"hash-{word}",
            locator=f"page {len(word)}",
            excerpt=f"{word} contains the root {component_form}.",
            payload={"headword": word, "definition": f"the meaning of {word}"},
        )
        store.apply_relation_decision(
            term_id,
            term_id,
            component_id,
            "has-component",
            basis="book",
            confidence=0.94,
            properties={"ordinal": 0, "surface": word[:4]},
            evidence_ids=[evidence_id],
        )
        checkpoint_job = store.enqueue_job(
            "projection-test-checkpoints",
            f"term:{term_id}",
            subject_entity_id=term_id,
            language=language,
            model="test-local-qwen",
            prompt_version=f"checkpoints-{word}",
        )
        part = {
            "morpheme_id": component_id,
            "surface": word[:4],
            "canonical_form": component_form,
            "kind": "root",
            "language": language,
            "meaning": meaning,
            "confidence": 0.94,
            "evidence_ids": [evidence_id],
            "basis": "book",
        }
        checkpoints = (
            (
                "accepted-meaning",
                {
                    "definition": f"the accepted meaning of {word}",
                    "part_of_speech": "noun",
                    "sense_note": "",
                    "confidence": 0.94,
                    "evidence_ids": [evidence_id],
                },
            ),
            ("accepted-morpheme-split", {"term": word, "parts": [part]}),
            (
                "accepted-origin-branches",
                {
                    "term": word,
                    "branches": [
                        {
                            "component_id": component_id,
                            "component_form": component_form,
                            "component_kind": "root",
                            "steps": [],
                        }
                    ],
                },
            ),
        )
        for stage, payload in checkpoints:
            store.save_job_artifact(
                checkpoint_job,
                stage,
                payload,
                language=language,
                validation_state="accepted",
                quality_score=0.94,
            )
        store.finish_job(checkpoint_job)
        return term_id, component_id, evidence_id

    @staticmethod
    def _seeder(
        store: KnowledgeStore, cards: CardStore
    ) -> AutonomousLexicalSeeder:
        return AutonomousLexicalSeeder(
            _IdleCorpus(), cards, store, model="test-local-qwen"
        )

    @staticmethod
    def _add_busy_subject(
        store: KnowledgeStore, seeder: AutonomousLexicalSeeder, name: str
    ) -> None:
        term_id = store.upsert_term("en", name, status="draft")
        subject_key = f"term:{term_id}"
        store.enqueue_job(
            "prepare-meaning",
            subject_key,
            subject_entity_id=term_id,
            language="en",
            prompt_version=f"busy-{name}",
        )
        seeder._active_discovery_subject_keys.add(subject_key)

    @staticmethod
    def _complete_claimed_projection(store: KnowledgeStore) -> str:
        claim = store.claim_lexical_projection_repair()
        if claim is None:
            raise AssertionError("expected a projection repair claim")
        planner = PreparationPlanner(
            store, model="test-local-qwen", prompt_version="projection-drain-test"
        )
        plan = planner.plan_projection_repair(
            str(claim["term_entity_id"]),
            str(claim["desired_fingerprint"]),
            failure_count=int(claim["failure_count"]),
        )
        job_id = plan.jobs["compose-origin-card"]
        if not store.bind_lexical_projection_repair(
            str(claim["term_entity_id"]),
            str(claim["desired_fingerprint"]),
            str(claim["lease_token"]),
            job_id,
        ):
            raise AssertionError("projection repair did not bind")
        job = store.claim_next_job(("compose-origin-card",))
        if job is None or str(job["job_id"]) != job_id:
            raise AssertionError("bound projection compose was not claimable")
        store.finish_job(job_id)
        store.reconcile_lexical_projection_repairs()
        return str(claim["term_entity_id"])

    def test_busy_discovery_still_queues_one_compose_only_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, cards = self._stores(temp)
            term_id, _, _ = self._subject(store, "backlogged")
            state = store.lexical_projection_repair_state(term_id)
            seeder = self._seeder(store, cards)
            self._add_busy_subject(store, seeder, "ordinary-work")

            result = seeder.run_bounded_discovery_once()

            self.assertEqual(result.status, "projection-repair-queued")
            projection_jobs = [
                job
                for job in store.jobs_for_subject(f"term:{term_id}")
                if job["source_fingerprint"] == state["desired_fingerprint"]
            ]
            self.assertEqual(len(projection_jobs), 1)
            self.assertEqual(projection_jobs[0]["job_type"], "compose-origin-card")
            self.assertEqual(projection_jobs[0]["status"], "queued")
            self.assertIn("-projection-local-p", projection_jobs[0]["prompt_version"])
            self.assertNotIn("-projection-g", projection_jobs[0]["prompt_version"])
            self.assertEqual(
                store.lexical_projection_repair_state(term_id)["status"], "planned"
            )

    def test_projection_admission_does_not_reconcile_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, cards = self._stores(temp)
            self._subject(store, "single-reconcile")
            seeder = self._seeder(store, cards)

            with mock.patch.object(
                store,
                "reconcile_lexical_projection_repairs",
                side_effect=AssertionError("claim owns transactional recovery"),
            ):
                result = seeder._plan_due_projection_repair()

            self.assertEqual(result.status, "projection-repair-queued")

    def test_planner_uses_existing_subject_and_is_idempotent_per_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, _ = self._stores(temp)
            term_id, _, _ = self._subject(store, "identity")
            state = store.lexical_projection_repair_state(term_id)
            planner = PreparationPlanner(
                store, model="test-local-qwen", prompt_version="projection-test"
            )
            with mock.patch.object(
                store,
                "upsert_term",
                side_effect=AssertionError("must not resolve a manual word"),
            ):
                first = planner.plan_projection_repair(
                    term_id,
                    state["desired_fingerprint"],
                    failure_count=state["failure_count"],
                )
                second = planner.plan_projection_repair(
                    term_id,
                    state["desired_fingerprint"],
                    failure_count=state["failure_count"],
                )
            self.assertEqual(first, second)
            job_id = first.jobs["compose-origin-card"]
            jobs = [
                job
                for job in store.jobs_for_subject(first.subject_key)
                if job["job_id"] == job_id
            ]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["source_fingerprint"], state["desired_fingerprint"])
            with closing(store._connect()) as connection:
                dependencies = connection.execute(
                    "SELECT COUNT(*) FROM job_dependencies WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(dependencies, 1)

    def test_success_applies_and_local_change_refreshes_existing_root_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, cards = self._stores(temp)
            term_id, component_id, _ = self._subject(store, "mutable")
            seeder = self._seeder(store, cards)
            first_fingerprint = store.lexical_projection_repair_state(term_id)[
                "desired_fingerprint"
            ]
            self.assertEqual(
                seeder.run_bounded_discovery_once().status,
                "projection-repair-queued",
            )
            worker = PreparationWorker(
                store, object(), object(), card_store=cards
            )
            with mock.patch("lkt.atomic.card_validation_errors", return_value=[]):
                first = worker.run_once(("compose-origin-card",))
            self.assertEqual(first.status, "complete")
            applied = store.lexical_projection_repair_state(term_id)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["applied_fingerprint"], first_fingerprint)

            corrected = "a corrected and more precise root meaning"
            self.assertEqual(
                store.upsert_morpheme(
                    "en", "muta-root", "root", corrected
                ),
                component_id,
            )
            dirty = store.lexical_projection_repair_state(term_id)
            self.assertEqual(dirty["status"], "pending")
            self.assertNotEqual(dirty["desired_fingerprint"], first_fingerprint)
            self.assertEqual(
                seeder.run_bounded_discovery_once().status,
                "projection-repair-queued",
            )
            with mock.patch("lkt.atomic.card_validation_errors", return_value=[]):
                second = worker.run_once(("compose-origin-card",))
            self.assertEqual(second.status, "complete")
            refreshed = [
                card
                for card in cards.accepted_for_modes(("root",))
                if card["query"] == "mutable"
            ]
            self.assertEqual(len(refreshed), 1)
            nodes = refreshed[0]["extensions"]["morphology_graph"]["nodes"]
            component = next(node for node in nodes if node["id"] == component_id)
            self.assertEqual(component["meaning"], corrected)

            stable = store.lexical_projection_repair_state(term_id)
            self.assertEqual(stable["status"], "applied")
            before_jobs = len(store.jobs_for_subject(f"term:{term_id}"))
            other_id, _, _ = self._subject(store, "elsewhere")
            self.assertNotEqual(other_id, term_id)
            self._add_busy_subject(store, seeder, "still-busy")
            result = seeder.run_bounded_discovery_once()
            self.assertIn(result.status, {"projection-repair-queued", "busy"})
            self.assertEqual(
                store.lexical_projection_repair_state(term_id)["status"], "applied"
            )
            self.assertEqual(
                len(store.jobs_for_subject(f"term:{term_id}")), before_jobs
            )

    def test_terminal_compose_failure_cools_down_and_falls_back_to_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, cards = self._stores(temp)
            term_id, _, _ = self._subject(store, "fallible")
            seeder = self._seeder(store, cards)
            self.assertEqual(
                seeder.run_bounded_discovery_once().status,
                "projection-repair-queued",
            )
            projection_job = next(
                job
                for job in store.jobs_for_subject(f"term:{term_id}")
                if "-projection-local-p" in job["prompt_version"]
            )
            for _ in range(2):
                claimed = store.claim_next_job(("compose-origin-card",))
                self.assertEqual(claimed["job_id"], projection_job["job_id"])
                store.finish_job(claimed["job_id"], error="invalid accepted projection")
            self._add_busy_subject(store, seeder, "fallback-work")

            result = seeder.run_bounded_discovery_once()

            self.assertEqual(result.status, "busy")
            state = store.lexical_projection_repair_state(term_id)
            self.assertEqual(state["status"], "cooldown")
            self.assertTrue(state["next_eligible_at"])

    def test_queue_before_bind_crash_is_adopted_without_a_second_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, cards = self._stores(temp)
            term_id, _, _ = self._subject(store, "recoverable")
            claim = store.claim_lexical_projection_repair(lease_seconds=30)
            planner = PreparationPlanner(
                store, model="test-local-qwen", prompt_version="autonomous-lexical-v4"
            )
            plan = planner.plan_projection_repair(
                term_id,
                claim["desired_fingerprint"],
                failure_count=claim["failure_count"],
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                       WHERE term_entity_id = ?""",
                    (term_id,),
                )
                connection.commit()
            seeder = self._seeder(store, cards)
            self._add_busy_subject(store, seeder, "post-crash-work")

            result = seeder.run_bounded_discovery_once()

            self.assertEqual(result.status, "busy")
            state = store.lexical_projection_repair_state(term_id)
            self.assertEqual(state["status"], "planned")
            self.assertEqual(
                state["compose_job_id"], plan.jobs["compose-origin-card"]
            )
            projection_jobs = [
                job
                for job in store.jobs_for_subject(f"term:{term_id}")
                if job["source_fingerprint"] == claim["desired_fingerprint"]
            ]
            self.assertEqual(len(projection_jobs), 1)

    def test_idle_projection_backlog_drains_past_burst_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, _ = self._stores(temp)
            expected = {
                self._subject(store, f"drainable-{index}")[0]
                for index in range(11)
            }

            completed = {
                self._complete_claimed_projection(store) for _ in range(11)
            }

            self.assertEqual(completed, expected)
            self.assertIsNone(store.claim_lexical_projection_repair())

    def test_repair_burst_yields_to_ready_work_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store, _ = self._stores(temp)
            for index in range(10):
                self._subject(store, f"fairness-{index}")
            for _ in range(8):
                self._complete_claimed_projection(store)
            ordinary_term = store.upsert_term(
                "en", "ordinary-ready-work", status="accepted"
            )
            ordinary_job = store.enqueue_job(
                "prepare-meaning",
                f"term:{ordinary_term}",
                subject_entity_id=ordinary_term,
                language="en",
                prompt_version="ordinary-ready-work",
            )

            restarted = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            self.assertIsNone(restarted.claim_lexical_projection_repair())
            claimed_ordinary = restarted.claim_next_job(("prepare-meaning",))
            self.assertIsNotNone(claimed_ordinary)
            self.assertEqual(claimed_ordinary["job_id"], ordinary_job)
            restarted.finish_job(ordinary_job)

            next_repair = restarted.claim_lexical_projection_repair()
            self.assertIsNotNone(next_repair)
            self.assertNotEqual(
                next_repair["term_entity_id"], ordinary_term
            )


if __name__ == "__main__":
    unittest.main()
