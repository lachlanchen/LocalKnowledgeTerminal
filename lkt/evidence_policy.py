from __future__ import annotations

import re
import unicodedata
from typing import Any


def _plain_letter_key(value: Any) -> str:
    """Compare lexical forms without punctuation or accent differences."""

    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(
        character.casefold()
        for character in decomposed
        if character.isalpha() and not unicodedata.combining(character)
    )


def _normalized_form_tokens(value: Any) -> tuple[str, ...]:
    """Return accent-insensitive tokens after repairing common UTF-8 mojibake."""

    text = str(value)
    try:
        text = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    normalized = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _origin_cross_reference_targets(value: Any) -> tuple[str, ...]:
    """Return bounded Word Origins targets from a pure ``see ...`` entry."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    match = re.fullmatch(r"see(?:\s+also)?\s+(.+?)[.;]?", text, re.IGNORECASE)
    if match is None:
        return ()
    targets: list[str] = []
    for raw in re.split(r"\s*(?:,|;|\band\b|&)\s*", match.group(1), flags=re.I):
        target = re.sub(r"\s+", " ", raw).strip(" .:-")
        if not re.fullmatch(r"[A-Za-z][A-Za-z' -]{0,59}", target):
            continue
        if target.casefold() not in {item.casefold() for item in targets}:
            targets.append(target)
    return tuple(targets[:4])


def _origin_source_record_matches(term: Any, record: dict[str, Any]) -> bool:
    """Accept only target-owned, cross-referenced, or explicit origin statements."""

    source_key = _plain_letter_key(term)
    excerpt = re.sub(r"\s+", " ", str(record.get("excerpt", ""))).strip()
    if not source_key or str(record.get("kind", "")) != "entry":
        return False

    cross_reference_keys = {
        _plain_letter_key(target)
        for target in _origin_cross_reference_targets(excerpt)
    }
    headword_key = _plain_letter_key(record.get("headword", ""))
    if headword_key == source_key:
        return not cross_reference_keys
    if source_key in cross_reference_keys:
        return True

    words = re.findall(r"[A-Za-z]+", str(term))
    if not words:
        return False
    target = r"\b" + r"[\s'-]+".join(re.escape(word) for word in words) + r"\b"
    follows_target = (
        rf"{target}(?:\s*\[\d+\])?\s+"
        r"(?:(?:is|was|were)\s+)?"
        r"(?:comes?|came|derives?|derived|originates?|originated|descends?|"
        r"descended|developed|borrowed|coined|formed|taken|adapted|adopted|"
        r"goes|went)\b"
    )
    leads_to_target = (
        r"\b(?:source of|gave rise to|yielded|produced|became|developed into|"
        r"borrowed as|coined as|known in English as|whence)\s+"
        rf"(?:the\s+)?(?:modern\s+)?(?:English\s+)?{target}"
    )
    named_subentry = rf"(?:^|[.;:]\s+){target}\s*\[\d+\]"
    return any(
        re.search(pattern, excerpt, flags=re.IGNORECASE)
        for pattern in (follows_target, leads_to_target, named_subentry)
    )


def card_evidence_record_relevant(
    source_text: Any,
    record: dict[str, Any],
    support_forms: tuple[str, ...] = (),
) -> bool:
    """Keep exact citations and records supporting a visible lexical form."""

    payload = record.get("payload", {})
    payload = payload if isinstance(payload, dict) else {}
    source_key = _plain_letter_key(source_text)
    headword = str(
        payload.get("headword") or record.get("headword") or source_text
    )
    if source_key and _plain_letter_key(headword) == source_key:
        return True

    kind = str(payload.get("kind") or record.get("kind") or "").strip().casefold()
    corpus_id = str(record.get("corpus_id", "")).strip().casefold()
    if kind.startswith("morphology-") or any(
        marker in corpus_id for marker in ("root-dictionary", "affix-dictionary")
    ):
        return True

    excerpt = str(record.get("raw_excerpt", record.get("excerpt", "")))
    flat_record = {
        **payload,
        "headword": headword,
        "kind": kind,
        "excerpt": excerpt,
    }
    if _origin_source_record_matches(source_text, flat_record):
        return True

    excerpt_tokens = set(_normalized_form_tokens(excerpt))
    for form in support_forms:
        form_key = _plain_letter_key(form)
        if not form_key or form_key == source_key or len(form_key) < 3:
            continue
        if _origin_source_record_matches(form, flat_record):
            return True
        form_tokens = _normalized_form_tokens(form)
        if form_tokens and all(token in excerpt_tokens for token in form_tokens):
            return True
    return False


def filter_card_evidence(
    card: dict[str, Any], *, support_forms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Filter serialized evidence with the same policy used during composition."""

    result = dict(card)
    raw_evidence = card.get("evidence")
    if not isinstance(raw_evidence, list):
        return result
    english = card.get("english")
    english = english if isinstance(english, dict) else {}
    source_text = str(
        card.get("query") or english.get("term") or card.get("title", "")
    )
    result["evidence"] = [
        item
        for item in raw_evidence
        if not isinstance(item, dict)
        or card_evidence_record_relevant(source_text, item, support_forms)
    ]
    return result
