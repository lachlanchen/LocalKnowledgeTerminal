from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lkt.pronunciation import (
    EspeakPronouncer,
    chinese_pinyin,
    chinese_ruby_tokens,
    is_arabic_script_text,
)


class PronunciationTests(unittest.TestCase):
    def test_arabic_script_gate_rejects_latin_leakage(self) -> None:
        self.assertTrue(is_arabic_script_text("اختراق مهم"))
        self.assertFalse(is_arabic_script_text("انBREAKTHROUGH"))
        self.assertFalse(is_arabic_script_text("breakthrough"))

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

    def test_ruby_tokens_use_supplied_compound_reading_without_pypinyin(self) -> None:
        with patch.dict("sys.modules", {"pypinyin": None}):
            self.assertEqual(
                chinese_ruby_tokens("算盘", "suànpán"),
                [{"t": "算盘", "r": "suànpán"}],
            )

    @patch("lkt.pronunciation.subprocess.run")
    def test_espeak_adapter_uses_fixed_voice_and_returns_provenance(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(stdout="\u026ansp\u02c8\u025bk\u0283\u0259n\n"),
            SimpleNamespace(stdout="eSpeak NG text-to-speech: 1.51\n"),
        ]
        result = EspeakPronouncer("/usr/bin/espeak-ng").pronounce(
            "inspection", "en"
        )
        self.assertEqual(result["reading"], "\u026ansp\u02c8\u025bk\u0283\u0259n")
        self.assertEqual(result["dialect"], "en-us")
        self.assertEqual(result["source"]["engine"], "espeak-ng")
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "/usr/bin/espeak-ng",
                "-q",
                "--ipa=3",
                "-v",
                "en-us",
                "inspection",
            ],
        )

    @patch("lkt.pronunciation.subprocess.run")
    def test_arabic_adapter_removes_partial_diacritics_before_phonemizing(
        self, run
    ) -> None:
        run.side_effect = [
            SimpleNamespace(stdout="mu\u0295\u02c8a\u02d0jan\u02cca\n"),
            SimpleNamespace(stdout="eSpeak NG text-to-speech: 1.51\n"),
        ]
        result = EspeakPronouncer("/usr/bin/espeak-ng").pronounce(
            "\u0645\u064f\u0639\u0627\u064a\u0646\u0629", "ar"
        )
        self.assertEqual(
            run.call_args_list[0].args[0][-1], "\u0645\u0639\u0627\u064a\u0646\u0629"
        )
        self.assertEqual(
            result["source"]["input_normalization"],
            "stripped-partial-diacritics",
        )


if __name__ == "__main__":
    unittest.main()
