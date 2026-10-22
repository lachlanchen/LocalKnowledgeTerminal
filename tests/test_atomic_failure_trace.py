from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from lkt.atomic import PreparationWorker
from lkt.knowledge import KnowledgeStore
from lkt.llm import AtomicJsonOutputError, LlamaCppClient


class AtomicFailureTraceTests(unittest.TestCase):
    def client(self, replies):
        client = LlamaCppClient("http://local-only.invalid/v1/chat/completions", "local-test-qwen")
        client._runtime_context_tokens = mock.Mock(return_value=3072)
        client._request = mock.Mock(side_effect=[({
            "choices": [{"finish_reason": "length", "message": {"content": reply}}],
            "usage": {"completion_tokens": 160, "prompt_tokens": 500},
        }, 1.0) for reply in replies])
        return client

    def test_parse_failure_retains_both_bounded_replies_and_token_limits(self):
        client = self.client(["x" * 14000, "still not JSON"])
        with self.assertRaises(AtomicJsonOutputError) as caught:
            client.complete_json("Return JSON.", "Fixture input", max_tokens=160)
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.model, "local-test-qwen")
        self.assertEqual(len(error.failures), 2)
        self.assertEqual(len(error.failures[0]["raw"]), 12000)
        self.assertEqual(error.failures[0]["finish_reason"], "length")
        self.assertEqual(error.failures[0]["output_limit"], 160)
        self.assertEqual(error.failures[1]["output_limit"], 256)
        self.assertEqual(client._request.call_count, 2)

    def test_syntax_recovery_still_succeeds_without_a_failure_exception(self):
        client = self.client(['{"meaning":"fixture",}'])
        result = client.complete_json("Return JSON.", "Fixture input")
        self.assertEqual(result["value"], {"meaning": "fixture"})
        self.assertEqual(result["metrics"]["requests"], 1)

    def worker_fixture(self, directory):
        store = KnowledgeStore(Path(directory) / "knowledge.sqlite3")
        job = store.enqueue_job("prepare-meaning", "fixture", max_attempts=1)
        worker = PreparationWorker(store, mock.Mock(), mock.Mock(model_name="local-test-qwen"))
        worker._prepare_meaning = mock.Mock(side_effect=AtomicJsonOutputError(
            "model response did not contain a JSON object", model="local-test-qwen",
            failures=[{"raw": "unfinished fixture", "finish_reason": "length"}],
        ))
        return store, worker, job

    def test_worker_retains_rejected_nonreusable_diagnostic_then_releases_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store, worker, job = self.worker_fixture(directory)
            worker.run_once(("prepare-meaning",))
            with closing(store._connect()) as c:
                row = c.execute("SELECT payload,reusable,validation_state FROM job_artifacts WHERE job_id=? AND stage='model-output-failure'", (job,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["validation_state"], "rejected")
                self.assertFalse(row["reusable"])
                self.assertEqual(json.loads(row["payload"])["failures"][0]["raw"], "unfinished fixture")
                self.assertEqual(c.execute("SELECT status FROM preparation_jobs WHERE job_id=?", (job,)).fetchone()[0], "failed")

    def test_diagnostic_write_failure_does_not_hold_the_worker_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store, worker, job = self.worker_fixture(directory)
            with mock.patch.object(store, "save_job_artifact", side_effect=sqlite3.OperationalError("diagnostic disk write failed")):
                worker.run_once(("prepare-meaning",))
            with closing(store._connect()) as c:
                self.assertEqual(c.execute("SELECT status FROM preparation_jobs WHERE job_id=?", (job,)).fetchone()[0], "failed")


if __name__ == "__main__":
    unittest.main()
