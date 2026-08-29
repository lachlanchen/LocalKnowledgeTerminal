from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path

from lkt.cli import run_atomic_watch


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

    def run_once(self) -> object | None:
        self.calls += 1
        return self.results.pop(0) if self.results else None


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

    def test_kiosk_autostart_is_bare_duplicate_safe_and_health_gated(self) -> None:
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "scripts" / "open_kiosk.sh").read_text(encoding="utf-8")
        desktop = (root / "desktop" / "lkt-kiosk.desktop").read_text(
            encoding="utf-8"
        )
        installer = (root / "scripts" / "install_pi.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:8090/?display", launcher)
        self.assertIn("http://127.0.0.1:8090/api/health", launcher)
        self.assertLess(launcher.index("pgrep -f"), launcher.index('exec "$BROWSER"'))
        self.assertIn('--user-data-dir="$PROFILE_DIR"', launcher)
        self.assertIn("--remote-debugging-address=127.0.0.1", launcher)
        self.assertNotIn("?mode=", launcher)
        self.assertIn("Exec=/usr/local/bin/lkt-open-kiosk", desktop)
        self.assertIn("TryExec=/usr/local/bin/lkt-open-kiosk", desktop)
        self.assertIn("X-GNOME-Autostart-enabled=true", desktop)
        self.assertIn("/usr/local/bin/lkt-open-kiosk", installer)
        self.assertIn("desktop/lkt-kiosk.desktop", installer)
        self.assertIn(".config/autostart/lkt-kiosk.desktop", installer)

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
        )
        self.assertEqual(status, 0)
        self.assertEqual(emitted, ["deck-card-1"])

    def test_watch_does_not_drain_queue_during_memory_pressure(self) -> None:
        worker = _Worker([SimpleNamespace(job_id="job-1", status="complete")])
        status = run_atomic_watch(
            worker,
            _StopAfterWaits(1),
            idle_seconds=2,
            job_delay=1,
            emit=lambda _result: self.fail("blocked work must not be emitted"),
            preparation_blocker=lambda: "only 80 MiB memory is available",
        )
        self.assertEqual(status, 0)
        self.assertEqual(worker.calls, 0)


if __name__ == "__main__":
    unittest.main()
