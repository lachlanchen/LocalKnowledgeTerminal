from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lkt.corpus import CorpusIndex, build_index


RECORDS = [
    {
        "id": "abacus",
        "headword": "abacus",
        "display_headword": "abacus",
        "section": "A",
        "date_label": "14th century",
        "plain_text": "The word abacus came through Latin from Greek abax, a counting board.",
        "related_targets": ["calculate"],
        "source_pages": [12],
    },
    {
        "id": "algorithm",
        "headword": "algorithm",
        "display_headword": "algorithm",
        "section": "A",
        "date_label": "13th century",
        "plain_text": "Algorithm reflects the Latinized name of al-Khwarizmi.",
        "related_targets": ["algebra"],
        "source_pages": [18],
    },
]


def make_index(directory: Path) -> CorpusIndex:
    source = directory / "entries.jsonl"
    source.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in RECORDS),
        encoding="utf-8",
    )
    database = directory / "corpus.sqlite3"
    result = build_index(source, database)
    assert result["entries"] == 2
    return CorpusIndex(database)


class CorpusTests(unittest.TestCase):
    def test_exact_and_lexical_search_keep_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_index(Path(temp))
            exact = index.search("abacus")
            lexical = index.search("counting")
            self.assertEqual(exact[0].headword, "abacus")
            self.assertEqual(exact[0].pages, (12,))
            self.assertEqual(lexical[0].entry_id, "abacus")
            self.assertEqual(index.count(), 2)

    def test_index_records_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            metadata = make_index(Path(temp)).metadata()
            self.assertEqual(metadata["entry_count"], "2")
            self.assertEqual(len(metadata["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
