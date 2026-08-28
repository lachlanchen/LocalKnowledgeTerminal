from __future__ import annotations

import json
import re
import unicodedata
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .corpus import CorpusIndex
from .knowledge import KnowledgeStore
from .lexicon import WordnetRag
from .llm import LlamaCppClient
from .models import Card, Evidence
from .morphology import MorphologyIndex
from .pronunciation import (
    EspeakPronouncer,
    chinese_pinyin,
    chinese_ruby_tokens,
    is_arabic_script_text,
)
from .store import CardStore


SUPPORTED_ATOMIC_JOBS = (
    "retrieve-evidence",
    "prepare-meaning",
    "split-morphemes",
    "expand-origin-branches",
    "extract-investigation-terms",
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


class WordEvidenceRetriever:
    """Small correction context from books plus sense-aligned OMW."""

    def __init__(
        self,
        corpus: CorpusIndex,
        roots: MorphologyIndex,
        affixes: MorphologyIndex,
        lexicon: WordnetRag,
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
        return [
            _book_record(item, self._hash(self.corpus))
            for item in self.corpus.search(plain, 6)
            if _lexically_related(plain, item)
        ][:3]


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
    ):
        self.store = store
        self.retriever = retriever
        self.model = model
        self.pronouncer = pronouncer or EspeakPronouncer()
        self.card_store = card_store

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
        seen_hints: set[tuple[str, str]] = set()
        for start in range(len(letters)):
            for end in range(start + 3, min(len(letters), start + 8) + 1):
                candidate = letters[start:end]
                for record in self.retriever.component_evidence(candidate, "root"):
                    key = (str(record.get("corpus_id", "")), str(record.get("entry_id", "")))
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
                "evidence_id": record.get("knowledge_evidence_id", ""),
                "source": record.get("source_title", record.get("corpus_id", "")),
                "headword": record.get("headword", ""),
                "kind": record.get("kind", ""),
                "component_hint": record.get("component_hint", ""),
                "component_surface": record.get("component_surface", ""),
                "excerpt": str(record.get("excerpt", ""))[:700],
            }
            for record in records
        ]
        required_shape = _book_anchored_shape(letters, records)
        shape_instruction = (
            "REQUIRED ORDERED SURFACES AND KINDS: "
            f"{json.dumps(required_shape, ensure_ascii=False)}\n"
            "Copy every required surface and kind exactly. Only supply its "
            "canonical form, language, meaning, confidence, and evidence IDs.\n"
            if required_shape
            else "No exact reviewed-book root anchor was found.\n"
        )
        prompt = f"""MORPHEME SPLIT
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
evidence_ids: only supplied evidence IDs that explicitly support this part

The concatenated surfaces must reproduce TERM exactly. Include every letter once,
include at least one root, and do not invent an extra part merely to add detail.
Use exact COMPONENT HINTS for roots when supplied. A prefix canonical form must
end in `-`; a suffix canonical form must begin with `-`. Meaning must be one plain
phrase, never a stringified list. Distinguish productive word structure from deep
history; history belongs to a later task. Use an empty evidence_ids array for
model knowledge. Never merge, shorten, rename, or reclassify a required part."""
        completion = self.model.complete_json(
            (
                "You fill metadata for a fixed, book-anchored morphology split."
                if required_shape
                else "You perform one conservative, reusable morphology split at a time."
            ),
            prompt,
            max_tokens=320,
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
        raw_parts = value.get("parts") if isinstance(value, dict) else None
        if not isinstance(raw_parts, list) or not 2 <= len(raw_parts) <= 5:
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
            if not supplied_canonical or not meaning or len(meaning.split()) > 10:
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
        if not any(part["kind"] == "root" for part in cleaned):
            raise ValueError("morpheme split has no root")
        if required_shape and [
            {"surface": part["surface"].casefold(), "kind": part["kind"]}
            for part in cleaned
        ] != required_shape:
            raise ValueError("morpheme split changed the book-anchored structure")

        for part in cleaned:
            direct = self.retriever.component_evidence(
                part["canonical_form"], part["kind"]
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
            if part["kind"] == "root" and not part["evidence_ids"]:
                raise ValueError(
                    f"root {part['canonical_form']!r} has no exact component-book evidence"
                )

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

        allowed_by_component: dict[str, set[str]] = {}
        context: list[dict[str, Any]] = []
        for part in parts:
            component_id = str(part.get("morpheme_id", ""))
            allowed = {
                str(item) for item in part.get("evidence_ids", []) if str(item)
            }
            for record in self.retriever.origin_evidence(
                str(part.get("canonical_form", ""))
            ):
                evidence_id = self.store.add_evidence(
                    str(record.get("corpus_id", "")),
                    str(record.get("entry_id", "")),
                    source_hash=str(record.get("source_hash", "")),
                    locator=str(record.get("locator", "")),
                    excerpt=str(record.get("excerpt", "")),
                    payload=record,
                )
                allowed.add(evidence_id)
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

        focus = next(
            (
                item
                for item in context
                if item["kind"] == "root" and item["evidence"]
            ),
            None,
        )
        if focus is None:
            raise ValueError("no cited root is available for origin expansion")
        focus_evidence = self.store.evidence_records(
            sorted(allowed_by_component[str(focus["component_id"])])
        )
        extracted_steps = _book_origin_steps(focus_evidence)
        prompt = f"""ONE ORIGIN BRANCH
MODERN WORD: {source['text']}
FIXED COMPONENT AND EVIDENCE: {json.dumps(focus, ensure_ascii=False)}

Return exactly one JSON object with `component_id` copied exactly and `steps`, an
array ordered oldest to newest. Use one to three historically useful steps. Each
step has:
form: concise attested or reconstructed historical form
language: ISO-style code such as la, fro, fr, grc, ine-pro, or en
period: concise era or language-stage label
meaning: at most 10 English words
confidence: number from 0 to 1
evidence_ids: only evidence IDs under that exact component

The final step develops into the fixed component. Do not repeat the modern word,
invent dates, or add a sibling component.
Book evidence is authoritative. Model knowledge must use an empty evidence_ids
array. Stop a branch when another step is uncertain. Prefer a small accurate
graph over a decorative graph."""
        completion = (
            {
                "value": {
                    "component_id": focus["component_id"],
                    "steps": extracted_steps,
                },
                "model": "deterministic-book-extractor",
                "metrics": {"model_calls": 0},
            }
            if extracted_steps
            else self.model.complete_json(
                "You expand exactly one bounded, backwards etymology branch.",
                prompt,
                max_tokens=288,
            )
        )
        value = completion.get("value")
        self.store.save_job_artifact(
            job["job_id"],
            "book-origin-draft" if extracted_steps else "model-origin-draft",
            {
                "term": source["text"],
                "value": value,
                "model": completion.get("model", self.model.model_name),
                "metrics": completion.get("metrics", {}),
            },
            language=source["language"],
            validation_state="candidate",
        )
        if not isinstance(value, dict) or str(value.get("component_id", "")) != str(
            focus["component_id"]
        ):
            raise ValueError("origin task changed the fixed component")
        raw_by_component = {str(focus["component_id"]): value}

        cleaned_branches: list[dict[str, Any]] = []
        total_steps = 0
        root_has_history = False
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
                selected = list(
                    dict.fromkeys(
                        str(item)
                        for item in raw.get("evidence_ids", [])
                        if str(item) in allowed_by_component[component_id]
                    )
                ) if isinstance(raw.get("evidence_ids"), list) else []
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
            root_has_history = root_has_history or (
                part.get("kind") == "root" and bool(steps)
            )
            cleaned_branches.append(
                {
                    "component_id": component_id,
                    "component_form": part["canonical_form"],
                    "component_kind": part["kind"],
                    "steps": steps,
                }
            )
        if total_steps < 1 or total_steps > 5 or not root_has_history:
            raise ValueError("origin task did not establish a bounded root history")

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
                    properties={"component_id": branch["component_id"]},
                )
                self.store.record_revision(
                    historical_id,
                    step,
                    model=str(completion.get("model", self.model.model_name)),
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
            "model": completion.get("model", self.model.model_name),
            "metrics": completion.get("metrics", {}),
        }
        self.store.record_revision(
            source["entity_id"],
            accepted,
            model=str(accepted["model"]),
            prompt_version=str(job.get("prompt_version", "")),
            reason="bounded recursive origin expansion",
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
            reading = str(translation.get("reading", "")).strip()
            segments = [
                {
                    "grapheme": visible_term,
                    "phoneme": reading,
                    "color_key": "p0",
                    "features": {"ruby": True},
                }
            ]
            system, dialect = "kana", "standard"
            confidence = float(translation.get("confidence", 0.8))
            method = {"engine": "accepted translation", "basis": "dictionary + model"}
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
            model="deterministic",
            prompt_version=str(job.get("prompt_version", "")),
            reason=f"atomic {language} pronunciation",
            accepted=True,
        )
        return self.store.save_job_artifact(
            job["job_id"],
            "accepted-pronunciation",
            accepted,
            language=language,
            validation_state="accepted",
            quality_score=confidence,
        )

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
                "reading": str(japanese["reading"]),
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
            if branch.get("component_kind") == "root":
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
                        "label": "Root history",
                        "kind": "root",
                        "node_ids": [*branch_ids, component_id, center_id],
                        "headline": headline,
                        "explanation": (
                            "This cited root carries the central history."
                            if branch_ids
                            else "This accepted root contributes to the modern word."
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
                "explanation": "One word, its fixed parts, and the cited root history.",
            },
            {
                "id": "parts",
                "label": "Word parts",
                "kind": "overview",
                "node_ids": [*part_ids, center_id],
                "headline": " · ".join(str(part["canonical_form"]) for part in parts),
                "explanation": "Prefix, root, and suffix combine into the modern word.",
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
                f"The cited root follows {'; '.join(root_headlines)}."
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
                "reading": str(japanese["reading"]),
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
        for derived_mode, focus_kinds, policy in (
            ("root", {"root"}, "accepted-atomic-root-view"),
            ("affix", {"prefix", "suffix"}, "accepted-atomic-affix-view"),
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
                part for part in parts if str(part["kind"]) in focus_kinds
            ]
            derived_graph["center_id"] = str(relevant_parts[0]["morpheme_id"])
            derived = deepcopy(card)
            derived.card_id = str(uuid.uuid4())
            derived.mode = derived_mode
            derived.title = " · ".join(
                str(part["canonical_form"]) for part in relevant_parts
            )
            derived.subtitle = f"{derived_mode.upper()} · {source['text']}"
            derived.created_at = datetime.now(UTC).isoformat()
            derived.model = "accepted atomic knowledge"
            derived.extensions = deepcopy(card.extensions)
            derived.extensions["experience"] = derived_mode
            derived.extensions["knowledge_policy"] = policy
            derived.extensions["morphology_graph"] = derived_graph
            cards.append(derived)

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
        for record in records:
            if str(record.get("knowledge_evidence_id", "")) not in evidence_ids:
                continue
            translations = record.get("translations")
            values = translations.get(language, []) if isinstance(translations, dict) else []
            for value in values if isinstance(values, list) else []:
                candidate = re.sub(r"\s+", " ", str(value)).strip()
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

        prompt = f"""SOURCE TERM: {term['text']}
ACCEPTED ENGLISH SENSE: {meaning['definition']}
TARGET LANGUAGE: {_LANGUAGE_NAMES[language]} ({language})
DICTIONARY CANDIDATES: {json.dumps(candidates[:10], ensure_ascii=False)}
SUPPORTING EVIDENCE IDS: {json.dumps(evidence_ids, ensure_ascii=False)}

Return exactly one JSON object with these keys:
term: the most natural concise equivalent for this exact sense
meaning: a short definition in the target language, at most 24 words
reading: kana for Japanese kanji, tone-marked pinyin for Chinese, simple Latin
transliteration for Arabic, or an empty string for French
usage_note: at most 14 English words, empty when unnecessary
confidence: number from 0 to 1
evidence_ids: non-empty array containing only supplied evidence IDs

When dictionary candidates are non-empty, term must exactly equal one candidate.
Use natural, non-redundant wording; never repeat a content word around "or".
For Arabic, term and meaning must contain Arabic script only. Never copy Latin
letters or the English source term into either field.
Do not add alternatives, markdown, etymology, or example sentences."""
        completion = self.model.complete_json(
            "You prepare one sense-aligned translation at a time. Preserve scripts accurately.",
            prompt,
            max_tokens=176,
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
        if language == "ar" and (
            not is_arabic_script_text(translated)
            or not is_arabic_script_text(translated_meaning)
        ):
            repair = self.model.complete_json(
                "You repair one Arabic lexical entry. Arabic fields must contain no Latin letters.",
                f"""ARABIC SCRIPT REPAIR
SOURCE ENGLISH SENSE: {meaning['definition']}
SUPPORTING EVIDENCE IDS: {json.dumps(evidence_ids, ensure_ascii=False)}

Return exactly one JSON object with these keys:
term: a natural Modern Standard Arabic noun using Arabic letters only; choose
the exact sense, such as اختراق, طفرة, إنجاز نوعي, or اكتشاف مهم when appropriate
meaning: a concise definition written entirely in Arabic, at most 18 words
reading: a simple Latin transliteration of the Arabic term
usage_note: at most 10 English words, or empty
confidence: number from 0 to 1
evidence_ids: non-empty array containing only supplied evidence IDs

Do not copy, transliterate, or include the English headword in term or meaning.
Do not use markdown, alternatives, labels, or explanations.""",
                max_tokens=160,
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
        if language == "ar" and not is_arabic_script_text(translated):
            raise ValueError("Arabic translation term contains mixed or non-Arabic script")
        if language == "ar" and not is_arabic_script_text(translated_meaning):
            raise ValueError("Arabic translation meaning contains mixed or non-Arabic script")
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
        selected = [
            str(item)
            for item in value.get("evidence_ids", [])
            if str(item) in evidence_ids
        ] if isinstance(value.get("evidence_ids"), list) else []
        if not selected:
            raise ValueError("translation did not cite supplied evidence")
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
    lexicon: WordnetRag,
    model: LlamaCppClient,
    card_store: CardStore,
) -> PreparationWorker:
    return PreparationWorker(
        store,
        WordEvidenceRetriever(corpus, roots, affixes, lexicon),
        model,
        EspeakPronouncer(),
        card_store,
    )
