from __future__ import annotations

import unittest

from lkt.llm import _extract_json


class LlmParsingTests(unittest.TestCase):
    def test_extracts_fenced_json_after_thinking(self) -> None:
        result = _extract_json(
            '<think>private reasoning</think>\n```json\n{"title":"語源","key_points":[]}\n```'
        )
        self.assertEqual(result["title"], "語源")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(ValueError):
            _extract_json("not structured")


if __name__ == "__main__":
    unittest.main()
