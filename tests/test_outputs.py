from __future__ import annotations

import unittest

from lkt.outputs import AudioOutput, EinkOutput, OutputUnavailable


class OutputTests(unittest.TestCase):
    def test_future_adapters_fail_explicitly(self) -> None:
        for adapter in (EinkOutput(), AudioOutput()):
            with self.subTest(adapter=adapter.name):
                with self.assertRaises(OutputUnavailable):
                    adapter.render(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
