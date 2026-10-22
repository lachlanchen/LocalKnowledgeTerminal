"""New cards must not wait forever for old optional language work."""

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import tempfile
import unittest

from lkt.deck import AutonomousLexicalSeeder, AutonomousMorphologySeeder
from lkt.knowledge import KnowledgeStore


class PublicationAdmissionTests(unittest.TestCase):
    def make_morphology(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        knowledge = KnowledgeStore(Path(directory.name) / "knowledge.sqlite3")
        seeder = AutonomousMorphologySeeder(
            {"root": SimpleNamespace(metadata=lambda: {"source_sha256": "root-book"})},
            Mock(), knowledge, model="local-test", modes=("root",), max_in_flight_subjects=2,
        )
        return knowledge, seeder

    def add_job(self, connection, name, kind, status="queued"):
        connection.execute(
            """INSERT INTO preparation_jobs
               (job_id,job_key,job_type,subject_key,prompt_version,source_fingerprint,
                status,created_at,updated_at)
               VALUES (?,?,?,?, 'autonomous-morphology-v1', ?, ?, '2026-01-01','2026-01-01')""",
            (name, name, kind, "term:" + name, "root-book:root:" + name, status),
        )

    def test_many_language_tails_do_not_consume_publication_slots(self):
        knowledge, seeder = self.make_morphology()
        with closing(knowledge._connect()) as connection:
            for number in range(58):
                for kind in ("prepare-pronunciation", "prepare-translation",
                             "prepare-grammar-properties", "compose-word-card"):
                    self.add_job(connection, f"old-{number}-{kind}", kind)
            connection.commit()
        self.assertEqual(seeder._in_flight_count(seeder._in_flight_preparations()), 0)
        with closing(knowledge._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM preparation_jobs WHERE status='queued'"
            ).fetchone()[0], 232)

    def test_active_publication_still_holds_its_slot(self):
        knowledge, seeder = self.make_morphology()
        with closing(knowledge._connect()) as connection:
            self.add_job(connection, "origin", "expand-origin-branches", "running")
            self.add_job(connection, "compose", "compose-origin-card")
            self.add_job(connection, "language", "prepare-translation")
            connection.commit()
        self.assertEqual(seeder._in_flight_count(seeder._in_flight_preparations()), 2)

    def test_failed_publication_releases_its_slot_without_deleting_language_jobs(self):
        knowledge, seeder = self.make_morphology()
        with closing(knowledge._connect()) as connection:
            self.add_job(connection, "origin", "expand-origin-branches", "failed")
            self.add_job(connection, "compose", "compose-origin-card", "failed")
            self.add_job(connection, "language", "prepare-translation")
            connection.commit()
        self.assertEqual(seeder._in_flight_count(seeder._in_flight_preparations()), 0)

    def discovery_seeder(self, jobs, *, revisiting=False):
        seeder = object.__new__(AutonomousLexicalSeeder)
        seeder.prompt_version = "autonomous-lexical-v4"
        seeder._active_discovery_subject_keys = {"subject"}
        seeder.knowledge = Mock()
        seeder.knowledge.unplanned_lexical_discoveries.return_value = []
        seeder.knowledge.active_lexical_discovery_subject_keys.return_value = {"subject"}
        seeder.knowledge.active_lexical_revisit_subject_keys.return_value = (
            {"subject"} if revisiting else set()
        )
        seeder.knowledge.jobs_for_subject.return_value = jobs
        seeder._reconcile_revisit_epochs = Mock()
        seeder._plan_due_projection_repair = Mock(return_value=None)
        seeder.run_discovery_once = Mock(return_value="new-discovery")
        return seeder

    def test_empty_mode_gets_a_turn_even_when_corpus_sizes_differ(self):
        seeder = AutonomousMorphologySeeder(
            {"root": SimpleNamespace(count=lambda: 100),
             "affix": SimpleNamespace(count=lambda: 1000000)},
            Mock(), Mock(), model="local-test",
        )
        seeder._prepared_record_ids = Mock(return_value={"root": {"r"}, "affix": {"a"}})
        seeder._in_flight_preparations = Mock(return_value={
            "root": {"subjects": set(), "entry_ids": set()},
            "affix": {"subjects": {"busy-affix"}, "entry_ids": {"a2"}},
        })
        seeder.run_mode = Mock(return_value=SimpleNamespace(status="queued"))
        seeder.run_once("balanced")
        self.assertEqual(seeder.run_mode.call_args.args[0], "root")

    def test_first_mode_leaves_capacity_for_the_other_book(self):
        from test_morphology import make_morphology_index
        from lkt.store import CardStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeder = AutonomousMorphologySeeder(
                {mode: make_morphology_index(root, mode) for mode in ("root", "affix")},
                CardStore(root / "cards.sqlite3"),
                KnowledgeStore(root / "knowledge.sqlite3"), model="local-test",
            )
            self.assertEqual(seeder.run_once("first").status, "queued")
            self.assertEqual(seeder._in_flight_count(seeder._in_flight_preparations()), 1)
            self.assertEqual(seeder.run_once("second").status, "queued")
            active = seeder._in_flight_preparations()
            self.assertEqual(len(active["root"]["subjects"]), 1)
            self.assertEqual(len(active["affix"]["subjects"]), 1)

    def test_optional_languages_do_not_block_the_next_discovery_batch(self):
        seeder = self.discovery_seeder([
            {"job_type": "prepare-translation", "status": "queued",
             "prompt_version": "autonomous-lexical-v4"},
        ])
        self.assertEqual(seeder.run_bounded_discovery_once("next"), "new-discovery")

    def test_independent_continuous_history_does_not_block_new_discovery(self):
        seeder = self.discovery_seeder([
            {"job_type": "expand-origin-branches", "status": "running",
             "prompt_version": "continuous-origin-v1-revisit-e1"},
        ], revisiting=True)
        self.assertEqual(seeder.run_bounded_discovery_once("next"), "new-discovery")

    def test_own_incomplete_history_still_bounds_discovery(self):
        seeder = self.discovery_seeder([
            {"job_type": "expand-origin-branches", "status": "running",
             "prompt_version": "autonomous-lexical-v4-revisit-e1"},
        ], revisiting=True)
        self.assertEqual(seeder.run_bounded_discovery_once("next").status, "busy")
        seeder.run_discovery_once.assert_not_called()

    def test_legacy_publication_with_a_different_prompt_still_holds_its_slot(self):
        seeder = self.discovery_seeder([
            {"job_type": "prepare-meaning", "status": "queued",
             "prompt_version": "older-source-planner"},
        ])
        self.assertEqual(seeder.run_bounded_discovery_once("next").status, "busy")
        seeder.run_discovery_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
