"""Checkpoint reuse and small local-model repairs for incomplete word analyses."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


MORPHEME_RECOVERY_POLICY = "checkpoint-focused-repair-v1"
MORPHEME_REPAIR_MAX_TOKENS = 512


def _letters(value: Any) -> str:
    return re.sub(r"[^A-Za-z]", "", str(value or "")).casefold()


def _compact(part: dict[str, Any]) -> dict[str, Any]:
    return {
        "surface": str(part.get("surface") or "")[:80],
        "canonical_form": str(part.get("canonical_form") or "")[:80],
        "kind": str(part.get("kind") or "")[:16],
        "meaning": " ".join(str(part.get("meaning") or "").split()[:8])[:100],
    }


def morpheme_context_fingerprint(term: str, context: dict[str, Any]) -> str:
    """Bind a retry to the actual book input, not changing neighbor suggestions."""
    diagnostics = context.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    value = {
        "term": _letters(term),
        "evidence": context.get("evidence", []),
        "required_parts": context.get("required_parts", []),
        "selected_evidence_ids": diagnostics.get("selected_evidence_ids", []),
    }
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def select_morpheme_checkpoint(
    drafts: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
    *,
    term: str,
    fingerprint: str,
    prompt_policy: str,
    model_name: str,
) -> dict[str, Any] | None:
    """Reuse only this job's matching draft, including traceable legacy drafts."""
    for artifact in reversed(drafts):
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            continue
        if _letters(payload.get("term")) != _letters(term):
            continue
        if str(payload.get("inference_policy") or "") != prompt_policy:
            continue
        if str(payload.get("model") or "") != model_name:
            continue
        value = payload.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("parts"), list) or not value["parts"]:
            continue
        saved_fingerprint = str(payload.get("input_fingerprint") or "")
        if not saved_fingerprint:
            # Older drafts did not store a fingerprint. Bind them to the latest
            # prompt snapshot preceding that exact draft, never a later retry.
            before = [
                item for item in contexts
                if item.get("created_at") and artifact.get("created_at")
                and str(item["created_at"]) <= str(artifact["created_at"])
            ]
            if not before:
                continue
            prior = max(before, key=lambda item: str(item["created_at"]))
            saved = prior.get("payload")
            if not isinstance(saved, dict) or saved.get("policy") != prompt_policy:
                continue
            if _letters(saved.get("term")) != _letters(term):
                continue
            saved_context = saved.get("context")
            if not isinstance(saved_context, dict):
                continue
            saved_fingerprint = morpheme_context_fingerprint(term, saved_context)
        if saved_fingerprint == fingerprint:
            return deepcopy(artifact)
    return None


def morpheme_coverage(candidate: Any, term: str) -> dict[str, Any]:
    """Locate model suggestions in order; gaps are text spans, not new morphemes."""
    letters = _letters(term)
    raw_parts = candidate.get("parts") if isinstance(candidate, dict) else None
    raw_parts = raw_parts if isinstance(raw_parts, list) else []
    parents: list[int] = []
    aligned: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    cursor = 0
    for index, item in enumerate(raw_parts[:12]):
        if not isinstance(item, dict):
            continue
        surface = _letters(item.get("surface"))
        if surface and surface == letters:
            parents.append(index)
            continue
        start = letters.find(surface, cursor) if surface else -1
        if start < 0:
            unmatched.append({"index": index, **_compact(item)})
            continue
        if start > cursor:
            gaps.append({"surface": letters[cursor:start], "start": cursor, "end": start})
        end = start + len(surface)
        aligned.append({"index": index, **_compact(item), "start": start, "end": end})
        cursor = end
    if cursor < len(letters):
        gaps.append({"surface": letters[cursor:], "start": cursor, "end": len(letters)})
    return {
        "whole_word_part_indices": parents,
        "aligned_suggestions": aligned,
        "unmatched_suggestions": unmatched,
        "uncovered_spans": gaps,
    }


def build_morpheme_repair_prompt(
    term: str, context: dict[str, Any], candidate: Any, reason: str,
) -> tuple[str, dict[str, Any]]:
    """Ask for the missing structure, not a second large all-purpose analysis."""
    coverage = morpheme_coverage(candidate, term)
    evidence = [
        item for item in context.get("evidence", []) if isinstance(item, dict)
    ]
    evidence.sort(key=lambda item: (
        0 if _letters(item.get("headword")) == _letters(term) else 1,
        0 if any(
            isinstance(match, dict) and match.get("kind") == "root"
            for match in item.get("candidate_matches", [])
        ) else 1,
    ))
    selected: list[dict[str, str]] = []
    remaining = 1050
    seen: set[str] = set()
    for item in evidence:
        if len(selected) >= 3 or remaining <= 0:
            break
        text = " ".join(str(item.get("excerpt") or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        limit = min(420, remaining)
        match = re.search(
            r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, re.I,
        )
        start = max(0, match.start() - 60) if match else 0
        excerpt = text[start:start + limit]
        remaining -= len(excerpt)
        selected.append({
            "headword": str(item.get("headword") or "")[:80],
            "excerpt": excerpt,
        })
    raw_parts = candidate.get("parts") if isinstance(candidate, dict) else []
    parents = [
        _compact(raw_parts[index])
        for index in coverage["whole_word_part_indices"][:1]
    ] if isinstance(raw_parts, list) else []
    required = [
        {key: item[key] for key in ("surface", "kind") if item.get(key)}
        for item in context.get("required_parts", []) if isinstance(item, dict)
    ]
    prompt = "\n\n".join((
        "LINGUISTIC REVIEW OF A LEXICAL STRUCTURE DRAFT",
        "SMALL MORPHEME REPAIR. TERM: " + term,
        "STRUCTURE PROBLEM: " + str(reason)[:220],
        "RETRIEVED BOOK TEXT: " + json.dumps(selected, ensure_ascii=False),
        "MODEL SUGGESTIONS AND UNCOVERED LETTERS: " + json.dumps(coverage, ensure_ascii=False),
        "WHOLE-WORD DESCRIPTION (not an extra component): " + json.dumps(parents, ensure_ascii=False),
        "BOOK-FIXED ORDERED PARTS: " + json.dumps(required, ensure_ascii=False) if required else
        "No complete book formula is fixed. Use the text and your linguistic knowledge.",
        (
            "Find the lexical base first, then prefixes and suffixes. The suggestions "
            "and their roles may be wrong; you may correct them. A final substring "
            "is not necessarily a suffix. Uncovered spans are letters needing analysis, "
            "not asserted roots. Do not just copy the incomplete draft."
        ),
        (
            "Return only one flat parts array, covering every letter of TERM exactly "
            "once in order. Include a root or free lexical base. Do not include a "
            "whole-word parent alongside smaller components. If the word is genuinely "
            "indivisible, one free part is acceptable. Copy book-fixed parts when supplied."
        ),
        (
            "Each part needs surface (exact letters here), canonical_form (lexical "
            "spelling, allowed to differ), kind (root, prefix, suffix, or free), language "
            "(code, or und), meaning (short English phrase), confidence, evidence_ids []. "
            "No derivatives, history, or commentary; earlier proposals are preserved."
        ),
        '{"parts":[{"surface":"","canonical_form":"","kind":"","language":"und","meaning":"","confidence":0.5,"evidence_ids":[]}]}',
    ))
    return prompt, {
        "policy": MORPHEME_RECOVERY_POLICY,
        "coverage": coverage,
        "selected_records": len(selected),
        "excerpt_characters": 1050 - remaining,
        "max_output_tokens": MORPHEME_REPAIR_MAX_TOKENS,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
