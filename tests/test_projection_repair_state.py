from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.knowledge import (
    CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION,
    KnowledgeStore,
)


class ProjectionRepairStateTests(unittest.TestCase):
    def store(self, temp: str) -> KnowledgeStore:
        return KnowledgeStore(Path(temp) / "knowledge.sqlite3")

    def subject(
        self,
        store: KnowledgeStore,
        word: str,
        *,
        kind: str = "root",
        meaning: str = "a test component",
    ) -> tuple[str, str]:
        term_id = store.upsert_term("en", word)
        form = f"{word}-part" if kind != "suffix" else f"-{word}-part"
        component_id = store.upsert_morpheme("en", form, kind, meaning)
        store.apply_relation_decision(
            term_id,
            term_id,
            component_id,
            "has-component",
            confidence=0.72,
            properties={"ordinal": 0, "surface": word[:3]},
        )
        return term_id, component_id

    def checkpoints(self, store: KnowledgeStore, term_id: str) -> str:
        job_id = store.enqueue_job(
            "retrieve-evidence",
            f"term:{term_id}",
            subject_entity_id=term_id,
            language="en",
            model="test-local-qwen",
            prompt_version=f"projection-checkpoints-{term_id}",
        )
        for stage in (
            "accepted-meaning",
            "accepted-morpheme-split",
            "accepted-origin-branches",
        ):
            store.save_job_artifact(
                job_id,
                stage,
                {"stage": stage},
                validation_state="accepted",
            )
        return job_id

    def test_schema_v12_and_claim_is_exactly_once_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.subject(store, "restartable")
            self.checkpoints(store, term_id)
            first = store.claim_lexical_projection_repair()
            self.assertIsNotNone(first)
            self.assertEqual(first["term_entity_id"], term_id)
            self.assertEqual(first["desired_modes"], ["root"])

            reopened = self.store(temp)
            self.assertIsNone(reopened.claim_lexical_projection_repair())
            with closing(reopened._connect()) as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                       WHERE term_entity_id = ?""",
                    (term_id,),
                )
                connection.commit()
            recovered = self.store(temp).claim_lexical_projection_repair()
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["term_entity_id"], term_id)
            self.assertNotEqual(recovered["lease_token"], first["lease_token"])
            self.assertEqual(version, "13")

    def test_fingerprint_is_stable_under_unrelated_graph_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.subject(store, "locality")
            before = store.lexical_projection_repair_state(term_id)
            self.assertIsNotNone(before)
            other_term, other_component = self.subject(store, "elsewhere")
            store.apply_relation_decision(
                other_term,
                other_term,
                other_component,
                "has-component",
                confidence=0.83,
                properties={"ordinal": 0, "surface": "else"},
            )
            after = store.lexical_projection_repair_state(term_id)
            self.assertEqual(
                after["desired_fingerprint"], before["desired_fingerprint"]
            )
            self.assertEqual(after["updated_at"], before["updated_at"])

    def test_one_active_repair_globally_blocks_another_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            subjects = {
                self.subject(store, "global-first")[0],
                self.subject(store, "global-second")[0],
            }
            for term_id in subjects:
                self.checkpoints(store, term_id)

            first = store.claim_lexical_projection_repair()
            self.assertIsNotNone(first)
            reopened = self.store(temp)
            self.assertIsNone(reopened.claim_lexical_projection_repair())

            compose_job_id = store.enqueue_job(
                "compose-origin-card",
                f"term:{first['term_entity_id']}",
                subject_entity_id=first["term_entity_id"],
                language="en",
                model="test-local-qwen",
                prompt_version=(
                    "projection-compose-" + first["desired_fingerprint"]
                ),
            )
            self.assertTrue(
                store.bind_lexical_projection_repair(
                    first["term_entity_id"],
                    first["desired_fingerprint"],
                    first["lease_token"],
                    compose_job_id,
                )
            )
            self.assertIsNone(reopened.claim_lexical_projection_repair())
            compose = store.claim_next_job(("compose-origin-card",))
            self.assertEqual(compose["job_id"], compose_job_id)
            store.finish_job(compose_job_id)
            outcomes = reopened.reconcile_lexical_projection_repairs()
            self.assertIn(
                {
                    "term_entity_id": first["term_entity_id"],
                    "outcome": "applied",
                },
                outcomes,
            )

            second = reopened.claim_lexical_projection_repair()
            self.assertIsNotNone(second)
            self.assertEqual(
                second["term_entity_id"],
                next(iter(subjects - {first["term_entity_id"]})),
            )

    def test_queue_before_bind_orphan_is_reconciled_across_restart(self) -> None:
        expectations = {
            "queued": ("planned", "orphan-adopted"),
            "running": ("planned", "orphan-adopted"),
            "complete": ("applied", "applied"),
            "failed": ("cooldown", "cooldown"),
        }
        for job_status, (state_status, outcome) in expectations.items():
            with self.subTest(job_status=job_status), tempfile.TemporaryDirectory() as temp:
                store = self.store(temp)
                term_id, _ = self.subject(store, f"orphan-{job_status}")
                self.checkpoints(store, term_id)
                claim = store.claim_lexical_projection_repair()
                compose_job_id = store.enqueue_job(
                    "compose-origin-card",
                    f"term:{term_id}",
                    subject_entity_id=term_id,
                    language="en",
                    model="test-local-qwen",
                    prompt_version=f"orphan-{job_status}",
                    source_fingerprint=claim["desired_fingerprint"],
                    max_attempts=1,
                )
                if job_status != "queued":
                    running = store.claim_next_job(("compose-origin-card",))
                    self.assertEqual(running["job_id"], compose_job_id)
                if job_status == "complete":
                    store.finish_job(compose_job_id)
                elif job_status == "failed":
                    store.finish_job(compose_job_id, error="terminal compose failure")
                with closing(store._connect()) as connection:
                    connection.execute(
                        """UPDATE lexical_projection_repairs
                           SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                           WHERE term_entity_id = ?""",
                        (term_id,),
                    )
                    connection.commit()

                reopened = self.store(temp)
                reconciled = reopened.reconcile_lexical_projection_repairs()
                state = reopened.lexical_projection_repair_state(term_id)
                self.assertEqual(state["status"], state_status)
                self.assertIn(
                    {"term_entity_id": term_id, "outcome": outcome},
                    reconciled,
                )
                if state_status == "planned":
                    self.assertEqual(state["compose_job_id"], compose_job_id)
                    self.assertEqual(
                        state["bound_fingerprint"], claim["desired_fingerprint"]
                    )
                    self.assertIsNone(
                        reopened.claim_lexical_projection_repair()
                    )
                elif state_status == "applied":
                    self.assertEqual(
                        state["applied_fingerprint"],
                        claim["desired_fingerprint"],
                    )
                else:
                    self.assertEqual(state["failure_count"], 1)
                    self.assertEqual(state["compose_job_id"], compose_job_id)

    def test_stale_fingerprint_orphan_does_not_bind_or_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.subject(store, "stale-orphan")
            self.checkpoints(store, term_id)
            first = store.claim_lexical_projection_repair()
            stale_job_id = store.enqueue_job(
                "compose-origin-card",
                f"term:{term_id}",
                subject_entity_id=term_id,
                language="en",
                model="test-local-qwen",
                prompt_version="stale-projection-compose",
                source_fingerprint="stale-" + first["desired_fingerprint"],
            )
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                       WHERE term_entity_id = ?""",
                    (term_id,),
                )
                connection.commit()

            reopened = self.store(temp)
            current = reopened.claim_lexical_projection_repair()
            self.assertIsNotNone(current)
            self.assertEqual(current["term_entity_id"], term_id)
            self.assertEqual(
                current["desired_fingerprint"], first["desired_fingerprint"]
            )
            state = reopened.lexical_projection_repair_state(term_id)
            self.assertEqual(state["status"], "claimed")
            self.assertNotEqual(state["compose_job_id"], stale_job_id)

    def test_component_change_redirties_an_applied_existing_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, component_id = self.subject(store, "mutable")
            self.checkpoints(store, term_id)
            claim = store.claim_lexical_projection_repair()
            self.assertIsNotNone(claim)
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            applied = store.lexical_projection_repair_state(term_id)
            self.assertEqual(applied["status"], "applied")

            same_id = store.upsert_morpheme(
                "en", "mutable-part", "root", "a corrected component meaning"
            )
            self.assertEqual(same_id, component_id)
            dirty = store.lexical_projection_repair_state(term_id)
            self.assertEqual(dirty["status"], "pending")
            self.assertNotEqual(
                dirty["desired_fingerprint"], applied["applied_fingerprint"]
            )
            second = store.claim_lexical_projection_repair()
            self.assertIsNotNone(second)
            self.assertEqual(second["term_entity_id"], term_id)

    def test_stale_published_projection_precedes_new_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            stale_term_id, stale_component_id = self.subject(store, "stale-view")
            self.checkpoints(store, stale_term_id)
            first = store.claim_lexical_projection_repair()
            self.assertEqual(first["term_entity_id"], stale_term_id)
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    stale_term_id, first["desired_fingerprint"]
                )
            )

            frontier_term_id, _ = self.subject(store, "new-frontier")
            self.checkpoints(store, frontier_term_id)
            store.upsert_morpheme(
                "en", "stale-view-part", "root", "corrected published meaning"
            )
            stale_state = store.lexical_projection_repair_state(stale_term_id)
            self.assertEqual(stale_state["status"], "pending")
            self.assertTrue(stale_state["applied_fingerprint"])

            with mock.patch.object(
                store,
                "_refresh_projection_repair_state",
                side_effect=AssertionError(
                    "known stale projections must bypass historical audit"
                ),
            ):
                preferred = store.claim_lexical_projection_repair()
            self.assertEqual(preferred["term_entity_id"], stale_term_id)
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    stale_term_id, preferred["desired_fingerprint"]
                )
            )
            frontier = store.claim_lexical_projection_repair()
            self.assertEqual(frontier["term_entity_id"], frontier_term_id)

    def test_periodic_audit_recovers_old_unmarked_role_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, retired_component = self.subject(store, "old-direct")
            retained_component = store.upsert_morpheme(
                "en", "old-direct-stem", "root", "retained root"
            )
            store.apply_relation_decision(
                term_id,
                term_id,
                retained_component,
                "has-component",
                confidence=0.81,
                properties={"ordinal": 1, "surface": "stem"},
            )
            self.checkpoints(store, term_id)
            claim = store.claim_lexical_projection_repair()
            self.assertTrue(
                store.complete_lexical_projection_repair(
                    term_id, claim["desired_fingerprint"]
                )
            )
            applied = store.lexical_projection_repair_state(term_id)

            # Simulate a pre-fix reprocessor: canonical graph data changed,
            # but no projection invalidation was written and its timestamp is
            # too old for the fast timestamp detector.
            with closing(store._connect()) as connection:
                connection.execute(
                    """UPDATE relation_assertions
                       SET status = 'archived', revision = revision + 1,
                           updated_at = '2000-01-01T00:00:00+00:00'
                       WHERE subject_entity_id = ?
                         AND source_entity_id = ?
                         AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (term_id, term_id, retired_component),
                )
                connection.execute(
                    """UPDATE entity_edges SET status = 'archived',
                           updated_at = '2000-01-01T00:00:00+00:00'
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component'""",
                    (term_id, retired_component),
                )
                connection.execute(
                    """DELETE FROM term_morphemes
                       WHERE term_id = ? AND morpheme_id = ?""",
                    (term_id, retired_component),
                )
                connection.commit()

            frontier_term_id, _ = self.subject(store, "audit-frontier")
            self.checkpoints(store, frontier_term_id)
            reopened = self.store(temp)
            original_refresh = reopened._refresh_projection_repair_state
            with mock.patch.object(
                reopened,
                "_refresh_projection_repair_state",
                wraps=original_refresh,
            ) as refresh:
                recovered = reopened.claim_lexical_projection_repair()
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["term_entity_id"], term_id)
            self.assertNotEqual(recovered["term_entity_id"], frontier_term_id)
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(recovered["desired_modes"], ["root"])
            self.assertNotEqual(
                recovered["desired_fingerprint"], applied["applied_fingerprint"]
            )

    def test_same_fingerprint_cools_down_but_novel_fingerprint_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.subject(store, "coolable")
            self.checkpoints(store, term_id)
            claim = store.claim_lexical_projection_repair()
            self.assertTrue(
                store.fail_lexical_projection_repair(
                    term_id,
                    claim["desired_fingerprint"],
                    "compose failed",
                    lease_token=claim["lease_token"],
                )
            )
            cooled = store.lexical_projection_repair_state(term_id)
            self.assertEqual(cooled["status"], "cooldown")
            self.assertTrue(cooled["next_eligible_at"])
            refreshed = store.refresh_lexical_projection_repair(term_id)
            self.assertEqual(refreshed["status"], "cooldown")
            self.assertEqual(
                refreshed["desired_fingerprint"], claim["desired_fingerprint"]
            )
            self.assertIsNone(store.claim_lexical_projection_repair())

            store.upsert_morpheme(
                "en", "coolable-part", "root", "new local evidence"
            )
            novel = store.lexical_projection_repair_state(term_id)
            self.assertEqual(novel["status"], "pending")
            self.assertEqual(novel["next_eligible_at"], "")
            next_claim = store.claim_lexical_projection_repair()
            self.assertIsNotNone(next_claim)
            self.assertNotEqual(
                next_claim["desired_fingerprint"], claim["desired_fingerprint"]
            )

    def test_graph_only_subject_without_checkpoints_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, _ = self.subject(store, "graph-only", kind="suffix")
            state = store.lexical_projection_repair_state(term_id)
            self.assertEqual(state["desired_modes"], ["affix"])
            self.assertIsNone(store.claim_lexical_projection_repair())

    def test_bootstrap_is_bounded_complete_and_idempotent_beyond_64(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            shared = store.upsert_morpheme("en", "bootstrap-root", "root", "base")
            for index in range(130):
                term_id = store.upsert_term("en", f"bootstrap-{index:03d}")
                store.apply_relation_decision(
                    term_id,
                    term_id,
                    shared,
                    "has-component",
                    confidence=0.7,
                    properties={"ordinal": 0, "surface": "boot"},
                )
            with closing(store._connect()) as connection:
                connection.execute("DELETE FROM lexical_projection_repairs")
                connection.execute(
                    "DELETE FROM lexical_projection_bootstrap_state"
                )
                connection.commit()

            first = store.bootstrap_lexical_projection_repairs(64)
            second = store.bootstrap_lexical_projection_repairs(64)
            third = store.bootstrap_lexical_projection_repairs(64)
            replay = store.bootstrap_lexical_projection_repairs(64)
            self.assertEqual(
                [first["scanned"], second["scanned"], third["scanned"]],
                [64, 64, 2],
            )
            self.assertTrue(third["complete"])
            self.assertEqual(replay["scanned"], 0)
            with closing(store._connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM lexical_projection_repairs"
                ).fetchone()[0]
            self.assertEqual(count, 130)

    def test_contract_bump_invalidates_once_without_semantic_graph_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_ids = [
                self.subject(store, f"contract-{index}")[0]
                for index in range(2)
            ]
            for term_id in term_ids:
                self.checkpoints(store, term_id)
            for _ in term_ids:
                claim = store.claim_lexical_projection_repair()
                self.assertIsNotNone(claim)
                self.assertTrue(
                    store.complete_lexical_projection_repair(
                        claim["term_entity_id"], claim["desired_fingerprint"]
                    )
                )
            applied = {
                term_id: store.lexical_projection_repair_state(term_id)[
                    "applied_fingerprint"
                ]
                for term_id in term_ids
            }
            with closing(store._connect()) as connection:
                graph_before = tuple(
                    connection.execute(
                        """SELECT
                               (SELECT value FROM schema_meta
                                WHERE key = 'relation_graph_revision'),
                               (SELECT COUNT(*) FROM relation_assertions),
                               (SELECT COUNT(*) FROM entity_edges)"""
                    ).fetchone()
                )

            next_version = CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION + 1
            with mock.patch(
                "lkt.knowledge.CURRENT_LEXICAL_PROJECTION_CONTRACT_VERSION",
                next_version,
            ):
                reopened = self.store(temp)
                pages = [
                    reopened.bootstrap_lexical_projection_repairs(1)
                    for _ in range(3)
                ]
                self.assertEqual(
                    [page["scanned"] for page in pages], [1, 1, 0]
                )
                self.assertEqual(
                    [page["marked"] for page in pages], [1, 1, 0]
                )
                self.assertTrue(pages[-1]["complete"])
                for term_id in term_ids:
                    dirty = reopened.lexical_projection_repair_state(term_id)
                    self.assertEqual(dirty["status"], "pending")
                    self.assertEqual(
                        dirty["applied_fingerprint"], applied[term_id]
                    )
                    self.assertNotEqual(
                        dirty["desired_fingerprint"], applied[term_id]
                    )

                restarted = self.store(temp)
                replay = restarted.bootstrap_lexical_projection_repairs(1)
                self.assertEqual(replay["scanned"], 0)
                self.assertEqual(replay["marked"], 0)
                with closing(restarted._connect()) as connection:
                    graph_after = tuple(
                        connection.execute(
                            """SELECT
                                   (SELECT value FROM schema_meta
                                    WHERE key = 'relation_graph_revision'),
                                   (SELECT COUNT(*) FROM relation_assertions),
                                   (SELECT COUNT(*) FROM entity_edges)"""
                        ).fetchone()
                    )
                    bootstrap = connection.execute(
                        """SELECT COUNT(*), MIN(complete), MAX(scanned_count)
                           FROM lexical_projection_bootstrap_state
                           WHERE scheduler_key = ?""",
                        (f"lexical-projection-contract-v{next_version}",),
                    ).fetchone()
                self.assertEqual(graph_after, graph_before)
                self.assertEqual(tuple(bootstrap), (1, 1, len(term_ids)))

    def test_assertion_failure_rolls_back_projection_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.store(temp)
            term_id, component_id = self.subject(store, "transactional")
            before = store.lexical_projection_repair_state(term_id)
            with mock.patch.object(
                store,
                "_advance_relation_graph_revision",
                side_effect=RuntimeError("forced rollback"),
            ):
                with self.assertRaises(RuntimeError):
                    store.apply_relation_decision(
                        term_id,
                        term_id,
                        component_id,
                        "has-component",
                        confidence=0.91,
                        properties={"ordinal": 0, "surface": "trans"},
                    )
            after = store.lexical_projection_repair_state(term_id)
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
