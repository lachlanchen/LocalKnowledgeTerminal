from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SampleFitReportTests(unittest.TestCase):
    def test_report_total_is_grounded_in_the_corpus_ledger(self) -> None:
        corpus = (ROOT / "docs" / "corpora.md").read_text(encoding="utf-8")
        private_table = corpus.split("## Compact public correction sources", 1)[0]
        counts = [
            int(value.replace(",", ""))
            for value in re.findall(r"\| ([\d,]+) \|", private_table)
        ]
        self.assertEqual(counts, [6994, 318, 291, 4018, 5179])

        report = (ROOT / "docs" / "sample-fit-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"**{sum(counts):,} structured records**", report)
        self.assertNotIn("19,119 structured records", report)

    def test_report_preserves_offer_and_evidence_boundaries(self) -> None:
        report = (ROOT / "docs" / "sample-fit-report.md").read_text(
            encoding="utf-8"
        ).casefold()
        self.assertIn("not a customer result", report)
        self.assertIn("not a customer result, testimonial, benchmark, sale", report)
        self.assertIn("custom ocr", report)
        self.assertIn("production deployment", report)
        self.assertIn("stores and sends nothing automatically", report)
        self.assertTrue((ROOT / "docs" / "assets" / "word-card.png").is_file())
        self.assertTrue((ROOT / "docs" / "assets" / "word-origin.png").is_file())


if __name__ == "__main__":
    unittest.main()
