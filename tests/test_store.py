from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lkt.store import CardStore


class StoreTests(unittest.TestCase):
    def test_raw_model_observations_are_persistent_and_marked_uncited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "knowledge.sqlite3"
            store = CardStore(database)
            saved = store.save_observation(
                "Explain RAG",
                "Retrieval-augmented generation uses retrieved context.",
                "test-qwen",
                {"elapsed_seconds": 2.0, "completion_tokens": 8},
            )
            restored = CardStore(database).recent_observations(1)[0]
            self.assertEqual(restored["observation_id"], saved["observation_id"])
            self.assertEqual(restored["prompt"], "Explain RAG")
            self.assertFalse(restored["grounded"])
            self.assertEqual(restored["metrics"]["completion_tokens"], 8)


if __name__ == "__main__":
    unittest.main()
