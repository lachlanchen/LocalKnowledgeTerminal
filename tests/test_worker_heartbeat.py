from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Condition, Event, Thread, get_ident

from lkt.cli import DurableWorkerHeartbeat, run_atomic_watch
from lkt.knowledge import KnowledgeStore


class WorkerHeartbeatTests(unittest.TestCase):
    def test_heartbeat_does_not_invalidate_knowledge_projection_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            knowledge = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
            before = knowledge.projection_generation()
            knowledge.record_worker_heartbeat("")
            self.assertEqual(knowledge.projection_generation(), before)

            knowledge.upsert_term("en", "cacheable")
            self.assertNotEqual(knowledge.projection_generation(), before)

    def test_artifacts_and_citations_invalidate_knowledge_projection_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            knowledge = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
            term_id = knowledge.upsert_term("en", "projectable")
            job_id = knowledge.enqueue_job(
                "prepare-translation",
                subject_key=f"term:{term_id}",
                subject_entity_id=term_id,
                language="ja",
                prompt_version="projection-test-v1",
            )
            before_artifact = knowledge.projection_generation()
            knowledge.save_job_artifact(
                job_id,
                "project-language-atoms",
                {"language": "ja", "text": "投影"},
                language="ja",
                validation_state="accepted",
            )
            self.assertNotEqual(
                knowledge.projection_generation(), before_artifact
            )

            before_evidence = knowledge.projection_generation()
            evidence_id = knowledge.add_evidence(
                "fixture", "entry-1", excerpt="A cited definition."
            )
            knowledge.link_evidence(term_id, evidence_id, claim="definition")
            self.assertNotEqual(
                knowledge.projection_generation(), before_evidence
            )

    def test_heartbeat_remains_available_during_graph_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "knowledge.sqlite3"
            knowledge = KnowledgeStore(database)
            completed = Event()
            failures = []
            writer = sqlite3.connect(database)
            writer.execute("BEGIN IMMEDIATE")

            def write_heartbeat():
                try:
                    knowledge.record_worker_heartbeat("")
                    self.assertTrue(knowledge.worker_status()["generation_ready"])
                except Exception as error:
                    failures.append(error)
                finally:
                    completed.set()

            thread = Thread(target=write_heartbeat, daemon=True)
            thread.start()
            try:
                self.assertTrue(completed.wait(3), "heartbeat waited for the graph writer")
                self.assertEqual(failures, [])
            finally:
                writer.rollback()
                writer.close()
                thread.join(5)

    def test_sidecar_staleness_does_not_fall_back_to_fresh_legacy_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "knowledge.sqlite3"
            knowledge = KnowledgeStore(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO worker_heartbeat VALUES (?, ?, ?, datetime('now'))",
                    ("atomic", "running", ""),
                )
                connection.commit()
            self.assertTrue(knowledge.worker_status()["ready"])
            knowledge.record_worker_heartbeat("")
            with closing(
                sqlite3.connect(str(database) + ".heartbeat.sqlite3")
            ) as connection:
                connection.execute(
                    "UPDATE worker_heartbeat SET updated_at = datetime('now', '-1 hour')"
                )
                connection.commit()
            self.assertFalse(knowledge.worker_status()["ready"])
            self.assertEqual(
                knowledge.worker_status()["blocker"], "atomic worker heartbeat is stale"
            )

    def test_interval_stays_below_default_health_timeout(self) -> None:
        for interval in (1, 5, 15, 30, 300):
            with self.subTest(interval=interval):
                heartbeat = DurableWorkerHeartbeat(None, interval_seconds=interval)
                self.assertGreaterEqual(heartbeat.interval_seconds, 1)
                self.assertLessEqual(heartbeat.interval_seconds, 5)

    def test_durable_heartbeat_advances_during_blocking_work_and_stops(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                knowledge = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
                condition = Condition()
                writes: list[tuple[str, str, int]] = []
                stop = Event()
                testcase = self

                class RecordingStore:
                    def record_worker_heartbeat(self, blocker, *, status):
                        knowledge.record_worker_heartbeat(blocker, status=status)
                        state = knowledge.worker_status()
                        with condition:
                            writes.append((status, state["updated_at"], get_ident()))
                            condition.notify_all()

                class BlockingModelWorker:
                    def run_once(self):
                        with condition:
                            start = len(writes)
                            testcase.assertTrue(
                                condition.wait_for(lambda: len(writes) >= start + 2, 5),
                                "heartbeat stopped while the model was blocked",
                            )
                            updates = writes[start:]
                        testcase.assertTrue(knowledge.worker_status()["generation_ready"])
                        testcase.assertGreater(len({row[1] for row in updates}), 1)
                        testcase.assertTrue(all(row[2] != get_ident() for row in updates))
                        if fail:
                            raise RuntimeError("model failure")
                        stop.set()
                        return {"status": "complete"}

                heartbeat = DurableWorkerHeartbeat(RecordingStore(), interval_seconds=1)
                heartbeat.start()
                try:
                    def run():
                        return run_atomic_watch(
                            BlockingModelWorker(), stop,
                            idle_seconds=0, job_delay=0, emit=lambda result: None,
                            preparation_blocker=lambda: "",
                            model_free_blocker=lambda: "",
                            heartbeat=heartbeat.update,
                        )

                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "model failure"):
                            run()
                    else:
                        self.assertEqual(run(), 0)
                finally:
                    heartbeat.close()
                self.assertFalse(heartbeat.thread.is_alive())
                self.assertEqual(writes[-1][0], "stopped")
                self.assertEqual(knowledge.worker_status()["status"], "stopped")

    def test_shutdown_waits_for_delayed_write_before_stopped_status(self) -> None:
        entered = Event()
        release = Event()
        closed = Event()
        writes: list[str] = []

        class DelayedStore:
            def record_worker_heartbeat(self, blocker, *, status):
                if status == "running":
                    entered.set()
                    release.wait(5)
                writes.append(status)

        heartbeat = DurableWorkerHeartbeat(DelayedStore(), interval_seconds=1)
        heartbeat.start()
        self.assertTrue(entered.wait(2))

        def close():
            heartbeat.close()
            closed.set()

        closer = Thread(target=close, daemon=True)
        closer.start()
        try:
            # The previous timed join returned after two seconds while the
            # running write was still blocked, allowing stopped -> running.
            self.assertFalse(closed.wait(2.2))
            self.assertEqual(writes, [])
        finally:
            release.set()
            closer.join(3)
        self.assertTrue(closed.is_set())
        self.assertFalse(heartbeat.thread.is_alive())
        self.assertEqual(writes, ["running", "stopped"])

    def test_transient_database_error_does_not_end_heartbeat(self) -> None:
        succeeded = Event()
        writes: list[str] = []

        class ContendedStore:
            def record_worker_heartbeat(self, blocker, *, status):
                writes.append(status)
                if len(writes) == 1:
                    raise sqlite3.OperationalError("database is locked")
                succeeded.set()

        heartbeat = DurableWorkerHeartbeat(ContendedStore(), interval_seconds=1)
        heartbeat.start()
        try:
            self.assertTrue(succeeded.wait(3))
        finally:
            heartbeat.close()
        self.assertEqual(writes, ["running", "running", "stopped"])
        self.assertFalse(heartbeat.thread.is_alive())
