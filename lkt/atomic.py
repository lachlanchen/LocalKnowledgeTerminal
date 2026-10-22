from __future__ import annotations

import hashlib as _morph_hashlib
import json as _morph_json

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
from .enrichment_membership import (
    ENRICHMENT_MEMBERSHIP_POLICY, classify_term_component_claims,
)
from .evidence_policy import card_evidence_record_relevant
from .freedict import FREEDICT_CORPUS_ID
from .knowledge import KnowledgeStore
from .language_projection import language_card_atoms
from .language_repair import align_translation_reading, repair_request, restore_translation_checkpoint
from .jmdict import JapaneseReadingIndex
from .languages import (
    DISPLAY_LANGUAGES,
    language_name,
    language_profile,
    normalize_language_code,
    ordered_language_codes,
    supports_english_morphology,
)
from .lexicon import LocalLexiconRag
from .lexical_refinement import (
    ORIGIN_PATH_POLICY,
    STRUCTURE_POLICY,
    retire_replaced_model_origin_paths,
    revise_origin_paths,
)
from .llm import AtomicJsonOutputError, LlamaCppClient
from .models import Card, Evidence
from .morphology import MorphologyIndex
from .partial_morphology import finalize_morpheme_analysis, word_history_anchor
from .partial_projection import component_projection_occurrence
from .morpheme_recovery import (
    MORPHEME_RECOVERY_POLICY,
    MORPHEME_REPAIR_MAX_TOKENS,
    build_morpheme_repair_prompt,
    morpheme_context_fingerprint,
    select_morpheme_checkpoint,
)
from .morphology_enrichment_prompting import (
    MORPHOLOGY_ENRICHMENT_MAX_TOKENS,
    build_morphology_enrichment_prompt,
)
from .morpheme_prompting import (
    MORPHEME_NORMALIZATION_POLICY,
    MORPHEME_PROMPT_POLICY,
    build_morpheme_prompt,
    component_candidates,
    morpheme_review_reason,
    morpheme_structure_error,
    normalize_morpheme_draft,
    pack_morpheme_context,
)
from .morphology_contract import (
    canonical_lexical_node_identity,
    canonical_morpheme_occurrence_identity,
)
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
    "prepare-entity-meaning",
    "split-morphemes",
    "expand-origin-branches",
    "extract-investigation-terms",
    "prepare-grammar-parts",
    "prepare-translation",
    "prepare-pronunciation",
    "prepare-grammar-properties",
    "compose-word-card",
    "compose-origin-card",
    "enrich-morphology-v3",
)
# Safety boundary for low-memory queue draining. Keep this list limited to
# bounded handlers that compose persisted artifacts and never call ``self.model``.
MODEL_FREE_ATOMIC_JOBS = (
    "compose-word-card",
    "compose-origin-card",
)
_SEMANTIC_ENRICHMENT_JOB_TYPES = (
    "prepare-meaning",
    "prepare-entity-meaning",
)
_SEMANTIC_RESERVATION_AFTER_OTHER_COMPLETIONS = 4
_SEMANTIC_LAST_ATTEMPT_META_KEY = "atomic_semantic_last_attempt_at"
_SEMANTIC_OTHER_ATTEMPT_META_KEY = "atomic_semantic_other_attempt_total"
_SEMANTIC_JOB_PROMPT_MARKER = "semantic-enrichment:"
_SEMANTIC_BINDING_SQL = """
    EXISTS (
        SELECT 1 FROM lexical_semantic_enrichments AS semantic
        WHERE semantic.enrichment_job_id = job.job_id
          AND semantic.entity_id = job.subject_entity_id
          AND semantic.language = job.language
          AND semantic.desired_fingerprint = job.source_fingerprint
    )
"""
_SEMANTIC_INTENT_SQL = """
    (
        (
            job.job_type IN ('prepare-meaning', 'prepare-entity-meaning')
            AND job.prompt_version LIKE 'semantic-enrichment:%'
        )
        OR EXISTS (
            SELECT 1 FROM lexical_semantic_enrichments AS semantic
            WHERE semantic.enrichment_job_id = job.job_id
              AND semantic.entity_id = job.subject_entity_id
              AND semantic.language = job.language
        )
    )
"""
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


def _language_code(value: Any) -> str:
    return normalize_language_code(value)


def _language_name(code: str) -> str:
    return language_name(code)


def _extra_language_card_values(
    language_atoms: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for language, atom in language_atoms.items():
        if language in {"en", "ja", "zh"}:
            continue
        profile = language_profile(language)
        reading = str(atom.get("reading", ""))
        key = profile.card_key
        result[key] = {
            "language": language,
            "label": profile.label,
            "direction": profile.direction,
            "term": str(atom.get("term", "")),
            "reading": reading,
            "pronunciation": reading,
            "meaning": str(atom.get("meaning", "")),
        }
    return result


def _language_card_atoms(
    source: dict[str, Any],
    meaning: dict[str, Any],
    translations: dict[str, dict[str, Any]],
    pronunciations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Project accepted atoms without requiring every requested language."""

    return language_card_atoms(source, meaning, translations, pronunciations)


def _legacy_language_fields(
    atoms: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    english_atom = atoms.get("en", {})
    japanese_atom = atoms.get("ja", {})
    chinese_atom = atoms.get("zh", {})
    return (
        {
            "term": str(english_atom.get("term", "")),
            "pronunciation": str(english_atom.get("reading", "")),
            "meaning": str(english_atom.get("meaning", "")),
        },
        {
            "term": str(japanese_atom.get("term", "")),
            "reading": str(japanese_atom.get("reading", "")),
            "meaning": str(japanese_atom.get("meaning", "")),
            "ruby_tokens": list(japanese_atom.get("ruby_tokens", [])),
        },
        {
            "simplified": str(chinese_atom.get("term", "")),
            "traditional": "",
            "pinyin": str(chinese_atom.get("reading", "")),
            "meaning": str(chinese_atom.get("meaning", "")),
            "ruby_tokens": list(chinese_atom.get("ruby_tokens", [])),
        },
    )
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


def _publication_provenance(
    basis: Any,
    evidence_ids: Any,
    available_ids: set[str],
    *,
    claim: str,
    claim_id: str,
    warnings: list[dict[str, str]],
) -> tuple[str, list[str]]:
    """Repair claim ownership without discarding usable model semantics."""

    supplied = (
        list(dict.fromkeys(str(item) for item in evidence_ids if str(item)))
        if isinstance(evidence_ids, list)
        else []
    )
    valid = [item for item in supplied if item in available_ids]
    invalid = [item for item in supplied if item not in available_ids]
    normalized_basis = str(basis or "model").strip().casefold()
    if normalized_basis == "book" and valid:
        if invalid:
            warnings.append(
                {
                    "claim": claim,
                    "claim_id": claim_id,
                    "action": "removed-invalid-citations",
                    "reason": "some supplied evidence IDs were not stored retrieved records",
                }
            )
        return "book", valid

    if normalized_basis == "book":
        action = "downgraded-to-model"
        reason = "no supplied evidence ID resolved to a stored retrieved record"
    elif normalized_basis == "model":
        if not supplied:
            return "model", []
        action = "removed-model-citations"
        reason = "model-owned claims cannot cite retrieved records"
    else:
        action = "downgraded-to-model"
        reason = "unknown provenance basis"
    warnings.append(
        {
            "claim": claim,
            "claim_id": claim_id,
            "action": action,
            "reason": reason,
        }
    )
    return "model", []


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
    def retrieve(
        self,
        term: str,
        *,
        source_language: str = "en",
        target_languages: tuple[str, ...] = DISPLAY_LANGUAGES,
    ) -> list[dict[str, Any]]: ...

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
    surfaces and supplies their provenance. Only a formula covering the whole
    word fixes its surface boundaries. Partial formulas remain reading context;
    their uncovered letters do not establish additional affixes.
    """

    word = re.sub(r"[^A-Za-z]", "", letters).casefold()
    if not word:
        return []
    component = re.compile(
        r"(?<![A-Za-z])([A-Za-z]{1,14})\s*[（(]([^（）()]{1,90})[）)]"
    )
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []
    for record in records:
        headword = re.sub(
            r"[^A-Za-z]", "", str(record.get("headword", ""))
        ).casefold()
        if not str(record.get("kind", "")).startswith("morphology-"):
            continue
        excerpt = re.sub(
            r"\(\s*=\\\)\s*(.*?)\s*\\\(\s*\)",
            lambda match: f"(={match.group(1)})",
            str(record.get("excerpt", "")),
        )
        excerpt = re.sub(
            r"\\mathrm\s*\{\s*([A-Za-z]{1,14})\s*\}", r"\1", excerpt
        )
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
            # Even an exact headword can give a shortened historical formula.
            # Completing that formula by labelling leftover letters as affixes
            # would turn a parser guess into a mandatory book-backed split.
            # Keep such records in RAG, but let the model resolve the surface
            # spelling and canonical forms instead of freezing invented parts.
            if fixed != word:
                continue
            evidence_ids = [
                str(record.get("knowledge_evidence_id", ""))
            ] if record.get("knowledge_evidence_id") else []
            shape: list[dict[str, Any]] = []
            for surface, match in zip(surfaces, run, strict=True):
                meaning = _clean_morpheme_meaning(
                    re.sub(r"^[=＝]\s*", "", match.group(2))
                )
                item = {
                    "surface": surface,
                    "kind": "",
                    "evidence_ids": evidence_ids,
                }
                if meaning and re.fullmatch(r"[A-Za-z][A-Za-z -]*", meaning):
                    item["meaning"] = meaning
                shape.append(item)
            # A lookup candidate's requested kind is not book role evidence.
            # Typed corpus observations are applied separately during
            # persistence; lexical classification here remains the model's job.
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
    """Keep a useful meaning within the accepted ten-word display contract."""

    def cleaned_text(item: Any) -> str:
        text = re.sub(r"\s*[,;/]\s*", " or ", str(item).strip())
        return re.sub(r"\s+", " ", text).strip(" .:-")

    def bounded_words(text: str) -> str:
        words = text.split()[:10]
        while words and words[-1].casefold() in {"and", "or"}:
            words.pop()
        return " ".join(words)

    if isinstance(value, (list, tuple)):
        alternatives: list[str] = []
        word_count = 0
        for item in value:
            alternative = cleaned_text(item)
            if not alternative:
                continue
            alternative_words = alternative.split()
            added_words = len(alternative_words) + (1 if alternatives else 0)
            if word_count + added_words > 10:
                if not alternatives:
                    return bounded_words(alternative)
                break
            alternatives.append(alternative)
            word_count += added_words
        return " or ".join(alternatives)
    return bounded_words(cleaned_text(value))


def _augment_exact_free_draft(
    value: Any,
    required_shape: list[dict[str, Any]],
    letters: str,
) -> Any:
    """Overlay a complete exact book formula on one weak whole-word draft.

    The raw model response is stored before this normalization.  This narrow
    repair never invents provenance: every accepted component must be present
    in one full-word formula and carry an existing book evidence identifier.
    """

    if not isinstance(value, dict):
        return value
    parts = value.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        return value
    original = parts[0]
    surface = re.sub(r"[^A-Za-z]", "", str(original.get("surface", "")))
    if surface.casefold() != letters or str(original.get("kind", "")).casefold() != "free":
        return value
    if len(required_shape) < 2 or "".join(
        str(item.get("surface", "")).casefold() for item in required_shape
    ) != letters:
        return value
    if any(
        str(item.get("kind", "")) not in {"prefix", "root", "suffix"}
        or not str(item.get("meaning", ""))
        or not isinstance(item.get("evidence_ids"), list)
        or not any(str(evidence_id) for evidence_id in item["evidence_ids"])
        for item in required_shape
    ):
        return value
    language = str(original.get("language", "en")).strip().casefold()
    if language not in {"en", "la"}:
        language = "en"
    try:
        confidence = float(original.get("confidence", 0.0))
    except (TypeError, ValueError):
        return value
    augmented = [
        {
            "surface": str(item["surface"]),
            "canonical_form": _morpheme_display_form(
                str(item["surface"]), str(item["kind"])
            ),
            "kind": str(item["kind"]),
            "language": language,
            "meaning": str(item["meaning"]),
            "confidence": confidence,
            # The validated required-shape path attaches the stored evidence.
            # The model response itself is never made to claim that citation.
            "evidence_ids": [],
        }
        for item in required_shape
    ]
    return {
        **value,
        "parts": augmented,
        "augmentation": "exact-full-word-book-formula",
    }


def _normalise_derivative_terms(
    values: Any,
    parts: list[dict[str, Any]],
    source_term: str,
    *,
    limit: int = 32,
) -> list[dict[str, Any]]:
    """Keep bounded model/RAG derivative suggestions without semantic vetoes."""

    if not isinstance(values, list):
        return []
    part_by_form: dict[str, dict[str, Any]] = {}
    excluded = {_plain_letter_key(source_term)}
    for part in parts:
        for field in ("canonical_form", "surface"):
            key = _plain_letter_key(part.get(field, ""))
            if key:
                part_by_form[key] = part
                excluded.add(key)

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        term = unicodedata.normalize(
            "NFKC", str(value.get("term", value.get("word", "")))
        )
        term = re.sub(r"\s+", " ", term).strip()
        key = _plain_letter_key(term)
        if (
            not key
            or key in excluded
            or key in seen
            or not re.fullmatch(r"[A-Za-z][A-Za-z'-]*", term)
            or key in {"derivativeword", "example", "placeholder", "term", "word"}
        ):
            continue
        note = re.sub(
            r"\s+",
            " ",
            str(value.get("note", value.get("relationship", ""))),
        ).strip()[:180]
        supplied_forms = value.get(
            "component_forms", value.get("components", value.get("component_form", []))
        )
        if isinstance(supplied_forms, str):
            supplied_forms = [supplied_forms]
        matched_parts = [
            part_by_form[key]
            for key in dict.fromkeys(
                _plain_letter_key(item)
                for item in supplied_forms
                if _plain_letter_key(item)
            )
            if key in part_by_form
        ] if isinstance(supplied_forms, list) else []
        output.append(
            {
                "term": term,
                "note": note,
                "component_forms": list(
                    dict.fromkeys(
                        str(part.get("canonical_form", ""))
                        for part in matched_parts
                        if str(part.get("canonical_form", ""))
                    )
                ),
                "component_ids": list(
                    dict.fromkeys(
                        str(part.get("morpheme_id", ""))
                        for part in matched_parts
                        if str(part.get("morpheme_id", ""))
                    )
                ),
                "component_kinds": list(
                    dict.fromkeys(
                        str(part.get("kind", ""))
                        for part in matched_parts
                        if str(part.get("kind", ""))
                    )
                ),
            }
        )
        seen.add(key)
        if len(output) >= limit:
            break
    return output


def _derivative_terms_for_view(
    payloads: list[dict[str, Any]],
    source_term: str,
    all_parts: list[dict[str, Any]],
    relevant_parts: list[dict[str, Any]],
    *,
    limit: int = 24,
) -> list[dict[str, str]]:
    """Select accepted derivative words associated with a Root/Affix view."""

    relevant_forms = {
        _plain_letter_key(part.get(field, ""))
        for part in relevant_parts
        for field in ("canonical_form", "surface")
        if _plain_letter_key(part.get(field, ""))
    }
    relevant_ids = {
        str(part.get("morpheme_id", ""))
        for part in relevant_parts
        if str(part.get("morpheme_id", ""))
    }
    relevant_kinds = {
        str(part.get("kind", ""))
        for part in relevant_parts
        if str(part.get("kind", ""))
    }
    raw: list[Any] = []
    for payload in payloads:
        for field in ("related_terms", "derivatives"):
            values = payload.get(field, []) if isinstance(payload, dict) else []
            if isinstance(values, list):
                raw.extend(values)

    selected: list[dict[str, Any]] = []
    for item in _normalise_derivative_terms(raw, all_parts, source_term, limit=48):
        forms = {_plain_letter_key(value) for value in item["component_forms"]}
        ids = set(item["component_ids"])
        kinds = set(item["component_kinds"])
        associated = bool(forms or ids or kinds)
        if associated and not (
            forms.intersection(relevant_forms)
            or ids.intersection(relevant_ids)
            or kinds.intersection(relevant_kinds)
        ):
            continue
        selected.append({"term": item["term"], "note": item["note"]})
        if len(selected) >= limit:
            break
    return selected


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


_PROJECTION_REPAIR_PROMPT_MARKER = "-projection-local-p"


def _projection_repair_fingerprint(job: dict[str, Any]) -> str:
    """Return only an explicitly local projection fingerprint from a compose job."""

    prompt_version = str(job.get("prompt_version", ""))
    fingerprint = str(job.get("source_fingerprint", "")).strip().casefold()
    if (
        _PROJECTION_REPAIR_PROMPT_MARKER not in prompt_version
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return ""
    return fingerprint


def _origin_history_headline(forms: list[Any]) -> str:
    displayed: list[str] = []
    previous_key = ""
    for value in forms:
        form = str(value).strip()
        if not form:
            continue
        normalized = _plain_letter_key(form) or " ".join(form.casefold().split())
        if displayed and normalized == previous_key:
            continue
        displayed.append(form)
        previous_key = normalized
    return " → ".join(displayed)


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


def _explicit_form_evidence_ids(
    form: Any, records: list[dict[str, Any]]
) -> list[str]:
    """Find exact contiguous form tokens in a record headword or excerpt."""

    needle = _normalized_form_tokens(form)
    if not needle:
        return []
    selected: list[str] = []
    for record in records:
        evidence_id = str(record.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        width = len(needle)
        visible = any(
            any(
                tokens[index : index + width] == needle
                for index in range(len(tokens) - width + 1)
            )
            for tokens in (
                _normalized_form_tokens(record.get("headword", "")),
                _normalized_form_tokens(record.get("excerpt", "")),
            )
        )
        if visible and evidence_id not in selected:
            selected.append(evidence_id)
    return selected


def _retrieved_component_evidence_ids(
    form: Any, kind: Any, records: list[dict[str, Any]]
) -> list[str]:
    """Raw corpus records are RAG context, never typed role evidence."""
    return []


def _apply_cited_component_roles(
    parts: list[dict[str, Any]], components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply unambiguous live book roles to the same ordinal/surface span."""
    from .morphology_contract import canonical_morpheme_identity

    cleaned = [dict(part) for part in parts]
    for ordinal, part in enumerate(cleaned):
        surface = str(part.get("surface", "")).strip().casefold().strip("-")
        matching: list[dict[str, Any]] = []
        for component in components:
            evidence_ids = [
                str(value).strip()
                for value in component.get("evidence_ids", ())
                if str(value).strip()
            ]
            if not evidence_ids:
                continue
            try:
                component_ordinal = int(component.get("ordinal", -1))
            except (TypeError, ValueError):
                continue
            component_surface = str(
                component.get("surface")
                or component.get("base")
                or component.get("form")
                or ""
            ).strip().casefold().strip("-")
            if component_ordinal == ordinal and component_surface == surface:
                matching.append(component)
        kinds = {
            str(component.get("kind", "")).strip().casefold()
            for component in matching
            if str(component.get("kind", "")).strip().casefold()
            in {"root", "prefix", "suffix"}
        }
        if len(kinds) > 1:
            raise ValueError(
                f"ambiguous cited morphology roles for component {ordinal}: {sorted(kinds)}"
            )
        if not kinds:
            continue
        role = next(iter(kinds))
        _base, display, _key = canonical_morpheme_identity(
            part.get("language", "en"),
            part.get("canonical_form") or part.get("surface"),
            role,
        )
        part["kind"] = role
        part["canonical_form"] = display
        part["evidence_ids"] = list(
            dict.fromkeys(
                [
                    *part.get("evidence_ids", ()),
                    *(
                        evidence_id
                        for component in matching
                        for evidence_id in component.get("evidence_ids", ())
                    ),
                ]
            )
        )
    return cleaned


def _semantic_claim_evidence(value: dict[str, Any]) -> set[str]:
    return {
        str(item).strip()
        for key in ("evidence_ids", "edge_evidence_ids")
        for item in value.get(key, [])
        if str(item).strip()
    }


def _semantic_claims_compatible(
    left: dict[str, Any], right: dict[str, Any], *, id_key: str
) -> bool:
    del id_key
    # Wording equality is not provenance. Rebinding immutable claims requires
    # at least one exact evidence record shared by both accepted projections.
    return bool(_semantic_claim_evidence(left) & _semantic_claim_evidence(right))


def _component_projection_identity(
    subject_entity_id: str,
    subject_form: str,
    component: dict[str, Any],
) -> tuple[str, ...] | None:
    try:
        return canonical_lexical_node_identity(
            subject_entity_id,
            "morpheme",
            component.get("language", ""),
            component.get("canonical_form")
            or component.get("base")
            or component.get("surface", ""),
            role=component.get("kind", ""),
        ) + component_projection_occurrence(subject_form, component)
    except ValueError:
        return None


def _canonical_component_projection_parts(
    subject_entity_id: str,
    artifact_parts: list[dict[str, Any]],
    accepted_components: list[dict[str, Any]] | None,
    *,
    subject_form: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Resolve immutable part IDs to supported subject-scoped identities."""
    candidates = [dict(item) for item in artifact_parts]
    if accepted_components is None:
        return candidates, {
            str(item.get("morpheme_id", "")): str(item.get("morpheme_id", ""))
            for item in candidates
            if str(item.get("morpheme_id", ""))
        }
    supported = [dict(item) for item in accepted_components]
    resolved: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    matched_supported: set[int] = set()

    def projected(
        component: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, Any]:
        component_id = str(component.get("morpheme_id", "")).strip()
        canonical_form = str(component.get("canonical_form", "")).strip()
        kind = str(component.get("kind", "")).strip().casefold()
        if kind == "prefix":
            canonical_form = canonical_form.rstrip("-") + "-"
        elif kind == "suffix":
            canonical_form = "-" + canonical_form.lstrip("-")
        graph_value = {
            **current,
            "morpheme_id": component_id,
            "canonical_form": canonical_form,
            "kind": kind,
            "language": str(component.get("language", "")).strip(),
            "meaning": str(component.get("meaning", "")).strip()
            or str(current.get("meaning", "")).strip(),
            "confidence": component.get("confidence", 0.7),
            "evidence_ids": list(component.get("evidence_ids", [])),
            "basis": str(component.get("basis", "model")).strip() or "model",
            "ordinal": int(component.get("ordinal", 0) or 0),
        }
        surface = str(component.get("surface", "")).strip()
        if surface:
            graph_value["surface"] = surface
        elif not str(graph_value.get("surface", "")).strip():
            graph_value["surface"] = canonical_form.strip("-")
        for key in ("source_span", "analysis_scope"):
            if key in component:
                graph_value[key] = component[key]
        return graph_value

    for candidate in candidates:
        candidate_identity = _component_projection_identity(
            subject_entity_id, subject_form, candidate
        )
        matches = [
            (index, component)
            for index, component in enumerate(supported)
            if index not in matched_supported
            and (
                str(candidate.get("morpheme_id", ""))
                in component.get("projection_alias_ids", [])
                or (
                    candidate_identity is not None
                    and _component_projection_identity(
                        subject_entity_id, subject_form, component
                    ) == candidate_identity
                    and (
                        str(candidate.get("morpheme_id", ""))
                        == str(component.get("morpheme_id", ""))
                        or _semantic_claims_compatible(
                            component, candidate, id_key="morpheme_id"
                        )
                    )
                )
            )
        ]
        if not matches:
            continue
        index, component = min(
            matches,
            key=lambda item: (
                0
                if str(candidate.get("morpheme_id", ""))
                == str(item[1].get("morpheme_id", ""))
                else 1,
                item[0],
            ),
        )
        matched_supported.add(index)
        resolved.append(projected(component, candidate))
        canonical_id = str(component.get("morpheme_id", "")).strip()
        aliases[canonical_id] = canonical_id
        candidate_id = str(candidate.get("morpheme_id", "")).strip()
        if candidate_id:
            aliases[candidate_id] = canonical_id

    for index, component in enumerate(supported):
        if index in matched_supported:
            continue
        resolved.append(projected(component, {}))
        component_id = str(component.get("morpheme_id", "")).strip()
        aliases[component_id] = component_id
    for component in supported:
        component_id = str(component.get("morpheme_id", "")).strip()
        for alias_id in component.get("projection_alias_ids", []):
            if str(alias_id).strip():
                aliases[str(alias_id).strip()] = component_id
    return resolved, aliases


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


def _origin_context_evidence(
    term: Any, records: list[dict[str, Any]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Give the model related book prose without granting citation authority."""
    text = re.sub(r"\s+", " ", str(term or "")).strip()
    if not text:
        return []
    pattern = re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)", re.I)
    candidates: list[tuple[int, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict) or str(record.get("kind", "")) != "entry":
            continue
        excerpt = re.sub(r"\s+", " ", str(record.get("excerpt", ""))).strip()
        exact = _plain_letter_key(record.get("headword", "")) == _plain_letter_key(text)
        match = pattern.search(excerpt)
        if not excerpt or (not exact and match is None):
            continue
        key = (
            str(record.get("corpus_id", "")),
            str(record.get("entry_id", "")),
            excerpt,
        )
        if key in seen:
            continue
        seen.add(key)
        start = max(0, match.start() - 600) if match is not None else 0
        candidates.append((0 if exact else 1, {
            "source": record.get("source_title") or record.get("corpus_id", ""),
            "headword": record.get("headword", ""),
            "locator": record.get("locator", ""),
            "excerpt": excerpt[start:start + 1200],
            "context_only": True,
        }))
    return [record for _, record in sorted(candidates, key=lambda item: item[0])[:limit]]


def _card_evidence_record_relevant(
    source_text: Any,
    record: dict[str, Any],
    support_forms: tuple[str, ...] = (),
) -> bool:
    """Keep exact citations and records supporting a visible lexical form."""

    return card_evidence_record_relevant(source_text, record, support_forms)


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
        metadata_warnings = []
        language_code = _ORIGIN_LANGUAGE_CODES.get(language, language)
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", language_code):
            step["language"] = "und"
            metadata_warnings.append("historical language unspecified")
            changes.append("retained-unspecified-historical-language")
        if not period:
            step["period"] = "unspecified"
            metadata_warnings.append("historical period unspecified")
            changes.append("retained-unspecified-historical-period")
        if metadata_warnings:
            step["metadata_warnings"] = metadata_warnings
            try:
                step["confidence"] = min(float(step.get("confidence", 0.35)), 0.35)
            except (TypeError, ValueError):
                step["confidence"] = 0.35
        retained.append(step)
    normalized["steps"] = retained
    return normalized, list(dict.fromkeys(changes))


def _attach_verbatim_origin_evidence(
    value: Any,
    *,
    evidence: list[dict[str, Any]],
    allowed_ids: set[str],
) -> tuple[Any, list[str]]:
    """Attach provenance only when a generated form occurs in retrieved text."""

    if not isinstance(value, dict):
        return value, []
    normalized = deepcopy(value)
    changes: list[str] = []
    steps = normalized.get("steps")
    if not isinstance(steps, list):
        return normalized, changes
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        selected = [
            evidence_id
            for evidence_id in _explicit_form_evidence_ids(raw.get("form", ""), evidence)
            if evidence_id in allowed_ids
        ]
        if raw.get("evidence_ids") != selected:
            raw["evidence_ids"] = selected
            changes.append("attached-verbatim-origin-evidence")
    return normalized, list(dict.fromkeys(changes))


def _origin_transition_provenance(
    step: dict[str, Any], later_form: str, evidence: list[dict[str, Any]],
    *, extracted_from_book: bool = False,
) -> dict[str, Any]:
    """Two attested forms are context, not proof of a directed transition."""

    later_support = set(_explicit_form_evidence_ids(later_form, evidence))
    shared = [item for item in step.get("evidence_ids", []) if item in later_support]
    direct = bool(extracted_from_book and shared and step.get("basis") == "book")
    confidence = float(step.get("confidence", 0.35))
    return {
        "edge_basis": "book" if direct else "model",
        "edge_confidence": min(confidence, 0.95 if direct else 0.7),
        "edge_evidence_ids": shared if direct else [],
        "edge_context_evidence_ids": [] if direct else shared,
        "edge_provenance_version": 2,
        "edge_later_form": later_form,
    }


def _normalize_dictionary_candidate(value: Any, language: str) -> str:
    """Remove dictionary join markup while preserving lexical characters."""

    candidate = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value))
    ).strip()
    if language in {"ja", "zh"}:
        candidate = re.sub(
            r"(?<=[^\W\d_])\s*\+\s*(?=[^\W\d_])", "", candidate
        )
    return candidate


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
    if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
        return "steps must contain one to five historical forms"
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
        supplied = (
            {str(item) for item in step.get("evidence_ids", [])}
            if isinstance(step.get("evidence_ids"), list)
            else set()
        )
        supported = set(_explicit_form_evidence_ids(form, evidence))
        allowed = fixed_provenance_ids or {
            str(item.get("evidence_id", "")) for item in evidence
        }
        if supplied and not supplied <= supported.intersection(allowed):
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

    def retrieve(
        self,
        term: str,
        *,
        source_language: str = "en",
        target_languages: tuple[str, ...] = DISPLAY_LANGUAGES,
    ) -> list[dict[str, Any]]:
        source_language = _language_code(source_language)
        records: list[dict[str, Any]] = []
        if supports_english_morphology(source_language):
            root_records = [
                item for item in self.roots.search(term, 8) if _lexically_related(term, item)
            ][:2]
            affix_records = [
                item
                for item in self.affixes.search(term, 8)
                if _lexically_related(term, item)
            ][:2]
            records.extend(
                [
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
            )
        records.extend(
            self.lexicon.search(
                term,
                source_language=source_language,
                target_languages=target_languages,
                limit=3,
            )
        )
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
        # These are candidate reading records, not typed role authority. Plain
        # definitions and imperfect OCR are still useful input to the model.
        source_hash = self._hash(index) if records else ""
        return [_book_record(item, source_hash) for item in records[:2]]

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


class _StaleSemanticJob(ValueError):
    """A semantic job no longer owns the exact state it was planned for."""


def _nonsemantic_attempt_total(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """SELECT COALESCE(SUM(job.attempts), 0) AS attempts
           FROM preparation_jobs AS job WHERE NOT """
        + _SEMANTIC_INTENT_SQL
    ).fetchone()
    return int(row["attempts"] or 0) if row is not None else 0


def _claim_atomic_job_by_semantic_binding(
    store: KnowledgeStore,
    job_types: tuple[str, ...] | list[str],
    *,
    semantic_binding: bool,
    preferred_subject_keys: tuple[str, ...] | list[str] = (),
) -> dict[str, Any] | None:
    """Claim one job while preserving the store's normal ordering.

    ``prepare-meaning`` is shared by ordinary lexical plans and the semantic
    debt lane, so neither the job type nor the subject-key spelling can safely
    identify semantic work.  The durable enrichment binding is the authority.
    Keeping the include/exclude predicate in the same transaction as the claim
    prevents two workers from selecting the same reserved job.
    """

    allowed = tuple(dict.fromkeys(str(item).strip() for item in job_types if str(item).strip()))
    if not allowed:
        return None
    preferred_subjects = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in preferred_subject_keys
            if str(item).strip()
        )
    )
    type_clause = " AND job.job_type IN (" + ",".join("?" for _ in allowed) + ")"
    parameters: tuple[Any, ...] = allowed
    affinity_order = ""
    if preferred_subjects:
        affinity_order = (
            "CASE WHEN job.subject_key IN ("
            + ",".join("?" for _ in preferred_subjects)
            + ") THEN 0 ELSE 1 END, "
        )
        parameters += preferred_subjects
    binding_clause = (
        " AND " + _SEMANTIC_BINDING_SQL
        if semantic_binding
        else " AND NOT " + _SEMANTIC_BINDING_SQL
    )
    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        store._fail_blocked_jobs(connection)
        row = connection.execute(
            """SELECT job.* FROM preparation_jobs AS job
               WHERE job.status = 'queued' AND job.attempts < job.max_attempts
                 """
            + type_clause
            + binding_clause
            + """
                  AND NOT EXISTS (
                      SELECT 1 FROM job_dependencies AS dependency
                      JOIN preparation_jobs AS prerequisite
                        ON prerequisite.job_id = dependency.depends_on_job_id
                      WHERE dependency.job_id = job.job_id
                        AND prerequisite.status <> 'complete'
                        AND (
                            job.job_type NOT IN (
                                'compose-word-card', 'compose-origin-card'
                            )
                            OR prerequisite.status <> 'failed'
                        )
                  )
               ORDER BY
                   """
            + affinity_order
            + """
                   CASE WHEN job.subject_key LIKE 'term:%' THEN 0 ELSE 1 END,
                   priority, created_at, job.subject_key, job.job_id
               LIMIT 1""",
            parameters,
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        timestamp = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE preparation_jobs
               SET status = 'running', attempts = attempts + 1,
                   locked_at = ?, updated_at = ?
               WHERE job_id = ?""",
            (timestamp, timestamp, row["job_id"]),
        )
        connection.commit()
        claimed = connection.execute(
            "SELECT * FROM preparation_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        return dict(claimed) if claimed else None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _job_has_semantic_enrichment_binding(
    store: KnowledgeStore, job: dict[str, Any]
) -> bool:
    connection = store._connect()
    try:
        row = connection.execute(
            """SELECT 1 FROM preparation_jobs AS job
               WHERE job.job_id = ? AND """
            + _SEMANTIC_BINDING_SQL
            + " LIMIT 1",
            (str(job.get("job_id", "")),),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _fail_atomic_job_terminal(
    store: KnowledgeStore, job_id: str, error: str
) -> None:
    """Cancel a stale semantic job without permitting an inference retry."""

    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE preparation_jobs
               SET status = 'failed', attempts = max_attempts, error = ?,
                   locked_at = '', updated_at = ?
               WHERE job_id = ?""",
            (error[:2000], datetime.now(UTC).isoformat(), job_id),
        )
        store._fail_blocked_jobs(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_semantic_worker_attempt(
    store: KnowledgeStore, job_id: str
) -> None:
    """Persist the latest bound semantic attempt for restart-safe spacing."""

    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT job.updated_at FROM preparation_jobs AS job
               WHERE job.job_id = ? AND job.attempts > 0""",
            (job_id,),
        ).fetchone()
        if row is not None:
            timestamp = str(row["updated_at"] or "")
            current = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_SEMANTIC_LAST_ATTEMPT_META_KEY,),
            ).fetchone()
            last_attempt = str(current["value"] or "") if current else ""
            if timestamp >= last_attempt:
                other_attempts = _nonsemantic_attempt_total(connection)
                connection.executemany(
                    """INSERT INTO schema_meta(key, value) VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (
                        (_SEMANTIC_LAST_ATTEMPT_META_KEY, timestamp),
                        (_SEMANTIC_OTHER_ATTEMPT_META_KEY, str(other_attempts)),
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_semantic_worker_attempt_nonfatal(
    store: KnowledgeStore, job_id: str
) -> None:
    try:
        _record_semantic_worker_attempt(store, job_id)
    except (OSError, sqlite3.Error):
        # The finalized job/state is authoritative. The next cadence read
        # reconstructs this best-effort watermark from durable job timestamps.
        return


def _semantic_enrichment_worker_slot_due(
    store: KnowledgeStore,
    *,
    after_other_jobs: int = _SEMANTIC_RESERVATION_AFTER_OTHER_COMPLETIONS,
) -> bool:
    """Return whether one ready bound semantic job owns the next worker slot.

    Job completions are the durable clock.  A recovered process therefore
    continues the same cadence, while a newly seeded lane with no competing
    work can still run through the work-conserving fallback in ``run_once``.
    """

    after_other_jobs = max(1, min(int(after_other_jobs), 32))
    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        observed = connection.execute(
            """SELECT MAX(job.updated_at) AS updated_at
               FROM preparation_jobs AS job
               WHERE job.attempts > 0 AND job.status <> 'running' AND """
            + _SEMANTIC_INTENT_SQL
        ).fetchone()
        stored = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_SEMANTIC_LAST_ATTEMPT_META_KEY,),
        ).fetchone()
        last_attempt = str(stored["value"] or "") if stored is not None else ""
        observed_at = str(observed["updated_at"] or "") if observed is not None else ""
        other_attempts = _nonsemantic_attempt_total(connection)
        stored_other = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_SEMANTIC_OTHER_ATTEMPT_META_KEY,),
        ).fetchone()
        try:
            baseline_other_attempts = int(stored_other["value"])
        except (TypeError, ValueError):
            baseline_other_attempts = other_attempts
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (
                    _SEMANTIC_OTHER_ATTEMPT_META_KEY,
                    str(baseline_other_attempts),
                ),
            )
        if observed_at > last_attempt:
            last_attempt = observed_at
            baseline_other_attempts = other_attempts
            connection.executemany(
                """INSERT INTO schema_meta(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (
                    (_SEMANTIC_LAST_ATTEMPT_META_KEY, last_attempt),
                    (
                        _SEMANTIC_OTHER_ATTEMPT_META_KEY,
                        str(baseline_other_attempts),
                    ),
                ),
            )
        candidate = connection.execute(
            """SELECT MIN(job.created_at) AS created_at
               FROM preparation_jobs AS job
               WHERE job.status = 'queued' AND job.attempts < job.max_attempts
                 AND job.job_type IN ('prepare-meaning', 'prepare-entity-meaning')
                 AND """
            + _SEMANTIC_BINDING_SQL
            + """
                 AND NOT EXISTS (
                     SELECT 1 FROM job_dependencies AS dependency
                     JOIN preparation_jobs AS prerequisite
                       ON prerequisite.job_id = dependency.depends_on_job_id
                     WHERE dependency.job_id = job.job_id
                       AND prerequisite.status <> 'complete'
                 )"""
        ).fetchone()
        candidate_created_at = (
            str(candidate["created_at"] or "") if candidate is not None else ""
        )
        if not candidate_created_at:
            connection.commit()
            return False
        connection.commit()
        return (
            max(0, other_attempts - baseline_other_attempts)
            >= after_other_jobs
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
        *,
        continuous_growth: bool = False,
    ):
        self.store = store
        self.retriever = retriever
        self.model = model
        self.pronouncer = pronouncer or EspeakPronouncer()
        self.card_store = card_store
        self.japanese_readings = japanese_readings
        self.continuous_growth = continuous_growth

    def _semantic_state_for_job(
        self, job: dict[str, Any]
    ) -> dict[str, Any] | None:
        entity_id = str(job.get("subject_entity_id", "")).strip()
        language = _language_code(str(job.get("language", "") or "en"))
        job_id = str(job.get("job_id", "")).strip()
        fingerprint = str(job.get("source_fingerprint", "")).strip()
        prompt_version = str(job.get("prompt_version", ""))
        state = self.store.lexical_semantic_enrichment_state(entity_id, language)
        state_job_id = str((state or {}).get("enrichment_job_id", "") or "")
        semantic_intent = (
            prompt_version.startswith(_SEMANTIC_JOB_PROMPT_MARKER)
            or state_job_id == job_id
        )
        if not semantic_intent:
            return None
        if (
            state is None
            or not job_id
            or not fingerprint
            or state_job_id != job_id
            or str(state.get("desired_fingerprint", "")) != fingerprint
            or str(state.get("job_type", "")) != str(job.get("job_type", ""))
            or str(state.get("status", "")) not in {"planned", "applied"}
        ):
            raise _StaleSemanticJob(
                "semantic job no longer owns its exact entity fingerprint"
            )
        return state

    def run_once(
        self, job_types: tuple[str, ...] | None = None
    ) -> AtomicRunResult | None:
        requested_job_types = (
            SUPPORTED_ATOMIC_JOBS if job_types is None else tuple(job_types)
        )
        unsupported = set(requested_job_types) - set(SUPPORTED_ATOMIC_JOBS)
        if unsupported:
            raise ValueError(
                "unsupported atomic job types: " + ", ".join(sorted(unsupported))
            )
        claimable_job_types = list(dict.fromkeys(requested_job_types))
        if self.card_store is None:
            claimable_job_types = [
                job_type
                for job_type in claimable_job_types
                if job_type not in MODEL_FREE_ATOMIC_JOBS
            ]
        if not claimable_job_types:
            return None
        job = None
        preferred_subject_keys = self.store.active_lexical_revisit_subject_keys()
        if job_types is None and self.continuous_growth:
            repair_memberships = getattr(
                self.store, "repair_model_enrichment_membership_scopes", None
            )
            if callable(repair_memberships):
                repair_memberships(limit=8)
            from .growth import claim_growth_work

            job = claim_growth_work(
                self.store,
                model=self.model.model_name,
                allowed_job_types=claimable_job_types,
            )
        if job_types is None and job is None:
            semantic_job_types = tuple(
                job_type
                for job_type in _SEMANTIC_ENRICHMENT_JOB_TYPES
                if job_type in claimable_job_types
            )
            if semantic_job_types and _semantic_enrichment_worker_slot_due(
                self.store
            ):
                job = _claim_atomic_job_by_semantic_binding(
                    self.store,
                    semantic_job_types,
                    semantic_binding=True,
                    preferred_subject_keys=preferred_subject_keys,
                )
            word_card_progress = getattr(self.store, "claim_word_card_progress", None)
            if (
                job is None and "compose-word-card" in claimable_job_types
                and callable(word_card_progress)
            ):
                job = word_card_progress()
            ready_composition_types = tuple(
                job_type
                for job_type in MODEL_FREE_ATOMIC_JOBS
                if job_type in claimable_job_types
            )
            if job is None and ready_composition_types:
                # Composition is bounded, model-free work over persisted
                # checkpoints. Claim one ready card before inference so a
                # continuously replenished low-number priority queue cannot
                # strand completed lexical plans. ``claim_next_job`` still
                # enforces dependencies and max_attempts, so a blocked or
                # exhausted compose falls through without delaying Qwen.
                job = self.store.claim_next_job(
                    ready_composition_types,
                    preferred_subject_keys=preferred_subject_keys,
                )
        if (
            job is None
            and
            job_types is None
            and "enrich-morphology-v3" in claimable_job_types
            and self.store.morphology_enrichment_worker_slot_due(
                after_other_jobs=6
            )
        ):
            job = self.store.claim_next_job(("enrich-morphology-v3",))
        if job is None:
            job = (
                _claim_atomic_job_by_semantic_binding(
                    self.store,
                    claimable_job_types,
                    semantic_binding=False,
                    preferred_subject_keys=preferred_subject_keys,
                )
                if job_types is None
                else self.store.claim_next_job(
                    claimable_job_types,
                    preferred_subject_keys=preferred_subject_keys,
                )
            )
        if job is None and job_types is None:
            # Do not idle when semantic work is the only ready work. The next
            # semantic attempt still resets the durable completion cadence.
            job = _claim_atomic_job_by_semantic_binding(
                self.store,
                claimable_job_types,
                semantic_binding=True,
                preferred_subject_keys=preferred_subject_keys,
            )
        if job is None and job_types is None:
            # Preserve recovery behavior for a stale job whose state changed
            # between planning and reconciliation.
            job = self.store.claim_next_job(
                claimable_job_types,
                preferred_subject_keys=preferred_subject_keys,
            )
        if job is None:
            return None
        semantic_enrichment_job = _job_has_semantic_enrichment_binding(
            self.store, job
        )
        try:
            if job["job_type"] == "retrieve-evidence":
                artifact_id = self._retrieve(job)
            elif job["job_type"] == "prepare-meaning":
                artifact_id = self._prepare_meaning(job)
            elif job["job_type"] == "prepare-entity-meaning":
                artifact_id = self._prepare_entity_meaning(job)
            elif job["job_type"] == "enrich-morphology-v3":
                artifact_id = self._enrich_morphology_v3(job)
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
        except _StaleSemanticJob as exc:
            _fail_atomic_job_terminal(self.store, str(job["job_id"]), str(exc))
            return AtomicRunResult(job["job_id"], job["job_type"], "failed", "")
        except Exception as exc:
            if isinstance(exc, AtomicJsonOutputError):
                try:
                    self.store.save_job_artifact(
                        str(job["job_id"]), "model-output-failure",
                        {"model": exc.model, "failures": list(exc.failures)},
                        language=str(job.get("language", "") or "en"),
                        reusable=False, validation_state="rejected", quality_score=0.0,
                    )
                except (OSError, sqlite3.Error, ValueError):
                    # Diagnostic writes must not prevent the normal job retry
                    # or terminal failure from releasing the worker.
                    pass
            self.store.finish_job(job["job_id"], error=str(exc))
            if (
                job["job_type"] in {"prepare-meaning", "prepare-entity-meaning"}
                and str(job.get("source_fingerprint", ""))
                and int(job.get("attempts", 1)) >= int(job.get("max_attempts", 1))
            ):
                self.store.fail_lexical_semantic_enrichment(
                    str(job["subject_entity_id"]),
                    str(job["source_fingerprint"]),
                    str(exc),
                    language=str(job.get("language", "") or "en"),
                    enrichment_job_id=str(job["job_id"]),
                )
            if semantic_enrichment_job:
                _record_semantic_worker_attempt_nonfatal(
                    self.store, str(job["job_id"])
                )
            return AtomicRunResult(job["job_id"], job["job_type"], "retry", "")
        self.store.finish_job(job["job_id"])
        if semantic_enrichment_job:
            _record_semantic_worker_attempt_nonfatal(
                self.store, str(job["job_id"])
            )
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
        source_language = _language_code(term["language"])
        targets = tuple(
            dict.fromkeys(
                str(item.get("language", ""))
                for item in self.store.jobs_for_subject(job["subject_key"])
                if item.get("job_type") == "prepare-translation"
                and str(item.get("language", ""))
            )
        )
        try:
            records = self.retriever.retrieve(
                str(term["text"]),
                source_language=source_language,
                target_languages=targets or DISPLAY_LANGUAGES,
            )
        except TypeError as exc:
            # Preserve small third-party/test adapters during the protocol
            # transition; production retrievers always receive the language.
            if "unexpected keyword argument" not in str(exc):
                raise
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
            {
                "term": term["text"],
                "source_language": source_language,
                "target_languages": list(targets),
                "records": saved,
            },
            language=source_language,
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
        semantic_state = self._semantic_state_for_job(job)
        if semantic_state is not None and str(semantic_state.get("status", "")) == "applied":
            accepted_artifacts = self.store.artifacts_for_job(
                str(job["job_id"]), stage="accepted-meaning"
            )
            if not accepted_artifacts:
                raise _StaleSemanticJob(
                    "applied semantic term job has no accepted artifact"
                )
            completed = self.store.complete_lexical_semantic_enrichment(
                str(job["subject_entity_id"]),
                str(job.get("source_fingerprint", "")),
                language=_language_code(str(job.get("language", "") or "en")),
                enrichment_job_id=str(job["job_id"]),
            )
            if not completed:
                raise _StaleSemanticJob(
                    "semantic term completion rejected the recovered artifact"
                )
            return str(accepted_artifacts[-1]["artifact_id"])
        term = self.store.term_record(str(job["subject_entity_id"]))
        source_language = _language_code(term["language"])
        artifacts = self.store.artifacts_for_subject(
            job["subject_key"], stage="retrieved-evidence"
        )
        if not artifacts:
            raise ValueError("retrieved evidence checkpoint is missing")
        records = artifacts[-1]["payload"].get("records", [])
        allowed_evidence = {
            str(record.get("knowledge_evidence_id", "")) for record in records
        }
        prompt = f"""SOURCE LANGUAGE: {_language_name(source_language)} ({source_language})
TERM: {term['text']}
RETRIEVED EVIDENCE: {self._meaning_context(records)}

Return exactly one JSON object with these keys:
definition: one clear dictionary sentence in {_language_name(source_language)}, at most 30 words
part_of_speech: noun, verb, adjective, adverb, pronoun, preposition,
conjunction, interjection, determiner, numeral, or other
sense_note: at most 18 words; distinguish the selected sense if needed
confidence: number from 0 to 1
evidence_ids: non-empty array containing only supplied id values

Use the retrieved evidence to select one core sense. Do not add etymology,
translations, examples, markdown, or claims absent from the evidence."""
        completion = self.model.complete_json(
            "You prepare one reusable, source-grounded lexical fact in the requested language.",
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
        if semantic_state is not None:
            self._semantic_state_for_job(job)

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
            source_language,
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
        artifact_id = self.store.save_job_artifact(
            job["job_id"],
            "accepted-meaning",
            accepted,
            language=source_language,
            validation_state="accepted",
            quality_score=confidence,
        )
        completed = self.store.complete_lexical_semantic_enrichment(
            str(job["subject_entity_id"]),
            str(job.get("source_fingerprint", "")),
            language=source_language,
            enrichment_job_id=str(job["job_id"]),
        )
        if semantic_state is not None and not completed:
            raise _StaleSemanticJob(
                "semantic term completion rejected the accepted artifact"
            )
        return artifact_id

    def _prepare_entity_meaning(self, job: dict[str, Any]) -> str:
        """Materialize one evidence-grounded meaning for a morphology entity."""

        from .morphology_meaning_prompting import build_morphology_meaning_prompt
        from .semantic_contract import SemanticContextUnavailable

        entity_id = str(job["subject_entity_id"])
        language = _language_code(str(job.get("language", "") or "en"))
        fingerprint = str(job.get("source_fingerprint", ""))

        def current_context() -> dict[str, Any]:
            try:
                return self.store.semantic_enrichment_context(entity_id, language)
            except SemanticContextUnavailable as error:
                raise _StaleSemanticJob(str(error)) from error

        self.store.refresh_lexical_semantic_enrichment(entity_id, language=language)
        semantic_state = self._semantic_state_for_job(job)
        if semantic_state is None:
            raise _StaleSemanticJob(
                "morpheme semantic job has no durable semantic binding"
            )
        context = current_context()
        if (
            str(context.get("entity_type", "")) != "morpheme"
            or str(context.get("entity_kind", "")) not in {"root", "prefix", "suffix"}
            or str(context.get("job_type", "")) != "prepare-entity-meaning"
        ):
            raise _StaleSemanticJob("semantic meaning task requires an accepted morphology entity")
        if not fingerprint or str(context.get("desired_fingerprint", "")) != fingerprint:
            self.store.refresh_lexical_semantic_enrichment(entity_id, language=language)
            raise _StaleSemanticJob("semantic meaning context fingerprint changed")

        accepted_artifacts = self.store.artifacts_for_job(
            str(job["job_id"]), stage="accepted-entity-meaning"
        )
        if accepted_artifacts:
            artifact = accepted_artifacts[-1]
            completed = self.store.complete_lexical_semantic_enrichment(
                entity_id,
                fingerprint,
                language=language,
                enrichment_job_id=str(job["job_id"]),
            )
            if not completed:
                raise _StaleSemanticJob(
                    "semantic morpheme completion rejected the recovered artifact"
                )
            return str(artifact["artifact_id"])

        if bool(context.get("has_current_meaning", False)):
            raise _StaleSemanticJob("semantic subject already has a current meaning")

        semantic_context = context.get("context")
        if not isinstance(semantic_context, dict):
            raise ValueError("semantic meaning context is missing")
        entities = [
            item
            for item in semantic_context.get("entities", [])
            if isinstance(item, dict)
        ]
        focus = next(
            (item for item in entities if str(item.get("id", "")) == entity_id),
            {},
        )
        label = re.sub(r"\s+", " ", str(focus.get("label", ""))).strip()
        if not label:
            raise ValueError("semantic meaning subject label is missing")

        system, prompt, inference = build_morphology_meaning_prompt(
            entity_id=entity_id, label=label, kind=str(context["entity_kind"]),
            language=language, language_name=_language_name(language),
            context=semantic_context,
        )
        if not inference["selected_evidence_ids"]:
            from .retrieval import AffixRag, RootRag
            from .semantic_retrieval import retrieve_semantic_books

            book_retriever = self.retriever
            if isinstance(book_retriever, WordEvidenceRetriever):
                engine = RootRag if str(context["entity_kind"]) == "root" else AffixRag
                book_retriever = engine(
                    book_retriever.roots, book_retriever.affixes, book_retriever.corpus
                ) if engine is RootRag else engine(
                    book_retriever.affixes, book_retriever.roots, book_retriever.corpus
                )
            retrieved, retrieval_trace = retrieve_semantic_books(
                book_retriever, entity_id=entity_id, label=label,
                source_language=str(context.get("source_language") or language),
                context=semantic_context,
            )
            inference["book_retrieval"] = retrieval_trace
            if retrieved:
                def persist_retrieval() -> list[str]:
                    self._semantic_state_for_job(job)
                    latest = current_context()
                    if str(latest.get("desired_fingerprint", "")) != fingerprint:
                        raise _StaleSemanticJob("semantic inputs changed during book retrieval")
                    identifiers = self.store.link_retrieved_semantic_evidence(
                        entity_id, retrieved
                    )
                    self.store.save_job_artifact(
                        str(job["job_id"]), "semantic-book-retrieval",
                        {"source_fingerprint": fingerprint, "evidence_ids": identifiers,
                         "retrieval": retrieval_trace},
                        language=language, reusable=True,
                        validation_state="candidate", quality_score=0.5,
                    )
                    return identifiers

                self.store.atomic_semantic_write(entity_id, persist_retrieval)
                self.store.refresh_lexical_semantic_enrichment(entity_id, language=language)
                # New corpus context belongs to a fresh durable input version.
                # Do not consume inference on the obsolete, evidence-free claim.
                raise _StaleSemanticJob("local book evidence added; fresh semantic pass required")

        allowed_evidence = set(inference["selected_evidence_ids"])
        aliases = inference["evidence_aliases"]
        definition_evidence = set(inference["definition_evidence_ids"])
        completion = self.model.complete_json(system, prompt, max_tokens=224)
        self.store.save_job_artifact(
            str(job["job_id"]),
            "entity-meaning-model-draft",
            {"context_fingerprint": fingerprint, "completion": completion,
             "meaning_inference": inference},
            language=language,
            reusable=True,
            validation_state="candidate",
            quality_score=0.5,
        )
        value = completion.get("value")
        if not isinstance(value, dict):
            raise ValueError("entity meaning task did not return an object")
        definition = re.sub(
            r"\s+", " ", str(value.get("meaning") or value.get("definition") or "")
        ).strip()
        if not definition:
            raise ValueError("entity meaning is empty")
        if any(marker in definition for marker in _ENCODING_DAMAGE):
            raise ValueError("entity meaning contains encoding damage")
        if len(definition) > 280:
            clipped = definition[:277].rsplit(" ", 1)[0].rstrip(" ,;:-")
            definition = f"{clipped or definition[:277]}..."
        selected = (
            list(
                dict.fromkeys(
                    aliases.get(str(item), str(item))
                    for item in value.get("evidence_ids", [])
                    if aliases.get(str(item), str(item)) in allowed_evidence
                )
            )
            if isinstance(value.get("evidence_ids"), list)
            else []
        )
        try:
            confidence = max(0.05, min(float(value.get("confidence", 0.6)), 0.99))
        except (TypeError, ValueError):
            confidence = 0.6
        if not selected:
            confidence = min(confidence, 0.35)
            grounding = "model-context-only"
        elif definition_evidence.intersection(selected):
            confidence = min(confidence, 0.8)
            grounding = "definition-local-context"
        else:
            confidence = min(confidence, 0.55)
            grounding = "related-local-context"
        model_name = str(completion.get("model", self.model.model_name))
        def persist() -> str:
            self._semantic_state_for_job(job)
            latest = current_context()
            if str(latest.get("desired_fingerprint", "")) != fingerprint:
                raise _StaleSemanticJob("meaning evidence changed during model inference")
            meaning_id = self.store.add_entity_meaning(
                entity_id,
                definition,
                language=language,
                domain_label=str(context["entity_kind"]),
                status="accepted",
                quality_score=confidence,
                basis="model",
                confidence=confidence,
                evidence_ids=selected,
                metadata={
                    "semantic_enrichment_job_id": str(job["job_id"]),
                    "source_fingerprint": fingerprint,
                    "grounding": grounding,
                    "meaning_prompt_policy": inference["policy"],
                    "context_evidence_ids": inference["selected_evidence_ids"],
                    "definition_evidence_ids": inference["definition_evidence_ids"],
                },
                revision_model=model_name,
                revision_prompt_version=str(job.get("prompt_version", "")),
                revision_reason="atomic morphology meaning preparation",
            )
            for evidence_id in selected:
                self.store.link_evidence(
                    meaning_id, evidence_id, claim=definition, confidence=confidence
                )
            accepted = {
                "entity_id": entity_id,
                "meaning_id": meaning_id,
                "definition": definition,
                "entity_kind": str(context["entity_kind"]),
                "language": language,
                "confidence": confidence,
                "evidence_ids": selected,
                "model": model_name,
                "metrics": completion.get("metrics", {}),
                "source_fingerprint": fingerprint,
                "grounding": grounding,
                "meaning_inference": inference,
            }
            artifact_id = self.store.save_job_artifact(
                str(job["job_id"]),
                "accepted-entity-meaning",
                accepted,
                language=language,
                reusable=True,
                validation_state="accepted",
                quality_score=confidence,
            )
            completed = self.store.complete_lexical_semantic_enrichment(
                entity_id,
                fingerprint,
                language=language,
                enrichment_job_id=str(job["job_id"]),
            )
            if not completed:
                raise _StaleSemanticJob(
                    "semantic morpheme completion rejected the accepted artifact"
                )
            return artifact_id

        return self.store.atomic_semantic_write(entity_id, persist)

    def _enrich_morphology_v3(self, job: dict[str, Any]) -> str:
        """Turn one retained v3 evidence cluster into typed, replayable claims."""

        store = getattr(self, "store", None)
        if store is None:
            store = getattr(self, "_store")
        model = getattr(self, "model", None)
        if model is None:
            model = getattr(self, "_model")
        job_id = str(job["job_id"])
        context = store.morphology_enrichment_context(job_id)
        accepted = store.artifacts_for_job(
            job_id, stage="accepted-morphology-v3-enrichment"
        )
        if accepted:
            artifact = accepted[-1]
            payload = dict(artifact.get("payload") or {})
            repair_memberships = getattr(store, "repair_model_enrichment_membership_scopes", None)
            if callable(repair_memberships):
                repair_memberships(limit=8)
            fingerprint = str(payload.get("output_fingerprint") or "")
            if not fingerprint:
                fingerprint = _morph_hashlib.sha256(
                    _morph_json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            store.complete_morphology_v3_enrichment(
                job_id,
                output_fingerprint=fingerprint,
                outcome={
                    "outcome": "accepted-replay",
                    "artifact_id": str(artifact["artifact_id"]),
                    "warnings": list(payload.get("warnings") or []),
                },
            )
            return str(artifact["artifact_id"])

        cluster_payload = dict(context.get("payload") or {})
        evidence = list(context.get("evidence") or [])
        drafts = store.artifacts_for_job(
            job_id, stage="morphology-v3-model-draft"
        )
        metrics: dict[str, Any] = {}
        inference: dict[str, Any] = {"policy": "legacy-v3-unfocused"}
        if drafts:
            saved = dict(drafts[-1].get("payload") or {})
            draft = saved.get("draft", {})
            if not isinstance(draft, dict):
                draft = {}
            if isinstance(saved.get("metrics"), dict):
                metrics = dict(saved["metrics"])
            if isinstance(saved.get("inference"), dict):
                inference = dict(saved["inference"])
        else:
            system, prompt, inference = build_morphology_enrichment_prompt(
                cluster_payload, evidence
            )
            response = model.complete_json(
                system, prompt, max_tokens=MORPHOLOGY_ENRICHMENT_MAX_TOKENS
            )
            if isinstance(response, dict) and "value" in response:
                raw_draft = response.get("value")
                raw_metrics = response.get("metrics", {})
            elif isinstance(response, tuple):
                raw_draft = response[0]
                raw_metrics = response[1] if len(response) > 1 else {}
            else:
                raw_draft = response
                raw_metrics = {}
            if isinstance(raw_draft, str):
                try:
                    raw_draft = _morph_json.loads(raw_draft)
                except (TypeError, ValueError):
                    raw_draft = {}
            draft = raw_draft if isinstance(raw_draft, dict) else {}
            if isinstance(raw_metrics, dict):
                metrics = raw_metrics
            elif hasattr(raw_metrics, "__dict__"):
                metrics = dict(vars(raw_metrics))
            elif raw_metrics:
                metrics = {"value": str(raw_metrics)}
            store.save_job_artifact(
                job_id,
                "morphology-v3-model-draft",
                {"draft": draft, "metrics": metrics, "inference": inference},
                language="",
                reusable=True,
                validation_state="candidate",
                quality_score=0.5,
            )

        warnings = [
            str(item)[:500]
            for item in draft.get("warnings", [])
            if isinstance(item, (str, int, float)) and str(item).strip()
        ] if isinstance(draft.get("warnings", []), list) else []
        allowed_kinds = {"root", "prefix", "suffix"}
        allowed_relations = {
            "allomorph-of",
            "opposed-to",
            "derived-from",
            "historical-form-of",
        }
        component_details: dict[tuple[str, str], dict[str, Any]] = {}

        def confidence(value: Any, default: float = 0.55) -> float:
            try:
                return max(0.05, min(float(value), 0.8))
            except (TypeError, ValueError):
                return default

        def clean_kind(value: Any) -> str:
            kind = str(value or "").strip().lower().replace("_", "-")
            aliases = {"stem": "root", "prefixal": "prefix", "suffixal": "suffix"}
            kind = aliases.get(kind, kind)
            return kind if kind in allowed_kinds else ""

        def clean_form(value: Any) -> str:
            return " ".join(str(value or "").strip().split())[:160]

        def clean_language(value: Any, default: str = "en") -> str:
            raw = clean_form(value).casefold().replace("_", "-")
            aliases = {
                "english": "en", "latin": "la", "ancient greek": "grc",
                "greek": "grc", "old french": "fro", "french": "fr",
                "arabic": "ar", "japanese": "ja", "chinese": "zh",
            }
            raw = aliases.get(raw, raw)
            if raw and len(raw) <= 24 and all(
                part.isalpha() and 1 < len(part) < 9 for part in raw.split("-")
            ):
                return raw
            return default

        raw_components = draft.get("components", [])
        if isinstance(raw_components, list):
            for item in raw_components:
                if not isinstance(item, dict):
                    warnings.append("ignored non-object component")
                    continue
                form = clean_form(item.get("form"))
                kind = clean_kind(item.get("kind"))
                if not form or not kind:
                    warnings.append("ignored component without a typed root/prefix/suffix")
                    continue
                component_details[(form.casefold().strip("- "), kind)] = item

        focus = dict(cluster_payload.get("focus") or {})
        focus_form_key = clean_form(focus.get("component_form")).casefold().strip("- ")
        focus_kind = clean_kind(focus.get("component_kind"))
        focus_entity_id = str(focus.get("component_entity_id") or "")
        made_entities: set[str] = set()
        made_terms: set[str] = set()
        made_edges: set[str] = set()
        made_assertions: set[str] = set()
        claims: list[dict[str, Any]] = []

        def record_assertion(
            subject_id: str,
            source_id: str,
            target_id: str,
            relation: str,
            score: float,
            properties: dict[str, Any] | None = None,
        ) -> None:
            assertion_id = store.accept_relation_assertion(
                subject_id,
                source_id,
                target_id,
                relation,
                basis="model",
                confidence=score,
                properties={
                    **(properties or {}),
                    "context_evidence_ids": list(
                        cluster_payload.get("evidence_ids") or []
                    ),
                    "enrichment_job_id": job_id,
                },
                evidence_ids=(),
                owner_type="morphology-enrichment",
                owner_id=job_id,
                owner_version=ENRICHMENT_MEMBERSHIP_POLICY,
            )
            made_assertions.add(assertion_id)

        def component_values(
            form_value: Any,
            kind_value: Any,
            item: dict[str, Any],
        ) -> tuple[str, str, str, str, float]:
            form = clean_form(form_value)
            kind = clean_kind(kind_value)
            detail = component_details.get((form.casefold().strip("- "), kind), {})
            meaning = clean_form(item.get("meaning") or detail.get("meaning"))
            language = clean_language(
                item.get("component_language")
                or item.get("language")
                or detail.get("language")
                or "en"
            )
            score = confidence(item.get("confidence", detail.get("confidence")))
            return form, kind, meaning, language, score

        def proposed_component(
            form: str,
            kind: str,
            meaning: str,
            language: str,
            score: float,
        ) -> str:
            if (
                focus_entity_id
                and focus_form_key == form.casefold().strip("- ")
                and (not focus_kind or focus_kind == kind)
            ):
                return focus_entity_id
            return store.upsert_morpheme(
                language,
                form,
                kind,
                meaning,
                status="draft",
                quality_score=score,
            )

        def accept_component(
            entity_id: str,
            form: str,
            kind: str,
            meaning: str,
            language: str,
            score: float,
        ) -> None:
            if entity_id == focus_entity_id:
                made_entities.add(entity_id)
                return
            accepted_id = store.upsert_morpheme(
                language,
                form,
                kind,
                meaning,
                status="accepted",
                quality_score=score,
            )
            made_entities.add(accepted_id)

        raw_term_components = draft.get("term_components", [])
        if isinstance(raw_term_components, list):
            memberships = classify_term_component_claims(raw_term_components)
            for item, membership in zip(raw_term_components, memberships):
                if not isinstance(item, dict):
                    warnings.append("ignored non-object term component")
                    continue
                term = clean_form(item.get("term"))
                form, kind, meaning, language, score = component_values(
                    item.get("component_form"), item.get("component_kind"), item
                )
                if not term or not form or not kind:
                    warnings.append("ignored untyped or incomplete term-component claim")
                    continue
                term_language = clean_language(item.get("term_language") or "en")
                focus_term = str(focus.get("term") or "")
                focus_term_id = str(focus.get("term_entity_id") or "")
                if focus_term_id and term.casefold() == focus_term.casefold():
                    term_id = focus_term_id
                else:
                    term_id = store.upsert_term(
                        term_language,
                        term,
                        kind="word",
                        status="draft",
                        quality_score=score,
                        payload={"source": "local-qwen-morphology-v3"},
                    )
                component_id = proposed_component(form, kind, meaning, language, score)
                if term_id == component_id:
                    warnings.append("ignored self-referential term-component claim")
                    continue
                try:
                    edge_id = store.add_edge(
                        term_id, component_id, "shares-component",
                        basis="model", confidence=score,
                        properties={
                            **membership,
                            "surface": clean_form(item.get("surface") or form),
                            "context_evidence_ids": list(cluster_payload.get("evidence_ids") or []),
                            "enrichment_job_id": job_id,
                        },
                    )
                except ValueError as exc:
                    warnings.append(f"ignored invalid term-component claim: {exc}"[:500])
                    continue
                record_assertion(
                    term_id,
                    term_id,
                    component_id,
                    "shares-component",
                    score,
                    {**membership, "surface": clean_form(item.get("surface") or form)},
                )
                if term_id != focus_term_id:
                    term_id = store.upsert_term(
                        term_language,
                        term,
                        kind="word",
                        status="accepted",
                        quality_score=score,
                        payload={"source": "local-qwen-morphology-v3"},
                    )
                accept_component(component_id, form, kind, meaning, language, score)
                made_entities.update((term_id, component_id))
                made_terms.add(term_id)
                made_edges.add(edge_id)
                claims.append(
                    {"type": "derivative", "term": term, "component": form,
                     "kind": kind, "confidence": score}
                )

        raw_history = draft.get("history", [])
        if isinstance(raw_history, list):
            for item in raw_history:
                if not isinstance(item, dict):
                    warnings.append("ignored non-object history claim")
                    continue
                current, current_kind, meaning, current_language, score = component_values(
                    item.get("current_form"), item.get("current_kind"), item
                )
                historical = clean_form(item.get("historical_form"))
                historical_kind = clean_kind(item.get("historical_kind") or current_kind)
                historical_language = clean_language(item.get("language") or "en")
                if not current or not current_kind or not historical or not historical_kind:
                    warnings.append("ignored untyped or incomplete history claim")
                    continue
                current_id = proposed_component(
                    current, current_kind, meaning, current_language, score
                )
                historical_id = proposed_component(
                    historical, historical_kind, meaning, historical_language, score
                )
                if current_id == historical_id:
                    warnings.append("ignored self-referential history claim")
                    continue
                edge_id = store.add_edge(
                    current_id,
                    historical_id,
                    "derived-from",
                    basis="model",
                    confidence=score,
                    properties={
                        "period": clean_form(item.get("period")),
                        "context_evidence_ids": list(cluster_payload.get("evidence_ids") or []),
                        "enrichment_job_id": job_id,
                    },
                )
                record_assertion(
                    current_id,
                    current_id,
                    historical_id,
                    "derived-from",
                    score,
                    {"period": clean_form(item.get("period"))},
                )
                accept_component(
                    current_id, current, current_kind, meaning, current_language, score
                )
                accept_component(
                    historical_id, historical, historical_kind, meaning,
                    historical_language, score
                )
                made_edges.add(edge_id)
                claims.append(
                    {"type": "history", "current": current,
                     "historical": historical, "confidence": score}
                )

        raw_relations = draft.get("relations", [])
        if isinstance(raw_relations, list):
            for item in raw_relations:
                if not isinstance(item, dict):
                    warnings.append("ignored non-object morphology relation")
                    continue
                source, source_kind, source_meaning, source_language, score = component_values(
                    item.get("source_form"), item.get("source_kind"), item
                )
                target = clean_form(item.get("target_form"))
                target_kind = clean_kind(item.get("target_kind"))
                relation = clean_form(item.get("relation")).lower()
                target_language = clean_language(
                    item.get("target_language") or item.get("language") or "en"
                )
                if (
                    not source or not source_kind or not target or not target_kind
                    or relation not in allowed_relations
                ):
                    warnings.append("ignored untyped or unsupported morphology relation")
                    continue
                source_id = proposed_component(
                    source, source_kind, source_meaning, source_language, score
                )
                target_id = proposed_component(
                    target, target_kind, clean_form(item.get("target_meaning")),
                    target_language, score
                )
                if source_id == target_id:
                    warnings.append("ignored self-referential morphology relation")
                    continue
                edge_id = store.add_edge(
                    source_id,
                    target_id,
                    relation,
                    basis="model",
                    confidence=score,
                    properties={
                        "context_evidence_ids": list(cluster_payload.get("evidence_ids") or []),
                        "enrichment_job_id": job_id,
                    },
                )
                record_assertion(
                    source_id,
                    source_id,
                    target_id,
                    relation,
                    score,
                )
                accept_component(
                    source_id, source, source_kind, source_meaning, source_language, score
                )
                accept_component(
                    target_id, target, target_kind,
                    clean_form(item.get("target_meaning")), target_language, score
                )
                made_edges.add(edge_id)
                claims.append(
                    {"type": "relation", "source": source, "target": target,
                     "relation": relation, "confidence": score}
                )

        repair_memberships = getattr(store, "repair_model_enrichment_membership_scopes", None)
        if made_terms and callable(repair_memberships):
            ordered_terms = sorted(made_terms)
            for start in range(0, len(ordered_terms), 8):
                repair_memberships(term_entity_ids=ordered_terms[start:start + 8])
        if not claims:
            warnings.append("local Qwen returned no safe typed connected claim")
        accepted_payload = {
            "cluster_id": str(context["cluster_id"]),
            "input_fingerprint": str(context["input_fingerprint"]),
            "claims": claims,
            "membership_policy": ENRICHMENT_MEMBERSHIP_POLICY,
            "entity_ids": sorted(made_entities),
            "edge_ids": sorted(made_edges),
            "assertion_ids": sorted(made_assertions),
            "context_evidence_ids": list(cluster_payload.get("evidence_ids") or []),
            "warnings": list(dict.fromkeys(warnings)),
            "metrics": metrics,
            "inference": inference,
        }
        output_fingerprint = _morph_hashlib.sha256(
            _morph_json.dumps(
                accepted_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        accepted_payload["output_fingerprint"] = output_fingerprint
        scores = [float(item.get("confidence", 0.0)) for item in claims]
        artifact_id = store.save_job_artifact(
            job_id,
            "accepted-morphology-v3-enrichment",
            accepted_payload,
            language="",
            reusable=True,
            validation_state="accepted",
            quality_score=(sum(scores) / len(scores)) if scores else 0.0,
        )
        store.complete_morphology_v3_enrichment(
            job_id,
            output_fingerprint=output_fingerprint,
            outcome={
                "outcome": "accepted" if claims else "accepted-without-safe-claim",
                "artifact_id": artifact_id,
                "claim_count": len(claims),
                "warning_count": len(accepted_payload["warnings"]),
            },
        )
        return artifact_id

    def _split_morphemes(self, job: dict[str, Any]) -> str:
        from .morpheme_gloss import MORPHEME_GLOSS_POLICY, prepare_morpheme_gloss

        source = self.store.term_record(str(job["subject_entity_id"]))
        if str(source.get("status", "")).strip().casefold() == "archived":
            raise _StaleSemanticJob("morpheme split subject was archived before persistence")
        if not supports_english_morphology(source["language"]):
            raise ValueError("morpheme splitting is limited to English book morphology")
        evidence_artifacts = self.store.artifacts_for_subject(
            job["subject_key"], stage="retrieved-evidence", validation_state="candidate",
        )
        if not evidence_artifacts:
            raise ValueError("current retrieval checkpoint is missing")
        records = list(evidence_artifacts[-1]["payload"].get("records", []))
        letters = re.sub(r"[^A-Za-z]", "", str(source["text"])).casefold()
        explicit_shape = _book_decomposition_shape(letters, records)
        if not explicit_shape:
            seen_hints: set[tuple[str, str, str, int, int]] = set()
            for kind, surface, start, end in component_candidates(letters):
                for record in self.retriever.component_evidence(surface, kind):
                    key = (
                        str(record.get("corpus_id", "")), str(record.get("entry_id", "")),
                        kind, start, end,
                    )
                    if key in seen_hints:
                        continue
                    seen_hints.add(key)
                    evidence_id = self.store.add_evidence(
                        key[0], key[1], source_hash=str(record.get("source_hash", "")),
                        locator=str(record.get("locator", "")),
                        excerpt=str(record.get("excerpt", "")), payload=record,
                    )
                    records.append({
                        **record, "knowledge_evidence_id": evidence_id,
                        "component_hint": kind, "component_surface": surface,
                        "component_start": start, "component_end": end,
                    })
            explicit_shape = _book_decomposition_shape(letters, records)
        allowed_evidence = {
            str(record.get("knowledge_evidence_id", "")) for record in records
        }
        previous_splits = self.store.artifacts_for_subject(
            job["subject_key"], stage="accepted-morpheme-split", validation_state="accepted",
        )
        previous_parts = list(previous_splits[-1]["payload"].get("parts", [])) if previous_splits else []
        existing_graph = self._lexical_view(
            str(source["entity_id"]), "word", [str(source["entity_id"])],
        )
        required_shape = explicit_shape
        context = pack_morpheme_context(
            str(source["text"]), records, required_shape, previous_parts,
            existing_graph, str(job["job_id"]),
        )
        input_fingerprint = morpheme_context_fingerprint(str(source["text"]), context)
        checkpoint = select_morpheme_checkpoint(
            self.store.artifacts_for_job(job["job_id"], stage="model-morpheme-draft"),
            self.store.artifacts_for_job(job["job_id"], stage="morpheme-prompt-context"),
            term=str(source["text"]), fingerprint=input_fingerprint,
            prompt_policy=MORPHEME_PROMPT_POLICY, model_name=self.model.model_name,
        )
        self.store.save_job_artifact(
            job["job_id"], "morpheme-prompt-context",
            {"term": source["text"], "policy": MORPHEME_PROMPT_POLICY, "context": context},
            language=source["language"], validation_state="candidate",
        )
        if checkpoint:
            saved = checkpoint["payload"]
            completion = {
                "value": deepcopy(saved["value"]), "model": saved["model"],
                "metrics": deepcopy(saved.get("metrics", {})),
            }
        else:
            completion = self.model.complete_json(
                "Analyze present-day word structure using retrieved books and your linguistic knowledge.",
                build_morpheme_prompt(str(source["text"]), context),
                max_tokens=800 if explicit_shape else 720,
            )
        fresh_model_calls = 0 if checkpoint else 1
        if not self.store.semantic_subject_writable(str(source["entity_id"])):
            raise _StaleSemanticJob("morpheme split subject was archived during inference")
        inference_trace = [{
            "stage": "cached-draft" if checkpoint else "draft",
            "checkpoint_artifact_id": str(checkpoint["artifact_id"]) if checkpoint else "",
            "model": completion.get("model", self.model.model_name),
            "metrics": deepcopy(completion.get("metrics", {})),
        }]
        raw_value = completion.get("value")
        value, draft_normalizations = normalize_morpheme_draft(
            raw_value, letters, required_shape,
        )
        normalizations = [
            {"stage": "draft", **item} for item in draft_normalizations
        ]
        if not checkpoint:
            draft_artifact_id = self.store.save_job_artifact(
                job["job_id"], "model-morpheme-draft",
                {"term": source["text"], "value": raw_value,
                 "normalizations": draft_normalizations,
                 "inference_policy": MORPHEME_PROMPT_POLICY,
                 "input_fingerprint": input_fingerprint,
                 "model": completion.get("model", self.model.model_name),
                 "metrics": completion.get("metrics", {})},
                language=source["language"], validation_state="candidate",
            )
        else:
            draft_artifact_id = str(checkpoint["artifact_id"])
        first_draft_derivatives: list[Any] = []
        if isinstance(value, dict):
            proposed = value.get("derivatives", value.get("related_terms", []))
            if isinstance(proposed, list):
                first_draft_derivatives = list(proposed)
        value = _augment_exact_free_draft(value, required_shape, letters)
        review_reason = morpheme_review_reason(
            value, letters, required_shape, records, previous_parts,
        )
        retained_first_draft = False
        repair_trace: dict[str, Any] = {}
        if review_reason:
            repair_prompt, repair_trace = build_morpheme_repair_prompt(
                str(source["text"]), context, value, review_reason,
            )
            if morpheme_structure_error(value, letters, required_shape):
                self.store.save_job_artifact(
                    job["job_id"], "partial-morpheme-analysis",
                    {
                        "term": source["text"],
                        "source_artifact_id": draft_artifact_id,
                        "input_fingerprint": input_fingerprint,
                        "proposed_parts": deepcopy(value.get("parts", []))
                        if isinstance(value, dict) else [],
                        "derivatives": deepcopy(first_draft_derivatives),
                        "structure_error": review_reason,
                        "repair": repair_trace,
                    },
                    language=source["language"], validation_state="candidate",
                    reusable=True,
                )
            reviewed = self.model.complete_json(
                "Repair one word decomposition using book evidence and linguistic knowledge.",
                repair_prompt, max_tokens=MORPHEME_REPAIR_MAX_TOKENS,
            )
            fresh_model_calls += 1
            if not self.store.semantic_subject_writable(str(source["entity_id"])):
                raise _StaleSemanticJob("morpheme split subject was archived during review")
            inference_trace.append({
                "stage": "review", "model": reviewed.get("model", self.model.model_name),
                "metrics": deepcopy(reviewed.get("metrics", {})),
            })
            raw_reviewed_value = reviewed.get("value")
            reviewed_value, review_normalizations = normalize_morpheme_draft(
                raw_reviewed_value, letters, required_shape,
            )
            normalizations.extend(
                {"stage": "review", **item} for item in review_normalizations
            )
            reviewed_error = morpheme_structure_error(reviewed_value, letters, required_shape)
            retained_first_draft = bool(
                reviewed_error and not morpheme_structure_error(value, letters, required_shape)
            )
            self.store.save_job_artifact(
                job["job_id"], "model-morpheme-review-draft",
                {"term": source["text"], "value": raw_reviewed_value,
                 "normalizations": review_normalizations,
                 "review_reason": review_reason, "remaining_structure_error": reviewed_error,
                 "repair": repair_trace,
                 "retained_first_draft": retained_first_draft,
                 "model": reviewed.get("model", self.model.model_name),
                 "metrics": reviewed.get("metrics", {})},
                language=source["language"],
                validation_state="rejected" if reviewed_error else "candidate",
            )
            if not retained_first_draft:
                value = reviewed_value
                if isinstance(value, dict):
                    derivatives = value.get("derivatives", value.get("related_terms", []))
                    value = {**value, "derivatives": [
                        *first_draft_derivatives,
                        *(derivatives if isinstance(derivatives, list) else []),
                    ]}
                completion = reviewed
        value = finalize_morpheme_analysis(
            value, str(source["text"]), previous_parts, required_shape,
        )
        previous_related = (
            previous_splits[-1]["payload"].get("related_terms", [])
            if previous_splits else []
        )
        current_related = value.get("derivatives", value.get("related_terms", []))
        value["derivatives"] = [
            *(current_related if isinstance(current_related, list) else []),
            *(previous_related if isinstance(previous_related, list) else []),
        ]
        partial_analysis = value["analysis_scope"] == "partial"
        if isinstance(value, dict):
            value = {**value, "morphology_inference": {
                "policy": MORPHEME_PROMPT_POLICY,
                "input_fingerprint": input_fingerprint,
                "draft_artifact_id": draft_artifact_id,
                "normalization_policy": MORPHEME_NORMALIZATION_POLICY,
                "recovery_policy": MORPHEME_RECOVERY_POLICY,
                "draft_checkpoint_artifact_id": str(checkpoint["artifact_id"]) if checkpoint else "",
                "fresh_model_calls": fresh_model_calls,
                "repair": repair_trace,
                "normalizations": normalizations,
                "context": context["diagnostics"],
                "review_reason": review_reason,
                "retained_first_draft": retained_first_draft,
                "inference_trace": inference_trace,
            }}

        raw_parts = value.get("parts") if isinstance(value, dict) else None
        if not isinstance(raw_parts, list) or not (0 if partial_analysis else 1) <= len(raw_parts) <= 8:
            raise ValueError("morpheme task returned an invalid number of parts")
        cleaned: list[dict[str, Any]] = []
        for item in raw_parts:
            if not isinstance(item, dict):
                raise ValueError("morpheme part is not an object")
            surface = re.sub(r"[^A-Za-z]", "", str(item.get("surface", "")))
            kind = str(item.get("kind", "")).strip().lower()
            supplied_canonical = str(item.get("canonical_form", "")).strip()
            supplied_language = str(item.get("language", "en")).strip().casefold()
            language = normalize_language_code(
                _ORIGIN_LANGUAGE_CODES.get(supplied_language, supplied_language)
            ) or "und"
            proposed_meaning = item.get("meaning")
            gloss = prepare_morpheme_gloss(
                proposed_meaning, _clean_morpheme_meaning(proposed_meaning)
            )
            meaning = gloss["meaning"]
            confidence = max(0.0, min(float(item.get("confidence", 0.0)), 1.0))
            if not surface or kind not in {"prefix", "root", "suffix", "free"}:
                raise ValueError("morpheme surface or kind is invalid")
            # Meaning is independently repairable. Do not throw away valid
            # morphology and derivatives for punctuation or an optional gloss.
            if not supplied_canonical:
                raise ValueError("morpheme canonical form is invalid")
            canonical_base = unicodedata.normalize("NFC", supplied_canonical.strip("-").strip())
            if len(canonical_base) > 64 or not re.fullmatch(
                r"\*?[^\W\d_]+(?:[-'][^\W\d_]+)*", canonical_base,
            ):
                raise ValueError("canonical form is not one lexical form")
            canonical = _morpheme_display_form(canonical_base, kind)
            normalizations: list[str] = []
            if canonical != supplied_canonical:
                normalizations.append("canonical-affix-notation")
            if gloss["meaning_pending"]:
                normalizations.append("deferred-component-meaning")
            elif meaning != re.sub(
                r"\s+", " ", str(item.get("meaning", "")).strip()
            ):
                normalizations.append("plain-meaning-phrase")
            selected = [
                str(evidence_id)
                for evidence_id in item.get("evidence_ids", [])
                if str(evidence_id) in allowed_evidence
            ] if isinstance(item.get("evidence_ids"), list) else []
            if confidence < 0.35:
                confidence = 0.35
                normalizations.append("retained-low-confidence-model-analysis")
            cleaned.append(
                {
                    "surface": surface,
                    "canonical_form": canonical,
                    "kind": kind,
                    "language": language,
                    **gloss,
                    "confidence": confidence,
                    "context_evidence_ids": list(dict.fromkeys(selected)),
                    "evidence_ids": [],
                    "normalizations": normalizations,
                }
            )
        value["morphology_inference"]["glosses"] = {
            "policy": MORPHEME_GLOSS_POLICY,
            "pending_forms": [
                part["canonical_form"] for part in cleaned if part["meaning_pending"]
            ],
        }
        if not partial_analysis and "".join(part["surface"] for part in cleaned).casefold() != letters:
            raise ValueError("morpheme surfaces do not reproduce the source term")
        if not partial_analysis and not any(part["kind"] in {"root", "free"} for part in cleaned):
            raise ValueError("lexical analysis has no root or free base after review")
        if required_shape and not partial_analysis:
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

        # Model/formula evidence is useful retrieval context but does not prove
        # a component role. Rebuild citations exclusively from live typed book
        # authority so an untyped formula surface remains model-basis.
        for part in cleaned:
            part["evidence_ids"] = []
        if not partial_analysis:
            cleaned = _apply_cited_component_roles(
                cleaned,
                self.store.accepted_morphology_components(
                    source["entity_id"], authoritative_only=True
                ),
            )
        for part, proposal in zip(cleaned, raw_parts, strict=True):
            if isinstance(proposal.get("source_span"), list):
                part["source_span"] = list(proposal["source_span"])
        for part in cleaned:
            retrieved = _retrieved_component_evidence_ids(
                part["canonical_form"], part["kind"], records
            )
            part["evidence_ids"] = list(
                dict.fromkeys([*retrieved, *part["evidence_ids"]])
            )
            if part["canonical_form"].strip("-").casefold() != part["surface"].casefold():
                # A cited surface/role does not itself attest the model's
                # canonical spelling adjustment. Retain it as model knowledge.
                part["context_evidence_ids"] = list(dict.fromkeys([
                    *part.get("context_evidence_ids", []), *part["evidence_ids"],
                ]))
                part["evidence_ids"] = []
                part["canonical_basis"] = "model"
            direct: list[dict[str, Any]] = []
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

        try:
            return self.store.atomic_semantic_write(
                str(source["entity_id"]),
                lambda: self._persist_accepted_morpheme_split(
                    job, source, cleaned, value, completion
                ),
            )
        except ValueError as exc:
            if str(exc).startswith("semantic subject is not writable:"):
                raise _StaleSemanticJob(
                    "morpheme split subject was archived before persistence"
                ) from exc
            raise

    def _persist_accepted_morpheme_split(
        self,
        job: dict[str, Any],
        source: dict[str, Any],
        cleaned: list[dict[str, Any]],
        value: dict[str, Any],
        completion: dict[str, Any],
    ) -> str:
        """Write one accepted lexical split inside the store-owned transaction."""

        accepted_parts: list[dict[str, Any]] = []
        active_assertion_ids: list[str] = []
        active_component_ids: list[str] = []
        atomic_owner_id = str(source["entity_id"])
        atomic_owner_version = str(job.get("prompt_version", "")).strip()
        inference = value.get("morphology_inference", {})
        inference = inference if isinstance(inference, dict) else {}
        context = inference.get("context", {})
        context = context if isinstance(context, dict) else {}
        selected_context = context.get("selected_evidence_ids", [])
        selected_context = selected_context if isinstance(selected_context, list) else []
        retrieved_context_ids = {
            str(item).strip() for item in selected_context if str(item).strip()
        }
        claim_input = str(inference.get("input_fingerprint", "")).strip()
        # A persisted draft identifies a real inference visit. Reusing that
        # checkpoint must not manufacture another independent correction vote.
        claim_epoch = str(inference.get("draft_artifact_id", "")).strip()
        claim_prompt = atomic_owner_version or str(
            inference.get("policy", MORPHEME_PROMPT_POLICY)
        ).strip()
        partial_analysis = value.get("analysis_scope") == "partial"
        deferred_parts: list[dict[str, Any]] = []
        for ordinal, part in enumerate(cleaned):
            basis = "book" if part["evidence_ids"] else "model"
            context_evidence_ids = sorted({
                *retrieved_context_ids, *part.get("context_evidence_ids", []),
            })
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
            component_modes = ["knowledge", "word"]
            component_modes.append(
                "root" if part["kind"] in {"root", "free"} else "affix"
            )
            assertion_id = self.store.accept_relation_assertion(
                source["entity_id"],
                source["entity_id"],
                morpheme_id,
                "shares-component" if partial_analysis else "has-component",
                basis=basis,
                confidence=confidence,
                properties={
                    # Partial membership is useful graph knowledge, not a
                    # positional assertion about a complete decomposition.
                    "proposal": partial_analysis,
                    "source_term_id": source["entity_id"],
                    "component_id": morpheme_id,
                    **({} if partial_analysis else {"ordinal": ordinal}),
                    "surface": part["surface"],
                    "component_kind": part["kind"],
                    "canonical_form": part["canonical_form"],
                    "meaning": part["meaning"],
                    "language": part["language"],
                    "modes": component_modes,
                    "analysis_scope": value.get("analysis_scope", "complete"),
                    "source_span": part.get("source_span"),
                },
                evidence_ids=part["evidence_ids"],
                context_evidence_ids=context_evidence_ids,
                input_fingerprint=claim_input,
                epoch_fingerprint=claim_epoch,
                owner_type="atomic-split",
                owner_id=atomic_owner_id,
                owner_version=atomic_owner_version,
                job_id=str(job["job_id"]),
                reason="atomic morpheme split",
                prompt_version=claim_prompt,
            )
            if not assertion_id:
                deferred_parts.append({
                    "reason": "component claim deferred by existing graph authority",
                    "proposal": deepcopy(part),
                })
                continue
            active_component_ids.append(morpheme_id)
            active_assertion_ids.append(assertion_id)
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
                    "context_evidence_ids": context_evidence_ids,
                }
            )
        # A valid proposal is not necessarily the partition accepted by the
        # store. Keep useful claims, but do not publish a false completion or
        # replace positional projections when an authoritative claim wins.
        persisted_analysis = finalize_morpheme_analysis(
            {"parts": accepted_parts}, str(source["text"]), [], [],
        )
        partial_analysis = partial_analysis or persisted_analysis["analysis_scope"] == "partial"
        value = {
            **value,
            "analysis_scope": "partial" if partial_analysis else "complete",
            "refinement_needed": partial_analysis or bool(value.get("refinement_needed")),
            "unresolved_spans": persisted_analysis["unresolved_spans"],
            "unresolved_proposals": [
                *deepcopy(value.get("unresolved_proposals", [])), *deferred_parts,
            ],
        }
        if not partial_analysis:
            for part in accepted_parts:
                self.store.link_morpheme(
                    source["entity_id"], part["morpheme_id"], part["ordinal"],
                    part["surface"], basis=part["basis"], confidence=part["confidence"],
                )
        derivative_values = value.get("derivatives", value.get("related_terms", []))
        derivative_values = derivative_values if isinstance(derivative_values, list) else []
        related_terms = _normalise_derivative_terms(
            derivative_values,
            accepted_parts,
            str(source["text"]),
            limit=max(32, len(derivative_values)),
        )
        components_by_id = {
            str(part["morpheme_id"]): part for part in accepted_parts
        }
        components_by_form = {
            _plain_letter_key(part["canonical_form"]): part
            for part in accepted_parts
        }
        persisted_related_terms: list[dict[str, Any]] = []
        for related in related_terms:
            derivative_id = self.store.upsert_term(
                "en", str(related["term"]), status="accepted"
            )
            component_ids = [
                str(component_id)
                for component_id in related.get("component_ids", [])
                if str(component_id) in components_by_id
            ]
            if not component_ids:
                component_ids = [
                    str(part["morpheme_id"])
                    for form in related.get("component_forms", [])
                    if (part := components_by_form.get(_plain_letter_key(form)))
                ]
            accepted_derivative_components: list[str] = []
            for component_id in dict.fromkeys(component_ids):
                component = components_by_id[component_id]
                derivative_modes = ["knowledge", "word"]
                derivative_modes.append(
                    "root"
                    if component["kind"] in {"root", "free"}
                    else "affix"
                )
                assertion_id = self.store.accept_relation_assertion(
                    source["entity_id"],
                    derivative_id,
                    component_id,
                    "shares-component",
                    basis="model",
                    confidence=0.55,
                    properties={
                        "source_term_id": source["entity_id"],
                        "component_id": component_id,
                        "component_kind": component["kind"],
                        "note": str(related.get("note", "")),
                        "proposal": True,
                        "modes": derivative_modes,
                    },
                    evidence_ids=[],
                    context_evidence_ids=sorted({
                        *retrieved_context_ids,
                        *component.get("context_evidence_ids", []),
                    }),
                    input_fingerprint=claim_input,
                    epoch_fingerprint=claim_epoch,
                    owner_type="atomic-split",
                    owner_id=atomic_owner_id,
                    owner_version=atomic_owner_version,
                    job_id=str(job["job_id"]),
                    reason="atomic derivative component proposal",
                    prompt_version=claim_prompt,
                )
                if not assertion_id:
                    continue
                active_assertion_ids.append(assertion_id)
                accepted_derivative_components.append(component_id)
            if accepted_derivative_components:
                persisted_related_terms.append(
                    {
                        **related,
                        "term_id": derivative_id,
                        "component_ids": accepted_derivative_components,
                    }
                )
        if not partial_analysis:
            self.store.reconcile_accepted_lexical_split(
                source["entity_id"],
                active_assertion_ids=active_assertion_ids,
                active_component_ids=active_component_ids,
                component_count=len(accepted_parts),
                owner_type="atomic-split",
                owner_id=atomic_owner_id,
                owner_version=atomic_owner_version,
            )
        accepted = {
            "term_id": source["entity_id"],
            "term": source["text"],
            "parts": accepted_parts,
            "related_terms": persisted_related_terms,
            "analysis_scope": value.get("analysis_scope", "complete"),
            "refinement_needed": bool(value.get("refinement_needed")),
            "unresolved_spans": deepcopy(value.get("unresolved_spans", [])),
            "unresolved_proposals": deepcopy(value.get("unresolved_proposals", [])),
            "partial_policy": value.get("partial_policy", ""),
            "analysis_policy": STRUCTURE_POLICY,
            "morphology_inference": value.get("morphology_inference", {}),
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
        quality = min((part["confidence"] for part in accepted_parts), default=0.35)
        if value.get("analysis_scope") == "partial":
            quality = min(quality, 0.6)
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
        if not supports_english_morphology(source["language"]):
            raise ValueError("origin expansion is limited to English book morphology")
        splits = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-morpheme-split",
            validation_state="accepted",
        )
        parts = list(splits[-1]["payload"].get("parts", [])) if splits else []
        if (
            not splits or splits[-1]["payload"].get("analysis_scope") == "partial"
            or not any(part.get("kind") in {"root", "free"} for part in parts)
        ):
            parts = [word_history_anchor(source), *parts]

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

        prior_origins = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-origin-branches",
            validation_state="accepted",
        )
        prior_branches = (
            list(prior_origins[-1]["payload"].get("branches", []))
            if prior_origins
            else []
        )
        prior_counts = {
            str(branch.get("component_id", "")): (
                len(branch.get("steps", []))
                + (
                    1
                    if str(branch.get("exploration_status", ""))
                    == "no-safe-history"
                    else 0
                )
            )
            for branch in prior_branches
            if isinstance(branch, dict) and isinstance(branch.get("steps", []), list)
        }
        base_has_prior_history = any(
            str(branch.get("component_kind", "")) in {"root", "free"}
            and (
                bool(branch.get("steps"))
                or str(branch.get("exploration_status", ""))
                == "no-safe-history"
            )
            for branch in prior_branches
            if isinstance(branch, dict)
        )
        focus_candidates = [item for item in context if item["evidence"]] or context
        if not base_has_prior_history:
            focus_candidates = [
                item for item in focus_candidates if item["kind"] in {"root", "free"}
            ] or [
                item for item in context if item["component_id"] == history_anchor_id
            ]
        focus = min(
            focus_candidates,
            key=lambda item: (
                prior_counts.get(str(item["component_id"]), 0),
                0 if item["kind"] in {"root", "free"} else 1,
                str(item["component_id"]),
            ),
            default=None,
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
        prompt_focus = {
            **prompt_focus,
            "related_word_origin_context": _origin_context_evidence(
                source["text"], source_origin_records,
            ),
        }
        evidence_instruction = (
            "evidence_ids: always an empty array; the system deterministically attaches "
            "an ID only when the form is printed verbatim in retrieved evidence"
        )
        prompt = f"""ONE ORIGIN BRANCH
MODERN WORD: {source['text']}
LEXICAL BASE AND RETRIEVED WORD-ORIGIN EVIDENCE:
{json.dumps(prompt_focus, ensure_ascii=False)}
EXISTING ACCEPTED HISTORY:
{json.dumps(prior_branches, ensure_ascii=False)}

Return exactly one JSON object with `component_id` copied exactly and `steps`, an
array representing the complete revised path for this component, oldest to newest.
Use one to five historically useful steps. Each
step has:
form: concise attested or reconstructed historical form
language: ISO-style code such as la, fro, fr, grc, ine-pro, ang, enm, or und if unknown
period: concise era or language-stage label, or unspecified if unknown
meaning: at most 10 English words
confidence: number from 0 to 1
{evidence_instruction}

The final step develops into the lexical base or modern word. A step must never
repeat the modern word or the fixed base itself. Do not use Modern English as a
historical step, invent dates, or add a sibling component.
Copy book-backed historical forms faithfully. You may add useful historical
hypotheses from your knowledge when a retrieved passage leaves a gap.
Use retrieved evidence first. A form printed verbatim will be labeled book by
the system. A useful local-model historical hypothesis may remain uncited and
will be visibly labeled model with conservative confidence. Never invent a
citation. Preserve useful accepted history and add a new connection when the
books or your linguistic knowledge support one. Uncertain but useful hypotheses
are allowed when visibly model-owned and conservatively scored. Related word-origin
context is reading material, not automatic proof of this component's history.
Every adjacent pair must be a plausible direct ancestry link, not merely two
related forms. Do not turn parallel cognates or neighboring dictionary examples
into successive ancestors. Printed forms can be book-attested while their
relationship remains a model hypothesis.
Trace the lexical form named above, not a synonym or the words used to define it.
A passage about another headword may explain that other word's ancestry without
explaining this word's ancestry. Shared meanings do not imply shared roots.
Correct an existing model path when it confuses these distinctions; do not
preserve an old model error merely because it was previously accepted. Return
the whole revised path, not just additions: the system will not concatenate
your path with the old one or invent a link between their endpoints.
Do not discard a useful form merely because its date or language is uncertain."""
        fixed_component_id = str(focus["component_id"])
        completion: dict[str, Any] | None = None
        review_reason = ""
        inference_trace: list[dict[str, Any]] = []
        book_steps = _book_origin_steps(focus_evidence)
        used_book_draft = bool(book_steps and not prior_origins)
        if used_book_draft:
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
                max_tokens=640,
            )
            inference_trace.append({
                "stage": "draft",
                "model": completion.get("model", self.model.model_name),
                "metrics": deepcopy(completion.get("metrics", {})),
            })
            raw_value = completion.get("value")
            value, normalizations = _normalize_origin_draft(
                raw_value,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
            )
            value, evidence_normalizations = _attach_verbatim_origin_evidence(
                value,
                evidence=focus_evidence,
                allowed_ids=allowed_by_component[fixed_component_id],
            )
            normalizations.extend(evidence_normalizations)
            review_reason = _origin_draft_review_reason(
                value,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
                fixed_provenance_ids=set(fixed_provenance_ids),
                evidence=focus_evidence,
            )
            # Review the remaining draft, not defects normalization already
            # repaired. In particular, removing the modern endpoint does not
            # warrant another model call when useful valid history remains.
            self.store.save_job_artifact(
                job["job_id"],
                "model-origin-draft",
                {
                    "term": source["text"],
                    "review_policy": "normalize-before-review-v1",
                    "review_reason": review_reason,
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
        if not used_book_draft and review_reason:
            review_prompt = f"""ONE ORIGIN BRANCH REVIEW
The first draft failed a structural or evidence check: {review_reason}.

MODERN WORD: {source['text']}
FIXED LEXICAL BASE AND EXACT RAG EVIDENCE:
{json.dumps(prompt_focus, ensure_ascii=False)}

REJECTED DRAFT:
{json.dumps(value, ensure_ascii=False)}

Return a corrected JSON object with exactly the fixed component_id and one to
five `steps` ordered oldest to newest. Every step includes form, language, period,
meaning, confidence, and evidence_ids. Use an ISO-style language code (la, fro,
fr, grc, ang, enm, or und if unknown); use unspecified for an unknown period.
Confidence is a number from 0 to 1. The last historical step develops into
the fixed base or modern word, but no step may repeat either of them. Do not use
Modern English. Prefer historical forms printed in the retrieved RAG evidence;
{evidence_instruction}. A useful local-model
historical hypothesis may remain uncited and will be labeled model by the
system. Keep meanings under ten English words and stop before a speculative
chain. Return JSON only."""
            completion = self.model.complete_json(
                "You independently review one failed RAG-grounded etymology branch.",
                review_prompt,
                max_tokens=720,
            )
            inference_trace.append({
                "stage": "review",
                "model": completion.get("model", self.model.model_name),
                "metrics": deepcopy(completion.get("metrics", {})),
            })
            reviewed_raw = completion.get("value")
            reviewed_value, review_normalizations = _normalize_origin_draft(
                reviewed_raw,
                component_id=fixed_component_id,
                modern_word=source["text"],
                base_form=str(focus["form"]),
            )
            reviewed_value, evidence_normalizations = _attach_verbatim_origin_evidence(
                reviewed_value,
                evidence=focus_evidence,
                allowed_ids=allowed_by_component[fixed_component_id],
            )
            review_normalizations.extend(evidence_normalizations)
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
        omitted_steps: list[dict[str, str]] = []
        total_steps = 0
        base_has_history = False
        for part in parts:
            component_id = str(part["morpheme_id"])
            branch = raw_by_component.get(component_id, {})
            raw_steps = branch.get("steps", []) if isinstance(branch, dict) else []
            if not isinstance(raw_steps, list) or len(raw_steps) > 5:
                raise ValueError("an origin branch has too many steps")
            steps: list[dict[str, Any]] = []
            seen_forms: set[tuple[str, str, str]] = set()
            for raw in raw_steps:
                if not isinstance(raw, dict):
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": "",
                            "reason": "origin step was not an object",
                        }
                    )
                    continue
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
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "invalid or missing historical language code",
                        }
                    )
                    continue
                if not form or len(form) > 90 or not period or len(period) > 80:
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "historical form or period was missing or too long",
                        }
                    )
                    continue
                if _plain_letter_key(form) in {
                    _plain_letter_key(source["text"]),
                    _plain_letter_key(part["canonical_form"]),
                }:
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "historical step repeated the modern word or component",
                        }
                    )
                    continue
                if language == "en" or "modern english" in period.casefold():
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "historical step used Modern English",
                        }
                    )
                    continue
                if not meaning:
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "historical meaning was missing",
                        }
                    )
                    continue
                if any(
                    marker in text
                    for marker in _ENCODING_DAMAGE
                    for text in (form, period, meaning)
                ):
                    omitted_steps.append(
                        {
                            "component_id": component_id,
                            "form": form[:90],
                            "reason": "historical step contained encoding damage",
                        }
                    )
                    continue
                try:
                    confidence = max(
                        0.0, min(float(raw.get("confidence", 0.35)), 1.0)
                    )
                except (TypeError, ValueError):
                    confidence = 0.35
                selected = [
                    evidence_id
                    for evidence_id in _explicit_form_evidence_ids(form, focus_evidence)
                    if evidence_id in allowed_by_component[component_id]
                    and (not fixed_provenance_ids or evidence_id in fixed_provenance_ids)
                ]
                basis = "book" if selected else "model"
                confidence = min(confidence, 0.95 if selected else 0.7)
                metadata_warnings = raw.get("metadata_warnings", [])
                if metadata_warnings or language == "und" or period == "unspecified":
                    # The form may be printed in a book while its chronology or
                    # language is still only a model hypothesis.
                    basis = "model"
                    confidence = min(confidence, 0.35)
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
                        **({"metadata_warnings": metadata_warnings} if metadata_warnings else {}),
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
                    "exploration_status": (
                        "history-found"
                        if steps
                        else (
                            "no-safe-history"
                            if component_id == fixed_component_id
                            else "unexplored"
                        )
                    ),
                }
            )
        cleaned_branches, revised_components = revise_origin_paths(
            cleaned_branches, prior_branches, fixed_component_id,
        )
        total_steps = sum(len(branch["steps"]) for branch in cleaned_branches)
        base_has_history = any(
            branch["component_kind"] in {"root", "free"} and branch["steps"]
            for branch in cleaned_branches
        )

        origin_model, origin_metrics = _origin_generation_metadata(
            completion, self.model.model_name
        )
        accepted_steps: list[dict[str, Any]] = []
        active_origin_assertions: set[str] = set()
        for branch in cleaned_branches:
            branch_evidence = self.store.evidence_records(
                sorted(allowed_by_component.get(str(branch["component_id"]), set()))
            )
            for index, step in enumerate(branch["steps"]):
                later_form = (
                    branch["steps"][index + 1]["form"]
                    if index + 1 < len(branch["steps"])
                    else branch["component_form"]
                )
                step.update(_origin_transition_provenance(
                    step, later_form, branch_evidence,
                    extracted_from_book=(
                        used_book_draft
                        and str(branch["component_id"]) == fixed_component_id
                    ) or (
                        step.get("edge_provenance_version") == 2
                        and step.get("edge_basis") == "book"
                        and step.get("edge_later_form") == later_form
                    ),
                ))
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
                event_id = self.store.add_history_event(
                    historical_id,
                    "form-transition",
                    f"{step['form']} developed toward the next recorded form",
                    language=step["language"],
                    period_label=step["period"],
                    properties={
                        "component_id": branch["component_id"],
                        "source_term_id": source["entity_id"],
                        "later_entity_id": later_id,
                        "basis": step["edge_basis"],
                        "confidence": step["edge_confidence"],
                    },
                    basis=step["edge_basis"],
                    confidence=step["edge_confidence"],
                )
                self.store.add_edge(
                    event_id,
                    later_id,
                    "developed-toward",
                    basis=step["edge_basis"],
                    confidence=step["edge_confidence"],
                    properties={"modes": ["knowledge", "word", "root", "affix"]},
                )
                self.store.add_edge(
                    event_id,
                    str(branch["component_id"]),
                    "explains-component",
                    basis=step["edge_basis"],
                    confidence=step["edge_confidence"],
                    properties={"modes": ["knowledge", "word", "root", "affix"]},
                )
                self.store.add_edge(
                    str(source["entity_id"]),
                    event_id,
                    "has-origin-event",
                    basis=step["edge_basis"],
                    confidence=step["edge_confidence"],
                    properties={"modes": ["knowledge", "word", "root", "affix"]},
                )
                for evidence_id in step["edge_evidence_ids"]:
                    self.store.link_evidence(
                        event_id,
                        evidence_id,
                        claim=f"{step['form']} historical transition",
                        confidence=step["edge_confidence"],
                    )
                origin_assertion_id = self.store.accept_origin_transition(
                    source["entity_id"],
                    historical_id,
                    later_id,
                    component_id=str(branch["component_id"]),
                    basis=step["edge_basis"],
                    confidence=step["edge_confidence"],
                    evidence_ids=step["edge_evidence_ids"],
                    context_evidence_ids=step["edge_context_evidence_ids"],
                    job_id=job["job_id"],
                    prompt_version=str(job.get("prompt_version", "")),
                )
                if origin_assertion_id:
                    active_origin_assertions.add(origin_assertion_id)
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

        quality = (
            min(step["confidence"] for step in accepted_steps)
            if accepted_steps
            else 0.5
        )
        accepted = {
            "term_id": source["entity_id"],
            "term": source["text"],
            "branches": cleaned_branches,
                "origin_path_policy": ORIGIN_PATH_POLICY,
                "origin_review": {
                    "policy": "normalize-before-review-v1",
                    "reason": review_reason,
                    "inference_trace": inference_trace,
                },
            "history_outcome": (
                "history-found"
                if total_steps and base_has_history
                else "no-safe-history"
            ),
            "omitted_steps": omitted_steps[:24],
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
        artifact_id = self.store.save_job_artifact(
            job["job_id"],
            "accepted-origin-branches",
            accepted,
            language=source["language"],
            validation_state="accepted",
            quality_score=quality,
        )
        retired = retire_replaced_model_origin_paths(
            self.store, str(source["entity_id"]),
            active_assertion_ids=active_origin_assertions,
            current_component_ids={str(branch["component_id"]) for branch in cleaned_branches},
            revised_component_ids=revised_components,
        )
        if retired:
            self.store.save_job_artifact(
                job["job_id"], "origin-path-reconciliation",
                {"policy": ORIGIN_PATH_POLICY, "retired_assertion_ids": retired},
                language=source["language"], validation_state="candidate",
            )
        return artifact_id

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
        language = _language_code(job.get("language", ""))
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
        if translation:
            align_translation_reading(
                self.store, job, translation, reading, method, evidence_ids,
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
        self,
        source: dict[str, Any],
        evidence_ids: list[str],
        *,
        support_forms: tuple[str, ...] = (),
    ) -> list[Evidence]:
        class ResolvedEvidence(Evidence):
            def __init__(self, *, evidence_id: str, **values: Any):
                super().__init__(**values)
                object.__setattr__(self, "evidence_id", evidence_id)

            def to_dict(self) -> dict[str, Any]:
                return {**super().to_dict(), "evidence_id": self.evidence_id}

        evidence: list[Evidence] = []
        for record in self.store.evidence_records(evidence_ids):
            if not _card_evidence_record_relevant(
                source["text"], record, support_forms
            ):
                continue
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
                ResolvedEvidence(
                    evidence_id=str(record["evidence_id"]),
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

    @staticmethod
    def _deduplicate_card_graph(
        nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        unique_nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        for node in nodes:
            node_id = str(node.get("id", "")).strip()
            if not node_id or node_id in node_ids:
                continue
            node_ids.add(node_id)
            unique_nodes.append(node)
        unique_edges: list[dict[str, Any]] = []
        edge_triples: set[tuple[str, str, str]] = set()
        for edge in edges:
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            relation = str(
                edge.get("relation", edge.get("relationship", ""))
            ).strip()
            triple = (source, target, relation)
            if (
                not source
                or not target
                or source not in node_ids
                or target not in node_ids
                or triple in edge_triples
            ):
                continue
            edge_triples.add(triple)
            unique_edges.append(edge)
        return unique_nodes, unique_edges

    @staticmethod
    def _include_related_term_completeness(
        view: dict[str, Any], related_terms: list[dict[str, Any]]
    ) -> None:
        completeness = view.setdefault("meaning_completeness", {})
        known_terms: dict[str, tuple[str, str]] = {}
        for node in view.get("nodes", []):
            if str(node.get("type", "")).strip().casefold() != "term":
                continue
            payload = node.get("payload", {})
            payload = payload if isinstance(payload, dict) else {}
            key = _plain_letter_key(node.get("label", payload.get("term", "")))
            if key:
                known_terms[key] = (
                    str(node.get("id", "")).strip(),
                    str(payload.get("meaning", "")).strip(),
                )
        missing_entity_ids = {
            str(item)
            for item in completeness.get("missing_entity_ids", [])
            if str(item)
        }
        missing_related_terms: list[str] = []
        for item in related_terms:
            term = re.sub(r"\s+", " ", str(item.get("term", ""))).strip()
            if not term or str(item.get("meaning", "")).strip():
                continue
            known_id, known_meaning = known_terms.get(
                _plain_letter_key(term), ("", "")
            )
            if known_meaning or (known_id and known_id in missing_entity_ids):
                continue
            if term.casefold() not in {value.casefold() for value in missing_related_terms}:
                missing_related_terms.append(term)
        base_missing = max(
            len(missing_entity_ids), int(completeness.get("missing_count", 0) or 0)
        )
        total_missing = base_missing + len(missing_related_terms)
        completeness.update(
            {
                "status": "partial" if total_missing else "complete",
                "missing_count": total_missing,
                "missing_related_terms": missing_related_terms[:16],
                "related_terms_truncated": len(missing_related_terms) > 16,
                "truncated": bool(completeness.get("truncated", False))
                or len(missing_related_terms) > 16,
            }
        )

    @staticmethod
    def _accepted_component_meaning(
        component: dict[str, Any],
        current: dict[str, Any] | None,
    ) -> str:
        graph_meaning = str(component.get("meaning", "")).strip()
        if graph_meaning:
            return graph_meaning
        if current is None:
            return ""
        return str(current.get("meaning", "")).strip()

    @staticmethod
    def _branch_component_id(
        branch_focus: dict[str, Any],
        relevant_parts: list[dict[str, Any]],
        mode: str,
    ) -> str:
        expected_kinds = {"root"} if mode == "root" else {"prefix", "suffix"}
        branch_node_ids = {
            str(entity_id).strip()
            for entity_id in branch_focus.get("node_ids", [])
            if str(entity_id).strip()
        }
        component_ids = list(
            dict.fromkeys(
                str(part.get("morpheme_id", "")).strip()
                for part in relevant_parts
                if str(part.get("kind", "")).strip().casefold()
                in expected_kinds
                and str(part.get("morpheme_id", "")).strip()
                in branch_node_ids
            )
        )
        if len(component_ids) != 1:
            raise ValueError(
                f"{mode} branch {branch_focus.get('id', '')!s} must resolve "
                "exactly one accepted component"
            )
        return component_ids[0]

    def _lexical_view(
        self,
        subject_entity_id: str,
        mode: str,
        focus_entity_ids: list[str],
        *,
        accepted_focus_node: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limits = {
            "knowledge": {"nodes": 24, "edges": 36, "depth": 2},
            "word": {"nodes": 32, "edges": 48, "depth": 4},
            "root": {"nodes": 32, "edges": 48, "depth": 3},
            "affix": {"nodes": 32, "edges": 48, "depth": 3},
        }[mode]
        normalized_focus_ids = list(
            dict.fromkeys(
                str(entity_id).strip()
                for entity_id in focus_entity_ids
                if str(entity_id).strip()
            )
        ) or [subject_entity_id]
        requested_branch_focus_id = ""
        projection_changed = False
        if mode in {"root", "affix"}:
            requested_branch_focus_id = normalized_focus_ids[0]
            limits["branch_focus_id"] = requested_branch_focus_id
        view = deepcopy(
            self.store.lexical_subgraph(subject_entity_id, mode, limits)
        )
        if requested_branch_focus_id:
            returned_branch_focus_id = str(
                view.get("branch_focus_id", "")
            ).strip()
            if returned_branch_focus_id != requested_branch_focus_id:
                accepted_focus_id = str(
                    (accepted_focus_node or {}).get("id", "")
                ).strip()
                accepted_focus_kind = str(
                    (accepted_focus_node or {}).get("type", "")
                ).strip().casefold()
                canonical_focus_nodes = [
                    node
                    for node in view.get("nodes", [])
                    if str(node.get("id", "")).strip()
                    == requested_branch_focus_id
                ]
                expected_kinds = (
                    {"root"} if mode == "root" else {"prefix", "suffix"}
                )
                if (
                    returned_branch_focus_id
                    or canonical_focus_nodes
                    or accepted_focus_id != requested_branch_focus_id
                    or accepted_focus_kind not in expected_kinds
                ):
                    raise ValueError(
                        f"{mode} lexical view returned branch focus "
                        f"{returned_branch_focus_id!r}; expected "
                        f"{requested_branch_focus_id!r}"
                    )
                view.setdefault("nodes", []).append(
                    {
                        "id": accepted_focus_id,
                        "type": "morpheme",
                        "label": str(
                            accepted_focus_node.get("form", accepted_focus_id)
                        ),
                        "payload": {
                            "kind": accepted_focus_kind,
                            "canonical_form": str(
                                accepted_focus_node.get("form", "")
                            ),
                            "meaning": str(
                                accepted_focus_node.get("meaning", "")
                            ),
                            "language": str(
                                accepted_focus_node.get("language", "")
                            ),
                            "basis": str(
                                accepted_focus_node.get("basis", "")
                            ),
                            "evidence_ids": list(
                                accepted_focus_node.get("evidence_ids", [])
                            ),
                        },
                    }
                )
                existing_node_ids = {
                    str(node.get("id", "")).strip()
                    for node in view.get("nodes", [])
                }
                if (
                    subject_entity_id != accepted_focus_id
                    and subject_entity_id not in existing_node_ids
                ):
                    raise ValueError(
                        "accepted branch focus fallback has no subject topology"
                    )
                if subject_entity_id != accepted_focus_id:
                    view.setdefault("edges", []).append(
                        {
                            "source": subject_entity_id,
                            "target": accepted_focus_id,
                            "relation": "has-component",
                            "basis": str(
                                accepted_focus_node.get("basis", "model")
                            )
                            or "model",
                            "confidence": accepted_focus_node.get(
                                "confidence", 0.7
                            ),
                            "evidence_ids": list(
                                accepted_focus_node.get("evidence_ids", [])
                            ),
                            "payload": {
                                "recovery_kind": "accepted-artifact-focus"
                            },
                        }
                    )
                view["nodes"], view["edges"] = self._deduplicate_card_graph(
                    list(view.get("nodes", [])), list(view.get("edges", []))
                )
                view["branch_focus_id"] = requested_branch_focus_id
                projection_changed = True
            focus_nodes = [
                node
                for node in view.get("nodes", [])
                if str(node.get("id", "")).strip()
                == requested_branch_focus_id
            ]
            expected_kinds = (
                {"root"} if mode == "root" else {"prefix", "suffix"}
            )
            if len(focus_nodes) != 1:
                raise ValueError(
                    f"{mode} lexical view must contain exactly one requested "
                    "branch focus node"
                )
            focus_node = focus_nodes[0]
            focus_payload = focus_node.get("payload", {})
            focus_kind = (
                str(focus_payload.get("kind", "")).strip().casefold()
                if isinstance(focus_payload, dict)
                else ""
            )
            if (
                str(focus_node.get("type", "")).strip().casefold()
                != "morpheme"
                or focus_kind not in expected_kinds
            ):
                raise ValueError(
                    f"{mode} lexical view branch focus has incompatible "
                    "node type or morpheme kind"
                )

        missing_meaning_ids: list[str] = []
        for node in view.get("nodes", []):
            node_id = str(node.get("id", "")).strip()
            node_type = str(node.get("type", "")).strip().casefold()
            payload = node.get("payload", {})
            meaning = (
                str(payload.get("meaning", "")).strip()
                if isinstance(payload, dict)
                else ""
            )
            if (
                node_id
                and not meaning
                and (
                    node_type == "morpheme"
                    or (node_type == "term" and node_id != subject_entity_id)
                )
            ):
                missing_meaning_ids.append(node_id)
        missing_meaning_ids = list(dict.fromkeys(missing_meaning_ids))
        missing_limit = 16
        view["meaning_completeness"] = {
            "status": "partial" if missing_meaning_ids else "complete",
            "missing_count": len(missing_meaning_ids),
            "missing_entity_ids": missing_meaning_ids[:missing_limit],
            "truncated": len(missing_meaning_ids) > missing_limit,
            "focus_meaning_pending": (
                requested_branch_focus_id in missing_meaning_ids
            ),
        }
        view["subject_entity_id"] = subject_entity_id
        view["mode"] = mode
        view["focus_entity_ids"] = normalized_focus_ids
        if projection_changed:
            view["projection_hash"] = _morph_hashlib.sha256(
                _morph_json.dumps(
                    {
                        "subject_entity_id": subject_entity_id,
                        "mode": mode,
                        "branch_focus_id": requested_branch_focus_id,
                        "nodes": view.get("nodes", []),
                        "edges": view.get("edges", []),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        return view

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
        if not meanings:
            raise ValueError("accepted meaning checkpoint is missing")

        meaning = meanings[-1]
        grammar_value = grammar[-1]["payload"] if grammar else meaning["payload"]
        language_atoms = _language_card_atoms(
            source, meaning, translations, pronunciations
        )
        english, japanese, chinese = _legacy_language_fields(language_atoms)
        evidence_ids = [str(item) for item in meaning["payload"]["evidence_ids"]]
        evidence = self._card_evidence(source, evidence_ids)
        if not evidence:
            raise ValueError("accepted meaning evidence could not be reconstructed")
        evidence_ids = [
            str(getattr(item, "evidence_id"))
            for item in evidence
            if getattr(item, "evidence_id", "")
        ]

        quality_values = [
            _artifact_quality(meaning),
            *(_artifact_quality(item) for item in grammar[-1:]),
            *(_artifact_quality(item) for item in translations.values()),
            *(_artifact_quality(item) for item in pronunciations.values()),
        ]
        quality = min(quality_values)
        definition = str(meaning["payload"]["definition"])
        lexical_view = self._lexical_view(
            str(source["entity_id"]), "knowledge", [str(source["entity_id"])]
        )
        card = Card(
            card_id=str(uuid.uuid4()),
            mode="knowledge",
            query=str(source["text"]),
            title=str(source["text"]),
            subtitle=str(grammar_value["part_of_speech"]).upper(),
            summary_en="",
            origin_story="",
            key_points=[],
            english=english,
            japanese=japanese,
            chinese=chinese,
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
                "lexical_view": lexical_view,
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
            },
            extra_languages=_extra_language_card_values(language_atoms),
            languages=language_atoms,
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
            language=_language_code(source["language"]),
            validation_state="accepted",
            quality_score=quality,
        )

    def _persist_composed_origin_cards(
        self,
        cards: list[Card],
        *,
        quality: float,
        authoritative_empty_modes: bool = False,
    ) -> dict[str, dict[str, Any]]:
        if self.card_store is None:
            raise RuntimeError("card store is required for origin composition")

        cleanup_plans: dict[str, tuple[str, tuple[str, ...]]] = {}
        output_queries = {
            " ".join(str(output_card.query).split()).casefold()
            for output_card in cards
            if " ".join(str(output_card.query).split())
        }
        authoritative_query = (
            cards[0].query
            if cards and authoritative_empty_modes and len(output_queries) == 1
            else ""
        )
        for mode in ("root", "affix"):
            mode_cards = [card for card in cards if card.mode == mode]
            if not mode_cards:
                if authoritative_query:
                    cleanup_plans[mode] = (authoritative_query, ())
                continue
            branch_keys: list[str] = []
            normalized_queries: set[str] = set()
            ambiguous = False
            for output_card in mode_cards:
                extensions = output_card.extensions
                raw_key = (
                    extensions.get("branch_focus_id", "")
                    if isinstance(extensions, dict)
                    else ""
                )
                branch_key = " ".join(str(raw_key).split()).casefold()
                normalized_query = " ".join(
                    str(output_card.query).split()
                ).casefold()
                if (
                    not branch_key
                    or not normalized_query
                    or branch_key in branch_keys
                ):
                    ambiguous = True
                    break
                branch_keys.append(branch_key)
                normalized_queries.add(normalized_query)
            if ambiguous or len(normalized_queries) != 1:
                continue
            cleanup_plans[mode] = (
                mode_cards[0].query,
                tuple(sorted(branch_keys)),
            )

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

        cleanup_results: dict[str, dict[str, Any]] = {}
        for mode in ("root", "affix"):
            plan = cleanup_plans.get(mode)
            if plan is None:
                continue
            query, branch_keys = plan
            cleanup_results[mode] = (
                self.card_store.archive_obsolete_morphology_branches(
                    mode,
                    query,
                    branch_keys,
                    authoritative=True,
                    allow_empty_authoritative=authoritative_empty_modes,
                )
            )
        return cleanup_results

    def _compose_origin_card(self, job: dict[str, Any]) -> str:
        if self.card_store is None:
            raise RuntimeError("card store is unavailable")
        projection_fingerprint = _projection_repair_fingerprint(job)
        if projection_fingerprint:
            prior_artifacts = self.store.artifacts_for_job(str(job["job_id"]))
            prior = next(
                (
                    item
                    for item in reversed(prior_artifacts)
                    if item.get("stage") == "accepted-origin-card"
                    and item.get("validation_state") == "accepted"
                ),
                None,
            )
            if prior is not None:
                complete = getattr(
                    self.store, "complete_lexical_projection_repair", None
                )
                if callable(complete):
                    complete(
                        str(job["subject_entity_id"]),
                        projection_fingerprint,
                        compose_job_id=str(job["job_id"]),
                    )
                return str(prior["artifact_id"])
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
        split = next(
            (item for item in reversed(artifacts)
             if item["stage"] == "accepted-morpheme-split"),
            None,
        )
        split_payload = split["payload"] if split is not None else {}
        origin = latest("accepted-origin-branches")
        translation_artifacts = {
            str(item["language"]): item
            for item in artifacts
            if item["stage"] == "accepted-translation"
        }
        pronunciation_artifacts = {
            str(item["language"]): item
            for item in artifacts
            if item["stage"] == "accepted-pronunciation"
        }
        all_translations = {
            str(item["language"]): item["payload"]
            for item in artifacts
            if item["stage"] == "accepted-translation"
        }
        all_pronunciations = {
            str(item["language"]): item["payload"]
            for item in artifacts
            if item["stage"] == "accepted-pronunciation"
        }
        language_atoms = _language_card_atoms(
            source, meaning, translation_artifacts, pronunciation_artifacts
        )
        english, japanese, chinese = _legacy_language_fields(language_atoms)
        artifact_parts = [dict(item) for item in split_payload.get("parts", [])]
        accepted_component_lookup = getattr(
            self.store, "accepted_morphology_components", None
        )
        accepted_components = (
            accepted_component_lookup(str(source["entity_id"]), include_partial=True)
            if callable(accepted_component_lookup)
            else None
        )
        candidate_parts, component_aliases = _canonical_component_projection_parts(
            str(source["entity_id"]),
            artifact_parts,
            accepted_components,
            subject_form=str(source["text"]),
        )
        branches = [dict(item) for item in origin["payload"].get("branches", [])]
        for branch in branches:
            component_id = str(branch.get("component_id", ""))
            branch["component_id"] = component_aliases.get(
                component_id, component_id
            )
        # A word can have real history without a morphological partition.
        # Immutable history artifacts must not restore withdrawn live claims.
        word_history_lookup = getattr(self.store, "accepted_word_history_edges", None)
        word_history_edges = (
            set(word_history_lookup(str(source["entity_id"])))
            if callable(word_history_lookup)
            else None
        )

        meaning_evidence = [
            str(item) for item in meaning["payload"].get("evidence_ids", [])
        ]
        if not meaning_evidence:
            raise ValueError("published word node has no retrieved evidence")
        branches_by_component = {
            str(branch.get("component_id", "")): branch for branch in branches
        }
        parts: list[dict[str, Any]] = []
        publication_component_basis: dict[str, str] = {}
        publication_component_evidence: dict[str, list[str]] = {}
        publication_edge_basis: dict[str, str] = {}
        publication_edge_evidence: dict[str, list[str]] = {}
        provenance_warnings: list[dict[str, str]] = []
        for part in candidate_parts:
            component_id = str(part.get("morpheme_id", ""))
            supplied = (
                list(dict.fromkeys(str(item) for item in part.get("evidence_ids", [])))
                if isinstance(part.get("evidence_ids"), list)
                else []
            )
            available = {
                str(record.get("evidence_id", ""))
                for record in self.store.evidence_records(supplied)
            }
            basis, selected = _publication_provenance(
                part.get("basis", "model"),
                supplied,
                available,
                claim="component",
                claim_id=component_id,
                warnings=provenance_warnings,
            )
            parts.append(part)
            publication_component_basis[component_id] = basis
            publication_component_evidence[component_id] = selected
            publication_edge_basis[component_id] = basis
            publication_edge_evidence[component_id] = selected
        retained_component_ids = {
            str(part.get("morpheme_id", "")) for part in parts
        }
        branches = [
            branch
            for branch in branches
            if str(branch.get("component_id", "")) in retained_component_ids
            or (
                str(branch.get("component_id", "")) == str(source["entity_id"])
                and (word_history_edges is None or bool(word_history_edges))
            )
        ]
        derived_parts = parts
        component_evidence = [
            str(evidence_id)
            for part in parts
            for evidence_id in publication_component_evidence[str(part["morpheme_id"])]
        ]
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
        graph_edges: list[dict[str, Any]] = []
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
                    "basis": publication_component_basis[part_id],
                    "evidence_ids": publication_component_evidence[part_id],
                    "confidence": (
                        "low"
                        if publication_component_basis[part_id] == "model"
                        else (
                            "high"
                            if float(part.get("confidence", 0)) >= 0.85
                            else "medium"
                        )
                    ),
                }
            )
            graph_edges.append(
                {
                    "source": part_id,
                    "target": center_id,
                    "relationship": f"{part['kind']}-of",
                    "basis": publication_edge_basis[part_id],
                    "evidence_ids": publication_edge_evidence[part_id],
                    "confidence": (
                        "low"
                        if publication_edge_basis[part_id] == "model"
                        else "high"
                    ),
                }
            )

        root_focus_areas: list[dict[str, Any]] = []
        root_headlines: list[str] = []
        for branch in branches:
            component_id = str(branch["component_id"])
            steps = list(branch.get("steps", []))
            word_anchor = component_id == center_id
            if word_anchor and word_history_edges is not None:
                available_ids = {
                    str(step.get("historical_form_id", "")) for step in steps
                } | {center_id}
                connected = {center_id}
                while True:
                    earlier_ids = {
                        earlier for earlier, later in word_history_edges
                        if later in connected and earlier in available_ids
                    }
                    if earlier_ids <= connected:
                        break
                    connected.update(earlier_ids)
                steps = [
                    step for step in steps
                    if str(step.get("historical_form_id", "")) in connected
                    and str(step.get("historical_form_id", "")) != center_id
                ]
            branch_ids: list[str] = []
            published_steps: list[dict[str, Any]] = []
            transition_evidence: dict[str, list[str]] = {}
            transition_basis: dict[str, str] = {}
            for step in steps:
                historical_id = str(step.get("historical_form_id", ""))
                if not historical_id or historical_id in node_ids:
                    continue
                step_evidence = list(
                    dict.fromkeys(str(item) for item in step.get("evidence_ids", []))
                )
                edge_evidence = list(
                    dict.fromkeys(
                        str(item) for item in step.get("edge_evidence_ids", [])
                    )
                )
                supplied = list(dict.fromkeys([*step_evidence, *edge_evidence]))
                available = {
                    str(record.get("evidence_id", ""))
                    for record in self.store.evidence_records(supplied)
                }
                step_basis, step_evidence = _publication_provenance(
                    step.get("basis", "model"),
                    step_evidence,
                    available,
                    claim="historical-node",
                    claim_id=historical_id,
                    warnings=provenance_warnings,
                )
                edge_basis, edge_evidence = _publication_provenance(
                    step.get("edge_basis", "book" if edge_evidence else "model"),
                    edge_evidence,
                    available,
                    claim="historical-edge",
                    claim_id=historical_id,
                    warnings=provenance_warnings,
                )
                node_ids.add(historical_id)
                branch_ids.append(historical_id)
                published_steps.append(step)
                transition_evidence[historical_id] = edge_evidence
                transition_basis[historical_id] = edge_basis
                graph_nodes.append(
                    {
                        "id": historical_id,
                        "type": "historical",
                        "form": str(step["form"]),
                        "meaning": str(step["meaning"]),
                        "language": language_name(str(step.get("language", ""))),
                        "period": str(step["period"]),
                        "history": f"Earlier form in {step['period']}",
                        "basis": step_basis,
                        "evidence_ids": step_evidence,
                        "confidence": (
                            "low"
                            if step_basis == "model"
                            else (
                                "high"
                                if float(step.get("confidence", 0)) >= 0.85
                                else "medium"
                            )
                        ),
                    }
                )
            steps = published_steps
            chain = [*branch_ids, component_id]
            transitions = (
                [
                    (earlier, later) for earlier, later in sorted(word_history_edges)
                    if earlier in branch_ids and later in {*branch_ids, center_id}
                ]
                if word_anchor and word_history_edges is not None
                else list(zip(chain, chain[1:]))
            )
            graph_edges.extend(
                {
                    "source": earlier,
                    "target": later,
                    "relationship": "developed-into",
                    "basis": transition_basis[earlier],
                    "evidence_ids": transition_evidence[earlier],
                    "confidence": (
                        "high" if transition_basis[earlier] == "book" else "low"
                    ),
                }
                for earlier, later in transitions
            )
            if (word_anchor and branch_ids) or (
                not word_anchor and branch.get("component_kind") in {"root", "free"}
            ):
                headline = _origin_history_headline(
                    [
                        *(step["form"] for step in steps),
                        *(
                            [] if word_anchor
                            else [parts_by_id[component_id]["canonical_form"]]
                        ),
                        source["text"],
                    ]
                )
                root_headlines.append(headline)
                root_focus_areas.append(
                    {
                        "id": f"root-history-{len(root_focus_areas) + 1}",
                        "label": (
                            "Root history"
                            if not word_anchor and branch.get("component_kind") == "root"
                            else "Word history"
                        ),
                        "kind": (
                            "root"
                            if not word_anchor and branch.get("component_kind") == "root"
                            else "word-history"
                        ),
                        "node_ids": list(dict.fromkeys([*branch_ids, component_id, center_id])),
                        "headline": headline,
                        "explanation": (
                            "This accepted lexical base carries the central history."
                            if branch_ids
                            else "This accepted base contributes to the modern word."
                        ),
                    }
                )

        focused_root_ids = {
            str(node_id)
            for area in root_focus_areas
            for node_id in area.get("node_ids", [])
        }
        for part in parts:
            component_id = str(part["morpheme_id"])
            if (
                str(part["kind"]) != "root"
                or component_id in focused_root_ids
            ):
                continue
            headline = _origin_history_headline(
                [str(part["canonical_form"]), str(source["text"])]
            )
            root_headlines.append(headline)
            root_focus_areas.append(
                {
                    "id": f"root-component-{component_id}",
                    "label": "Root",
                    "kind": "root",
                    "node_ids": [component_id, center_id],
                    "headline": headline,
                    "explanation": (
                        "This accepted graph component contributes to the modern word."
                    ),
                }
            )

        graph_nodes, graph_edges = self._deduplicate_card_graph(
            graph_nodes, graph_edges
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
                "explanation": (
                    "One word, its analyzed structure, and provenance-labeled history."
                ),
            },
        ]
        if parts:
            focus_areas.append({
                "id": "parts",
                "label": "Word parts",
                "kind": "overview",
                "node_ids": [*part_ids, center_id],
                "headline": " · ".join(str(part["canonical_form"]) for part in parts),
                "explanation": "Only linguistically supported bases and affixes are shown.",
            })
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
        history_evidence = [
            str(evidence_id)
            for collection in (graph_nodes, graph_edges)
            for claim in collection
            if claim.get("basis") == "book"
            for evidence_id in claim.get("evidence_ids", [])
        ]
        evidence_ids = list(
            dict.fromkeys([*history_evidence, *component_evidence, *meaning_evidence])
        )
        support_forms = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    *(part.get("canonical_form", "") for part in parts),
                    *(node.get("form", "") for node in graph_nodes),
                )
                if str(value).strip()
            )
        )
        evidence = self._card_evidence(
            source, evidence_ids, support_forms=support_forms
        )
        if not evidence:
            raise ValueError("accepted origin evidence could not be reconstructed")
        evidence_ids = [
            str(getattr(item, "evidence_id"))
            for item in evidence
            if getattr(item, "evidence_id", "")
        ]

        graph = {
            "center_id": center_id,
            "nodes": graph_nodes,
            "edges": graph_edges,
            "focus_areas": focus_areas,
            "provenance_warnings": provenance_warnings,
        }
        word_lexical_view = self._lexical_view(
            center_id, "word", [center_id]
        )
        if (
            accepted_components is not None
            and not parts
            and len(graph_nodes) == 1
            and not graph_edges
        ):
            graph["status"] = "awaiting-enrichment"
            graph["status_reason"] = (
                "No current origin relationships; local enrichment is pending."
            )
            focus_areas[0]["explanation"] = graph["status_reason"]
        word_related_terms = _derivative_terms_for_view(
            [split_payload, origin["payload"]],
            str(source["text"]),
            parts,
            parts,
            limit=16,
        )
        self._include_related_term_completeness(
            word_lexical_view, word_related_terms
        )

        source_checkpoints = [item for item in (meaning, split, origin) if item is not None]
        quality = min(_artifact_quality(item) for item in source_checkpoints)
        if any(
            publication_component_basis[str(part["morpheme_id"])] == "model"
            for part in parts
        ):
            quality = min(quality, 0.7)
        card = Card(
            card_id=str(uuid.uuid4()),
            mode="word",
            query=str(source["text"]),
            title=str(source["text"]),
            subtitle=(
                " · ".join(str(part["canonical_form"]) for part in parts)
                or ("Word history" if root_headlines else "History awaiting enrichment")
            ),
            summary_en=str(meaning["payload"]["definition"]),
            origin_story=(
                f"The lexical history follows {'; '.join(root_headlines)}."
                if root_headlines
                else ""
            ),
            key_points=[],
            english=english,
            japanese=japanese,
            chinese=chinese,
            memory_hook="",
            related_terms=word_related_terms,
            evidence=evidence,
            model="accepted atomic knowledge",
            created_at=datetime.now(UTC).isoformat(),
            extensions={
                "experience": "word",
                "knowledge_policy": "accepted-atomic-origin-view",
                "knowledge_subject": job["subject_key"],
                "knowledge_artifact_ids": [
                    item["artifact_id"] for item in source_checkpoints
                ],
                "evidence_ids": evidence_ids,
                "lexical_view": word_lexical_view,
                "morphology_graph": graph,
                "outputs": ["web"],
                "future_outputs": ["eink", "audio"],
            },
            extra_languages=_extra_language_card_values(language_atoms),
            languages=language_atoms,
        )
        cards = [card]
        for derived_mode, focus_kinds, part_kinds, policy in (
            _derived_origin_view_specs(derived_parts)
        ):
            branch_focuses = [
                deepcopy(area)
                for area in focus_areas
                if area["kind"] in focus_kinds
            ]
            if not branch_focuses:
                continue
            for branch_focus in branch_focuses:
                branch_node_ids = {
                    str(node_id) for node_id in branch_focus.get("node_ids", [])
                }
                relevant_parts = [
                    part
                    for part in parts
                    if str(part["kind"]) in part_kinds
                    and str(part["morpheme_id"]) in branch_node_ids
                ]
                if not relevant_parts:
                    continue
                derived_graph = deepcopy(graph)
                derived_graph["focus_areas"] = [
                    branch_focus,
                    deepcopy(focus_areas[0]),
                ]
                derived_graph["center_id"] = str(relevant_parts[0]["morpheme_id"])
                derived = deepcopy(card)
                derived.card_id = str(uuid.uuid4())
                derived.mode = derived_mode
                derived.title = str(
                    branch_focus.get("headline")
                    or " · ".join(
                        str(part["canonical_form"]) for part in relevant_parts
                    )
                )
                derived.subtitle = f"{derived_mode.upper()} · {source['text']}"
                derived.related_terms = _derivative_terms_for_view(
                    [split_payload, origin["payload"]],
                    str(source["text"]),
                    parts,
                    relevant_parts,
                )
                if derived_mode == "affix":
                    derived.origin_story = _affix_origin_story(relevant_parts)
                derived.created_at = datetime.now(UTC).isoformat()
                derived.model = "accepted atomic knowledge"
                derived.extensions = deepcopy(card.extensions)
                derived.extensions["experience"] = derived_mode
                derived.extensions["knowledge_policy"] = policy
                branch_component_id = self._branch_component_id(
                    branch_focus,
                    relevant_parts,
                    derived_mode,
                )
                derived.extensions["branch_focus_id"] = str(branch_focus["id"])
                lexical_view = self._lexical_view(
                    center_id,
                    derived_mode,
                    [branch_component_id],
                    accepted_focus_node=next(
                        (
                            node
                            for node in graph_nodes
                            if str(node.get("id", "")) == branch_component_id
                        ),
                        None,
                    ),
                )
                self._include_related_term_completeness(
                    lexical_view, derived.related_terms
                )
                derived.extensions["lexical_view"] = lexical_view
                derived.extensions["meaning_completeness"] = deepcopy(
                    lexical_view["meaning_completeness"]
                )
                derived.extensions["morphology_graph"] = derived_graph
                cards.append(derived)

        publication_errors = {
            f"{output_card.mode}:{index}": card_validation_errors(
                output_card.to_dict()
            )
            for index, output_card in enumerate(cards)
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

        branch_cleanup = self._persist_composed_origin_cards(
            cards,
            quality=quality,
            authoritative_empty_modes=bool(projection_fingerprint),
        )
        accepted = {
            "card_id": card.card_id,
            "mode": card.mode,
            "quality": quality,
            "knowledge_artifact_ids": card.extensions["knowledge_artifact_ids"],
            "derived_card_ids": {
                (
                    f"{output_card.mode}:"
                    f"{output_card.extensions.get('branch_focus_id', 'overview')}"
                ): output_card.card_id
                for output_card in cards
                if output_card is not card
            },
            "branch_cleanup": branch_cleanup,
            "card": card.to_dict(),
        }
        artifact_id = self.store.save_job_artifact(
            job["job_id"],
            "accepted-origin-card",
            accepted,
            language="en",
            validation_state="accepted",
            quality_score=quality,
        )
        if projection_fingerprint:
            complete = getattr(
                self.store, "complete_lexical_projection_repair", None
            )
            if callable(complete):
                complete(
                    str(job["subject_entity_id"]),
                    projection_fingerprint,
                    compose_job_id=str(job["job_id"]),
                )
        return artifact_id

    def _prepare_translation(self, job: dict[str, Any]) -> str:
        language = _language_code(job.get("language", ""))
        repair_context = repair_request(self.store, job)
        term = self.store.term_record(str(job["subject_entity_id"]))
        source_language = _language_code(term["language"])
        if language == source_language:
            raise ValueError("source language is not a translation target")
        if repair_context.get("repair_mode") == "restore-checkpoint":
            return restore_translation_checkpoint(
                self.store, job, repair_context["previous_translation"],
            )
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
        matching_meanings = [
            artifact for artifact in meaning_artifacts
            if str(artifact.get("language", "")) == source_language
        ]
        meaning = (matching_meanings or meaning_artifacts)[-1]["payload"]
        evidence_ids = [str(item) for item in meaning.get("evidence_ids", [])]
        candidates: list[str] = []
        candidate_evidence: dict[str, list[str]] = {}
        freedict_candidate_evidence: dict[str, list[str]] = {}
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
                candidate = _normalize_dictionary_candidate(value, language)
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
                if candidate and record_evidence_id:
                    candidate_evidence.setdefault(candidate, []).append(
                        record_evidence_id
                    )
                    if (
                        exact_bilingual
                        and str(record.get("corpus_id", "")) == FREEDICT_CORPUS_ID
                    ):
                        freedict_candidate_evidence.setdefault(candidate, []).append(
                            record_evidence_id
                        )

        evidenced_candidates = [
            candidate
            for candidate in candidates
            if candidate_evidence.get(candidate)
        ]
        deterministic_selection: dict[str, Any] | None = None
        prior_translations = self.store.artifacts_for_subject(
            job["subject_key"],
            stage="accepted-translation",
            validation_state="accepted",
        )
        for artifact in reversed(prior_translations):
            if str(artifact.get("language", "")) != language:
                continue
            prior_payload = artifact.get("payload", {})
            if not isinstance(prior_payload, dict):
                continue
            prior_term = " ".join(
                str(prior_payload.get("term", "")).split()
            ).casefold()
            exact_matches = [
                candidate
                for candidate in evidenced_candidates
                if " ".join(candidate.split()).casefold() == prior_term
            ]
            if len(exact_matches) != 1:
                continue
            deterministic_selection = {
                "candidate": exact_matches[0],
                "reason": "prior-exact-evidenced-match",
                "prior_artifact_id": str(artifact.get("artifact_id", "")),
                "prior_payload": prior_payload,
            }
            break
        if deterministic_selection is None and len(evidenced_candidates) == 1:
            # FreeDict bilingual records contain the translated headword but no
            # target-language prose definition or reading. Languages requiring
            # an independent reading stay on the Qwen path on their first pass.
            if language not in {"ja", "zh", "ar"}:
                deterministic_selection = {
                    "candidate": evidenced_candidates[0],
                    "reason": "single-evidenced-candidate",
                    "prior_artifact_id": "",
                    "prior_payload": {},
                }

        prompt = f"""SOURCE LANGUAGE: {_language_name(source_language)} ({source_language})
SOURCE TERM: {term['text']}
ACCEPTED SOURCE SENSE: {meaning['definition']}
TARGET LANGUAGE: {_language_name(language)} ({language})
DICTIONARY CANDIDATES: {json.dumps(candidates[:10], ensure_ascii=False)}

Return exactly one JSON object with these keys:
term: the most natural concise equivalent for this exact sense
meaning: a short definition in the target language, at most 24 words
reading: kana for Japanese kanji, tone-marked pinyin for Chinese, simple Latin
transliteration for Arabic, or a conventional reading/transliteration when useful
usage_note: at most 14 English words, empty when unnecessary
confidence: number from 0 to 1

Dictionary candidates are evidence, not a compulsory answer list. Choose one
only if it fits this exact sense and part of speech; otherwise translate the
sense naturally. A headword match alone does not establish a sense match.
For Chinese, term must be a Chinese word in Han characters, not the source word
or its pinyin. For Japanese, use a natural Japanese form.
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
EXACT SOURCE SENSE: {meaning['definition']}
DICTIONARY CANDIDATES: {json.dumps(candidates[:10], ensure_ascii=False)}

Return exactly one compact JSON object with only these keys:
term: the natural Modern Standard Arabic equivalent in Arabic script
meaning: a concise Arabic definition, at most 12 words
reading: a simple Latin transliteration of term
confidence: number from 0 to 1

Use a candidate only if it matches the exact source sense. Headword dictionaries
may list a different sense; prefer a natural sense-aligned equivalent in that case.
Term and meaning must contain no
Latin letters. Return no alternatives, markdown, labels, IDs, or explanation.
End immediately after the JSON object."""
            system_prompt = "Return one compact Arabic lexical JSON object only."
            token_budget = 128
        if repair_context or job.get("error"):
            # Repeating a failed prompt or copying the same accepted answer is
            # not a review. Supply the actual defect and fresh retrieved context.
            deterministic_selection = None
            context = [
                {key: record.get(key, "") for key in
                 ("headword", "definition", "excerpt", "kind", "translation_scope")}
                for record in records[:4]
            ]
            prompt += (
                "\nLOCAL EVIDENCE-GROUNDED REPAIR\n"
                "Reconsider the previous answer; do not copy it automatically.\n"
                "OBSERVATIONS: " + json.dumps(repair_context.get("issues", [])) +
                "\nPREVIOUS ERROR: " + str(job.get("error", ""))[:300] +
                "\nPREVIOUS ANSWER: " + json.dumps(
                    repair_context.get("previous_translation", {}), ensure_ascii=False
                )[:1000] +
                "\nRETRIEVED CONTEXT: " + json.dumps(context, ensure_ascii=False)[:2400] +
                "\nTranslate only the stated source sense. A book may discuss other\n"
                "senses or history; do not copy that discussion into the definition.\n"
                "Return the same small JSON object, without commentary."
            )
        deterministic_reason = ""
        deterministic_source_artifact_id = ""
        if deterministic_selection is not None:
            selected_candidate = str(deterministic_selection["candidate"])
            prior_payload = deterministic_selection["prior_payload"]
            deterministic_reason = str(deterministic_selection["reason"])
            deterministic_source_artifact_id = str(
                deterministic_selection["prior_artifact_id"]
            )
            deterministic_value = {
                "term": selected_candidate,
                "meaning": str(prior_payload.get("meaning", "")).strip()
                or selected_candidate,
                "reading": str(prior_payload.get("reading", "")).strip(),
                "usage_note": str(prior_payload.get("usage_note", "")).strip(),
                "confidence": prior_payload.get("confidence", 1.0),
            }
            completion = {
                "value": deterministic_value,
                "raw": json.dumps(deterministic_value, ensure_ascii=False),
                "model": str(
                    prior_payload.get("model", "deterministic-dictionary-v1")
                ),
                "metrics": {
                    "elapsed_seconds": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "tokens_per_second": 0.0,
                    "engine": "deterministic-dictionary-v1",
                    "qwen_avoided": True,
                },
            }
        else:
            completion = self.model.complete_json(
                system_prompt,
                prompt,
                max_tokens=token_budget,
            )
        observation_raw = completion.get("raw")
        if not isinstance(observation_raw, str) or not observation_raw.strip():
            observation_raw = json.dumps(
                completion.get("value"), ensure_ascii=False, default=str
            )
        observation_metrics = completion.get("metrics")
        if not isinstance(observation_metrics, dict):
            observation_metrics = {}
        self.store.save_job_artifact(
            job["job_id"],
            (
                "dictionary-translation-draft"
                if deterministic_reason
                else "model-translation-draft"
            ),
            {
                "source_term": str(term["text"])[:160],
                "language": language,
                "value": completion.get("value"),
                "raw": observation_raw[:4_000],
                "model": str(
                    completion.get("model")
                    or getattr(self.model, "model_name", "unknown")
                )[:200],
                "metrics": {
                    str(key)[:80]: metric
                    for key, metric in list(observation_metrics.items())[:20]
                    if isinstance(metric, (str, int, float, bool)) or metric is None
                },
                "selection_reason": deterministic_reason,
                "reused_from_artifact_id": deterministic_source_artifact_id,
            },
            language=language,
            reusable=bool(deterministic_reason),
            validation_state="candidate",
        )
        value = completion.get("value")
        if not isinstance(value, dict):
            raise ValueError("translation task did not return an object")
        raw_translated = re.sub(r"\s+", " ", str(value.get("term", ""))).strip()
        translated = _normalize_dictionary_candidate(raw_translated, language)
        translated_meaning = re.sub(
            r"\s+", " ", str(value.get("meaning", ""))
        ).strip()
        reading = re.sub(r"\s+", " ", str(value.get("reading", ""))).strip()
        usage_note = _clean_usage_note(value.get("usage_note", ""), language)
        normalizations: list[str] = []
        warnings: list[dict[str, str]] = []
        if translated != raw_translated:
            normalizations.append("normalized-dictionary-join-markup")
        sole_arabic_candidate = ""
        sole_freedict_arabic_candidate = ""
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
                if freedict_candidate_evidence.get(sole_arabic_candidate):
                    sole_freedict_arabic_candidate = sole_arabic_candidate
                if not is_arabic_script_text(translated):
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
            if sole_arabic_candidate and not is_arabic_script_text(translated):
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
        diagnostic_value: Any = value
        if (
            language == "ar"
            and sole_freedict_arabic_candidate
            and translated == sole_freedict_arabic_candidate
            and not is_arabic_script_text(translated_meaning)
        ):
            rejected_meaning = translated_meaning
            offending_tokens = list(
                dict.fromkeys(
                    token
                    for token in re.findall(
                        r"[^\W\d_]+(?:[-'][^\W\d_]+)*", rejected_meaning
                    )
                    if any(
                        character.isalpha()
                        and not unicodedata.name(character, "UNKNOWN").startswith(
                            "ARABIC"
                        )
                        for character in token
                    )
                )
            )
            offending_scripts = sorted(
                {
                    unicodedata.name(character, "UNKNOWN").split(" ", 1)[0]
                    for token in offending_tokens
                    for character in token
                    if character.isalpha()
                    and not unicodedata.name(character, "UNKNOWN").startswith(
                        "ARABIC"
                    )
                }
            )
            meaning_repair = self.model.complete_json(
                "Repair only the Arabic meaning using the supplied retrieved lexical evidence.",
                f"""ARABIC RAG MEANING-ONLY REPAIR
ACCEPTED ENGLISH SENSE: {meaning['definition']}
EXACT RETRIEVED ARABIC CANDIDATE: {json.dumps(sole_freedict_arabic_candidate, ensure_ascii=False)}
RETRIEVED CANDIDATE EVIDENCE IDS: {json.dumps(freedict_candidate_evidence[sole_freedict_arabic_candidate], ensure_ascii=False)}
REJECTED MIXED-SCRIPT MEANING: {json.dumps(rejected_meaning, ensure_ascii=False)}
OFFENDING NON-ARABIC TOKENS: {json.dumps(offending_tokens, ensure_ascii=False)}
OFFENDING SCRIPTS: {json.dumps(offending_scripts, ensure_ascii=False)}

Return exactly one compact JSON object with only this key:
meaning: a concise Modern Standard Arabic definition of the accepted English
sense, written entirely in Arabic script, at most 18 words

Do not return, rewrite, or transliterate the term or reading. Do not copy any
offending token. Return no IDs, alternatives, markdown, labels, or explanation.
End immediately after the JSON object.""",
                max_tokens=96,
            )
            meaning_repair_value = meaning_repair.get("value")
            diagnostic_value = meaning_repair_value
            completion = meaning_repair
            if (
                isinstance(meaning_repair_value, dict)
                and set(meaning_repair_value) == {"meaning"}
            ):
                repaired_meaning = re.sub(
                    r"\s+", " ", str(meaning_repair_value.get("meaning", ""))
                ).strip()
                translated_meaning = repaired_meaning
                if is_arabic_script_text(repaired_meaning):
                    normalizations.append(
                        "repaired-mixed-script-arabic-meaning-with-local-rag"
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
                raw = json.dumps(diagnostic_value, ensure_ascii=False)
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
        if candidates and translated not in candidates:
            warnings.append(
                {
                    "code": "model-translation-outside-dictionary-candidates",
                    "message": (
                        "the useful local-model translation was retained as an "
                        "uncited candidate rather than rejected"
                    ),
                }
            )
        if language == "ar":
            cleaned_meaning = _collapse_repeated_arabic_alternative(
                translated_meaning
            )
            if cleaned_meaning != translated_meaning:
                translated_meaning = cleaned_meaning
                normalizations.append("collapsed-repeated-arabic-alternative")
        if language == "ar" and _has_repeated_arabic_content_word(translated_meaning):
            warnings.append(
                {
                    "code": "repeated-arabic-content-word",
                    "message": (
                        "Arabic translation meaning repeats a content word; "
                        "the model-authored meaning was retained without semantic rewriting"
                    ),
                }
            )
        if language == "zh":
            reading = chinese_pinyin(translated, reading)
        elif language == "fr":
            reading = ""
        if language in {"ja", "zh", "ar"} and not reading:
            raise ValueError("translation reading is missing")
        dictionary_evidence_ids = list(
            dict.fromkeys(candidate_evidence.get(translated, []))
        )
        dictionary_backed = (
            translated in candidates and bool(dictionary_evidence_ids)
        )
        selected = dictionary_evidence_ids if dictionary_backed else []
        provenance_basis = "book" if dictionary_backed else "model"
        confidence = max(0.0, min(float(value.get("confidence", 0.0)), 1.0))
        if provenance_basis == "model":
            confidence = min(confidence, 0.7)
            if translated in candidates and not dictionary_evidence_ids:
                warnings.append(
                    {
                        "code": "dictionary-candidate-without-target-evidence",
                        "message": (
                            "the selected dictionary candidate had no target-specific "
                            "evidence ID, so it was retained as model-owned"
                        ),
                    }
                )
        unsupported_unchanged_form = (
            "".join(
                character
                for character in unicodedata.normalize("NFKC", translated).casefold()
                if character.isalnum()
            )
            == "".join(
                character
                for character in unicodedata.normalize(
                    "NFKC", str(term["text"])
                ).casefold()
                if character.isalnum()
            )
            and not candidate_evidence.get(translated)
        )
        if unsupported_unchanged_form:
            warnings.append(
                {
                    "code": "retained-unsupported-unchanged-language-form",
                    "message": (
                        "the unchanged local-model form was retained, but its "
                        "confidence was capped because no dictionary record supports it"
                    ),
                }
            )
            confidence = min(confidence, 0.55)
        if confidence < 0.6:
            warnings.append(
                {
                    "code": "retained-low-confidence-model-translation",
                    "message": (
                        "low confidence was retained visibly instead of blocking graph growth"
                    ),
                }
            )
            confidence = max(0.35, confidence)

        model_name = str(completion.get("model", self.model.model_name))
        prompt_version = str(job.get("prompt_version", ""))
        provenance_metadata = {
            "selection": (
                deterministic_reason
                if deterministic_reason
                else "exact-evidenced-dictionary-candidate"
                if dictionary_backed
                else "local-model-output"
            ),
            "selected_term": translated,
            "dictionary_candidate_match": translated in candidates,
            "dictionary_evidence_ids": selected,
            "model": model_name,
            "prompt_version": prompt_version,
            "execution_engine": (
                "deterministic-dictionary-v1"
                if deterministic_reason
                else "local-qwen"
            ),
            "reused_from_artifact_id": deterministic_source_artifact_id,
        }

        target_id = self.store.upsert_term(
            language, translated, status="accepted", quality_score=confidence
        )
        translation_id = self.store.add_translation(
            term["entity_id"],
            language,
            translated,
            definition=translated_meaning,
            definition_model=str(
                completion.get("model", self.model.model_name)
            ),
            definition_prompt_version=str(job.get("prompt_version", "")),
            transliteration=reading,
            usage_note=usage_note,
            source_meaning_id=str(meaning["meaning_id"]),
            target_term_id=target_id,
            status="accepted",
            quality_score=confidence,
            basis=provenance_basis,
            confidence=confidence,
            evidence_ids=selected,
            metadata=provenance_metadata,
        )
        accepted = {
            "translation_id": translation_id,
            "target_term_id": target_id,
            "source_term": term["text"],
            "source_language": source_language,
            "source_meaning_id": str(meaning["meaning_id"]),
            "language": language,
            "term": translated,
            "meaning": translated_meaning,
            "reading": reading,
            "usage_note": usage_note,
            "confidence": confidence,
            "evidence_ids": selected,
            "dictionary_candidates": candidates[:10],
            "dictionary_evidence_ids": selected,
            "provenance": {
                "basis": provenance_basis,
                "evidence_ids": selected,
                **provenance_metadata,
            },
            "normalizations": normalizations,
            "warnings": warnings,
            "model": model_name,
            "execution_engine": provenance_metadata["execution_engine"],
            "reused_from_artifact_id": deterministic_source_artifact_id,
            "metrics": completion.get("metrics", {}),
            "repair_review": {
                "issues": repair_context.get("issues", []),
                "source_sense_fingerprint": repair_context.get("source_sense_fingerprint", ""),
                "request_fingerprint": repair_context.get("fingerprint", ""),
            } if repair_context else {},
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
