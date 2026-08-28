from __future__ import annotations

import unittest

from lkt.pronunciation import chinese_pinyin


class PronunciationTests(unittest.TestCase):
    def test_full_sentence_pinyin_keeps_tones_and_punctuation(self) -> None:
        self.assertEqual(
            chinese_pinyin("这不是能犹豫的事儿。"),
            "zhè bú shì néng yóu yù de shì ér。",
        )

    def test_non_chinese_text_uses_supplied_fallback(self) -> None:
        self.assertEqual(chinese_pinyin("LKT", "local"), "local")


if __name__ == "__main__":
    unittest.main()
