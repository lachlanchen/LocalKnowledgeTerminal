from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lkt.freedict import FreeDictRag, build_freedict_index


SAMPLE_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <text><body>
    <entry>
      <form><orth>Predecessor</orth></form>
      <sense><cit type="trans"><quote>السلف</quote></cit></sense>
    </entry>
    <entry>
      <form><orth>Predecessor</orth></form>
      <sense><cit type="trans"><quote>السابق</quote></cit></sense>
    </entry>
    <entry>
      <form><orth>Successor</orth></form>
      <sense><cit type="trans"><quote>الخلف</quote></cit></sense>
    </entry>
  </body></text>
</TEI>
"""


class FreeDictTests(unittest.TestCase):
    def test_builds_exact_compact_index_and_preserves_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "eng-ara.tei"
            database = root / "eng-ara.sqlite3"
            source.write_text(SAMPLE_TEI, encoding="utf-8")
            result = build_freedict_index(source, database)

            self.assertEqual(result["entries"], 3)
            records = FreeDictRag(database).search("predecessor")
            self.assertEqual(
                [record["translations"]["ar"][0] for record in records],
                ["السلف", "السابق"],
            )
            self.assertTrue(all(record["source_hash"] for record in records))
            self.assertTrue(
                all(record["kind"] == "bilingual-dictionary" for record in records)
            )
            self.assertEqual(FreeDictRag(database).search("predecessors"), [])


if __name__ == "__main__":
    unittest.main()
