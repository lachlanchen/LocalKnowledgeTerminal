from __future__ import annotations

import unittest

from lkt.pronunciation import chinese_pinyin, chinese_ruby_tokens


class PronunciationTests(unittest.TestCase):
    def test_full_sentence_pinyin_keeps_tones_and_punctuation(self) -> None:
        self.assertEqual(
            chinese_pinyin("这不是能犹豫的事儿。"),
            "zhè bú shì néng yóu yù de shì ér。",
        )

    def test_non_chinese_text_uses_supplied_fallback(self) -> None:
        self.assertEqual(chinese_pinyin("LKT", "local"), "local")

    def test_ruby_tokens_align_pinyin_with_han_characters(self) -> None:
        self.assertEqual(
            chinese_ruby_tokens("中国。"),
            [
                {"t": "中", "r": "zhōng"},
                {"t": "国", "r": "guó"},
                {"t": "。"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
