from __future__ import annotations

import unittest
from unittest.mock import patch

from lkt.device import (
    background_preparation_blocker,
    is_memory_pressure_blocker,
    memory_pressure_blocker,
    pi_power_blocker,
)


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

    def test_resident_four_b_model_keeps_a_one_gib_interactive_reserve(self) -> None:
        self.assertEqual(
            memory_pressure_blocker(
                "MemTotal:       8245248 kB\nMemAvailable:   1493072 kB\n"
            ),
            "",
        )

    def test_model_free_floor_is_lower_but_still_bounded(self) -> None:
        meminfo = "MemTotal:       8245248 kB\nMemAvailable:    819200 kB\n"
        self.assertIn("memory", memory_pressure_blocker(meminfo))
        self.assertEqual(
            memory_pressure_blocker(meminfo, min_available_mib=768.0),
            "",
        )

    def test_only_the_memory_guard_is_safe_for_model_free_planning(self) -> None:
        self.assertTrue(
            is_memory_pressure_blocker(
                "background preparation paused: only 900 MiB memory is available"
            )
        )
        self.assertFalse(
            is_memory_pressure_blocker(
                "background preparation paused: device temperature is 80.0 C"
            )
        )

    def test_memory_is_checked_when_thermal_telemetry_is_unavailable(self) -> None:
        with (
            patch("lkt.device.shutil.which", return_value=None),
            patch(
                "lkt.device.Path.read_text",
                side_effect=[
                    OSError("no thermal zone"),
                    "MemTotal:       8192000 kB\nMemAvailable:    524288 kB\n",
                ],
            ),
        ):
            self.assertIn("memory", background_preparation_blocker())


if __name__ == "__main__":
    unittest.main()
