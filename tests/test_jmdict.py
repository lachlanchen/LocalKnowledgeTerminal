from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lkt.jmdict import JapaneseReadingIndex, build_jmdict_index


class JapaneseReadingIndexTests(unittest.TestCase):
    def test_builds_exact_form_readings_with_sense_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "jmdict-common.json"
            source.write_text(
                json.dumps(
                    {
                        "version": "3.6.2",
                        "dictDate": "2026-08-24",
                        "words": [
                            {
                                "id": "123",
                                "kanji": [{"common": True, "text": "風俗", "tags": []}],
                                "kana": [
                                    {
                                        "common": True,
                                        "text": "ふうぞく",
                                        "tags": [],
                                        "appliesToKanji": ["*"],
                                    }
                                ],
                                "sense": [
                                    {
                                        "partOfSpeech": ["n"],
                                        "appliesToKanji": ["*"],
                                        "appliesToKana": ["*"],
                                        "gloss": [
                                            {"lang": "eng", "text": "manners and customs"}
                                        ],
                                    }
                                ],
                            },
                            {
                                "id": "456",
                                "kanji": [],
                                "kana": [
                                    {
                                        "common": True,
                                        "text": "あっさり",
                                        "tags": [],
                                        "appliesToKanji": ["*"],
                                    }
                                ],
                                "sense": [
                                    {
                                        "partOfSpeech": ["adv"],
                                        "appliesToKanji": ["*"],
                                        "appliesToKana": ["*"],
                                        "gloss": [{"lang": "eng", "text": "easily"}],
                                    }
                                ],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = root / "jmdict.sqlite3"
            result = build_jmdict_index(
                source, database, release="3.6.2+20260824122934"
            )

            self.assertEqual(result["readings"], 2)
            index = JapaneseReadingIndex(database)
            reading = index.lookup("風俗")[0]
            self.assertEqual(reading["reading"], "ふうぞく")
            self.assertEqual(reading["glosses"], ["manners and customs"])
            self.assertEqual(index.lookup("ふうしょく"), [])
            self.assertEqual(index.lookup("あっさり")[0]["reading"], "あっさり")
            self.assertEqual(index.status()["release"], "3.6.2+20260824122934")

    def test_restricted_reading_is_not_attached_to_another_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "jmdict.json"
            source.write_text(
                json.dumps(
                    {
                        "version": "test",
                        "dictDate": "2026-08-24",
                        "words": [
                            {
                                "id": "1",
                                "kanji": [
                                    {"common": True, "text": "上手", "tags": []},
                                    {"common": False, "text": "上手い", "tags": []},
                                ],
                                "kana": [
                                    {
                                        "common": True,
                                        "text": "じょうず",
                                        "tags": [],
                                        "appliesToKanji": ["上手"],
                                    }
                                ],
                                "sense": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = root / "jmdict.sqlite3"
            build_jmdict_index(source, database)
            index = JapaneseReadingIndex(database)
            self.assertEqual(index.lookup("上手")[0]["reading"], "じょうず")
            self.assertEqual(index.lookup("上手い"), [])


if __name__ == "__main__":
    unittest.main()
