from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lkt.morphology import MorphologyIndex, build_morphology_index


RECORDS = [
    {
        "id": "p0058-t065-r001",
        "source_page": 58,
        "headword": "SPECT",
        "cells": [
            "Root SPECT, SPEC [SPIC, SPI, SPY]",
            "From Latin spect, spec: to look, to see.",
        ],
    },
    {
        "id": "p0058-t077-r001",
        "source_page": 58,
        "headword": "aspect",
        "cells": [
            "aspect",
            "ad (=to) + spect (=look); appearance or viewpoint",
        ],
    },
    {
        "id": "front-matter",
        "source_page": 4,
        "headword": None,
        "cells": ["Publisher", "Example Press"],
    },
]


def make_morphology_index(directory: Path, kind: str = "root") -> MorphologyIndex:
    source = directory / f"{kind}-entries-polished.jsonl"
    source.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in RECORDS),
        encoding="utf-8",
    )
    database = directory / f"{kind}.sqlite3"
    result = build_morphology_index(
        source,
        database,
        f"test-{kind}-dictionary",
        f"Test {kind.title()} Dictionary",
        kind,
    )
    assert result["records"] == 2
    return MorphologyIndex(database)


class MorphologyTests(unittest.TestCase):
    def test_indexes_real_entry_shape_and_skips_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_morphology_index(Path(temp))
            result = index.search("aspect", 2)
            self.assertEqual(result[0].entry_id, "p0058-t077-r001")
            self.assertEqual(result[0].pages, (58,))
            self.assertEqual(result[0].kind, "morphology-root")
            self.assertIn("ad (=to)", result[0].excerpt)
            self.assertEqual(index.count(), 2)

    def test_fts_finds_a_root_definition_and_records_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_morphology_index(Path(temp))
            result = index.search("look")
            self.assertIn("SPECT", {item.headword for item in result})
            metadata = index.metadata()
            self.assertEqual(metadata["source_title"], "Test Root Dictionary")
            self.assertEqual(len(metadata["source_sha256"]), 64)

    def test_exact_does_not_return_incidental_full_text_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_morphology_index(Path(temp))
            self.assertEqual(index.exact("look"), [])
            self.assertEqual(index.exact("SPECT")[0].headword, "SPECT")


if __name__ == "__main__":
    unittest.main()
