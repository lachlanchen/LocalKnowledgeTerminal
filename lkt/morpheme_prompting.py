"""Small, evidence-balanced prompts for cumulative lexical decomposition."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Iterator


MORPHEME_PROMPT_POLICY = "focused-morphology-v2"
MORPHEME_NORMALIZATION_POLICY = "word-container-normalization-v2"
DERIVATIVE_BATCH_HINT = 3


def _letters(value: Any) -> str:
    return re.sub(r"[^A-Za-z]", "", str(value)).casefold()


def component_candidates(word: str) -> Iterator[tuple[str, str, int, int]]:
    """Suggest lookup spans, not accepted morphemes or mechanically fixed cuts."""
    letters = _letters(word)
    size = len(letters)
    for width in range(1, min(8, size - 1) + 1):
        yield "prefix", letters[:width], 0, width
        yield "suffix", letters[-width:], size - width, size
    for start in range(size):
        for end in range(start + 3, min(size, start + 8) + 1):
            yield "root", letters[start:end], start, end


def _excerpt(record: dict[str, Any], term: str, limit: int = 480) -> str:
    text = str(record.get("excerpt") or record.get("definition") or "")
    text = re.sub(r"\\(?:mathrm|mathbf|text|textit)\s*\{([^{}]*)\}", r"\1", text)
    text = text.replace("\\(", "").replace("\\)", "")
    text = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, re.I)
    start = max(0, match.start() - 100) if match else 0
    if len(text) - start < limit:
        start = max(0, len(text) - limit)
    return text[start:start + limit]


def _compact_part(part: dict[str, Any]) -> dict[str, Any]:
    meaning = part.get("meaning", "")
    if isinstance(meaning, (list, tuple)):
        meaning = " or ".join(str(item) for item in meaning)
    return {
        **{key: part[key] for key in (
            "surface", "canonical_form", "kind", "language", "basis",
        ) if key in part},
        "meaning": " ".join(str(meaning).split()[:10]),
    }


def _role_signal(item: dict[str, Any], kind: str) -> int:
    headword = item["headword"].strip()
    text = item["excerpt"].casefold()
    marker = {"prefix": "\u524d\u7f00", "suffix": "\u540e\u7f00", "root": "\u8bcd\u6839"}[kind]
    notation = (kind == "prefix" and headword.endswith("-")) or (
        kind == "suffix" and headword.startswith("-")
    )
    return int(notation) * 4 + int(kind in text or marker in text) * 2 + int("=" in text)


def pack_morpheme_context(
    term: str,
    records: list[dict[str, Any]],
    required_shape: list[dict[str, Any]],
    previous_parts: list[dict[str, Any]],
    existing_graph: dict[str, Any],
    visit_key: str,
) -> dict[str, Any]:
    """Retain target evidence and all three component roles within one small input."""
    word = _letters(term)
    required_ids = {
        str(identifier) for part in required_shape
        for identifier in part.get("evidence_ids", []) if identifier
    }
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = (str(record.get("corpus_id", "")), str(record.get("entry_id", "")))
        if not any(key):
            key = ("evidence", str(record.get("knowledge_evidence_id", "")))
        item = unique.setdefault(key, {
            "evidence_id": str(record.get("knowledge_evidence_id", "")),
            "source": str(record.get("source_title") or record.get("corpus_id", ""))[:100],
            "headword": str(record.get("headword", ""))[:90],
            "kind": str(record.get("kind", "")),
            "excerpt": _excerpt(record, term),
            "candidate_matches": [],
        })
        excerpt = _excerpt(record, term)
        if len(excerpt) > len(item["excerpt"]):
            item["excerpt"] = excerpt
        role = str(record.get("component_hint", "")).casefold()
        surface = _letters(record.get("component_surface", ""))
        if role in {"prefix", "root", "suffix"} and surface:
            match = {"kind": role, "surface": surface}
            for field in ("component_start", "component_end"):
                if isinstance(record.get(field), int):
                    match[field.removeprefix("component_")] = record[field]
            if match not in item["candidate_matches"]:
                item["candidate_matches"].append(match)

    items = list(unique.values())

    def priority(item: dict[str, Any]) -> tuple[int, int, str]:
        exact = _letters(item["headword"]) == word
        return (
            0 if item["evidence_id"] in required_ids else 1 if exact else 2,
            0 if item["kind"].startswith("morphology-") else 1,
            item["headword"],
        )

    selected: list[dict[str, Any]] = []

    def take(item: dict[str, Any]) -> None:
        if item not in selected and len(selected) < 8:
            selected.append(item)

    for item in sorted(items, key=priority):
        if item["evidence_id"] in required_ids:
            take(item)
    exact = [item for item in items if _letters(item["headword"]) == word]
    if exact:
        take(min(exact, key=priority))
    for role in ("prefix", "suffix", "root"):
        candidates = [item for item in items if any(
            match["kind"] == role and 0 < len(match["surface"]) < len(word)
            for match in item["candidate_matches"]
        )]
        if candidates:
            take(min(candidates, key=lambda item: (
                -_role_signal(item, role),
                len(_letters(item["headword"])) * (-1 if role == "root" else 1),
                priority(item),
            )))
    for item in sorted(items, key=priority):
        take(item)

    remaining = 2400
    evidence: list[dict[str, Any]] = []
    for item in selected:
        excerpt = item["excerpt"][:remaining]
        remaining -= len(excerpt)
        evidence.append({
            **item,
            "evidence_id": "" if required_shape else item["evidence_id"],
            "excerpt": excerpt,
            "candidate_matches": item["candidate_matches"][:6],
        })
    nodes = existing_graph.get("nodes", []) if isinstance(existing_graph, dict) else []
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    neighbors: set[str] = set()
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        payload = node.get("payload") if isinstance(node.get("payload"), dict) else {}
        kind = str(node.get("entity_type") or node.get("type") or node.get("kind") or "")
        if kind not in {"term", "word"}:
            continue
        label = str(payload.get("text") or node.get("label") or "").strip()
        if _letters(label) != word and re.fullmatch(r"[A-Za-z][A-Za-z'-]{1,48}", label):
            neighbors.add(label)
    ordered = sorted(neighbors)
    if ordered:
        offset = int(hashlib.sha256(visit_key.encode()).hexdigest()[:8], 16) % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
    return {
        "evidence": evidence,
        "required_parts": [{key: part[key] for key in ("surface", "kind") if part.get(key)} for part in required_shape],
        "previous_parts": [_compact_part(part) for part in previous_parts[:8] if isinstance(part, dict)],
        "known_neighbors": ordered[:12],
        "diagnostics": {
            "retrieved_records": len(records),
            "unique_records": len(items),
            "selected_records": len(evidence),
            "excerpt_characters": 2400 - remaining,
            "selected_evidence_ids": [item["evidence_id"] for item in selected if item["evidence_id"]],
            "derivative_batch_hint": DERIVATIVE_BATCH_HINT,
        },
    }


def build_morpheme_prompt(
    term: str, context: dict[str, Any], *, review_reason: str = "", draft: Any = None,
) -> str:
    reviewing = bool(review_reason)
    heading = "LINGUISTIC REVIEW OF A LEXICAL STRUCTURE DRAFT" if reviewing else "MORPHEME SPLIT / LEXICAL STRUCTURE ANALYSIS"
    required = context["required_parts"]
    shape = (
        "REQUIRED ORDERED SURFACES AND KINDS: " + json.dumps(required, ensure_ascii=False)
        + "\nCopy these book-fixed surfaces in order and every supplied kind."
        if required else
        "No full book formula is fixed. Use linguistic knowledge as well as the retrieved material."
    )
    provenance = (
        "Return empty evidence_ids; the system attaches fixed book provenance."
        if required else
        "Use only supplied evidence IDs for supporting context, and no IDs for your own hypotheses."
    )
    review = ""
    if reviewing:
        parts = draft.get("parts", []) if isinstance(draft, dict) else []
        compact = [_compact_part(part) if isinstance(part, dict) else part for part in parts[:8]] if isinstance(parts, list) else parts
        review = "\nREVIEW REASON: " + review_reason + "\nFIRST DRAFT: " + json.dumps({"parts": compact}, ensure_ascii=False)
    derivatives = (
        "For this review return derivatives: []; focus on the parts. Earlier derivative proposals are retained separately."
        if reviewing else
        f"Optionally suggest a small batch of up to {DERIVATIVE_BATCH_HINT} new derivatives, each with term, a short note, and component_forms copied from the analyzed canonical forms. "
        "An empty batch is fine. This is a per-visit workload, not a limit on a word's eventual neighbors. "
        "Prefer new words over KNOWN NEIGHBORS. Similar meaning alone is not derivation: do not submit synonyms as derivatives."
    )
    return f"""{heading}
TERM: {term}
CURRENT EVIDENCE: {json.dumps(context['evidence'], ensure_ascii=False)}
PREVIOUS STRUCTURE: {json.dumps(context['previous_parts'], ensure_ascii=False)}
KNOWN NEIGHBORS: {json.dumps(context['known_neighbors'], ensure_ascii=False)}
{shape}{review}

Identify the lexical base first, then any productive prefixes and suffixes.
Candidate matches are reading hints, not proof that a string must be a component.
A missing printed formula does not make a word indivisible. Use your linguistic
knowledge for a useful present-day analysis; history is handled by another task.
Use a whole-word free part when the word is genuinely indivisible or lexicalized,
not merely because the dictionaries supply a definition rather than a formula.
Previous model analyses are revisable, not authoritative templates.
parts is one flat partition of TERM, not a graph or alternative analyses.
Do not include the whole word alongside its components or add a parent part.
A whole-word part belongs only in an analysis where it is the sole part.

Return one JSON object with parts and derivatives. Each of one to eight parts has:
surface: exact consecutive letters of TERM, keeping spelling changes here
canonical_form: lexical form, which may differ from the spelling in surface
kind: prefix, root, suffix, or free
language: ISO-style code, or und if unknown
meaning: one short plain English phrase, at most ten words
confidence: number from 0 to 1
evidence_ids: array of supplied IDs or []
{provenance}

Cover every letter exactly once in order, ignoring punctuation separators, and
include a root or free base. Prefix display forms end in -, suffixes start in -.
Do not add translations, pronunciation, history, or explanations outside JSON.
{derivatives}"""


def morpheme_structure_error(
    candidate: Any, letters: str, required_shape: list[dict[str, Any]],
) -> str:
    if not isinstance(candidate, dict):
        return "the draft is not a JSON object"
    parts = candidate.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 8:
        return "parts must contain one to eight objects"
    if any(not isinstance(part, dict) for part in parts):
        return "a part is not an object"
    surfaces = [_letters(part.get("surface", "")) for part in parts]
    kinds = [str(part.get("kind", "")).strip().casefold() for part in parts]
    if not all(surfaces) or "".join(surfaces) != letters:
        return "the surfaces do not cover every word letter exactly once"
    if any(kind not in {"prefix", "root", "suffix", "free"} for kind in kinds):
        return "a component kind is invalid"
    if not set(kinds).intersection({"root", "free"}):
        return "the analysis has no root or free lexical base"
    if any(
        not isinstance(part.get("canonical_form"), str)
        or not part["canonical_form"].strip() for part in parts
    ):
        return "a canonical form is missing"
    if required_shape:
        if surfaces != [_letters(part.get("surface", "")) for part in required_shape]:
            return "the draft changed a book-fixed surface"
        if any(part.get("kind") and part["kind"] != kind for part, kind in zip(required_shape, kinds)):
            return "the draft changed a book-fixed component kind"
    return ""


def normalize_morpheme_draft(
    candidate: Any, letters: str, required_shape: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Recover a flat split without inventing, reordering, or retyping parts.

    Some model responses include a whole-word parent alongside a complete
    decomposition. Only remove that parent when all the remaining parts form
    an independently valid split, including any explicit book boundaries.
    The caller retains the raw model artifact; this function never mutates it.
    """
    if not isinstance(candidate, dict):
        return candidate, []
    word_letters = _letters(letters)
    parts = candidate.get("parts")
    if (
        not word_letters or not isinstance(parts, list) or len(parts) < 3
        or any(not isinstance(part, dict) for part in parts)
    ):
        return candidate, []
    removed = [
        index for index, part in enumerate(parts)
        if _letters(part.get("surface", "")) == word_letters
    ]
    if not removed:
        return candidate, []
    removed_set = set(removed)
    retained = [index for index in range(len(parts)) if index not in removed_set]
    if len(retained) < 2:
        return candidate, []
    proposal = deepcopy(candidate)
    proposal["parts"] = [proposal["parts"][index] for index in retained]
    if morpheme_structure_error(proposal, word_letters, required_shape or []):
        return candidate, []
    return proposal, [{
        "code": "removed-redundant-word-container",
        "removed_part_indices": removed,
        "retained_part_indices": retained,
        "reason": "The remaining model-proposed parts cover the word exactly once.",
    }]


def morpheme_review_reason(
    candidate: Any, letters: str, required_shape: list[dict[str, Any]],
    records: list[dict[str, Any]], previous_parts: list[dict[str, Any]],
) -> str:
    error = morpheme_structure_error(candidate, letters, required_shape)
    if error:
        return error
    parts = candidate["parts"]
    if len(parts) != 1 or str(parts[0].get("kind", "")).strip().casefold() != "free":
        return ""
    shorter_hints = any(
        str(record.get("component_hint", "")) in {"prefix", "root", "suffix"}
        and 0 < len(_letters(record.get("component_surface", ""))) < len(letters)
        for record in records
    )
    if shorter_hints or len(previous_parts) > 1:
        return (
            "only the whole word was returned despite shorter candidate components; "
            "reconsider the lexical base and affixes without requiring a printed formula"
        )
    return ""
