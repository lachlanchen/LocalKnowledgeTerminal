from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lkt.knowledge import KnowledgeStore
from lkt.morphology_contract import CURRENT_MORPHOLOGY_PARSER_VERSION
from lkt.web import autonomous_morphology_status


class RuntimeQueuePolicyTests(unittest.TestCase):
    @staticmethod
    def _cluster(
        store: KnowledgeStore, cluster_id: str, parser_version: str, job_id: str
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        connection = store._connect()
        try:
            store._ensure_morphology_enrichment_schema(connection)
            connection.execute(
                """INSERT INTO morphology_enrichment_clusters_v3(
                       cluster_id, parser_version, cluster_key, cluster_kind,
                       priority_class, canonical_subject_key,
                       evidence_fingerprint, input_fingerprint, payload_json,
                       status, last_job_id, created_at, updated_at
                   ) VALUES (?, ?, ?, 'declaration', 0, ?, ?, ?, '{}',
                             'queued', ?, ?, ?)""",
                (
                    cluster_id,
                    parser_version,
                    f"cluster-key:{cluster_id}",
                    f"component:{cluster_id}",
                    f"evidence:{cluster_id}",
                    f"input:{cluster_id}",
                    job_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_fresh_store_status_does_not_require_morphology_runtime_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            job_id = store.enqueue_job("prepare-meaning", "term:fresh-status")

            status = store.status()

            self.assertEqual(status["queued_jobs"], 1)
            self.assertEqual(status["counts"]["preparation_jobs"], 1)
            self.assertEqual(
                store.claim_next_job(("prepare-meaning",))["job_id"], job_id
            )

    def test_retired_parser_job_is_not_runnable_or_counted_as_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            stale_id = "retired-cluster"
            current_id = "current-cluster"
            stale_job = store.enqueue_job(
                "enrich-morphology-v3",
                f"morphology-v3:{stale_id}",
                priority=1,
            )
            current_job = store.enqueue_job(
                "enrich-morphology-v3",
                f"morphology-v3:{current_id}",
                priority=2,
            )
            self._cluster(store, stale_id, "retired-parser-version", stale_job)
            self._cluster(
                store,
                current_id,
                CURRENT_MORPHOLOGY_PARSER_VERSION,
                current_job,
            )

            self.assertEqual(store.status()["queued_jobs"], 1)
            self.assertTrue(store.morphology_enrichment_worker_slot_due())
            claimed = store.claim_next_job(("enrich-morphology-v3",))

            self.assertEqual(claimed["job_id"], current_job)
            connection = store._connect()
            try:
                stale = connection.execute(
                    "SELECT status, error FROM preparation_jobs WHERE job_id = ?",
                    (stale_job,),
                ).fetchone()
                cluster = connection.execute(
                    """SELECT status, last_outcome_json
                       FROM morphology_enrichment_clusters_v3
                       WHERE cluster_id = ?""",
                    (stale_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(stale["status"], "paused")
            self.assertIn("retired morphology parser version", stale["error"])
            self.assertEqual(cluster["status"], "cooldown")
            self.assertEqual(
                json.loads(cluster["last_outcome_json"])["outcome"],
                "retired-parser-version",
            )


class MorphologyHealthContractTests(unittest.TestCase):
    def test_health_uses_active_projection_counts_and_names_legacy_metrics(self) -> None:
        class ProjectionStore:
            @staticmethod
            def accepted_deck_info(mode: str) -> dict[str, object]:
                return {
                    "root": {
                        "total": 74,
                        "revision": "74:2026-09-04T23:25:34+00:00",
                    },
                    "affix": {
                        "total": 49,
                        "revision": "49:2026-09-04T23:25:35+00:00",
                    },
                }[mode]

        legacy = {
            "ready": True,
            "accepted": 91,
            "modes": {
                "root": {"accepted": 61},
                "affix": {"accepted": 30},
            },
        }
        v3 = {
            "ready": True,
            "observed": 11516,
            "materialized": 6838,
            "ambiguous": 4320,
            "qwen_ready": 4320,
            "graph_revision": 9064,
            "enrichment": {"clusters": {"complete": 8, "queued": 5}},
        }
        progress = {**legacy, "legacy_cards": legacy, "v3": v3}
        service = SimpleNamespace(
            morphology={},
            store=ProjectionStore(),
            model=SimpleNamespace(model_name="local-qwen-test"),
        )
        with mock.patch("lkt.web.AutonomousMorphologySeeder") as seeder:
            seeder.return_value.progress.return_value = progress

            status = autonomous_morphology_status(service, mock.Mock())

        self.assertEqual(status["active_cards"], 123)
        self.assertEqual(status["accepted"], 123)
        self.assertEqual(status["modes"]["root"]["active_cards"], 74)
        self.assertEqual(status["modes"]["affix"]["active_cards"], 49)
        self.assertEqual(status["graph_revision"], 9064)
        self.assertEqual(status["updated_at"], "2026-09-04T23:25:35+00:00")
        self.assertEqual(status["legacy_cards"]["accepted"], 91)


if __name__ == "__main__":
    unittest.main()
