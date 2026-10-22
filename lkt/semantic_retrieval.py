"""Bounded local-book lookup for morphology subjects missing usable evidence."""
from __future__ import annotations

from collections.abc import Mapping
from itertools import islice
import re
import unicodedata
from typing import Any

from .models import Evidence
from .morphology import _is_index_only
from .semantic_contract import SEMANTIC_BOOK_RETRIEVAL_POLICY


def usable_book_excerpt(value: Any) -> bool:
    text = " ".join(str(value or "").split())
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return bool(text) and not _is_index_only(text) and not bool(
        re.fullmatch(r"(?:see|cf\.?)\s+[\w\s'*-]+[.;]?", normalized)
    )


def needs_book_retrieval(records: list[dict[str, Any]]) -> bool:
    return not any(
        isinstance(record, dict)
        and record.get("evidence_id")
        and usable_book_excerpt(record.get("excerpt"))
        for record in records
    )


def retrieve_semantic_books(
    retriever: Any, *, entity_id: str, label: str, source_language: str,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Search the focus and up to two connected words, without asserting roles."""
    entities = {
        str(item.get("id", "")): item
        for item in context.get("entities", [])
        if isinstance(item, dict)
    }
    labels = [label]
    for edge in context.get("assertions", []):
        if not isinstance(edge, dict):
            continue
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        other = target if source == entity_id else source if target == entity_id else ""
        neighbor = entities.get(other, {})
        if neighbor.get("type") == "term":
            labels.append(str(neighbor.get("label", "")))

    queries = []
    seen_queries = set()
    for value in labels:
        query = " ".join(unicodedata.normalize("NFKC", value).split())[:80]
        key = query.casefold().strip("-* ")
        if not key or key in seen_queries:
            continue
        queries.append(query)
        seen_queries.add(key)
        if len(queries) == 3:
            break

    records = []
    seen_records = set()
    searched = []
    omitted = 0
    for query in queries:
        searched.append(query)
        for value in islice(
            retriever.retrieve(query, source_language=source_language), 12
        ):
            if isinstance(value, Evidence):
                record = value.to_dict()
            elif isinstance(value, Mapping):
                record = dict(value)
            else:
                omitted += 1
                continue
            corpus_id = str(record.get("corpus_id", "")).strip()
            entry_id = str(record.get("entry_id") or record.get("source_entry_id") or "").strip()
            excerpt = str(record.get("excerpt") or "")
            key = (corpus_id, entry_id)
            if (
                not corpus_id or not entry_id or key in seen_records
                or not usable_book_excerpt(excerpt)
            ):
                omitted += 1
                continue
            # IDs and excerpts originate in corpus records, never model output.
            record.pop("evidence_id", None)
            record.update(corpus_id=corpus_id, entry_id=entry_id, excerpt=excerpt)
            records.append(record)
            seen_records.add(key)
            if len(records) == 6:
                break
        if len(records) == 6:
            break
    return records, {
        "policy": SEMANTIC_BOOK_RETRIEVAL_POLICY,
        "queries": searched,
        "retrieved_records": len(records),
        "omitted_records": omitted,
    }
