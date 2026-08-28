from __future__ import annotations

import unittest

from lkt.device import memory_pressure_blocker, pi_power_blocker


class DeviceReadinessTests(unittest.TestCase):
    def test_current_undervoltage_pauses_background_generation(self) -> None:
        self.assertIn("undervolted", pi_power_blocker("throttled=0x50005"))

    def test_historical_flags_do_not_block_after_recovery(self) -> None:
        self.assertEqual(pi_power_blocker("throttled=0x50000"), "")

    def test_healthy_pi_is_ready(self) -> None:
        self.assertEqual(pi_power_blocker("throttled=0x0"), "")

    def test_low_available_memory_pauses_optional_generation(self) -> None:
        self.assertIn(
            "memory",
            memory_pressure_blocker(
                "MemTotal:       8192000 kB\nMemAvailable:    524288 kB\n"
            ),
        )
        self.assertEqual(
            memory_pressure_blocker(
                "MemTotal:       8192000 kB\nMemAvailable:   2097152 kB\n"
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
