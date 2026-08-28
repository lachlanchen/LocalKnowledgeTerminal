from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lkt.card_books import CardBookIndex, build_card_book_index


def record(
    item_id: str,
    kind: str,
    ordinal: int,
    en: str,
    ja: str,
    zh: str,
    *,
    page: int | None = None,
    locator: str = "",
) -> dict[str, object]:
    evidence: dict[str, object] = {}
    if page is not None:
        evidence["pdf_page"] = page
    if locator:
        evidence["epub_member"] = locator
    return {
        "id": item_id,
        "kind": kind,
        "ordinal": ordinal,
        "source": {"language": "zh" if kind == "answer" else "en", "primary": zh if kind == "answer" else en, "follow_ups": []},
        "languages": {
            "en": {"primary": en, "follow_ups": []},
            "ja": {
                "primary": ja,
                "follow_ups": [],
                "tokens": {"primary": [{"t": "心配", "r": "しんぱい"}, {"t": "しないで"}]},
            },
            "zh": {"primary": zh, "follow_ups": []},
        },
        "evidence": evidence,
        "source_hash": item_id * 3,
    }


def make_card_book(
    directory: Path, kind: str, records: list[dict[str, object]]
) -> CardBookIndex:
    source = directory / f"{kind}.jsonl"
    source.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records),
        encoding="utf-8",
    )
    database = directory / f"{kind}.sqlite3"
    result = build_card_book_index(
        source,
        database,
        f"test-{kind}-book",
        f"Test {kind.title()} Book",
        kind,
    )
    assert result["items"] == len(records)
    return CardBookIndex(database)


class CardBookTests(unittest.TestCase):
    def test_answer_draw_is_reproducible_and_keeps_reviewed_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = make_card_book(
                root,
                "answer",
                [
                    record("answer-001", "answer", 1, "Begin now", "今始めて", "现在开始", page=7),
                    record("answer-002", "answer", 2, "Wait", "待って", "等待", page=8),
                ],
            )
            first = index.draw("Should I begin?")
            second = index.draw("Should I begin?")
            self.assertEqual(first.entry_id, second.entry_id)
            self.assertEqual(first.source_title, "Test Answer Book")
            self.assertIn(first.pages[0], (7, 8))
            self.assertTrue(first.translations["ja"]["ruby_tokens"])
            self.assertNotIn("tokens", first.translations["ja"])
            self.assertEqual(index.metadata()["item_count"], "2")

    def test_question_searches_multilingual_text_and_keeps_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_card_book(
                Path(temp),
                "question",
                [
                    record(
                        "question-001",
                        "question",
                        1,
                        "How does technology shape us?",
                        "技術は私たちをどう形作る？",
                        "科技如何塑造我们？",
                        locator="OEBPS/questions_split_000.xhtml",
                    ),
                    record(
                        "question-002",
                        "question",
                        2,
                        "What makes a friendship last?",
                        "友情はなぜ続く？",
                        "什么让友谊长久？",
                        locator="OEBPS/questions_split_001.xhtml",
                    ),
                ],
            )
            result = index.search("technology", 1)[0]
            self.assertEqual(result.entry_id, "question-001")
            self.assertEqual(result.kind, "question")
            self.assertEqual(result.locator, "OEBPS/questions_split_000.xhtml")
            self.assertEqual(result.pages, ())

    def test_unseen_draw_walks_every_record_once_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index = make_card_book(
                Path(temp),
                "answer",
                [
                    record("answer-001", "answer", 1, "Begin", "始める", "开始"),
                    record("answer-002", "answer", 2, "Wait", "待つ", "等待"),
                ],
            )
            first = index.draw_unseen("cycle-one", set())
            self.assertIsNotNone(first)
            second = index.draw_unseen("cycle-one", {first.entry_id})
            self.assertIsNotNone(second)
            self.assertNotEqual(first.entry_id, second.entry_id)
            self.assertIsNone(
                index.draw_unseen("cycle-one", {first.entry_id, second.entry_id})
            )


if __name__ == "__main__":
    unittest.main()
