from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CorpusEntry:
    entry_id: str
    headword: str
    display_headword: str
    section: str
    date_label: str
    plain_text: str
    related_targets: tuple[str, ...] = ()
    source_pages: tuple[int, ...] = ()

    def evidence(self, excerpt: str) -> "Evidence":
        return Evidence(
            entry_id=self.entry_id,
            headword=self.display_headword or self.headword,
            section=self.section,
            date_label=self.date_label,
            pages=self.source_pages,
            excerpt=excerpt,
            corpus_id="word-origins",
            source_title="Word Origins",
            kind="entry",
        )


@dataclass(frozen=True)
class Evidence:
    entry_id: str
    headword: str
    section: str
    date_label: str
    pages: tuple[int, ...]
    excerpt: str
    corpus_id: str = "word-origins"
    source_title: str = "Word Origins"
    kind: str = "entry"
    locator: str = ""
    translations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["pages"] = list(self.pages)
        return value


@dataclass
class Card:
    card_id: str
    mode: str
    query: str
    title: str
    subtitle: str
    summary_en: str
    origin_story: str
    key_points: list[str]
    english: dict[str, str]
    japanese: dict[str, Any]
    chinese: dict[str, Any]
    memory_hook: str
    related_terms: list[dict[str, str]]
    evidence: list[Evidence]
    model: str
    created_at: str
    grounded: bool = True
    schema_version: str = "1.2"
    extensions: dict[str, Any] = field(default_factory=dict)
    origin_graph: list[dict[str, str]] = field(default_factory=list)
    extra_languages: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence"] = [item.to_dict() for item in self.evidence]
        return result
