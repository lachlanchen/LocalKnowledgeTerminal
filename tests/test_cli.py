from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lkt.atomic import MODEL_FREE_ATOMIC_JOBS
from lkt.cli import command_generate, run_atomic_watch


class _StopAfterWaits:
    def __init__(self, waits: int):
        self.remaining = waits

    def is_set(self) -> bool:
        return self.remaining <= 0

    def wait(self, _seconds: float) -> bool:
        self.remaining -= 1
        return self.is_set()


class _Worker:
    def __init__(self, results: list[object | None]):
        self.results = list(results)
        self.calls = 0
        self.job_type_filters: list[tuple[str, ...] | None] = []

    def run_once(self, job_types: tuple[str, ...] | None = None) -> object | None:
        self.calls += 1
        self.job_type_filters.append(job_types)
        return self.results.pop(0) if self.results else None


class GenerateCommandTests(unittest.TestCase):
    def test_lexical_modes_queue_one_shared_atomic_plan_without_card_service(self) -> None:
        settings = SimpleNamespace(
            knowledge_db="knowledge.sqlite3",
            llm_model="local-qwen-test",
        )
        knowledge = Mock()
        knowledge.jobs_for_subject.return_value = [
            {"job_id": "retrieve-1", "status": "queued"}
        ]
        planner = Mock()
        planner.plan_word.return_value = SimpleNamespace(
            subject_entity_id="term-1",
            subject_key="term:term-1",
            jobs={"retrieve-evidence": "retrieve-1"},
        )
        with (
            patch("lkt.cli._settings", return_value=settings),
            patch("lkt.cli.KnowledgeStore", return_value=knowledge),
            patch("lkt.cli.PreparationPlanner", return_value=planner),
            patch("lkt.cli._service") as service,
        ):
            for mode in ("knowledge", "word", "root", "affix"):
                output = StringIO()
                with self.subTest(mode=mode), redirect_stdout(output):
                    self.assertEqual(
                        command_generate(SimpleNamespace(query="inspection", mode=mode)),
                        0,
                    )
                    payload = json.loads(output.getvalue())
                    self.assertEqual(payload["status"], "queued")
                    self.assertEqual(payload["requested_mode"], mode)
                    self.assertEqual(payload["subject_key"], "term:term-1")

        self.assertEqual(planner.plan_word.call_count, 4)
        service.assert_not_called()

    def test_reviewed_mode_keeps_service_then_acquires_and_enriches_card(self) -> None:
        settings = SimpleNamespace(
            knowledge_db="knowledge.sqlite3",
            llm_model="local-qwen-test",
        )
        card_payload = {"card_id": "answer-card-1", "mode": "answer"}
        card = Mock(card_id="answer-card-1")
        card.to_dict.return_value = card_payload
        service = Mock()
        service.create.return_value = card
        knowledge = Mock()
        planner = Mock()
        with (
            patch("lkt.cli._settings", return_value=settings),
            patch("lkt.cli._service", return_value=service),
            patch("lkt.cli.KnowledgeStore", return_value=knowledge),
            patch("lkt.cli.PreparationPlanner", return_value=planner),
        ):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    command_generate(
                        SimpleNamespace(query="Look more closely.", mode="answer")
                    ),
                    0,
                )

        self.assertEqual(json.loads(output.getvalue()), card_payload)
        service.create.assert_called_once_with("Look more closely.", "answer")
        knowledge.acquire_card_book_card.assert_called_once_with(card_payload)
        planner.plan_card_enrichment.assert_called_once_with("answer-card-1")


class AtomicWatchTests(unittest.TestCase):
    def test_watch_emits_completed_work_and_waits_when_idle(self) -> None:
        emitted: list[str] = []
        worker = _Worker(
            [SimpleNamespace(job_id="job-1", status="complete"), None]
        )
        status = run_atomic_watch(
            worker,
            _StopAfterWaits(2),
            idle_seconds=2,
            job_delay=1,
            emit=lambda result: emitted.append(result.job_id),
            preparation_blocker=lambda: "",
        )
        self.assertEqual(status, 0)
        self.assertEqual(emitted, ["job-1"])

    def test_system_worker_is_single_job_low_priority_and_recoverable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        unit = (root / "systemd" / "lkt-worker.service").read_text(encoding="utf-8")
        self.assertIn("work-atomic --watch --recover-running", unit)
        self.assertIn("--job-delay 1", unit)
        self.assertIn("--autoprepare-book-deck", unit)
        self.assertIn("--autoprepare-lexical-deck", unit)
        self.assertIn("--autoprepare-modes answer question", unit)
        self.assertIn("Nice=10", unit)
        self.assertIn("CPUWeight=25", unit)

    def test_model_service_preserves_an_interactive_memory_reserve(self) -> None:
        root = Path(__file__).resolve().parents[1]
        unit = (root / "systemd" / "lkt-llm.service").read_text(encoding="utf-8")
        self.assertIn("MemoryHigh=5G", unit)
        self.assertIn("MemoryMax=6G", unit)
        self.assertIn("MemorySwapMax=128M", unit)
        self.assertIn("OOMPolicy=stop", unit)
        self.assertIn("Environment=LKT_MODEL_CONTEXT=3072", unit)
        self.assertIn("Environment=LKT_BATCH_SIZE=128", unit)
        self.assertIn("--cache-ram 256", unit)
        self.assertIn("--ctx-checkpoints 4", unit)
        self.assertIn("--sleep-idle-seconds 600", unit)

    def test_knowledge_runtime_pins_the_full_jmdict_index(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_jmdict.sh").read_text(
            encoding="utf-8"
        )
        runtime = (root / "scripts" / "install_knowledge_runtime.sh").read_text(
            encoding="utf-8"
        )
        pi_installer = (root / "scripts" / "install_pi.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('JMDICT_RELEASE="3.6.2+20260824122934"', installer)
        self.assertIn(
            "d9b74539bce7df82491a57ad96a0634a988129db6ca4a362f7221bc5e736871f",
            installer,
        )
        self.assertIn("jmdict-eng-3.6.2+20260824122934.json.tgz", installer)
        self.assertNotIn("jmdict-eng-common", installer)
        self.assertIn("scripts/install_jmdict.sh", runtime)
        self.assertIn("LKT_JMDICT_DB=", pi_installer)

    def test_display_autostart_is_bare_duplicate_safe_and_health_gated(self) -> None:
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "scripts" / "open_kiosk.sh").read_text(encoding="utf-8")
        desktop = (root / "desktop" / "lkt-kiosk.desktop").read_text(
            encoding="utf-8"
        )
        installer = (root / "scripts" / "install_pi.sh").read_text(
            encoding="utf-8"
        )
        service_installer = (root / "scripts" / "install_services.sh").read_text(
            encoding="utf-8"
        )
        updater = (root / "scripts" / "update_pi.sh").read_text(encoding="utf-8")
        tmux_updater = (root / "scripts" / "update_pi_tmux.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:8090/?display", launcher)
        self.assertIn("http://127.0.0.1:8090/api/health", launcher)
        self.assertLess(launcher.index("pgrep -f"), launcher.index('exec "$BROWSER"'))
        self.assertIn('--user-data-dir="$PROFILE_DIR"', launcher)
        self.assertIn("--start-fullscreen", launcher)
        self.assertIn('--app="$LKT_KIOSK_URL"', launcher)
        self.assertIn("--disable-extensions", launcher)
        self.assertNotIn("--kiosk", launcher)
        self.assertIn("--remote-debugging-address=127.0.0.1", launcher)
        self.assertNotIn("?mode=", launcher)
        self.assertIn("Exec=/usr/local/bin/lkt-open-kiosk", desktop)
        self.assertIn("TryExec=/usr/local/bin/lkt-open-kiosk", desktop)
        self.assertIn("X-GNOME-Autostart-enabled=true", desktop)
        self.assertIn("scripts/install_services.sh", installer)
        for unit in ("lkt-llm.service", "lkt-web.service", "lkt-worker.service"):
            self.assertIn(unit, service_installer)
        self.assertIn("/usr/local/bin/lkt-open-kiosk", service_installer)
        self.assertIn("desktop/lkt-kiosk.desktop", service_installer)
        self.assertIn(".config/autostart/lkt-kiosk.desktop", service_installer)
        self.assertIn("systemctl enable", service_installer)
        self.assertIn("http://127.0.0.1:8081/health", service_installer)
        self.assertIn("http://127.0.0.1:8090/api/health", service_installer)
        self.assertIn("scripts/install_services.sh", updater)
        self.assertIn("--restart", updater)
        self.assertIn("tmux new-session", tmux_updater)
        self.assertIn("remain-on-exit", tmux_updater)

    def test_watch_runs_bounded_idle_action(self) -> None:
        emitted: list[str] = []
        status = run_atomic_watch(
            _Worker([None]),
            _StopAfterWaits(1),
            idle_seconds=2,
            job_delay=1,
            emit=lambda result: emitted.append(result.job_id),
            idle_action=lambda: SimpleNamespace(job_id="deck-card-1"),
            idle_action_interval=120,
            preparation_blocker=lambda: "",
        )
        self.assertEqual(status, 0)
        self.assertEqual(emitted, ["deck-card-1"])

    def test_watch_drains_atomic_queue_before_periodic_balancer(self) -> None:
        emitted: list[str] = []
        worker = _Worker(
            [
                SimpleNamespace(job_id="atomic-job-1", status="complete"),
                SimpleNamespace(job_id="atomic-job-2", status="complete"),
                None,
            ]
        )
        status = run_atomic_watch(
            worker,
            _StopAfterWaits(3),
            idle_seconds=2,
            job_delay=1,
            emit=lambda result: emitted.append(result.job_id),
            periodic_action=lambda: SimpleNamespace(job_id="balanced-seed"),
            periodic_action_interval=120,
            preparation_blocker=lambda: "",
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            emitted, ["atomic-job-1", "atomic-job-2", "balanced-seed"]
        )
        self.assertEqual(worker.calls, 3)

    def test_watch_does_not_drain_queue_during_memory_pressure(self) -> None:
        worker = _Worker([SimpleNamespace(job_id="job-1", status="complete")])
        status = run_atomic_watch(
            worker,
            _StopAfterWaits(1),
            idle_seconds=2,
            job_delay=1,
            emit=lambda _result: self.fail("blocked work must not be emitted"),
            preparation_blocker=lambda: "only 80 MiB memory is available",
            model_free_blocker=lambda: "only 80 MiB memory is available",
        )
        self.assertEqual(status, 0)
        self.assertEqual(worker.calls, 0)

    def test_watch_only_drains_model_free_composition_below_llm_floor(self) -> None:
        emitted: list[str] = []
        worker = _Worker(
            [
                SimpleNamespace(
                    job_id="compose-1",
                    job_type="compose-word-card",
                    status="complete",
                )
            ]
        )
        status = run_atomic_watch(
            worker,
            _StopAfterWaits(1),
            idle_seconds=2,
            job_delay=1,
            emit=lambda result: emitted.append(result.job_id),
            preparation_blocker=lambda: "only 800 MiB memory is available",
            model_free_blocker=lambda: "",
        )
        self.assertEqual(status, 0)
        self.assertEqual(emitted, ["compose-1"])
        self.assertEqual(worker.job_type_filters, [MODEL_FREE_ATOMIC_JOBS])
        self.assertEqual(
            set(MODEL_FREE_ATOMIC_JOBS),
            {"compose-word-card", "compose-origin-card"},
        )
        self.assertNotIn("prepare-translation", MODEL_FREE_ATOMIC_JOBS)
        self.assertNotIn("prepare-pronunciation", MODEL_FREE_ATOMIC_JOBS)


if __name__ == "__main__":
    unittest.main()
