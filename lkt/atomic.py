from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .corpus import CorpusIndex
from .knowledge import KnowledgeStore
from .lexicon import WordnetRag
from .llm import LlamaCppClient
from .models import Evidence
from .morphology import MorphologyIndex
from .pronunciation import chinese_pinyin


SUPPORTED_ATOMIC_JOBS = (
    "retrieve-evidence",
    "prepare-meaning",
    "prepare-translation",
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


class AtomicModel(Protocol):
    model_name: str

    def complete_json(
        self, system: str, prompt: str, *, max_tokens: int = 256
    ) -> dict[str, Any]: ...


class AtomicRetriever(Protocol):
    def retrieve(self, term: str) -> list[dict[str, Any]]: ...


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
    ):
        self.store = store
        self.retriever = retriever
        self.model = model

    def run_once(self) -> AtomicRunResult | None:
        job = self.store.claim_next_job(SUPPORTED_ATOMIC_JOBS)
        if job is None:
            return None
        try:
            if job["job_type"] == "retrieve-evidence":
                artifact_id = self._retrieve(job)
            elif job["job_type"] == "prepare-meaning":
                artifact_id = self._prepare_meaning(job)
            else:
                artifact_id = self._prepare_translation(job)
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
            job["job_id"], "retrieved-evidence", {"term": term["text"], "records": saved}
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
            job["job_id"], "accepted-meaning", accepted, language="en"
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
            job["subject_key"], stage="accepted-meaning"
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
        if language == "ar" and not re.search(r"[\u0600-\u06ff]", translated):
            raise ValueError("Arabic translation has no Arabic script")
        if language == "zh":
            reading = chinese_pinyin(translated, reading)
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
            usage_note=str(value.get("usage_note", "")).strip()[:180],
            source_meaning_id=str(meaning["meaning_id"]),
            target_term_id=target_id,
            status="accepted",
            quality_score=confidence,
        )
        for evidence_id in selected:
            self.store.link_evidence(
                translation_id,
                evidence_id,
                claim=f"{term['text']} → {translated}",
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
            "usage_note": str(value.get("usage_note", "")).strip()[:180],
            "confidence": confidence,
            "evidence_ids": selected,
            "dictionary_candidates": candidates[:10],
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
            job["job_id"], "accepted-translation", accepted, language=language
        )


def build_worker(
    store: KnowledgeStore,
    corpus: CorpusIndex,
    roots: MorphologyIndex,
    affixes: MorphologyIndex,
    lexicon: WordnetRag,
    model: LlamaCppClient,
) -> PreparationWorker:
    return PreparationWorker(
        store,
        WordEvidenceRetriever(corpus, roots, affixes, lexicon),
        model,
    )
