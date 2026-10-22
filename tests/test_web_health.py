from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lkt.web import (
    handler_factory,
    health_statistics_refresh_interval,
    live_health_response,
)


class LiveHealthTests(unittest.TestCase):
    def test_slow_statistics_refresh_uses_bounded_adaptive_backoff(self):
        self.assertEqual(health_statistics_refresh_interval(0), 60.0)
        self.assertEqual(health_statistics_refresh_interval(10), 60.0)
        self.assertEqual(health_statistics_refresh_interval(500), 2000.0)
        self.assertEqual(health_statistics_refresh_interval(5000), 3600.0)
        self.assertEqual(
            health_statistics_refresh_interval(500, last_refresh_failed=True),
            60.0,
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "state.sqlite3"
        with closing(self.connect()) as connection:
            connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute("INSERT INTO schema_meta VALUES (?,?)", (
                "occasional_book_growth_v1",
                json.dumps({"last_mode": "question", "status": "prepared", "next_due_at": 2000}),
            ))
            connection.commit()
        self.knowledge = Mock()
        self.knowledge._connect.side_effect = self.connect
        self.knowledge.worker_status.return_value = {
            "ready": True, "generation_ready": True, "status": "running",
            "blocker": "", "age_seconds": 2,
        }
        self.cards = Mock()
        self.cards.accepted_deck_info.side_effect = lambda mode: {
            "total": 29 if mode == "question" else 33, "revision": "new",
        }
        self.snapshot = {
            "status": "ready",
            "worker": {"status": "stopped", "age_seconds": 0},
            "generation": {"ready": False, "blocker": "atomic worker stopped"},
            "autonomous_deck": {"accepted": 60},
            "statistics_cache": {"age_seconds": 120, "stale": True},
        }

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def response(self):
        return live_health_response(self.snapshot, self.knowledge, self.cards)

    def test_worker_restart_immediately_overrides_cached_stopped_state(self):
        result = self.response()
        self.assertEqual(result["worker"]["status"], "running")
        self.assertTrue(result["generation"]["ready"])
        self.assertEqual(result["generation"]["blocker"], "")

    def test_worker_stop_immediately_overrides_cached_ready_state(self):
        self.snapshot["generation"] = {"ready": True, "blocker": ""}
        self.knowledge.worker_status.return_value = {
            "ready": False, "generation_ready": False, "status": "stopped",
            "blocker": "atomic worker stopped", "age_seconds": 90,
        }
        result = self.response()
        self.assertFalse(result["generation"]["ready"])
        self.assertEqual(result["worker"]["age_seconds"], 90)

    def test_heartbeat_age_is_read_again_on_each_response(self):
        first = self.response()
        self.knowledge.worker_status.return_value = {
            **self.knowledge.worker_status.return_value, "age_seconds": 17,
        }
        second = self.response()
        self.assertEqual(first["worker"]["age_seconds"], 2)
        self.assertEqual(second["worker"]["age_seconds"], 17)
        self.assertEqual(self.knowledge.worker_status.call_count, 2)

    def test_cached_statistics_and_source_coverage_are_not_mutated(self):
        before = copy.deepcopy(self.snapshot)
        result = self.response()
        self.assertEqual(self.snapshot, before)
        self.assertEqual(result["autonomous_deck"]["accepted"], 60)
        self.assertTrue(result["statistics_cache"]["stale"])

    def test_visible_deck_totals_and_schedule_are_live_and_separate(self):
        result = self.response()
        self.assertEqual(result["live_decks"]["modes"]["question"]["total"], 29)
        self.assertEqual(len(result["live_decks"]["modes"]), 6)
        self.assertEqual(result["book_growth"]["last_mode"], "question")
        self.assertEqual(result["book_growth"]["next_due_at"], 2000)
        self.knowledge.status.assert_not_called()
        self.cards.accepted_for_modes.assert_not_called()

    def test_source_startup_is_not_hidden_by_running_worker(self):
        self.snapshot["status"] = "starting"
        self.snapshot["generation"] = {"ready": False, "blocker": "Local Qwen is starting"}
        result = self.response()
        self.assertFalse(result["generation"]["ready"])
        self.assertEqual(result["generation"]["blocker"], "Local Qwen is starting")

    def test_unreadable_worker_heartbeat_does_not_reuse_old_ready_flag(self):
        self.knowledge.worker_status.side_effect = sqlite3.OperationalError("locked")
        with self.assertLogs("lkt.web", level="WARNING"):
            result = self.response()
        self.assertFalse(result["generation"]["ready"])
        self.assertEqual(result["worker"]["status"], "unavailable")

    def test_unreadable_counts_do_not_destroy_live_worker_status(self):
        self.cards.accepted_deck_info.side_effect = sqlite3.OperationalError("locked")
        with self.assertLogs("lkt.web", level="WARNING"):
            result = self.response()
        self.assertFalse(result["live_decks"]["ready"])
        self.assertEqual(result["worker"]["status"], "running")

    def test_missing_book_state_is_not_reported_as_a_running_schedule(self):
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM schema_meta")
            connection.commit()
        result = self.response()
        self.assertFalse(result["book_growth"]["available"])
        self.assertEqual(result["book_growth"]["status"], "not-observed")

    def test_bad_book_state_does_not_break_health_response(self):
        with closing(self.connect()) as connection:
            connection.execute("UPDATE schema_meta SET value='not json'")
            connection.commit()
        with self.assertLogs("lkt.web", level="WARNING"):
            result = self.response()
        self.assertEqual(result["book_growth"]["status"], "unavailable")
        self.assertTrue(result["generation"]["ready"])

    def test_health_route_uses_live_overlay_while_statistics_refresh_is_pending(self):
        with (
            patch("lkt.web.KnowledgeStore", return_value=self.knowledge),
            patch("lkt.web.Thread") as thread,
        ):
            handler_type = handler_factory(
                SimpleNamespace(knowledge_db=self.database),
                SimpleNamespace(store=self.cards),
                SimpleNamespace(model_name="local-qwen"),
            )
            handler = object.__new__(handler_type)
            handler.path = "/api/health"
            handler._json = Mock()
            handler.do_GET()
            result = handler._json.call_args.args[0]
        self.assertEqual(result["worker"]["status"], "running")
        self.assertTrue(result["statistics_cache"]["refreshing"])
        self.assertIsNone(result["statistics_cache"]["age_seconds"])
        self.assertEqual(result["live_decks"]["modes"]["question"]["total"], 29)
        thread.assert_called_once()

    def test_random_card_header_uses_the_generation_that_hydrated_its_body(self):
        self.knowledge.projection_generation.side_effect = ("generation-1", "generation-2")
        rendered = {"card_id": "card-1", "mode": "word", "title": "Word"}
        with (
            patch("lkt.web.KnowledgeStore", return_value=self.knowledge),
            patch("lkt.web.Thread"),
            patch("lkt.web.acquire_random_renderable_card", return_value=rendered),
        ):
            handler_type = handler_factory(
                SimpleNamespace(knowledge_db=self.database),
                SimpleNamespace(store=self.cards),
                SimpleNamespace(model_name="local-qwen"),
            )
            handler = object.__new__(handler_type)
            handler.path = "/api/cards/random?mode=word"
            handler._json = Mock()
            handler.do_GET()

        _, keyword_arguments = handler._json.call_args
        self.assertEqual(
            keyword_arguments["extra_headers"]["X-Knowledge-Generation"],
            "generation-1",
        )
        self.assertEqual(self.knowledge.projection_generation.call_count, 1)

    def test_rotation_hydration_rejects_a_live_sparse_graph(self):
        self.knowledge.projection_generation.return_value = "generation-1"
        self.knowledge.content_for_card.return_value = None
        stored = {"card_id": "card-1", "mode": "word", "title": "Word"}
        self.cards.get_accepted.return_value = stored
        with (
            patch("lkt.web.KnowledgeStore", return_value=self.knowledge),
            patch("lkt.web.Thread"),
            patch("lkt.web.live_card_rotation_eligible", return_value=False),
        ):
            handler_type = handler_factory(
                SimpleNamespace(knowledge_db=self.database),
                SimpleNamespace(store=self.cards),
                SimpleNamespace(model_name="local-qwen"),
            )
            handler = object.__new__(handler_type)
            handler.path = "/api/cards/card-1?rotation=1"
            handler._json = Mock()
            handler.do_GET()

        self.assertEqual(handler._json.call_args.args[1], 409)


if __name__ == "__main__":
    unittest.main()
