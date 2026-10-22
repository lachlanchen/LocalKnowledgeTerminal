"""Shared language-card JSON, independent of diagram traversal limits."""
from __future__ import annotations

import json
import unicodedata
from contextlib import closing
from typing import Any

from .languages import language_profile, normalize_language_code, ordered_language_codes


def _key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _quality(artifact: dict[str, Any]) -> float:
    value = artifact.get("quality_score")
    if value is None:
        value = artifact.get("payload", {}).get("confidence", 0.0)
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def language_card_atoms(source, meaning, translations, pronunciations):
    source_language = normalize_language_code(source.get("language", "en"))
    payload = meaning.get("payload", {})
    raw = {
        source_language: {
            "language": source_language, "term": str(source.get("text", "")),
            "meaning": str(payload.get("definition", "")),
            "term_id": str(source.get("entity_id", "")),
            "meaning_id": str(payload.get("meaning_id", "")),
            "source": True, "translation_artifact_id": "",
            "meaning_artifact_id": str(meaning.get("artifact_id", "")),
            "confidence": _quality(meaning),
        }
    }
    for language, artifact in translations.items():
        payload = artifact.get("payload", {})
        if not isinstance(payload, dict) or not str(payload.get("term", "")).strip():
            continue
        code = normalize_language_code(language)
        raw[code] = {
            "language": code, "term": str(payload.get("term", "")),
            "meaning": str(payload.get("meaning", "")),
            "reading": str(payload.get("reading", "")),
            "term_id": str(payload.get("target_term_id", "")),
            "meaning_id": str(payload.get("target_meaning_id", "")),
            "translation_id": str(payload.get("translation_id", "")),
            "source": False,
            "translation_artifact_id": str(artifact.get("artifact_id", "")),
            "confidence": _quality(artifact),
        }
    for language, artifact in pronunciations.items():
        payload = artifact.get("payload", {})
        if not isinstance(payload, dict):
            continue
        code = normalize_language_code(language)
        atom = raw.get(code)
        if atom is None:
            # An old pronunciation must not resurrect a superseded translation.
            continue
        if payload.get("term") and _key(payload["term"]) != _key(atom["term"]):
            continue
        if (payload.get("target_term_id") and atom.get("term_id")
                and payload["target_term_id"] != atom["term_id"]):
            continue
        atom.update({
            "reading": str(payload.get("reading", "")),
            "pronunciation": str(payload.get("reading", "")),
            "pronunciation_id": str(payload.get("pronunciation_id", "")),
            "pronunciation_artifact_id": str(artifact.get("artifact_id", "")),
            "system": str(payload.get("system", "")),
            "segments": list(payload.get("segments", [])),
        })
        atom["confidence"] = min(float(atom.get("confidence", 1.0)), _quality(artifact))
    result = {}
    for code in ordered_language_codes(source_language, raw):
        if code not in raw:
            continue
        atom = raw[code]
        profile = language_profile(code)
        atom.update({
            "label": profile.label, "direction": profile.direction,
            "reading_system": str(atom.get("system", profile.reading_system)),
            "ruby_tokens": [
                {"t": str(item.get("grapheme", "")), "r": str(item.get("phoneme", ""))}
                for item in atom.get("segments", [])
                if isinstance(item, dict) and item.get("grapheme") and item.get("phoneme")
            ],
            "state": "complete" if atom.get("term") and atom.get("meaning") else "partial",
        })
        result[code] = atom
    return result


def accepted_language_artifacts(store, subject_id: str):
    """Read one consistent set of accepted checkpoints, not bounded graph nodes."""
    with closing(store._connect()) as connection:
        rows = connection.execute(
            """SELECT a.artifact_id,a.job_id,a.stage,a.language,a.payload,
                      a.quality_score,a.created_at
               FROM job_artifacts a JOIN preparation_jobs j USING(job_id)
               WHERE j.subject_entity_id=? AND a.validation_state='accepted'
                 AND a.stage IN ('accepted-meaning','accepted-translation',
                                 'accepted-pronunciation')
               ORDER BY a.created_at,a.artifact_id""", (subject_id,),
        ).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def project_language_atoms(store, subject_id: str):
    source = store.term_record(subject_id)
    artifacts = accepted_language_artifacts(store, subject_id)
    meanings = [
        item for item in artifacts if item["stage"] == "accepted-meaning"
        and item["language"] == source["language"]
    ]
    if not meanings:
        return {}
    translations = {
        item["language"]: item for item in artifacts
        if item["stage"] == "accepted-translation"
    }
    pronunciations = {
        item["language"]: item for item in artifacts
        if item["stage"] == "accepted-pronunciation"
    }
    result = language_card_atoms(source, meanings[-1], translations, pronunciations)
    for atom in result.values():
        atom["projection"] = "accepted-language-checkpoints"
    return result
