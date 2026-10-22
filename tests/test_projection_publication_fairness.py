from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lkt.knowledge import KnowledgeStore
from tests import test_projection_lane as fixtures

LANE_KEY = "lexical_projection_last_publication_lane_v1"


class ProjectionPublicationFairnessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "knowledge.sqlite3"
        self.fixture = fixtures.ProjectionLaneTests()
        self.store, self.cards = self.fixture._stores(directory.name)

    def subject(self, word, *, published_at=None, created_at=None, updated_at=None):
        term, _, _ = self.fixture._subject(self.store, word)
        with closing(self.store._connect()) as connection:
            if published_at is not None:
                connection.execute(
                    "UPDATE lexical_projection_repairs SET applied_fingerprint=?, "
                    "last_applied_at=?, status='pending' WHERE term_entity_id=?",
                    ("previous-" + term, published_at, term),
                )
            for column, value in (("created_at", created_at), ("updated_at", updated_at)):
                if value is not None:
                    connection.execute(
                        f"UPDATE lexical_projection_repairs SET {column}=? WHERE term_entity_id=?",
                        (value, term),
                    )
            connection.commit()
        return term

    def release(self, claim):
        # Simulate a replenished queue after this selection without fabricating
        # card publication. Integration/live tests cover actual composition.
        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE lexical_projection_repairs SET status='pending', "
                "lease_token='', lease_expires_at='', compose_job_id=NULL, "
                "bound_fingerprint='' WHERE term_entity_id=?",
                (claim["term_entity_id"],),
            )
            connection.commit()

    def last_lane(self):
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", (LANE_KEY,),
            ).fetchone()
        return row[0] if row else None

    def test_replenished_refreshes_cannot_starve_first_publication(self):
        published = self.subject("existing", published_at="2001-01-01T00:00:00+00:00")
        frontier = self.subject("frontier")
        selected = []
        for _ in range(4):
            claim = self.store.claim_lexical_projection_repair()
            selected.append((claim["term_entity_id"], claim["publication_lane"]))
            self.release(claim)
        self.assertEqual(selected, [
            (frontier, "unpublished"), (published, "published"),
            (frontier, "unpublished"), (published, "published"),
        ])

    def test_rotation_survives_reopening_the_store(self):
        published = self.subject("existing", published_at="2001-01-01T00:00:00+00:00")
        frontier = self.subject("frontier")
        selected = []
        for _ in range(4):
            self.store = KnowledgeStore(self.database, serving=True)
            claim = self.store.claim_lexical_projection_repair()
            selected.append(claim["term_entity_id"])
            self.release(claim)
        self.assertEqual(selected, [frontier, published, frontier, published])

    def test_empty_group_lends_its_turn_and_new_work_is_not_delayed(self):
        published = self.subject("existing", published_at="2001-01-01T00:00:00+00:00")
        first = self.store.claim_lexical_projection_repair()
        self.assertEqual(first["term_entity_id"], published)
        self.assertEqual(self.last_lane(), "published")
        self.release(first)
        frontier = self.subject("frontier")
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], frontier,
        )

    def test_future_cooldown_is_respected_before_borrowing_the_turn(self):
        published = self.subject("existing", published_at="2001-01-01T00:00:00+00:00")
        frontier = self.subject("frontier")
        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE lexical_projection_repairs SET status='cooldown', "
                "next_eligible_at='2999-01-01T00:00:00+00:00' WHERE term_entity_id=?",
                (frontier,),
            )
            connection.commit()
        claim = self.store.claim_lexical_projection_repair()
        self.assertEqual(claim["term_entity_id"], published)
        self.assertEqual(self.last_lane(), "published")

    def test_active_lease_does_not_consume_the_next_group_turn(self):
        published = self.subject("existing", published_at="2001-01-01T00:00:00+00:00")
        self.subject("frontier")
        first = self.store.claim_lexical_projection_repair()
        self.assertEqual(self.last_lane(), "unpublished")
        self.assertIsNone(self.store.claim_lexical_projection_repair())
        self.assertEqual(self.last_lane(), "unpublished")
        self.release(first)
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], published,
        )

    def test_empty_queue_does_not_advance_the_rotation(self):
        self.assertIsNone(self.store.claim_lexical_projection_repair())
        self.assertIsNone(self.last_lane())
        frontier = self.subject("frontier")
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], frontier,
        )

    def test_refresh_age_is_last_publication_not_recent_graph_update(self):
        older = self.subject(
            "antiquity", published_at="2001-01-01T00:00:00+00:00",
            created_at="2000-01-01T00:00:00+00:00", updated_at="2100-01-01T00:00:00+00:00",
        )
        self.subject(
            "recentness", published_at="2002-01-01T00:00:00+00:00",
            created_at="2000-01-01T00:00:00+00:00", updated_at="2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], older,
        )

    def test_frontier_age_is_not_reset_by_repeated_graph_updates(self):
        older = self.subject(
            "antiquity", created_at="2000-01-01T00:00:00+00:00",
            updated_at="2100-01-01T00:00:00+00:00",
        )
        self.subject(
            "recentness", created_at="2001-01-01T00:00:00+00:00",
            updated_at="2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], older,
        )

    def test_rotation_and_lease_roll_back_together_on_receipt_failure(self):
        frontier = self.subject("frontier")
        with closing(self.store._connect()) as connection:
            connection.execute(
                "CREATE TRIGGER fail_lane_receipt BEFORE INSERT ON schema_meta "
                f"WHEN NEW.key='{LANE_KEY}' BEGIN "
                "SELECT RAISE(ABORT, 'lane receipt failed'); END"
            )
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "lane receipt failed"):
            self.store.claim_lexical_projection_repair()
        self.assertIsNone(self.last_lane())
        state = self.store.lexical_projection_repair_state(frontier)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["lease_token"], "")
        with closing(self.store._connect()) as connection:
            connection.execute("DROP TRIGGER fail_lane_receipt")
            connection.commit()
        self.assertEqual(
            self.store.claim_lexical_projection_repair()["term_entity_id"], frontier,
        )
