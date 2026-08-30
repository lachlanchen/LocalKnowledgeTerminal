from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .corpus import CorpusIndex
from .knowledge import KnowledgeStore
from .jmdict import JapaneseReadingIndex
from .lexicon import LocalLexiconRag
from .llm import LlamaCppClient
from .models import Card, Evidence
from .morphology import MorphologyIndex
from .pronunciation import (
    EspeakPronouncer,
    chinese_pinyin,
    chinese_ruby_tokens,
    is_arabic_script_text,
)
from .store import CardStore, card_validation_errors


SUPPORTED_ATOMIC_JOBS = (
    "retrieve-evidence",
    "prepare-meaning",
    "split-morphemes",
    "expand-origin-branches",
    "extract-investigation-terms",
    "prepare-grammar-parts",
    "prepare-translation",
    "prepare-pronunciation",
    "prepare-grammar-properties",
    "compose-word-card",
    "compose-origin-card",
)
_PARTS_OF_SPEECH = {
    "noun",
    "verb",
    "adjective",
    "adverb",
    "pronoun",
    "preposition",
    "conjunction",
    "interjection",
    "determiner",
    "numeral",
    "other",
}
_ENCODING_DAMAGE = ("\ufffd", "Ã", "Â", "â€", "åŒ", "æ˜", "çš")
_LANGUAGE_NAMES = {
    "ja": "Japanese",
    "zh": "Simplified Chinese",
    "fr": "French",
    "ar": "Arabic",
}
_ARABIC_CONNECTORS = {"أو", "او", "و"}
_INVESTIGATION_STOPWORDS = {
    "about", "after", "again", "against", "between", "could", "from",
    "have", "into", "more", "other", "people", "same", "than", "that",
    "their", "there", "these", "they", "this", "those", "through", "very",
    "what", "when", "where", "which", "while", "with", "would", "your",
}
_CONTENT_LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "zh": "Simplified Chinese",
}
_GRAMMAR_ROLES = {
    "subject",
    "predicate",
    "object",
    "modifier",
    "connector",
    "clause",
    "other",
}
_GRAMMAR_PARTS_OF_SPEECH = _PARTS_OF_SPEECH | {
    "auxiliary",
    "particle",
    "phrase",
    "clause",
    "punctuation",
}
_GRAMMAR_COLORS = {
    "subject": "grammar-subject",
    "predicate": "grammar-predicate",
    "object": "grammar-object",
    "modifier": "grammar-modifier",
    "connector": "grammar-connector",
    "clause": "grammar-clause",
    "other": "grammar-other",
}
_GRAMMAR_ROLE_PARTS_OF_SPEECH = {
    "subject": {"noun", "pronoun", "phrase", "clause"},
    "predicate": {"verb", "auxiliary", "phrase", "clause"},
    "object": {"noun", "pronoun", "phrase", "clause"},
    "modifier": {
        "adjective",
        "adverb",
        "preposition",
        "determiner",
        "numeral",
        "auxiliary",
        "phrase",
        "clause",
    },
    "connector": {"conjunction", "particle"},
    "clause": {"phrase", "clause"},
}


_ORIGIN_LANGUAGE_CODES = {
    "ancient greek": "grc",
    "english": "en",
    "french": "fr",
    "latin": "la",
    "middle english": "enm",
    "old english": "ang",
    "old french": "fro",
    "proto-germanic": "gem-pro",
    "proto-indo-european": "ine-pro",
}


def _artifact_quality(artifact: dict[str, Any]) -> float:
    """Prefer reviewed metadata, falling back to the accepted payload confidence."""

    value = artifact.get("quality_score")
    if value is None:
        payload = artifact.get("payload")
        value = payload.get("confidence") if isinstance(payload, dict) else None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _origin_generation_metadata(
    completion: dict[str, Any] | None, fallback_model: str
) -> tuple[str, dict[str, Any]]:
    """Describe either a local-model draft or deterministic retrieved-book work."""

    if completion is None:
        return "retrieved book evidence", {}
    metrics = completion.get("metrics", {})
    return (
        str(completion.get("model", fallback_model)),
        dict(metrics) if isinstance(metrics, dict) else {},
    )


class AtomicModel(Protocol):
    model_name: str

    def complete_json(
        self, system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]: ...


class AtomicRetriever(Protocol):
    def retrieve(self, term: str) -> list[dict[str, Any]]: ...

    def component_evidence(self, form: str, kind: str) -> list[dict[str, Any]]: ...

    def origin_evidence(self, form: str) -> list[dict[str, Any]]: ...


class AtomicPronouncer(Protocol):
    def pronounce(self, text: str, language: str) -> dict[str, Any]: ...


def _book_record(item: Evidence, source_hash: str = "") -> dict[str, Any]:
    value = item.to_dict()
    return {
        **value,
        "source_hash": source_hash,
        "locator": item.locator or ", ".join(str(page) for page in item.pages),
    }


def _lexically_related(term: str, item: Evidence) -> bool:
    word = "".join(re.findall(r"[a-z]+", term.casefold()))
    headword = "".join(re.findall(r"[a-z]+", item.headword.casefold()))
    if not word or not headword:
        return False
    if word == headword:
        return True
    if item.kind == "morphology-affix" and len(headword) >= 2:
        return word.startswith(headword) or word.endswith(headword)
    return len(headword) >= 4 and (
        word.startswith(headword) or headword.startswith(word)
    )


def _clean_usage_note(value: Any, target_language: str = "") -> str:
    """Keep only optional, concise English metadata; never leak duplicate prose."""
    note = re.sub(r"\s+", " ", str(value or "")).strip()
    words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", note)
    if not note or len(words) > 14:
        return ""
    if not re.fullmatch(r"[\x20-\x7e]+", note) or not words:
        return ""
    if target_language == "fr" and {
        word.casefold() for word in words
    } & {"avec", "des", "dans", "et", "la", "le", "les", "pour", "sens", "une"}:
        return ""
    return note[:180]


def _align_grammar_parts(
    text: str, raw_parts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map model-proposed phrases back onto every exact source character."""

    if not 1 <= len(raw_parts) <= 8:
        raise ValueError("grammar task must return one to eight parts")
    cursor = 0
    aligned: list[dict[str, Any]] = []
    for raw in raw_parts:
        if not isinstance(raw, dict):
            raise ValueError("grammar part is not an object")
        requested = str(raw.get("surface", "")).strip()
        if not requested or any(marker in requested for marker in _ENCODING_DAMAGE):
            raise ValueError("grammar part has invalid text")
        pieces = re.split(r"(\s+)", requested)
        pattern = "".join(r"\s+" if piece.isspace() else re.escape(piece) for piece in pieces)
        match = re.search(pattern, text[cursor:])
        if match is None:
            raise ValueError("grammar part was not copied from the reviewed text")
        start = cursor + match.start()
        end = cursor + match.end()
        gap = text[cursor:start]
        if any(character.isalnum() for character in gap):
            raise ValueError("grammar parts skipped reviewed text")
        if gap and aligned:
            aligned[-1]["surface"] += gap
        surface = text[start:end]
        if gap and not aligned:
            surface = gap + surface
        aligned.append({**raw, "surface": surface})
        cursor = end
    tail = text[cursor:]
    if any(character.isalnum() for character in tail):
        raise ValueError("grammar parts did not reach the end of reviewed text")
    if tail:
        aligned[-1]["surface"] += tail
    if "".join(str(part["surface"]) for part in aligned) != text:
        raise ValueError("grammar parts do not reconstruct the reviewed text")
    return aligned


def _align_grammar_draft(text: str, value: Any) -> list[dict[str, Any]]:
    """Recover only exact, complete source spans from a small-model draft.

    Qwen occasionally returns one valid part without the requested outer
    ``parts`` array, or appends prompt text after an already complete span.
    Neither case needs another inference call: accept the shortest prefix that
    reconstructs the reviewed text exactly and discard everything after it.
    Partial, rewritten, or reordered source text still fails closed in
    ``_align_grammar_parts``.
    """

    if not isinstance(value, dict):
        raise ValueError("grammar task did not return an object")
    raw_parts = value.get("parts")
    if not isinstance(raw_parts, list):
        raw_parts = [value] if value.get("surface") else None
    if not isinstance(raw_parts, list):
        raise ValueError("grammar task did not return parts")

    last_error: ValueError | None = None
    for end in range(1, min(len(raw_parts), 8) + 1):
        try:
            return _align_grammar_parts(text, raw_parts[:end])
        except ValueError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("grammar task did not return parts")


def _grammar_role_matches(role: str, part_of_speech: str, surface: str) -> bool:
    """Reject internally contradictory role labels without parsing language."""

    if role == "other":
        return True
    allowed = _GRAMMAR_ROLE_PARTS_OF_SPEECH.get(role, set())
    if part_of_speech not in allowed:
        return False
    if role == "connector" and len(surface.strip()) > 32:
        return False
    return True


def _normalise_grammar_role(role: str, part_of_speech: str, surface: str) -> str:
    """Conservatively repair a contradictory controlled model label."""

    if _grammar_role_matches(role, part_of_speech, surface):
        return role
    if len(surface.strip()) > 32 or part_of_speech in {"phrase", "clause"}:
        return "clause"
    if part_of_speech in {"conjunction", "particle"}:
        return "connector"
    if part_of_speech in {"verb", "auxiliary"}:
        return "predicate"
    if part_of_speech in {
        "adjective",
        "adverb",
        "preposition",
        "determiner",
        "numeral",
    }:
        return "modifier"
    return "other"


def _normalise_grammar_labels(
    role: str, part_of_speech: str, surface: str
) -> tuple[str, str]:
    """Normalize a controlled role/POS pair while retaining its source text."""

    normalized_role = _normalise_grammar_role(role, part_of_speech, surface)
    normalized_part_of_speech = part_of_speech
    if not _grammar_role_matches(
        normalized_role, normalized_part_of_speech, surface
    ) and normalized_role == "clause":
        normalized_part_of_speech = "clause"
    return normalized_role, normalized_part_of_speech


def _has_repeated_arabic_content_word(value: str) -> bool:
    plain = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    words = re.findall(r"[\u0621-\u064a]+", plain)
    previous = ""
    for word in words:
        if word in _ARABIC_CONNECTORS:
            continue
        if previous == word:
            return True
        previous = word
    return False


def _book_anchored_shape(
    letters: str, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build a minimal surface shape around the strongest exact book root.

    The model may describe the parts, but it must not redraw a root that an
    exact reviewed-book lookup has already located inside the source word.
    """

    candidates: list[tuple[int, int, str]] = []
    for record in records:
        if record.get("component_hint") != "root":
            continue
        surface = re.sub(
            r"[^A-Za-z]", "", str(record.get("component_surface", ""))
        ).casefold()
        if not surface:
            surface = re.sub(
                r"[^A-Za-z]", "", str(record.get("headword", ""))
            ).casefold()
        if len(surface) < 3:
            continue
        start = letters.find(surface)
        while start >= 0:
            candidates.append((start, start + len(surface), surface))
            start = letters.find(surface, start + 1)
    if not candidates:
        return []

    start, end, surface = sorted(
        set(candidates), key=lambda item: (-len(item[2]), item[0], item[2])
    )[0]
    shape: list[dict[str, Any]] = []
    if start:
        shape.append({"surface": letters[:start], "kind": "prefix"})
    shape.append({"surface": surface, "kind": "root"})
    if end < len(letters):
        shape.append({"surface": letters[end:], "kind": "suffix"})
    return shape


def _book_decomposition_shape(
    letters: str, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Recover an explicit ordered split from an exact morphology entry.

    The polished books commonly spell a word out as ``pre(=before) +
    de(=down) + cess(=go)``.  This is stronger evidence than an unrelated
    root whose spelling happens to occur inside the word.  Keep uncertain
    component roles blank for the model to classify; the book fixes the
    surfaces and supplies their provenance.
    """

    word = re.sub(r"[^A-Za-z]", "", letters).casefold()
    if not word:
        return []
    component = re.compile(r"(?<![A-Za-z])([A-Za-z]{1,14})\s*[（(][^（）()]{1,90}[）)]")
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []
    for record in records:
        headword = re.sub(
            r"[^A-Za-z]", "", str(record.get("headword", ""))
        ).casefold()
        if headword != word or not str(record.get("kind", "")).startswith(
            "morphology-"
        ):
            continue
        excerpt = str(record.get("excerpt", ""))
        matches = list(component.finditer(excerpt))
        for start in range(len(matches)):
            run = [matches[start]]
            for following in matches[start + 1 :]:
                between = excerpt[run[-1].end() : following.start()]
                if not re.fullmatch(r"\s*[+＋]\s*", between):
                    break
                run.append(following)
            if len(run) < 2:
                continue
            surfaces = [match.group(1).casefold() for match in run]
            fixed = "".join(surfaces)
            position = word.find(fixed)
            if position < 0:
                continue
            end = position + len(fixed)
            evidence_ids = [
                str(record.get("knowledge_evidence_id", ""))
            ] if record.get("knowledge_evidence_id") else []
            shape: list[dict[str, Any]] = []
            if position:
                shape.append(
                    {
                        "surface": word[:position],
                        "kind": "prefix",
                        "evidence_ids": [],
                    }
                )
            shape.extend(
                {
                    "surface": surface,
                    "kind": "",
                    "evidence_ids": evidence_ids,
                }
                for surface in surfaces
            )
            if end < len(word):
                # When the reviewed formula stops before a trailing derivation,
                # its final named component is the lexical root and the
                # uncovered tail is the suffix.
                shape[-1]["kind"] = "root"
                shape.append(
                    {
                        "surface": word[end:],
                        "kind": "suffix",
                        "evidence_ids": [],
                    }
                )
            candidates.append((len(fixed), len(surfaces), shape))
    if not candidates:
        return []
    return sorted(candidates, key=lambda item: (-item[0], -item[1]))[0][2]


def _morpheme_display_form(surface: str, kind: str) -> str:
    """Apply one deterministic notation convention to an already matched part."""

    base = re.sub(r"^-+|-+$", "", surface.strip())
    if kind == "prefix":
        return f"{base}-"
    if kind == "suffix":
        return f"-{base}"
    return base


def _clean_morpheme_meaning(value: Any) -> str:
    """Keep a model meaning as one short, punctuation-free English phrase."""

    if isinstance(value, (list, tuple)):
        text = " or ".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value).strip()
    text = re.sub(r"\s*[,;/]\s*", " or ", text)
    return re.sub(r"\s+", " ", text).strip(" .:-")


def _derived_origin_view_specs(
    parts: list[dict[str, Any]],
) -> tuple[tuple[str, set[str], set[str], str], ...]:
    """Return only morphology views supported by actual analyzed part kinds."""

    kinds = {str(part.get("kind", "")) for part in parts}
    specs: list[tuple[str, set[str], set[str], str]] = []
    if "root" in kinds:
        specs.append(
            ("root", {"root"}, {"root"}, "accepted-atomic-root-view")
        )
    if kinds.intersection({"prefix", "suffix"}):
        specs.append(
            (
                "affix",
                {"prefix", "suffix"},
                {"prefix", "suffix"},
                "accepted-atomic-affix-view",
            )
        )
    return tuple(specs)


def _affix_origin_story(parts: list[dict[str, Any]]) -> str:
    """Describe only accepted affix parts without overstating provenance."""

    affixes = [
        part for part in parts if str(part.get("kind", "")) in {"prefix", "suffix"}
    ]
    if not affixes:
        return ""
    details = "; ".join(
        f"{part['canonical_form']} as “{part['meaning']}”" for part in affixes
    )
    if all(part.get("evidence_ids") for part in affixes):
        return f"Cited affix evidence supports {details}."
    return f"Accepted affix analysis gives {details}."


def _plain_letter_key(value: Any) -> str:
    """Compare historical forms without punctuation or accent differences."""

    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(
        character.casefold()
        for character in decomposed
        if character.isascii() and character.isalpha()
    )


def _text_form_keys(value: Any) -> set[str]:
    """Return comparable word forms, repairing common UTF-8 mojibake first."""

    text = str(value)
    candidates = [text]
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    else:
        candidates.append(repaired)
    return {
        key
        for candidate in candidates
        for token in re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", candidate)
        if (key := _plain_letter_key(token))
    }


def _explicit_form_evidence_ids(
    form: Any, records: list[dict[str, Any]]
) -> list[str]:
    """Cite only records that visibly contain the proposed historical form."""

    key = _plain_letter_key(form)
    if not key:
        return []
    return list(
        dict.fromkeys(
            str(record.get("evidence_id", ""))
            for record in records
            if key in _text_form_keys(record.get("excerpt", ""))
            and str(record.get("evidence_id", ""))
        )
    )


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
        key = target.casefold()
        if key not in {item.casefold() for item in targets}:
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


def _origin_source_evidence_supported(
    term: Any, records: list[dict[str, Any]]
) -> bool:
    """Require an owned statement or a resolved exact-headword cross-reference."""

    entries = [
        record
        for record in records
        if isinstance(record, dict) and str(record.get("kind", "")) == "entry"
    ]
    if any(_origin_source_record_matches(term, record) for record in entries):
        return True

    source_key = _plain_letter_key(term)
    frontier = {source_key} if source_key else set()
    visited: set[str] = set()
    for _depth in range(3):
        next_frontier: set[str] = set()
        for record in entries:
            headword_key = _plain_letter_key(record.get("headword", ""))
            if headword_key not in frontier or headword_key in visited:
                continue
            targets = {
                _plain_letter_key(target)
                for target in _origin_cross_reference_targets(record.get("excerpt", ""))
                if _plain_letter_key(target)
            }
            if not targets:
                return True
            next_frontier.update(targets)
        visited.update(frontier)
        frontier = next_frontier - visited
        if not frontier:
            break
    return False


def _normalize_origin_draft(
    value: Any, *, component_id: str, modern_word: str, base_form: str
) -> tuple[Any, list[str]]:
    """Repair application-owned identity and redundant modern endpoints."""

    if not isinstance(value, dict):
        return value, []
    normalized = deepcopy(value)
    changes: list[str] = []
    if str(normalized.get("component_id", "")) != component_id:
        normalized["component_id"] = component_id
        changes.append("restored-system-component-id")

    raw_steps = normalized.get("steps")
    if not isinstance(raw_steps, list):
        return normalized, changes
    forbidden = {_plain_letter_key(modern_word), _plain_letter_key(base_form)}
    retained: list[Any] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            retained.append(raw)
            continue
        step = dict(raw)
        form_key = _plain_letter_key(step.get("form", ""))
        language = str(step.get("language", "")).strip().casefold()
        period = str(step.get("period", "")).strip().casefold()
        if language in {"en", "english"} and "old english" in period:
            step["language"] = "ang"
            changes.append("normalized-old-english-code")
            language = "ang"
        elif language in {"en", "english"} and "middle english" in period:
            step["language"] = "enm"
            changes.append("normalized-middle-english-code")
            language = "enm"
        if (
            form_key in forbidden
            or "modern english" in period
            or language in {"en", "english"}
        ):
            changes.append("removed-redundant-modern-endpoint")
            continue
        retained.append(step)
    normalized["steps"] = retained
    return normalized, list(dict.fromkeys(changes))


def _origin_draft_review_reason(
    value: Any,
    *,
    component_id: str,
    modern_word: str,
    base_form: str,
    fixed_provenance_ids: set[str],
    evidence: list[dict[str, Any]],
) -> str:
    """Identify a weak origin draft before spending a whole job retry on it."""

    if not isinstance(value, dict) or str(value.get("component_id", "")) != component_id:
        return "the component_id changed"
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 3:
        return "steps must contain one to three historical forms"
    forbidden = {_plain_letter_key(modern_word), _plain_letter_key(base_form)}
    for step in steps:
        if not isinstance(step, dict):
            return "a step is not an object"
        form = str(step.get("form", "")).strip()
        language = str(step.get("language", "")).strip().casefold()
        period = str(step.get("period", "")).strip().casefold()
        if not form or _plain_letter_key(form) in forbidden:
            return "a historical step repeats the modern word or lexical base"
        if language == "en" or "modern english" in period:
            return "Modern English was incorrectly used as a historical step"
        if fixed_provenance_ids and not (
            set(_explicit_form_evidence_ids(form, evidence)) & fixed_provenance_ids
        ):
            return "a historical form is not visibly supported by the exact book entry"
    return ""


def _book_origin_steps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract an explicit Latin <- Indo-European chain without inference."""

    quote = r"[‘'\"]"
    close_quote = r"[’'\"]"
    pattern = re.compile(
        rf"Latin\s+([^\W\d_][\w-]*)\s+{quote}([^’'\"]+){close_quote}"
        rf".{{0,180}}?Indo-European base\s+(\*[A-Za-z-]+)\s+"
        rf"{quote}([^’'\"]+){close_quote}",
        flags=re.IGNORECASE,
    )
    for record in records:
        match = pattern.search(str(record.get("excerpt", "")))
        if not match:
            continue
        evidence_id = str(record.get("evidence_id", ""))
        if not evidence_id:
            continue
        pie_meaning = _clean_morpheme_meaning(match.group(4))
        latin_meaning = _clean_morpheme_meaning(match.group(2))
        if not pie_meaning or not latin_meaning:
            continue
        return [
            {
                "form": match.group(3),
                "language": "ine-pro",
                "period": "Proto-Indo-European",
                "meaning": pie_meaning,
                "confidence": 0.95,
                "evidence_ids": [evidence_id],
            },
            {
                "form": match.group(1),
                "language": "la",
                "period": "Latin",
                "meaning": latin_meaning,
                "confidence": 0.95,
                "evidence_ids": [evidence_id],
            },
        ]
    return []


def _collapse_repeated_arabic_alternative(value: str) -> str:
    """Remove only the objectively redundant `word or same-word` construction."""
    return re.sub(
        r"([\u0621-\u064a]+)\s+(?:\u0623\u0648|\u0627\u0648)\s+\1",
        r"\1",
        value,
    )


def _strip_exact_latin_headword(value: str, source_term: str) -> str:
    """Remove only a standalone copy of the supplied Latin source headword.

    A small local model sometimes appends the English headword to an otherwise
    Arabic definition. Do not generalize this into a Latin-token scrubber:
    unknown Latin words must survive here so the strict script validator can
    reject the draft.
    """

    headword = re.sub(r"\s+", " ", str(source_term)).strip()
    if not re.fullmatch(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*", headword):
        return str(value)
    token = rf"(?<![^\W\d_]){re.escape(headword)}(?![^\W\d_])"
    cleaned = str(value)
    for opening, closing in (
        ("(", ")"),
        ("[", "]"),
        ("{", "}"),
        ('"', '"'),
        ("'", "'"),
        ("\u201c", "\u201d"),
        ("\u2018", "\u2019"),
    ):
        cleaned = re.sub(
            rf"{re.escape(opening)}\s*{token}\s*{re.escape(closing)}",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(token, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return re.sub(r"\s+([\u060c\u061b\u061f,.!?;:])", r"\1", cleaned)


class WordEvidenceRetriever:
    """Small correction context from books plus sense-aligned OMW."""

    def __init__(
        self,
        corpus: CorpusIndex,
        roots: MorphologyIndex,
        affixes: MorphologyIndex,
        lexicon: LocalLexiconRag,
    ):
        self.corpus = corpus
        self.roots = roots
        self.affixes = affixes
        self.lexicon = lexicon

    @staticmethod
    def _hash(index: Any) -> str:
        try:
            return str(index.metadata().get("source_sha256", ""))
        except (FileNotFoundError, OSError):
            return ""

    def retrieve(self, term: str) -> list[dict[str, Any]]:
        root_records = [
            item for item in self.roots.search(term, 8) if _lexically_related(term, item)
        ][:2]
        affix_records = [
            item
            for item in self.affixes.search(term, 8)
            if _lexically_related(term, item)
        ][:2]
        records = [
            *(
                _book_record(item, self._hash(self.corpus))
                for item in self.corpus.search(term, 3)
            ),
            *(
                _book_record(item, self._hash(self.roots))
                for item in root_records
            ),
            *(
                _book_record(item, self._hash(self.affixes))
                for item in affix_records
            ),
        ]
        records.extend(self.lexicon.search(term, limit=3))
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            key = (str(record.get("corpus_id", "")), str(record.get("entry_id", "")))
            if not all(key) or key in seen:
                continue
            seen.add(key)
            result.append(record)
        return result

    def component_evidence(self, form: str, kind: str) -> list[dict[str, Any]]:
        index = self.roots if kind == "root" else self.affixes
        records = index.exact(form.strip("-"), 4)
        result: list[dict[str, Any]] = []
        for item in records:
            excerpt = item.excerpt.casefold()
            if kind == "root" and not (
                item.headword.isupper() or "词根" in item.excerpt[:80]
            ):
                continue
            if not any(marker in excerpt for marker in ("=", "意为", "means", "来自")):
                continue
            result.append(_book_record(item, self._hash(index)))
        return result[:2]

    def origin_evidence(self, form: str) -> list[dict[str, Any]]:
        plain = re.sub(r"[^A-Za-z]", "", form)
        if len(plain) < 3:
            return []
        records: list[Evidence] = []
        seen: set[str] = set()
        pending = [plain]
        followed: set[str] = set()
        for _depth in range(3):
            next_targets: list[str] = []
            for query in pending:
                query_key = _plain_letter_key(query)
                if not query_key or query_key in followed:
                    continue
                followed.add(query_key)
                for item in self.corpus.search(query, 6):
                    exact = _plain_letter_key(item.headword) == query_key
                    if not exact and not _lexically_related(query, item):
                        continue
                    if item.entry_id not in seen:
                        records.append(item)
                        seen.add(item.entry_id)
                    if exact:
                        next_targets.extend(
                            _origin_cross_reference_targets(item.excerpt)
                        )
            pending = next_targets
            if not pending or len(records) >= 6:
                break
        return [
            _book_record(item, self._hash(self.corpus)) for item in records[:6]
        ]


@dataclass(frozen=True)
class AtomicRunResult:
    job_id: str
    job_type: str
    status: str
    artifact_id: str = ""


class PreparationWorker:
    """Execute bounded jobs and commit each accepted artifact immediately."""

    def __init__(
        self,
        store: KnowledgeStore,
        retriever: AtomicRetriever,
        model: AtomicModel,
        pronouncer: AtomicPronouncer | None = None,
        card_store: CardStore | None = None,
        japanese_readings: JapaneseReadingIndex | None = None,
    ):
        self.store = store
        self.retriever = retriever
        self.model = model
        self.pronouncer = pronouncer or EspeakPronouncer()
        self.card_store = card_store
        self.japanese_readings = japanese_readings

    def run_once(self) -> AtomicRunResult | None:
        job_types = list(SUPPORTED_ATOMIC_JOBS)
        if self.card_store is None:
            job_types.remove("compose-word-card")
            job_types.remove("compose-origin-card")
        job = self.store.claim_next_job(job_types)
        if job is None:
            return None
        try:
            if job["job_type"] == "retrieve-evidence":
                artifact_id = self._retrieve(job)
            elif job["job_type"] == "prepare-meaning":
                artifact_id = self._prepare_meaning(job)
            elif job["job_type"] == "split-morphemes":
                artifact_id = self._split_morphemes(job)
            elif job["job_type"] == "expand-origin-branches":
                artifact_id = self._expand_origin_branches(job)
            elif job["job_type"] == "extract-investigation-terms":
                artifact_id = self._extract_investigation_terms(job)
            elif job["job_type"] == "prepare-grammar-parts":
                artifact_id = self._prepare_grammar_parts(job)
            elif job["job_type"] == "prepare-translation":
                artifact_id = self._prepare_translation(job)
            elif job["job_type"] == "prepare-pronunciation":
                artifact_id = self._prepare_pronunciation(job)
            elif job["job_type"] == "prepare-grammar-properties":
                artifact_id = self._prepare_grammar_properties(job)
            elif job["job_type"] == "compose-origin-card":
                artifact_id = self._compose_origin_card(job)
            else:
                artifact_id = self._compose_word_card(job)
        except Exception as exc:
            self.store.finish_job(job["job_id"], error=str(exc))
            return AtomicRunResult(job["job_id"], job["job_type"], "retry", "")
        self.store.finish_job(job["job_id"])
        return AtomicRunResult(job["job_id"], job["job_type"], "complete", artifact_id)

    def run(self, limit: int = 1) -> list[AtomicRunResult]:
        results: list[AtomicRunResult] = []
        for _ in range(max(1, min(int(limit), 100))):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
            if result.status != "complete":
                break
        return results

    def _retrieve(self, job: dict[str, Any]) -> str:
        term = self.store.term_record(str(job["subject_entity_id"]))
        records = self.retriever.retrieve(str(term["text"]))
        if not records:
            raise ValueError(f"no book or dictionary evidence for {term['text']!r}")
        saved: list[dict[str, Any]] = []
        for record in records:
            evidence_id = self.store.add_evidence(
                str(record.get("corpus_id", "")),
                str(record.get("entry_id", "")),
                source_hash=str(record.get("source_hash", "")),
                locator=str(record.get("locator", "")),
                excerpt=str(record.get("excerpt") or record.get("definition") or ""),
                payload=record,
            )
            self.store.link_evidence(
                term["entity_id"], evidence_id, claim="retrieval candidate", confidence=0.6
            )
            saved.append({**record, "knowledge_evidence_id": evidence_id})
        return self.store.save_job_artifact(
            job["job_id"],
            "retrieved-evidence",
            {"term": term["text"], "records": saved},
            validation_state="candidate",
        )

    @staticmethod
    def _meaning_context(records: list[dict[str, Any]]) -> str:
        compact = []
        for record in records[:5]:
            compact.append(
                {
                    "id": record.get("knowledge_evidence_id", ""),
                    "source": record.get("source_title", record.get("corpus_id", "")),
                    "headword": record.get("headword", ""),
                    "part_of_speech": record.get("part_of_speech", ""),
                    "definition": str(record.get("definition", ""))[:280],
                    "excerpt": str(record.get("excerpt", ""))[:320],
                }
            )
        return json.dumps(compact, ensure_ascii=False)

    def _prepare_meaning(self, job: dict[str, Any]) -> str:
        term = self.store.term_record(str(job["subject_entity_id"]))
        artifacts = self.store.artifacts_for_subject(
            job["subject_key"], stage="retrieved-evidence"
        )
        if not artifacts:
            raise ValueError("retrieved evidence checkpoint is missing")
        records = artifacts[-1]["payload"].get("records", [])
        allowed_evidence = {
            str(record.get("knowledge_evidence_id", "")) for record in records
        }
        prompt = f"""TERM: {term['text']}
RETRIEVED EVIDENCE: {self._meaning_context(records)}

Return exactly one JSON object with these keys:
definition: one clear English dictionary sentence, at most 30 words
part_of_speech: noun, verb, adjective, adverb, pronoun, preposition,
conjunction, interjection, determiner, numeral, or other
sense_note: at most 18 words; distinguish the selected sense if needed
confidence: number from 0 to 1
evidence_ids: non-empty array containing only supplied id values

Use the retrieved evidence to select one core sense. Do not add etymology,
translations, examples, markdown, or claims absent from the evidence."""
        completion = self.model.complete_json(
            "You prepare one reusable, source-grounded lexical fact at a time.",
            prompt,
            max_tokens=192,
        )
        value = completion.get("value")
        if not isinstance(value, dict):
            raise ValueError("meaning task did not return an object")
        definition = re.sub(r"\s+", " ", str(value.get("definition", ""))).strip()
        if not definition or len(definition) > 320:
            raise ValueError("meaning definition is empty or too long")
        if any(marker in definition for marker in _ENCODING_DAMAGE):
            raise ValueError("meaning definition contains encoding damage")
        part_of_speech = str(value.get("part_of_speech", "other")).strip().lower()
        if part_of_speech not in _PARTS_OF_SPEECH:
            raise ValueError("meaning part of speech is invalid")
        selected = [
            str(item) for item in value.get("evidence_ids", []) if str(item) in allowed_evidence
        ] if isinstance(value.get("evidence_ids"), list) else []
        if not selected:
            raise ValueError("meaning did not cite supplied evidence")
        confidence = max(0.0, min(float(value.get("confidence", 0.0)), 1.0))
        if confidence < 0.55:
            raise ValueError("meaning confidence is below acceptance threshold")

        term_id = self.store.upsert_term(
            term["language"],
            term["text"],
            term["kind"],
            status="accepted",
            quality_score=confidence,
            payload=term["payload"],
        )
        meaning_id = self.store.add_meaning(
            term_id,
            "en",
            definition,
            part_of_speech=part_of_speech,
            domain_label=str(value.get("sense_note", "")).strip()[:180],
            status="accepted",
        )
        for evidence_id in selected:
            self.store.link_evidence(
                meaning_id, evidence_id, claim=definition, confidence=confidence
            )
        accepted = {
            "term_id": term_id,
            "meaning_id": meaning_id,
            "definition": definition,
            "part_of_speech": part_of_speech,
            "sense_note": str(value.get("sense_note", "")).strip()[:180],
            "confidence": confidence,
            "evidence_ids": selected,
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            meaning_id,
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="atomic meaning preparation",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-meaning",
            accepted,
            language="en",
            validation_state="accepted",
            quality_score=confidence,
        )

    def _split_morphemes(self, job: dict[str, Any]) -> str:
        source = self.store.term_record(str(job["subject_entity_id"]))
        evidence_artifacts = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="retrieved-evidence",
            validation_state="candidate",
        )
        if not evidence_artifacts:
            raise ValueError("current retrieval checkpoint is missing")
        records = list(evidence_artifacts[-1]["payload"].get("records", []))
        letters = re.sub(r"[^A-Za-z]", "", str(source["text"])).casefold()
        explicit_shape = _book_decomposition_shape(letters, records)
        seen_hints: set[tuple[str, str]] = set()
        if not explicit_shape:
            for start in range(len(letters)):
                for end in range(start + 3, min(len(letters), start + 8) + 1):
                    candidate = letters[start:end]
                    for record in self.retriever.component_evidence(candidate, "root"):
                        key = (
                            str(record.get("corpus_id", "")),
                            str(record.get("entry_id", "")),
                        )
                        if key in seen_hints:
                            continue
                        seen_hints.add(key)
                        evidence_id = self.store.add_evidence(
                            key[0],
                            key[1],
                            source_hash=str(record.get("source_hash", "")),
                            locator=str(record.get("locator", "")),
                            excerpt=str(record.get("excerpt", "")),
                            payload=record,
                        )
                        records.append(
                            {
                                **record,
                                "knowledge_evidence_id": evidence_id,
                                "component_hint": "root",
                                "component_surface": candidate,
                                "component_start": start,
                                "component_end": end,
                            }
                        )
        allowed_evidence = {
            str(record.get("knowledge_evidence_id", "")) for record in records
        }
        context = [
            {
                "evidence_id": (
                    "" if explicit_shape else record.get("knowledge_evidence_id", "")
                ),
                "source": record.get("source_title", record.get("corpus_id", "")),
                "headword": record.get("headword", ""),
                "kind": record.get("kind", ""),
                "component_hint": record.get("component_hint", ""),
                "component_surface": record.get("component_surface", ""),
                "excerpt": str(record.get("excerpt", ""))[:700],
            }
            for record in records
        ]
        # Only an explicit reviewed-book decomposition fixes a surface shape.
        # A substring hit from the root index remains RAG context for Qwen; it
        # must not mechanically turn every leftover letter into an affix.
        required_shape = explicit_shape
        prompt_shape = [
            {
                "surface": item["surface"],
                **({"kind": item["kind"]} if item.get("kind") else {}),
            }
            for item in required_shape
        ]
        shape_instruction = (
            "REQUIRED ORDERED SURFACES AND KINDS: "
            f"{json.dumps(prompt_shape, ensure_ascii=False)}\n"
            "Return one part for every required surface, in that exact order. "
            "Copy a kind when it is supplied; classify any omitted kind. Only supply its "
            "canonical form, language, meaning, confidence, and evidence IDs.\n"
            if required_shape
            else "No exact reviewed-book root anchor was found.\n"
        )
        evidence_instruction = (
            "evidence_ids: always an empty array; fixed book provenance is attached "
            "by the system after validation"
            if explicit_shape
            else "evidence_ids: only supplied evidence IDs that explicitly support this part"
        )
        prompt = f"""MORPHEME SPLIT / LEXICAL STRUCTURE ANALYSIS
TERM: {source['text']}
CURRENT EVIDENCE: {json.dumps(context, ensure_ascii=False)}
{shape_instruction}

Return exactly one JSON object with key `parts`, an ordered array. Each part has:
surface: exact consecutive letters from TERM
canonical_form: standard display form, using a trailing hyphen for a prefix and
  a leading hyphen for a suffix
kind: prefix, root, suffix, or free
language: en or la
meaning: at most 10 English words
confidence: number from 0 to 1
{evidence_instruction}

The concatenated surfaces must reproduce TERM exactly. Include every letter once
and identify at least one root or free lexical base. Do not force a split merely
because letter groups resemble affixes. When the evidence does not establish a
reliable present-day decomposition, return one whole-TERM part with kind `free`.
Use exact COMPONENT HINTS when they genuinely describe this word. A prefix
canonical form must end in `-`; a suffix canonical form must begin with `-`.
Meaning must be one plain phrase, never a stringified list. Distinguish productive
word structure from historical ancestry; history belongs to the next RAG task.
Use an empty evidence_ids array for model knowledge. Never merge, shorten, rename,
or reclassify a required reviewed-book part."""
        completion = self.model.complete_json(
            (
                "You fill metadata for a fixed, book-anchored morphology split."
                if required_shape
                else (
                    "You make a conservative linguistic judgment about lexical "
                    "structure; an unsplit word is a valid answer."
                )
            ),
            prompt,
            max_tokens=384 if explicit_shape else 320,
        )
        value = completion.get("value")
        self.store.save_job_artifact(
            job["job_id"],
            "model-morpheme-draft",
            {
                "term": source["text"],
                "value": value,
                "model": completion.get("model", self.model.model_name),
                "metrics": completion.get("metrics", {}),
            },
            language=source["language"],
            validation_state="candidate",
        )

        def draft_needs_review(candidate: Any) -> bool:
            if not isinstance(candidate, dict):
                return True
            draft_parts = candidate.get("parts")
            if not isinstance(draft_parts, list) or not 1 <= len(draft_parts) <= 5:
                return True
            if not all(isinstance(item, dict) for item in draft_parts):
                return True
            surfaces = "".join(
                re.sub(r"[^A-Za-z]", "", str(item.get("surface", "")))
                for item in draft_parts
            )
            kinds = {
                str(item.get("kind", "")).strip().casefold()
                for item in draft_parts
            }
            return (
                surfaces.casefold() != letters
                or not kinds.intersection({"root", "free"})
            )

        if draft_needs_review(value):
            review_prompt = f"""LINGUISTIC REVIEW OF A LEXICAL STRUCTURE DRAFT
TERM: {source['text']}
RETRIEVED WORD-ORIGIN, ROOT, AFFIX, AND DICTIONARY EVIDENCE:
{json.dumps(context, ensure_ascii=False)}
FIRST DRAFT: {json.dumps(value, ensure_ascii=False)}
{shape_instruction}

Independently review the draft using the retrieved evidence and linguistic
judgment. Return exactly one JSON object with `parts` in the same schema as the
first task. Correct a wrongly labelled prefix, root, suffix, or free base. Never
force a decomposition to satisfy a template. If a reliable synchronic split is
not established, return one whole-TERM `free` part. Every letter must be covered
exactly once, and the answer must contain a root or free lexical base. Book-backed
claims may cite only supplied evidence IDs; model knowledge must cite none."""
            reviewed = self.model.complete_json(
                "You are the second-pass linguist reviewing RAG evidence, not a string splitter.",
                review_prompt,
                max_tokens=384,
            )
            value = reviewed.get("value")
            self.store.save_job_artifact(
                job["job_id"],
                "model-morpheme-review-draft",
                {
                    "term": source["text"],
                    "value": value,
                    "model": reviewed.get("model", self.model.model_name),
                    "metrics": reviewed.get("metrics", {}),
                },
                language=source["language"],
                validation_state="candidate",
            )
            completion = reviewed

        raw_parts = value.get("parts") if isinstance(value, dict) else None
        if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 5:
            raise ValueError("morpheme task returned an invalid number of parts")
        cleaned: list[dict[str, Any]] = []
        for item in raw_parts:
            if not isinstance(item, dict):
                raise ValueError("morpheme part is not an object")
            surface = re.sub(r"[^A-Za-z]", "", str(item.get("surface", "")))
            kind = str(item.get("kind", "")).strip().lower()
            supplied_canonical = str(item.get("canonical_form", "")).strip()
            language = str(item.get("language", "en")).strip().lower()
            meaning = _clean_morpheme_meaning(item.get("meaning", ""))
            confidence = max(0.0, min(float(item.get("confidence", 0.0)), 1.0))
            if not surface or kind not in {"prefix", "root", "suffix", "free"}:
                raise ValueError("morpheme surface or kind is invalid")
            if language not in {"en", "la"}:
                raise ValueError("morpheme language is not en or la")
            # The prompt aims for ten words, but a sound compact gloss should
            # not be discarded over a harmless few-word overrun. Presentation
            # remains bounded while linguistic judgment wins over word counting.
            if not supplied_canonical or not meaning or len(meaning.split()) > 16:
                raise ValueError("morpheme canonical form or meaning is invalid")
            if not re.fullmatch(r"[A-Za-z][A-Za-z -]*", meaning):
                raise ValueError("morpheme meaning is not a plain English phrase")
            if supplied_canonical.strip("-").casefold() != surface.casefold():
                raise ValueError("canonical form does not match its surface letters")
            canonical = _morpheme_display_form(surface, kind)
            normalizations: list[str] = []
            if canonical != supplied_canonical:
                normalizations.append("canonical-affix-notation")
            if meaning != re.sub(
                r"\s+", " ", str(item.get("meaning", "")).strip()
            ):
                normalizations.append("plain-meaning-phrase")
            selected = [
                str(evidence_id)
                for evidence_id in item.get("evidence_ids", [])
                if str(evidence_id) in allowed_evidence
            ] if isinstance(item.get("evidence_ids"), list) else []
            if confidence < 0.65:
                raise ValueError("morpheme confidence is below threshold")
            cleaned.append(
                {
                    "surface": surface,
                    "canonical_form": canonical,
                    "kind": kind,
                    "language": language,
                    "meaning": meaning,
                    "confidence": confidence,
                    "context_evidence_ids": list(dict.fromkeys(selected)),
                    "evidence_ids": [],
                    "normalizations": normalizations,
                }
            )
        if "".join(part["surface"] for part in cleaned).casefold() != str(
            source["text"]
        ).casefold():
            raise ValueError("morpheme surfaces do not reproduce the source term")
        if not any(part["kind"] in {"root", "free"} for part in cleaned):
            raise ValueError("lexical analysis has no root or free base after review")
        if required_shape:
            if [part["surface"].casefold() for part in cleaned] != [
                str(item["surface"]).casefold() for item in required_shape
            ]:
                raise ValueError("morpheme split changed the book-anchored surfaces")
            for part, required in zip(cleaned, required_shape, strict=True):
                if required.get("kind") and part["kind"] != required["kind"]:
                    raise ValueError("morpheme split changed a book-anchored kind")
                for evidence_id in required.get("evidence_ids", []):
                    if evidence_id not in part["evidence_ids"]:
                        part["evidence_ids"].append(evidence_id)

        for part in cleaned:
            direct = (
                self.retriever.component_evidence(
                    part["canonical_form"], part["kind"]
                )
                if part["kind"] in {"root", "prefix", "suffix"}
                else []
            )
            for record in direct:
                evidence_id = self.store.add_evidence(
                    str(record.get("corpus_id", "")),
                    str(record.get("entry_id", "")),
                    source_hash=str(record.get("source_hash", "")),
                    locator=str(record.get("locator", "")),
                    excerpt=str(record.get("excerpt", "")),
                    payload=record,
                )
                if evidence_id not in part["evidence_ids"]:
                    part["evidence_ids"].append(evidence_id)
            # A useful local model analysis is allowed to remain uncited. The
            # next task grounds historical claims in the exact Word Origins
            # entry; absence from a component dictionary is not evidence that
            # the linguistic analysis is wrong.

        accepted_parts: list[dict[str, Any]] = []
        for ordinal, part in enumerate(cleaned):
            basis = "book" if part["evidence_ids"] else "model"
            confidence = min(
                part["confidence"], 0.95 if basis == "book" else 0.8
            )
            morpheme_id = self.store.upsert_morpheme(
                part["language"],
                part["canonical_form"],
                part["kind"],
                part["meaning"],
                status="accepted",
                quality_score=confidence,
            )
            self.store.link_morpheme(
                source["entity_id"],
                morpheme_id,
                ordinal,
                part["surface"],
                basis=basis,
                confidence=confidence,
            )
            for evidence_id in part["evidence_ids"]:
                self.store.link_evidence(
                    morpheme_id,
                    evidence_id,
                    claim=f"{part['canonical_form']}: {part['meaning']}",
                    confidence=confidence,
                )
            accepted_parts.append(
                {
                    **part,
                    "morpheme_id": morpheme_id,
                    "ordinal": ordinal,
                    "basis": basis,
                    "confidence": confidence,
                }
            )
        accepted = {
            "term_id": source["entity_id"],
            "term": source["text"],
            "parts": accepted_parts,
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            source["entity_id"],
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="atomic morpheme split",
            accepted=True,
        )
        quality = min(part["confidence"] for part in accepted_parts)
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-morpheme-split",
            accepted,
            language=source["language"],
            validation_state="accepted",
            quality_score=quality,
        )

    def _expand_origin_branches(self, job: dict[str, Any]) -> str:
        source = self.store.term_record(str(job["subject_entity_id"]))
        splits = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-morpheme-split",
            validation_state="accepted",
        )
        if not splits:
            raise ValueError("accepted morpheme split is missing")
        parts = list(splits[-1]["payload"].get("parts", []))
        if not parts:
            raise ValueError("accepted morpheme split has no parts")

        source_origin_ids: set[str] = set()
        source_origin_records: list[dict[str, Any]] = []
        retrievals = self.store.artifacts_for_subject(
            job["subject_key"], stage="retrieved-evidence"
        )
        if retrievals:
            for record in retrievals[-1]["payload"].get("records", []):
                if isinstance(record, dict):
                    source_origin_records.append(record)
                if not _origin_source_record_matches(source["text"], record):
                    continue
                evidence_id = str(record.get("knowledge_evidence_id", ""))
                if evidence_id:
                    source_origin_ids.add(evidence_id)

        base_parts = [
            part for part in parts if str(part.get("kind", "")) in {"root", "free"}
        ]
        if not base_parts:
            raise ValueError("accepted lexical analysis has no root or free base")
        history_anchor = next(
            (
                part
                for part in base_parts
                if _plain_letter_key(part.get("surface", ""))
                == _plain_letter_key(source["text"])
            ),
            max(base_parts, key=lambda item: len(str(item.get("surface", "")))),
        )
        history_anchor_id = str(history_anchor.get("morpheme_id", ""))

        allowed_by_component: dict[str, set[str]] = {}
        context: list[dict[str, Any]] = []
        for part in parts:
            component_id = str(part.get("morpheme_id", ""))
            allowed = {
                str(item) for item in part.get("evidence_ids", []) if str(item)
            }
            origin_records = self.retriever.origin_evidence(
                str(part.get("canonical_form", ""))
            )
            if component_id == history_anchor_id:
                source_origin_records.extend(
                    record for record in origin_records if isinstance(record, dict)
                )
            for record in origin_records:
                evidence_id = self.store.add_evidence(
                    str(record.get("corpus_id", "")),
                    str(record.get("entry_id", "")),
                    source_hash=str(record.get("source_hash", "")),
                    locator=str(record.get("locator", "")),
                    excerpt=str(record.get("excerpt", "")),
                    payload=record,
                )
                allowed.add(evidence_id)
            if component_id == history_anchor_id:
                allowed.update(source_origin_ids)
            allowed_by_component[component_id] = allowed
            evidence = self.store.evidence_records(sorted(allowed))
            context.append(
                {
                    "component_id": component_id,
                    "form": part.get("canonical_form", ""),
                    "kind": part.get("kind", ""),
                    "meaning": part.get("meaning", ""),
                    "basis": part.get("basis", "model"),
                    "evidence": [
                        {
                            "evidence_id": record["evidence_id"],
                            "source": record["corpus_id"],
                            "locator": record["locator"],
                            "excerpt": str(record["excerpt"])[:760],
                        }
                        for record in evidence
                    ],
                }
            )

        if (
            str(history_anchor.get("kind", "")) == "free"
            and _plain_letter_key(history_anchor.get("canonical_form", ""))
            == _plain_letter_key(source["text"])
            and not _origin_source_evidence_supported(
                source["text"], source_origin_records
            )
        ):
            raise ValueError(
                "free-word origin lacks exact, cross-referenced, or explicit target evidence"
            )

        focus = next(
            (
                item
                for item in context
                if item["kind"] in {"root", "free"} and item["evidence"]
            ),
            next(
                (
                    item
                    for item in context
                    if item["component_id"] == history_anchor_id
                ),
                None,
            ),
        )
        if focus is None:
            raise ValueError("no lexical base is available for origin expansion")
        focus_evidence = self.store.evidence_records(
            sorted(allowed_by_component[str(focus["component_id"])])
        )
        fixed_provenance_ids = sorted(
            source_origin_ids
            & allowed_by_component[str(focus["component_id"])]
        )
        prompt_focus = focus
        if fixed_provenance_ids:
            prompt_focus = {
                **focus,
                "evidence": [
                    {
                        key: value
                        for key, value in record.items()
                        if key != "evidence_id"
                    }
                    for record in focus["evidence"]
                ],
            }
        evidence_instruction = (
            "evidence_ids: always an empty array; exact source provenance is attached "
            "by the system after validation"
            if fixed_provenance_ids
            else "evidence_ids: only evidence IDs under that exact component"
        )
        prompt = f"""ONE ORIGIN BRANCH
MODERN WORD: {source['text']}
LEXICAL BASE AND RETRIEVED WORD-ORIGIN EVIDENCE:
{json.dumps(prompt_focus, ensure_ascii=False)}

Return exactly one JSON object with `component_id` copied exactly and `steps`, an
array ordered oldest to newest. Use one to three historically useful steps. Each
step has:
form: concise attested or reconstructed historical form
language: ISO-style code such as la, fro, fr, grc, ine-pro, ang, or enm
period: concise era or language-stage label
meaning: at most 10 English words
confidence: number from 0 to 1
{evidence_instruction}

The final step develops into the lexical base or modern word. A step must never
repeat the modern word or the fixed base itself. Do not use Modern English as a
historical step, invent dates, or add a sibling component.
When exact source evidence is supplied, use only historical forms visibly printed
in that evidence and copy their spelling exactly.
Book evidence is authoritative. Model knowledge must use an empty evidence_ids
array. Stop a branch when another step is uncertain. Prefer a small accurate
graph over a decorative graph."""
        fixed_component_id = str(focus["component_id"])
        completion: dict[str, Any] | None = None
        book_steps = _book_origin_steps(focus_evidence)
        if book_steps:
            value: Any = {
                "component_id": fixed_component_id,
                "steps": book_steps,
            }
            self.store.save_job_artifact(
                job["job_id"],
                "book-origin-draft",
                {"term": source["text"], "value": value},
                language=source["language"],
                validation_state="candidate",
            )
        else:
            completion = self.model.complete_json(
                "You reason over RAG evidence to expand one bounded etymology branch.",
                prompt,
                max_tokens=384,
            )
            raw_value = completion.get("value")
            value, normalizations = _normalize_origin_draft(
                raw_value,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
            )
            review_reason = _origin_draft_review_reason(
                value,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
                fixed_provenance_ids=set(fixed_provenance_ids),
                evidence=focus_evidence,
            )
            if (
                not review_reason
                and fixed_provenance_ids
                and "removed-redundant-modern-endpoint" in normalizations
            ):
                review_reason = "the draft incorrectly used a Modern English endpoint"
            self.store.save_job_artifact(
                job["job_id"],
                "model-origin-draft",
                {
                    "term": source["text"],
                    "value": raw_value,
                    "model": completion.get("model", self.model.model_name),
                    "metrics": completion.get("metrics", {}),
                },
                language=source["language"],
                validation_state="superseded" if review_reason else "candidate",
            )
            if normalizations:
                self.store.save_job_artifact(
                    job["job_id"],
                    "normalized-origin-draft",
                    {
                        "term": source["text"],
                        "normalizations": normalizations,
                        "value": value,
                    },
                    language=source["language"],
                    validation_state="candidate",
                )
        if not book_steps and review_reason:
            review_prompt = f"""ONE ORIGIN BRANCH REVIEW
The first draft failed a structural or evidence check: {review_reason}.

MODERN WORD: {source['text']}
FIXED LEXICAL BASE AND EXACT RAG EVIDENCE:
{json.dumps(prompt_focus, ensure_ascii=False)}

REJECTED DRAFT:
{json.dumps(value, ensure_ascii=False)}

Return a corrected JSON object with exactly the fixed component_id and one to
three `steps` ordered oldest to newest. The last historical step develops into
the fixed base or modern word, but no step may repeat either of them. Do not use
Modern English. Use only historical forms printed verbatim in the exact RAG
evidence; {evidence_instruction}. Keep meanings under ten English words and
stop before an uncertain ancestor. Return JSON only."""
            completion = self.model.complete_json(
                "You independently review one failed RAG-grounded etymology branch.",
                review_prompt,
                max_tokens=448,
            )
            reviewed_raw = completion.get("value")
            reviewed_value, review_normalizations = _normalize_origin_draft(
                reviewed_raw,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
            )
            reviewed_reason = _origin_draft_review_reason(
                reviewed_value,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
                fixed_provenance_ids=set(fixed_provenance_ids),
                evidence=focus_evidence,
            )
            self.store.save_job_artifact(
                job["job_id"],
                "model-origin-review-draft",
                {
                    "term": source["text"],
                    "review_reason": review_reason,
                    "value": reviewed_raw,
                    "model": completion.get("model", self.model.model_name),
                    "metrics": completion.get("metrics", {}),
                },
                language=source["language"],
                validation_state="candidate" if not reviewed_reason else "rejected",
            )
            if review_normalizations:
                self.store.save_job_artifact(
                    job["job_id"],
                    "normalized-origin-draft",
                    {
                        "term": source["text"],
                        "normalizations": review_normalizations,
                        "value": reviewed_value,
                    },
                    language=source["language"],
                    validation_state=(
                        "candidate" if not reviewed_reason else "rejected"
                    ),
                )
            # Never replace a repairable first draft with a structurally worse
            # reviewer response. A remaining substantive error still consumes
            # only the normal bounded job retry.
            if not reviewed_reason:
                value = reviewed_value
        if not isinstance(value, dict):
            raise ValueError("origin task did not return an object")
        raw_by_component = {str(focus["component_id"]): value}

        cleaned_branches: list[dict[str, Any]] = []
        total_steps = 0
        base_has_history = False
        for part in parts:
            component_id = str(part["morpheme_id"])
            branch = raw_by_component.get(component_id, {})
            raw_steps = branch.get("steps", []) if isinstance(branch, dict) else []
            if not isinstance(raw_steps, list) or len(raw_steps) > 3:
                raise ValueError("an origin branch has too many steps")
            steps: list[dict[str, Any]] = []
            seen_forms: set[tuple[str, str, str]] = set()
            for raw in raw_steps:
                if not isinstance(raw, dict):
                    raise ValueError("origin step is not an object")
                form = re.sub(r"\s+", " ", str(raw.get("form", ""))).strip()
                period = re.sub(r"\s+", " ", str(raw.get("period", ""))).strip()
                meaning = _clean_morpheme_meaning(raw.get("meaning", ""))
                supplied_language = re.sub(
                    r"\s+", " ", str(raw.get("language", ""))
                ).strip().casefold()
                language = _ORIGIN_LANGUAGE_CODES.get(
                    supplied_language, supplied_language
                )
                if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", language):
                    raise ValueError("origin step has an invalid language code")
                if not form or len(form) > 90 or not period or len(period) > 80:
                    raise ValueError("origin form or period is missing or too long")
                if _plain_letter_key(form) in {
                    _plain_letter_key(source["text"]),
                    _plain_letter_key(part["canonical_form"]),
                }:
                    raise ValueError("origin step repeats the modern word or component")
                if language == "en" or "modern english" in period.casefold():
                    raise ValueError("origin step incorrectly uses Modern English")
                if not meaning or len(meaning.split()) > 10:
                    raise ValueError("origin meaning is missing or too long")
                if any(
                    marker in text
                    for marker in _ENCODING_DAMAGE
                    for text in (form, period, meaning)
                ):
                    raise ValueError("origin step contains encoding damage")
                confidence = max(
                    0.0, min(float(raw.get("confidence", 0.0)), 1.0)
                )
                if confidence < 0.65:
                    raise ValueError("origin confidence is below threshold")
                if fixed_provenance_ids and component_id == focus["component_id"]:
                    selected = _explicit_form_evidence_ids(form, focus_evidence)
                    selected = [
                        evidence_id
                        for evidence_id in selected
                        if evidence_id in fixed_provenance_ids
                    ]
                    if not selected:
                        raise ValueError(
                            "origin step is not explicit in exact source evidence"
                        )
                else:
                    selected = (
                        list(
                            dict.fromkeys(
                                str(item)
                                for item in raw.get("evidence_ids", [])
                                if str(item) in allowed_by_component[component_id]
                            )
                        )
                        if isinstance(raw.get("evidence_ids"), list)
                        else []
                    )
                basis = "book" if selected else "model"
                confidence = min(confidence, 0.95 if basis == "book" else 0.75)
                key = (language, form.casefold(), period.casefold())
                if key in seen_forms:
                    continue
                seen_forms.add(key)
                steps.append(
                    {
                        "form": form,
                        "language": language,
                        "period": period,
                        "meaning": meaning,
                        "confidence": confidence,
                        "basis": basis,
                        "evidence_ids": selected,
                    }
                )
            total_steps += len(steps)
            base_has_history = base_has_history or (
                part.get("kind") in {"root", "free"} and bool(steps)
            )
            cleaned_branches.append(
                {
                    "component_id": component_id,
                    "component_form": part["canonical_form"],
                    "component_kind": part["kind"],
                    "steps": steps,
                }
            )
        if total_steps < 1 or total_steps > 5 or not base_has_history:
            raise ValueError("origin task did not establish a bounded lexical history")

        prior_origins = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-origin-branches",
            validation_state="accepted",
        )
        if prior_origins:
            self.store.retire_origin_analysis(
                source["entity_id"],
                "superseded by a newly validated bounded origin branch",
            )

        origin_model, origin_metrics = _origin_generation_metadata(
            completion, self.model.model_name
        )
        accepted_steps: list[dict[str, Any]] = []
        for branch in cleaned_branches:
            later_id = str(branch["component_id"])
            for step in reversed(branch["steps"]):
                historical_id = self.store.add_historical_form(
                    step["language"],
                    step["form"],
                    period_label=step["period"],
                    meaning=step["meaning"],
                    status="accepted",
                    quality_score=step["confidence"],
                )
                for evidence_id in step["evidence_ids"]:
                    self.store.link_evidence(
                        historical_id,
                        evidence_id,
                        claim=f"{step['form']}: {step['meaning']}",
                        confidence=step["confidence"],
                    )
                self.store.add_edge(
                    historical_id,
                    later_id,
                    "developed-into",
                    basis=step["basis"],
                    confidence=step["confidence"],
                    properties={
                        "component_id": branch["component_id"],
                        "term_id": source["entity_id"],
                    },
                )
                self.store.record_revision(
                    historical_id,
                    step,
                    model=origin_model,
                    prompt_version=str(job.get("prompt_version", "")),
                    reason="atomic origin branch expansion",
                    accepted=True,
                )
                step["historical_form_id"] = historical_id
                later_id = historical_id
                accepted_steps.append(step)

        quality = min(step["confidence"] for step in accepted_steps)
        accepted = {
            "term_id": source["entity_id"],
            "term": source["text"],
            "branches": cleaned_branches,
            "model": origin_model,
            "metrics": origin_metrics,
        }
        self.store.record_revision(
            source["entity_id"],
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="book-grounded lexical origin expansion",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-origin-branches",
            accepted,
            language=source["language"],
            validation_state="accepted",
            quality_score=quality,
        )

    def _extract_investigation_terms(self, job: dict[str, Any]) -> str:
        source = self.store.content_record(str(job["subject_entity_id"]))
        if source["language"] != "en" or source["kind"] not in {"answer", "question"}:
            raise ValueError("investigation extraction requires English Answer/Question content")
        evidence = self.store.evidence_for_entity(source["entity_id"])
        if not evidence:
            raise ValueError("reviewed content evidence is missing")
        words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", source["text"])
        by_normalized: dict[str, str] = {}
        for word in words:
            by_normalized.setdefault(word.casefold(), word)
        prompt = f"""REVIEWED {source['kind'].upper()} TEXT:
{source['text']}

Return exactly one JSON object with `terms`, an array of one to three useful
English vocabulary items. Each item has:
surface: one complete word copied exactly from the reviewed text
note: why it is worth investigating, at most 10 English words
confidence: number from 0 to 1

Choose meaningful content words, not names, numbers, auxiliaries, determiners,
or generic glue words. Do not change an inflected form, invent a lemma, explain
the sentence, add translations, or include markdown."""
        completion = self.model.complete_json(
            "You select a few reusable words from fixed reviewed text.",
            prompt,
            max_tokens=192,
        )
        value = completion.get("value")
        self.store.save_job_artifact(
            job["job_id"],
            "model-investigation-draft",
            {
                "source_entity_id": source["entity_id"],
                "value": value,
                "model": completion.get("model", self.model.model_name),
                "metrics": completion.get("metrics", {}),
            },
            language="en",
            validation_state="candidate",
        )
        raw_terms = value.get("terms") if isinstance(value, dict) else None
        if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= 3:
            raise ValueError("investigation task returned an invalid number of terms")

        cleaned: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in raw_terms:
            if not isinstance(raw, dict):
                rejected.append({"surface": "", "reason": "not an object"})
                continue
            requested = str(raw.get("surface", "")).strip()
            normalized = requested.casefold()
            surface = by_normalized.get(normalized, "")
            if not surface:
                rejected.append({"surface": requested, "reason": "absent from source text"})
                continue
            if normalized in _INVESTIGATION_STOPWORDS or len(normalized) < 4:
                rejected.append({"surface": requested, "reason": "too generic"})
                continue
            if normalized in seen:
                rejected.append({"surface": requested, "reason": "duplicate"})
                continue
            note = _clean_usage_note(raw.get("note", ""))
            if not note or len(note.split()) > 10:
                rejected.append({"surface": requested, "reason": "invalid note"})
                continue
            try:
                confidence = max(0.0, min(float(raw.get("confidence", 0.0)), 0.75))
            except (TypeError, ValueError):
                rejected.append({"surface": requested, "reason": "invalid confidence"})
                continue
            if confidence < 0.55:
                rejected.append({"surface": requested, "reason": "low confidence"})
                continue
            seen.add(normalized)
            cleaned.append(
                {
                    "surface": surface,
                    "term": normalized,
                    "note": note,
                    "confidence": confidence,
                }
            )
        if not cleaned:
            raise ValueError("investigation task produced no distinct terms")

        evidence_ids = [str(item["evidence_id"]) for item in evidence]
        accepted_terms: list[dict[str, Any]] = []
        for ordinal, item in enumerate(cleaned):
            term_id = self.store.upsert_term(
                "en",
                item["term"],
                status="accepted",
                quality_score=item["confidence"],
            )
            self.store.add_edge(
                source["entity_id"],
                term_id,
                "contains-investigation-term",
                basis="model",
                confidence=item["confidence"],
                properties={
                    "ordinal": ordinal,
                    "surface": item["surface"],
                    "note": item["note"],
                    "selection_basis": "bounded-model-selection",
                    "source_key": source["source_key"],
                },
            )
            for evidence_id in evidence_ids:
                self.store.link_evidence(
                    term_id,
                    evidence_id,
                    claim=f"appears in reviewed {source['kind']} text as {item['surface']!r}",
                    confidence=1.0,
                )
            accepted_terms.append({**item, "term_id": term_id, "ordinal": ordinal})

        accepted = {
            "source_entity_id": source["entity_id"],
            "source_key": source["source_key"],
            "kind": source["kind"],
            "terms": accepted_terms,
            "rejected_terms": rejected,
            "evidence_ids": evidence_ids,
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            source["entity_id"],
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="bounded investigation-term extraction",
            accepted=True,
        )
        quality = min(item["confidence"] for item in accepted_terms)
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-investigation-terms",
            accepted,
            language="en",
            validation_state="accepted",
            quality_score=quality,
        )

    def _prepare_grammar_parts(self, job: dict[str, Any]) -> str:
        source = self.store.content_record(str(job["subject_entity_id"]))
        language = str(job.get("language", ""))
        if language not in _CONTENT_LANGUAGE_NAMES or source["language"] != language:
            raise ValueError("grammar task language does not match reviewed content")
        if source["status"] != "accepted":
            raise ValueError("grammar task requires accepted reviewed content")
        if source["kind"] not in {"answer", "question"}:
            raise ValueError("grammar task requires reviewed Answer/Question content")
        evidence = self.store.evidence_for_entity(source["entity_id"])
        if not evidence:
            raise ValueError("reviewed content evidence is missing")

        prompt = f"""LANGUAGE: {_CONTENT_LANGUAGE_NAMES[language]}
REVIEWED {source['kind'].upper()} TEXT:
{source['text']}

Return exactly one JSON object with:
summary: one short description of the sentence pattern, at most 14 words
parts: one to eight contiguous grammatical phrases covering the reviewed text

Every part has:
surface: exact consecutive text copied from the reviewed text, including nearby punctuation
lemma: a short base form when useful, otherwise an empty string
role: exactly one of subject, predicate, object, modifier, connector, clause, other
part_of_speech: exactly one of noun, verb, adjective, adverb, pronoun,
preposition, conjunction, interjection, determiner, numeral, auxiliary,
particle, phrase, clause, punctuation, other
confidence: number from 0 to 1

Preserve every character once and in order. Use a few meaningful phrases, not
one part per character. Do not translate, rewrite, explain, add markdown, or
return text outside the JSON object. The top-level object must contain
`summary` and `parts`; never return one bare part. Shape:
{{"summary":"...","parts":[{{"surface":"...","lemma":"","role":"other","part_of_speech":"phrase","confidence":0.8}}]}}"""
        completion = self.model.complete_json(
            "You segment fixed reviewed text into a few exact grammar phrases.",
            prompt,
            max_tokens=384,
        )
        value = completion.get("value")
        self.store.save_job_artifact(
            job["job_id"],
            "model-grammar-draft",
            {
                "source_entity_id": source["entity_id"],
                "value": value,
                "model": completion.get("model", self.model.model_name),
                "metrics": completion.get("metrics", {}),
            },
            language=language,
            validation_state="candidate",
        )
        aligned = _align_grammar_draft(source["text"], value)

        parts: list[dict[str, Any]] = []
        for item in aligned:
            model_role = (
                str(item.get("role", "")).strip().casefold().replace(" ", "-")
            )
            model_part_of_speech = (
                str(item.get("part_of_speech", ""))
                .strip()
                .casefold()
                .replace(" ", "-")
            )
            lemma = re.sub(r"\s+", " ", str(item.get("lemma", ""))).strip()[:80]
            if model_part_of_speech not in _GRAMMAR_PARTS_OF_SPEECH:
                raise ValueError("grammar part of speech is invalid")
            role, part_of_speech = _normalise_grammar_labels(
                model_role if model_role in _GRAMMAR_ROLES else "",
                model_part_of_speech,
                str(item["surface"]),
            )
            if not _grammar_role_matches(role, part_of_speech, str(item["surface"])):
                raise ValueError("grammar role contradicts its part of speech")
            if any(marker in lemma for marker in _ENCODING_DAMAGE):
                raise ValueError("grammar lemma has encoding damage")
            try:
                confidence = max(
                    0.0, min(float(item.get("confidence", 0.0)), 0.75)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("grammar confidence is invalid") from exc
            if confidence < 0.55:
                raise ValueError("grammar confidence is below acceptance threshold")
            parts.append(
                {
                    "surface": str(item["surface"]),
                    "lemma": lemma,
                    "role": role,
                    "part_of_speech": part_of_speech,
                    "reading": "",
                    "color_key": _GRAMMAR_COLORS[role],
                    "confidence": confidence,
                    "features": {
                        "basis": "bounded-model-segmentation",
                        "model_role": model_role,
                        "model_part_of_speech": model_part_of_speech,
                        "role_normalized": model_role != role,
                    },
                }
            )
        summary = re.sub(
            r"\s+", " ", str(value.get("summary", "")) if isinstance(value, dict) else ""
        ).strip()[:180]
        if not summary and len(parts) == 1:
            summary = f"Single {parts[0]['part_of_speech']} expression"
        if not summary or any(marker in summary for marker in _ENCODING_DAMAGE):
            raise ValueError("grammar summary is invalid")
        if language == "en" and len(summary.split()) > 14:
            raise ValueError("grammar summary is too long")

        quality = min(part["confidence"] for part in parts)
        analysis_id = self.store.add_grammar_analysis(
            source["entity_id"],
            language,
            summary,
            parts,
            basis="model",
            status="accepted",
            quality_score=quality,
        )
        evidence_ids = [str(item["evidence_id"]) for item in evidence]
        for evidence_id in evidence_ids:
            self.store.link_evidence(
                analysis_id,
                evidence_id,
                claim=f"grammar segmentation of reviewed {source['kind']} text",
                confidence=quality,
            )
        accepted = {
            "analysis_id": analysis_id,
            "source_entity_id": source["entity_id"],
            "source_key": source["source_key"],
            "kind": source["kind"],
            "language": language,
            "summary": summary,
            "parts": parts,
            "evidence_ids": evidence_ids,
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            analysis_id,
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="bounded reviewed-content grammar segmentation",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-grammar-parts",
            accepted,
            language=language,
            validation_state="accepted",
            quality_score=quality,
        )

    def _prepare_pronunciation(self, job: dict[str, Any]) -> str:
        language = str(job.get("language", ""))
        if language not in {"en", *_LANGUAGE_NAMES}:
            raise ValueError(f"unsupported pronunciation language: {language}")
        source = self.store.term_record(str(job["subject_entity_id"]))
        evidence_ids: list[str] = []
        if language == source["language"]:
            target_term_id = source["entity_id"]
            visible_term = str(source["text"])
            meanings = self.store.artifacts_for_subject(
                job["subject_key"],
                stage="accepted-meaning",
                validation_state="accepted",
            )
            if not meanings:
                raise ValueError("accepted meaning checkpoint is missing")
            evidence_ids = [str(item) for item in meanings[-1]["payload"]["evidence_ids"]]
            translation: dict[str, Any] = {}
        else:
            translations = [
                artifact
                for artifact in self.store.artifacts_for_subject(
                    job["subject_key"],
                    stage="accepted-translation",
                    validation_state="accepted",
                )
                if artifact["language"] == language
            ]
            if not translations:
                raise ValueError(f"accepted {language} translation checkpoint is missing")
            translation = translations[-1]["payload"]
            target_term_id = str(translation["target_term_id"])
            visible_term = str(translation["term"])
            evidence_ids = [str(item) for item in translation.get("evidence_ids", [])]

        method: dict[str, Any]
        revision_model = "deterministic"
        if language == "zh":
            reading = chinese_pinyin(visible_term, str(translation.get("reading", "")))
            ruby = chinese_ruby_tokens(visible_term)
            segments = [
                {
                    "grapheme": token["t"],
                    "phoneme": token["r"],
                    "color_key": f"p{index % 6}",
                    "features": {"ruby": True},
                }
                for index, token in enumerate(ruby)
                if token.get("r")
            ]
            system, dialect, confidence = "pinyin", "Mandarin", 1.0
            method = {"engine": "pypinyin", "basis": "accepted translation"}
        elif language == "ja":
            supplied_reading = str(translation.get("reading", "")).strip()
            candidates: list[dict[str, Any]] = []
            if self.japanese_readings is not None:
                try:
                    candidates = self.japanese_readings.lookup(visible_term)
                except (FileNotFoundError, OSError, sqlite3.Error):
                    candidates = []
            allowed_readings = list(
                dict.fromkeys(str(item["reading"]) for item in candidates)
            )
            selected_by = "accepted-translation"
            if supplied_reading in allowed_readings:
                reading = supplied_reading
                selected_by = "exact-dictionary-match"
            elif len(allowed_readings) == 1:
                reading = allowed_readings[0]
                selected_by = "unique-dictionary-reading"
            elif allowed_readings:
                meanings = self.store.artifacts_for_subject(
                    job["subject_key"],
                    stage="accepted-meaning",
                    validation_state="accepted",
                )
                english_sense = (
                    str(meanings[-1]["payload"].get("definition", ""))
                    if meanings
                    else ""
                )
                review_prompt = f"""JAPANESE READING REVIEW
SOURCE ENGLISH WORD: {source['text']}
ACCEPTED ENGLISH SENSE: {english_sense}
JAPANESE FORM: {visible_term}
JAPANESE MEANING: {translation.get('meaning', '')}
CURRENT UNVERIFIED READING: {supplied_reading}
EXACT JMDICT CANDIDATES:
{json.dumps(candidates, ensure_ascii=False)}

Choose the one exact `reading` whose JMdict gloss best matches the accepted
sense. Return only {{"reading":"one supplied candidate"}}. Never create a new
reading and do not rewrite the Japanese form."""
                completion = self.model.complete_json(
                    "You select one exact dictionary reading for a fixed Japanese form.",
                    review_prompt,
                    max_tokens=64,
                )
                value = completion.get("value")
                reading = (
                    str(value.get("reading", "")).strip()
                    if isinstance(value, dict)
                    else ""
                )
                self.store.save_job_artifact(
                    job["job_id"],
                    "model-japanese-reading-review",
                    {
                        "term": visible_term,
                        "supplied_reading": supplied_reading,
                        "allowed_readings": allowed_readings,
                        "value": value,
                        "model": completion.get("model", self.model.model_name),
                        "metrics": completion.get("metrics", {}),
                    },
                    language="ja",
                    validation_state="candidate",
                )
                if reading not in allowed_readings:
                    raise ValueError("Japanese reading review left the JMdict candidates")
                selected_by = "sense-aligned-local-model-selection"
                revision_model = str(
                    completion.get("model", self.model.model_name)
                )
            else:
                reading = supplied_reading

            selected_records = [
                item for item in candidates if str(item["reading"]) == reading
            ]
            for record in selected_records:
                gloss = "; ".join(str(item) for item in record.get("glosses", [])[:4])
                jmdict_evidence = self.store.add_evidence(
                    str(record["corpus_id"]),
                    str(record["entry_id"]),
                    source_hash=str(record.get("source_hash", "")),
                    locator=str(record.get("locator", "")),
                    excerpt=f"{visible_term}【{reading}】 {gloss}".strip(),
                    payload=record,
                )
                evidence_ids.append(jmdict_evidence)
            segments = [
                {
                    "grapheme": visible_term,
                    "phoneme": reading,
                    "color_key": "p0",
                    "features": {"ruby": True},
                }
            ]
            system, dialect = "kana", "standard"
            if selected_records:
                confidence = (
                    0.95
                    if selected_by == "sense-aligned-local-model-selection"
                    else 1.0
                )
            else:
                confidence = min(float(translation.get("confidence", 0.8)), 0.7)
            method = {
                "engine": "JMdict" if selected_records else "accepted translation",
                "basis": "exact local dictionary" if selected_records else "unverified model reading",
                "selection": selected_by,
                "candidate_count": len(allowed_readings),
                **(
                    {
                        "release": self.japanese_readings.metadata().get("release", ""),
                        "source_sha256": self.japanese_readings.metadata().get(
                            "source_sha256", ""
                        ),
                    }
                    if selected_records and self.japanese_readings is not None
                    else {}
                ),
            }
        else:
            generated = self.pronouncer.pronounce(visible_term, language)
            reading = str(generated["reading"])
            segments = list(generated.get("segments", []))
            system = str(generated.get("system", "ipa"))
            dialect = str(generated.get("dialect", ""))
            confidence = 0.85 if language == "ar" else 0.9
            method = dict(generated.get("source", {}))
            engine_evidence = self.store.add_evidence(
                f"espeak-ng:{method.get('version', 'local')}",
                f"{dialect}:{visible_term}",
                source_hash=str(method.get("version", "")),
                locator="local deterministic IPA",
                excerpt=reading,
                payload={**method, "term": visible_term, "reading": reading},
            )
            evidence_ids.append(engine_evidence)

        if not reading or not segments:
            raise ValueError("pronunciation reading or aligned segments are missing")
        pronunciation_id = self.store.add_pronunciation(
            target_term_id,
            language,
            system,
            reading,
            segments,
            dialect=dialect,
            status="accepted",
            quality_score=confidence,
        )
        for evidence_id in dict.fromkeys(evidence_ids):
            self.store.link_evidence(
                pronunciation_id,
                evidence_id,
                claim=f"{visible_term} pronunciation",
                confidence=confidence,
            )
        accepted = {
            "pronunciation_id": pronunciation_id,
            "target_term_id": target_term_id,
            "language": language,
            "term": visible_term,
            "system": system,
            "reading": reading,
            "dialect": dialect,
            "segments": segments,
            "method": method,
            "confidence": confidence,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }
        self.store.record_revision(
            pronunciation_id,
            accepted,
            model=revision_model,
            prompt_version=str(job.get("prompt_version", "")),
            reason=f"atomic {language} pronunciation",
            accepted=True,
        )
        artifact_id = self.store.save_job_artifact(
            job["job_id"],
            "accepted-pronunciation",
            accepted,
            language=language,
            validation_state="accepted",
            quality_score=confidence,
        )
        if language == "ja" and method.get("engine") == "JMdict":
            self.store.supersede_pronunciation_artifacts(
                job["subject_key"], language, artifact_id
            )
        return artifact_id

    def _prepare_grammar_properties(self, job: dict[str, Any]) -> str:
        source = self.store.term_record(str(job["subject_entity_id"]))
        meanings = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-meaning",
            validation_state="accepted",
        )
        if not meanings:
            raise ValueError("accepted meaning checkpoint is missing")
        meaning = meanings[-1]["payload"]
        part_of_speech = str(meaning.get("part_of_speech", "")).strip()
        if part_of_speech not in _PARTS_OF_SPEECH:
            raise ValueError("accepted meaning has no controlled part of speech")
        confidence = float(meaning.get("confidence", 0.0))
        parts = [
            {
                "surface": source["text"],
                "lemma": source["text"],
                "role": "headword",
                "part_of_speech": part_of_speech,
                "color_key": f"grammar-{part_of_speech}",
                "features": {"meaning_id": meaning["meaning_id"]},
            }
        ]
        analysis_id = self.store.add_grammar_analysis(
            source["entity_id"],
            source["language"],
            part_of_speech,
            parts,
            analysis_type="word",
            status="accepted",
            quality_score=confidence,
        )
        evidence_ids = [str(item) for item in meaning.get("evidence_ids", [])]
        for evidence_id in evidence_ids:
            self.store.link_evidence(
                analysis_id,
                evidence_id,
                claim=f"{source['text']} part of speech: {part_of_speech}",
                confidence=confidence,
            )
        accepted = {
            "analysis_id": analysis_id,
            "term_id": source["entity_id"],
            "language": source["language"],
            "term": source["text"],
            "part_of_speech": part_of_speech,
            "parts": parts,
            "confidence": confidence,
            "evidence_ids": evidence_ids,
        }
        self.store.record_revision(
            analysis_id,
            accepted,
            model="deterministic",
            prompt_version=str(job.get("prompt_version", "")),
            reason="atomic word grammar properties",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-grammar-properties",
            accepted,
            language=source["language"],
            validation_state="accepted",
            quality_score=confidence,
        )

    def _card_evidence(
        self, source: dict[str, Any], evidence_ids: list[str]
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        for record in self.store.evidence_records(evidence_ids):
            payload = record["payload"]
            page_values = payload.get("pages", [])
            pages = (
                tuple(
                    int(page)
                    for page in page_values
                    if isinstance(page, int) or str(page).isdigit()
                )
                if isinstance(page_values, list)
                else ()
            )
            evidence.append(
                Evidence(
                    entry_id=str(
                        payload.get("entry_id") or record["source_entry_id"]
                    ),
                    headword=str(payload.get("headword") or source["text"]),
                    section=str(payload.get("section", "")),
                    date_label=str(payload.get("date_label", "")),
                    pages=pages,
                    excerpt=str(record["excerpt"]),
                    corpus_id=str(record["corpus_id"]),
                    source_title=str(
                        payload.get("source_title", record["corpus_id"])
                    ),
                    kind=str(payload.get("kind", "evidence")),
                    locator=str(record["locator"]),
                    translations=(
                        dict(payload["translations"])
                        if isinstance(payload.get("translations"), dict)
                        else {}
                    ),
                )
            )
        return evidence

    def _compose_word_card(self, job: dict[str, Any]) -> str:
        if self.card_store is None:
            raise RuntimeError("card store is unavailable")
        source = self.store.term_record(str(job["subject_entity_id"]))
        artifacts = self.store.artifacts_for_subject(
            job["subject_key"], validation_state="accepted"
        )
        meanings = [item for item in artifacts if item["stage"] == "accepted-meaning"]
        grammar = [
            item for item in artifacts if item["stage"] == "accepted-grammar-properties"
        ]
        translations = {
            item["language"]: item
            for item in artifacts
            if item["stage"] == "accepted-translation"
        }
        pronunciations = {
            item["language"]: item
            for item in artifacts
            if item["stage"] == "accepted-pronunciation"
        }
        required_translations = {"ja", "zh", "fr", "ar"}
        required_pronunciations = {"en", "ja", "zh", "fr", "ar"}
        if not meanings or not grammar:
            raise ValueError("accepted meaning or grammar checkpoint is missing")
        if not required_translations.issubset(translations):
            raise ValueError("one or more accepted translations are missing")
        if not required_pronunciations.issubset(pronunciations):
            raise ValueError("one or more accepted pronunciations are missing")

        meaning = meanings[-1]
        grammar_value = grammar[-1]["payload"]
        translation_values = {
            language: translations[language]["payload"]
            for language in required_translations
        }
        pronunciation_values = {
            language: pronunciations[language]["payload"]
            for language in required_pronunciations
        }
        evidence_ids = [str(item) for item in meaning["payload"]["evidence_ids"]]
        evidence = self._card_evidence(source, evidence_ids)
        if not evidence:
            raise ValueError("accepted meaning evidence could not be reconstructed")

        def ruby(language: str) -> list[dict[str, str]]:
            return [
                {"t": str(segment["grapheme"]), "r": str(segment["phoneme"])}
                for segment in pronunciation_values[language].get("segments", [])
                if str(segment.get("grapheme", "")) and str(segment.get("phoneme", ""))
            ]

        quality_values = [
            _artifact_quality(meaning),
            _artifact_quality(grammar[-1]),
            *(_artifact_quality(item) for item in translations.values()),
            *(_artifact_quality(item) for item in pronunciations.values()),
        ]
        quality = min(quality_values)
        definition = str(meaning["payload"]["definition"])
        japanese = translation_values["ja"]
        chinese = translation_values["zh"]
        french = translation_values["fr"]
        arabic = translation_values["ar"]
        card = Card(
            card_id=str(uuid.uuid4()),
            mode="knowledge",
            query=str(source["text"]),
            title=str(source["text"]),
            subtitle=str(grammar_value["part_of_speech"]).upper(),
            summary_en="",
            origin_story="",
            key_points=[],
            english={
                "term": str(source["text"]),
                "pronunciation": str(pronunciation_values["en"]["reading"]),
                "meaning": definition,
            },
            japanese={
                "term": str(japanese["term"]),
                "reading": str(pronunciation_values["ja"]["reading"]),
                "meaning": str(japanese["meaning"]),
                "ruby_tokens": ruby("ja"),
            },
            chinese={
                "simplified": str(chinese["term"]),
                "traditional": "",
                "pinyin": str(pronunciation_values["zh"]["reading"]),
                "meaning": str(chinese["meaning"]),
                "ruby_tokens": ruby("zh"),
            },
            memory_hook="",
            related_terms=[],
            evidence=evidence,
            model=str(job.get("model", "local atomic pipeline")),
            created_at=datetime.now(UTC).isoformat(),
            extensions={
                "experience": "knowledge",
                "knowledge_policy": "accepted-atomic-view",
                "knowledge_subject": job["subject_key"],
                "knowledge_artifact_ids": [
                    str(item["artifact_id"])
                    for item in artifacts
                    if item["stage"] in {
                        "accepted-meaning",
                        "accepted-translation",
                        "accepted-pronunciation",
                        "accepted-grammar-properties",
                    }
                ],
                "evidence_ids": evidence_ids,
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
            },
            extra_languages={
                "french": {
                    "term": str(french["term"]),
                    "pronunciation": str(pronunciation_values["fr"]["reading"]),
                    "meaning": str(french["meaning"]),
                },
                "arabic": {
                    "term": str(arabic["term"]),
                    "reading": str(pronunciation_values["ar"]["reading"]),
                    "meaning": str(arabic["meaning"]),
                },
            },
        )
        self.card_store.save(card)
        self.card_store.publish(
            card.card_id,
            quality_score=quality,
            review_note="composed only from accepted atomic knowledge",
        )
        self.card_store.supersede_others(card.mode, card.query, card.card_id)
        accepted = {
            "card_id": card.card_id,
            "mode": card.mode,
            "quality": quality,
            "knowledge_artifact_ids": card.extensions["knowledge_artifact_ids"],
            "card": card.to_dict(),
        }
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-word-card",
            accepted,
            language="en",
            validation_state="accepted",
            quality_score=quality,
        )

    def _compose_origin_card(self, job: dict[str, Any]) -> str:
        if self.card_store is None:
            raise RuntimeError("card store is unavailable")
        source = self.store.term_record(str(job["subject_entity_id"]))
        artifacts = self.store.artifacts_for_subject(
            job["subject_key"], validation_state="accepted"
        )

        def latest(stage: str, language: str = "") -> dict[str, Any]:
            matches = [
                item
                for item in artifacts
                if item["stage"] == stage
                and (not language or item["language"] == language)
            ]
            if not matches:
                label = f" {language}" if language else ""
                raise ValueError(f"accepted {stage}{label} checkpoint is missing")
            return matches[-1]

        meaning = latest("accepted-meaning")
        split = latest("accepted-morpheme-split")
        origin = latest("accepted-origin-branches")
        translations = {
            language: latest("accepted-translation", language)["payload"]
            for language in ("ja", "zh", "fr", "ar")
        }
        pronunciations = {
            language: latest("accepted-pronunciation", language)["payload"]
            for language in ("en", "ja", "zh", "fr", "ar")
        }
        parts = list(split["payload"].get("parts", []))
        branches = list(origin["payload"].get("branches", []))
        if not parts or not branches:
            raise ValueError("accepted origin structure is empty")

        meaning_evidence = [
            str(item) for item in meaning["payload"].get("evidence_ids", [])
        ]
        component_evidence = [
            str(evidence_id)
            for part in parts
            for evidence_id in part.get("evidence_ids", [])
        ]
        history_evidence = [
            str(evidence_id)
            for branch in branches
            for step in branch.get("steps", [])
            for evidence_id in step.get("evidence_ids", [])
        ]
        evidence_ids = list(
            dict.fromkeys([*history_evidence, *component_evidence, *meaning_evidence])
        )
        evidence = self._card_evidence(source, evidence_ids)
        if not evidence:
            raise ValueError("accepted origin evidence could not be reconstructed")

        center_id = str(source["entity_id"])
        graph_nodes: list[dict[str, Any]] = [
            {
                "id": center_id,
                "type": "word",
                "form": str(source["text"]),
                "meaning": str(meaning["payload"]["definition"]),
                "language": "English",
                "history": "Modern word",
                "basis": "book",
                "evidence_ids": meaning_evidence,
                "confidence": "high",
            }
        ]
        graph_edges: list[dict[str, str]] = []
        node_ids = {center_id}
        parts_by_id = {str(part["morpheme_id"]): part for part in parts}
        for part in parts:
            part_id = str(part["morpheme_id"])
            node_ids.add(part_id)
            graph_nodes.append(
                {
                    "id": part_id,
                    "type": str(part["kind"]),
                    "form": str(part["canonical_form"]),
                    "meaning": str(part["meaning"]),
                    "language": str(part["language"]),
                    "history": "Fixed word component",
                    "basis": str(part["basis"]),
                    "evidence_ids": list(part.get("evidence_ids", [])),
                    "confidence": (
                        "high" if float(part.get("confidence", 0)) >= 0.85 else "medium"
                    ),
                }
            )
            graph_edges.append(
                {
                    "source": part_id,
                    "target": center_id,
                    "relationship": f"{part['kind']}-of",
                }
            )

        root_focus_areas: list[dict[str, Any]] = []
        root_headlines: list[str] = []
        for branch in branches:
            component_id = str(branch["component_id"])
            steps = list(branch.get("steps", []))
            branch_ids: list[str] = []
            for step in steps:
                historical_id = str(step.get("historical_form_id", ""))
                if not historical_id or historical_id in node_ids:
                    continue
                node_ids.add(historical_id)
                branch_ids.append(historical_id)
                graph_nodes.append(
                    {
                        "id": historical_id,
                        "type": "historical",
                        "form": str(step["form"]),
                        "meaning": str(step["meaning"]),
                        "language": str(step["period"]),
                        "history": f"Earlier form in {step['period']}",
                        "basis": str(step["basis"]),
                        "evidence_ids": list(step.get("evidence_ids", [])),
                        "confidence": (
                            "high"
                            if float(step.get("confidence", 0)) >= 0.85
                            else "medium"
                        ),
                    }
                )
            chain = [*branch_ids, component_id]
            graph_edges.extend(
                {
                    "source": earlier,
                    "target": later,
                    "relationship": "developed-into",
                }
                for earlier, later in zip(chain, chain[1:])
            )
            if branch.get("component_kind") in {"root", "free"}:
                headline = " → ".join(
                    [
                        *(str(step["form"]) for step in steps),
                        str(parts_by_id[component_id]["canonical_form"]),
                        str(source["text"]),
                    ]
                )
                root_headlines.append(headline)
                root_focus_areas.append(
                    {
                        "id": f"root-history-{len(root_focus_areas) + 1}",
                        "label": (
                            "Root history"
                            if branch.get("component_kind") == "root"
                            else "Word history"
                        ),
                        "kind": (
                            "root"
                            if branch.get("component_kind") == "root"
                            else "word-history"
                        ),
                        "node_ids": [*branch_ids, component_id, center_id],
                        "headline": headline,
                        "explanation": (
                            "This cited lexical base carries the central history."
                            if branch_ids
                            else "This accepted base contributes to the modern word."
                        ),
                    }
                )

        all_ids = [str(node["id"]) for node in graph_nodes]
        part_ids = [str(part["morpheme_id"]) for part in parts]
        focus_areas: list[dict[str, Any]] = [
            {
                "id": "overview",
                "label": "Whole origin",
                "kind": "overview",
                "node_ids": all_ids,
                "headline": str(source["text"]),
                "explanation": "One word, its analyzed structure, and cited history.",
            },
            {
                "id": "parts",
                "label": "Word parts",
                "kind": "overview",
                "node_ids": [*part_ids, center_id],
                "headline": " · ".join(str(part["canonical_form"]) for part in parts),
                "explanation": "Only linguistically supported bases and affixes are shown.",
            },
        ]
        focus_areas.extend(root_focus_areas)
        for part in parts:
            part_kind = str(part["kind"])
            if part_kind not in {"prefix", "suffix"}:
                continue
            focus_areas.append(
                {
                    "id": f"{part_kind}-{part['morpheme_id']}",
                    "label": part_kind.title(),
                    "kind": part_kind,
                    "node_ids": [str(part["morpheme_id"]), center_id],
                    "headline": (
                        f"{part['canonical_form']} → {source['text']}"
                    ),
                    "explanation": (
                        f"{part['canonical_form']} contributes “{part['meaning']}”."
                    ),
                }
            )
        graph = {
            "center_id": center_id,
            "nodes": graph_nodes,
            "edges": graph_edges,
            "focus_areas": focus_areas,
        }

        def ruby(language: str) -> list[dict[str, str]]:
            return [
                {"t": str(segment["grapheme"]), "r": str(segment["phoneme"])}
                for segment in pronunciations[language].get("segments", [])
                if str(segment.get("grapheme", ""))
                and str(segment.get("phoneme", ""))
            ]

        japanese = translations["ja"]
        chinese = translations["zh"]
        french = translations["fr"]
        arabic = translations["ar"]
        quality = min(
            _artifact_quality(item)
            for item in (meaning, split, origin)
        )
        card = Card(
            card_id=str(uuid.uuid4()),
            mode="word",
            query=str(source["text"]),
            title=str(source["text"]),
            subtitle=" · ".join(str(part["canonical_form"]) for part in parts),
            summary_en=str(meaning["payload"]["definition"]),
            origin_story=(
                f"The cited lexical history follows {'; '.join(root_headlines)}."
                if root_headlines
                else ""
            ),
            key_points=[],
            english={
                "term": str(source["text"]),
                "pronunciation": str(pronunciations["en"]["reading"]),
                "meaning": str(meaning["payload"]["definition"]),
            },
            japanese={
                "term": str(japanese["term"]),
                "reading": str(pronunciations["ja"]["reading"]),
                "meaning": str(japanese["meaning"]),
                "ruby_tokens": ruby("ja"),
            },
            chinese={
                "simplified": str(chinese["term"]),
                "traditional": "",
                "pinyin": str(pronunciations["zh"]["reading"]),
                "meaning": str(chinese["meaning"]),
                "ruby_tokens": ruby("zh"),
            },
            memory_hook="",
            related_terms=[],
            evidence=evidence,
            model="accepted atomic knowledge",
            created_at=datetime.now(UTC).isoformat(),
            extensions={
                "experience": "word",
                "knowledge_policy": "accepted-atomic-origin-view",
                "knowledge_subject": job["subject_key"],
                "knowledge_artifact_ids": [
                    meaning["artifact_id"], split["artifact_id"], origin["artifact_id"]
                ],
                "evidence_ids": evidence_ids,
                "morphology_graph": graph,
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
            },
            extra_languages={
                "french": {
                    "term": str(french["term"]),
                    "pronunciation": str(pronunciations["fr"]["reading"]),
                    "meaning": str(french["meaning"]),
                },
                "arabic": {
                    "term": str(arabic["term"]),
                    "reading": str(pronunciations["ar"]["reading"]),
                    "meaning": str(arabic["meaning"]),
                },
            },
        )
        cards = [card]
        for derived_mode, focus_kinds, part_kinds, policy in (
            _derived_origin_view_specs(parts)
        ):
            selected_focuses = [
                deepcopy(area)
                for area in focus_areas
                if area["kind"] in focus_kinds
            ]
            if not selected_focuses:
                continue
            selected_focuses.append(deepcopy(focus_areas[0]))
            derived_graph = deepcopy(graph)
            derived_graph["focus_areas"] = selected_focuses
            relevant_parts = [
                part for part in parts if str(part["kind"]) in part_kinds
            ]
            derived_graph["center_id"] = str(relevant_parts[0]["morpheme_id"])
            derived = deepcopy(card)
            derived.card_id = str(uuid.uuid4())
            derived.mode = derived_mode
            derived.title = " · ".join(
                str(part["canonical_form"]) for part in relevant_parts
            )
            derived.subtitle = f"{derived_mode.upper()} · {source['text']}"
            if derived_mode == "affix":
                derived.origin_story = _affix_origin_story(relevant_parts)
            derived.created_at = datetime.now(UTC).isoformat()
            derived.model = "accepted atomic knowledge"
            derived.extensions = deepcopy(card.extensions)
            derived.extensions["experience"] = derived_mode
            derived.extensions["knowledge_policy"] = policy
            derived.extensions["morphology_graph"] = derived_graph
            cards.append(derived)

        publication_errors = {
            output_card.mode: card_validation_errors(output_card.to_dict())
            for output_card in cards
        }
        publication_errors = {
            mode: errors for mode, errors in publication_errors.items() if errors
        }
        if publication_errors:
            details = "; ".join(
                f"{mode}: {', '.join(errors)}"
                for mode, errors in publication_errors.items()
            )
            raise ValueError(f"origin views failed prepublication validation: {details}")

        for output_card in cards:
            self.card_store.save(output_card)
            self.card_store.publish(
                output_card.card_id,
                quality_score=quality,
                review_note=(
                    f"composed {output_card.mode} view only from accepted origin atoms"
                ),
            )
            self.card_store.supersede_others(
                output_card.mode, output_card.query, output_card.card_id
            )
        accepted = {
            "card_id": card.card_id,
            "mode": card.mode,
            "quality": quality,
            "knowledge_artifact_ids": card.extensions["knowledge_artifact_ids"],
            "derived_card_ids": {
                output_card.mode: output_card.card_id
                for output_card in cards
                if output_card is not card
            },
            "card": card.to_dict(),
        }
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-origin-card",
            accepted,
            language="en",
            validation_state="accepted",
            quality_score=quality,
        )

    def _prepare_translation(self, job: dict[str, Any]) -> str:
        language = str(job.get("language", ""))
        if language not in _LANGUAGE_NAMES:
            raise ValueError(f"unsupported translation language: {language}")
        term = self.store.term_record(str(job["subject_entity_id"]))
        evidence_artifacts = self.store.artifacts_for_subject(
            job["subject_key"], stage="retrieved-evidence"
        )
        meaning_artifacts = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-meaning",
            validation_state="accepted",
        )
        if not evidence_artifacts or not meaning_artifacts:
            raise ValueError("translation prerequisites are missing")
        records = evidence_artifacts[-1]["payload"].get("records", [])
        meaning = meaning_artifacts[-1]["payload"]
        evidence_ids = [str(item) for item in meaning.get("evidence_ids", [])]
        candidates: list[str] = []
        candidate_evidence: dict[str, list[str]] = {}
        for record in records:
            record_evidence_id = str(record.get("knowledge_evidence_id", ""))
            exact_bilingual = (
                record.get("kind") == "bilingual-dictionary"
                and record.get("translation_scope") == "exact-headword"
                and str(record.get("headword", "")).casefold()
                == str(term["text"]).casefold()
            )
            if record_evidence_id not in evidence_ids and not exact_bilingual:
                continue
            translations = record.get("translations")
            values = translations.get(language, []) if isinstance(translations, dict) else []
            for value in values if isinstance(values, list) else []:
                candidate = re.sub(r"\s+", " ", str(value)).strip()
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
                if candidate and record_evidence_id:
                    candidate_evidence.setdefault(candidate, []).append(
                        record_evidence_id
                    )

        prompt = f"""SOURCE TERM: {term['text']}
ACCEPTED ENGLISH SENSE: {meaning['definition']}
TARGET LANGUAGE: {_LANGUAGE_NAMES[language]} ({language})
DICTIONARY CANDIDATES: {json.dumps(candidates[:10], ensure_ascii=False)}

Return exactly one JSON object with these keys:
term: the most natural concise equivalent for this exact sense
meaning: a short definition in the target language, at most 24 words
reading: kana for Japanese kanji, tone-marked pinyin for Chinese, simple Latin
transliteration for Arabic, or an empty string for French
usage_note: at most 14 English words, empty when unnecessary
confidence: number from 0 to 1

When dictionary candidates are non-empty, term must exactly equal one candidate.
Source provenance is attached by the system after validation; do not return IDs.
Use natural, non-redundant wording; never repeat a content word around "or".
For Arabic, term and meaning must contain Arabic script only. Never copy Latin
letters or the English source term into either field.
Do not add alternatives, markdown, etymology, or example sentences."""
        system_prompt = (
            "You prepare one sense-aligned translation at a time. Preserve scripts accurately."
        )
        token_budget = 176
        if language == "ar":
            prompt = f"""ARABIC TRANSLATION
TARGET LANGUAGE: Arabic (ar)
SOURCE TERM: {term['text']}
EXACT ENGLISH SENSE: {meaning['definition']}
DICTIONARY CANDIDATES: {json.dumps(candidates[:10], ensure_ascii=False)}

Return exactly one compact JSON object with only these keys:
term: the natural Modern Standard Arabic equivalent in Arabic script
meaning: a concise Arabic definition, at most 12 words
reading: a simple Latin transliteration of term
confidence: number from 0 to 1

If candidates are supplied, copy one exactly. Term and meaning must contain no
Latin letters. Return no alternatives, markdown, labels, IDs, or explanation.
End immediately after the JSON object."""
            system_prompt = "Return one compact Arabic lexical JSON object only."
            token_budget = 128
        completion = self.model.complete_json(
            system_prompt,
            prompt,
            max_tokens=token_budget,
        )
        value = completion.get("value")
        if not isinstance(value, dict):
            raise ValueError("translation task did not return an object")
        translated = re.sub(r"\s+", " ", str(value.get("term", ""))).strip()
        translated_meaning = re.sub(
            r"\s+", " ", str(value.get("meaning", ""))
        ).strip()
        reading = re.sub(r"\s+", " ", str(value.get("reading", ""))).strip()
        usage_note = _clean_usage_note(value.get("usage_note", ""), language)
        normalizations: list[str] = []
        sole_arabic_candidate = ""
        if language == "ar":
            valid_candidates = [
                candidate
                for candidate in candidates
                if len(candidate) <= 160
                and is_arabic_script_text(candidate)
                and not any(marker in candidate for marker in _ENCODING_DAMAGE)
                and candidate_evidence.get(candidate)
            ]
            if len(valid_candidates) == 1:
                sole_arabic_candidate = valid_candidates[0]
                if translated != sole_arabic_candidate:
                    translated = sole_arabic_candidate
                    normalizations.append("selected-sole-arabic-dictionary-candidate")
            cleaned_meaning = _strip_exact_latin_headword(
                translated_meaning, str(term["text"])
            )
            if cleaned_meaning != translated_meaning:
                translated_meaning = cleaned_meaning
                normalizations.append("removed-source-headword-from-arabic-meaning")
        if language == "ar" and (
            not is_arabic_script_text(translated)
            or not is_arabic_script_text(translated_meaning)
        ):
            repair = self.model.complete_json(
                "You repair one Arabic lexical entry. Arabic fields must contain no Latin letters.",
                f"""ARABIC SCRIPT REPAIR
SOURCE ENGLISH SENSE: {meaning['definition']}
DICTIONARY CANDIDATE: {json.dumps(sole_arabic_candidate, ensure_ascii=False)}

Return exactly one JSON object with these keys:
term: the natural Modern Standard Arabic equivalent using Arabic letters only
meaning: a concise definition written entirely in Arabic, at most 18 words
reading: a simple Latin transliteration of the Arabic term
confidence: number from 0 to 1

Do not copy, transliterate, or include the English headword in term or meaning.
Source provenance is attached by the system after validation; do not return IDs.
Do not return usage notes, markdown, alternatives, labels, or explanations.
End immediately after the JSON object.""",
                max_tokens=192,
            )
            repaired_value = repair.get("value")
            if not isinstance(repaired_value, dict):
                raise ValueError("Arabic script repair did not return an object")
            value = repaired_value
            completion = repair
            translated = re.sub(r"\s+", " ", str(value.get("term", ""))).strip()
            translated_meaning = re.sub(
                r"\s+", " ", str(value.get("meaning", ""))
            ).strip()
            reading = re.sub(r"\s+", " ", str(value.get("reading", ""))).strip()
            usage_note = _clean_usage_note(value.get("usage_note", ""), language)
            normalizations.append("repaired-arabic-script")
            if sole_arabic_candidate and translated != sole_arabic_candidate:
                translated = sole_arabic_candidate
                if "selected-sole-arabic-dictionary-candidate" not in normalizations:
                    normalizations.append(
                        "selected-sole-arabic-dictionary-candidate"
                    )
            cleaned_meaning = _strip_exact_latin_headword(
                translated_meaning, str(term["text"])
            )
            if cleaned_meaning != translated_meaning:
                translated_meaning = cleaned_meaning
                if (
                    "removed-source-headword-from-arabic-meaning"
                    not in normalizations
                ):
                    normalizations.append(
                        "removed-source-headword-from-arabic-meaning"
                    )
        if not translated or len(translated) > 160:
            raise ValueError("translation term is empty or too long")
        if not translated_meaning or len(translated_meaning) > 320:
            raise ValueError("translation meaning is empty or too long")
        if any(
            marker in text
            for marker in _ENCODING_DAMAGE
            for text in (translated, translated_meaning, reading)
        ):
            raise ValueError("translation contains encoding damage")
        if candidates and translated not in candidates:
            raise ValueError("translation did not use a supplied dictionary candidate")
        if language == "ja" and not re.search(
            r"[\u3040-\u30ff\u3400-\u9fff]", translated
        ):
            raise ValueError("Japanese translation has no Japanese script")
        if language == "zh" and not re.search(r"[\u3400-\u9fff]", translated):
            raise ValueError("Chinese translation has no Han characters")

        def reject_arabic_script(field: str) -> None:
            error = (
                f"Arabic translation {field} contains mixed or non-Arabic script"
            )
            raw = completion.get("raw", "")
            if not isinstance(raw, str) or not raw:
                raw = json.dumps(value, ensure_ascii=False)
            metrics = completion.get("metrics", {})
            bounded_metrics = (
                {
                    key: metrics[key]
                    for key in (
                        "elapsed_seconds",
                        "prompt_tokens",
                        "completion_tokens",
                        "tokens_per_second",
                    )
                    if key in metrics
                }
                if isinstance(metrics, dict)
                else {}
            )
            self.store.save_job_artifact(
                job["job_id"],
                "rejected-translation",
                {
                    "source_term": str(term["text"])[:160],
                    "language": language,
                    "error": error,
                    "candidate": {
                        "term": translated[:320],
                        "meaning": translated_meaning[:640],
                        "reading": reading[:320],
                    },
                    "normalizations": normalizations[:8],
                    "raw": raw[:4_000],
                    "model": str(
                        completion.get("model", self.model.model_name)
                    )[:200],
                    "metrics": bounded_metrics,
                },
                language=language,
                reusable=False,
                validation_state="rejected",
                quality_score=0.0,
            )
            raise ValueError(error)

        if language == "ar" and not is_arabic_script_text(translated):
            reject_arabic_script("term")
        if language == "ar" and not is_arabic_script_text(translated_meaning):
            reject_arabic_script("meaning")
        if language == "ar":
            cleaned_meaning = _collapse_repeated_arabic_alternative(
                translated_meaning
            )
            if cleaned_meaning != translated_meaning:
                translated_meaning = cleaned_meaning
                normalizations.append("collapsed-repeated-arabic-alternative")
        if language == "ar" and _has_repeated_arabic_content_word(translated_meaning):
            raise ValueError("Arabic translation meaning repeats a content word")
        if language == "zh":
            reading = chinese_pinyin(translated, reading)
        elif language == "fr":
            reading = ""
        if language in {"ja", "zh", "ar"} and not reading:
            raise ValueError("translation reading is missing")
        selected = list(
            dict.fromkeys(
                [*evidence_ids, *candidate_evidence.get(translated, [])]
            )
        )
        if not selected:
            raise ValueError("translation has no retrieved source evidence")
        confidence = max(0.0, min(float(value.get("confidence", 0.0)), 1.0))
        if confidence < 0.6:
            raise ValueError("translation confidence is below acceptance threshold")

        target_id = self.store.upsert_term(
            language, translated, status="accepted", quality_score=confidence
        )
        translation_id = self.store.add_translation(
            term["entity_id"],
            language,
            translated,
            transliteration=reading,
            usage_note=usage_note,
            source_meaning_id=str(meaning["meaning_id"]),
            target_term_id=target_id,
            status="accepted",
            quality_score=confidence,
        )
        for evidence_id in selected:
            self.store.link_evidence(
                translation_id,
                evidence_id,
                claim=f"{term['text']} to {translated}",
                confidence=confidence,
            )
        accepted = {
            "translation_id": translation_id,
            "target_term_id": target_id,
            "source_term": term["text"],
            "language": language,
            "term": translated,
            "meaning": translated_meaning,
            "reading": reading,
            "usage_note": usage_note,
            "confidence": confidence,
            "evidence_ids": selected,
            "dictionary_candidates": candidates[:10],
            "dictionary_evidence_ids": candidate_evidence.get(translated, []),
            "normalizations": normalizations,
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            translation_id,
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason=f"atomic {language} translation",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-translation",
            accepted,
            language=language,
            validation_state="accepted",
            quality_score=confidence,
        )


def build_worker(
    store: KnowledgeStore,
    corpus: CorpusIndex,
    roots: MorphologyIndex,
    affixes: MorphologyIndex,
    lexicon: LocalLexiconRag,
    model: LlamaCppClient,
    card_store: CardStore,
    japanese_readings: JapaneseReadingIndex | None = None,
) -> PreparationWorker:
    return PreparationWorker(
        store,
        WordEvidenceRetriever(corpus, roots, affixes, lexicon),
        model,
        EspeakPronouncer(),
        card_store,
        japanese_readings,
    )
