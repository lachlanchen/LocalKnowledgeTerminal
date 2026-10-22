"""Small, book-first tasks for cumulative morphology graph enrichment."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


MORPHOLOGY_ENRICHMENT_PROMPT_POLICY = "focused-graph-enrichment-v2"
MORPHOLOGY_ENRICHMENT_MAX_TOKENS = 768
_EXCERPT_BUDGET = 2200
_HINT_BUDGET = 600

_TERM_SHAPE = {
    "term_components": [{
        "term": "", "term_language": "en", "component_form": "", "surface": "",
        "component_kind": "root|prefix|suffix", "component_language": "en",
        "meaning": "", "confidence": 0.0,
    }],
    "warnings": [],
}
_HISTORY_SHAPE = {
    "history": [{
        "current_form": "", "current_kind": "root|prefix|suffix",
        "component_language": "en", "historical_form": "",
        "historical_kind": "root|prefix|suffix", "language": "",
        "period": "", "meaning": "", "confidence": 0.0,
    }],
    "warnings": [],
}
_RELATION_SHAPE = {
    "relations": [{
        "source_form": "", "source_kind": "root|prefix|suffix",
        "language": "en", "meaning": "", "target_form": "",
        "target_kind": "root|prefix|suffix", "target_language": "",
        "target_meaning": "",
        "relation": "allomorph-of|opposed-to|derived-from|historical-form-of",
        "confidence": 0.0,
    }],
    "warnings": [],
}


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _window(value: Any, needles: list[str], limit: int) -> str:
    text = " ".join(str(value or "").split())
    start = 0
    for needle in needles:
        if not needle:
            continue
        match = re.search(
            r"(?<![A-Za-z])" + re.escape(needle) + r"(?![A-Za-z])",
            text, re.IGNORECASE,
        )
        if match:
            start = max(0, match.start() - 120)
            break
    if len(text) - start < limit:
        start = max(0, len(text) - limit)
    return text[start:start + limit]


def build_morphology_enrichment_prompt(
    payload: dict[str, Any], evidence: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    """Treat parser observations as search hints, never as accepted facts.

    The persisted input/output contracts stay unchanged. A small per-visit
    request does not cap the graph's eventual neighbors or discard extra claims.
    """
    focus = payload.get("focus")
    focus = focus if isinstance(focus, dict) else {}
    source = payload.get("source")
    source = source if isinstance(source, dict) else {}
    term = _text(focus.get("term") or source.get("headword"), 80)
    component = _text(focus.get("component_form"), 80)
    observations = payload.get("observations")
    observations = [
        item for item in observations if isinstance(item, dict)
    ] if isinstance(observations, list) else []
    subtypes = {str(item.get("subtype") or "").casefold() for item in observations}
    if subtypes.intersection({"allomorph", "opposition"}):
        task, shape = "relate-components", _RELATION_SHAPE
        instruction = (
            "Explain one spelling-variant or contrast relationship between real "
            "morphemes. Co-occurrence in a word is not ancestry."
        )
    elif subtypes.intersection({"history", "source"}):
        task, shape = "trace-history", _HISTORY_SHAPE
        instruction = (
            "Trace one historical step for an actual root or affix. A dictionary "
            "definition saying 'originate from' is not itself an etymology. "
            "History needs actual lexical forms, not language names or periods."
        )
    else:
        task, shape = "connect-components", _TERM_SHAPE
        instruction = (
            "Identify the lexical base first, then its productive affixes. "
            "Connect the focus word or a useful related word to those components. "
            "Put each component's concise English meaning in the same row."
        )

    blocks: list[dict[str, str]] = []
    selected_ids: list[str] = []
    seen: set[str] = set()
    remaining = _EXCERPT_BUDGET
    records = [item for item in evidence if isinstance(item, dict)]
    fallback = not any(str(item.get("excerpt") or "").strip() for item in records)
    if fallback and source.get("raw_excerpt"):
        records = [{"excerpt": source["raw_excerpt"], "locator": source.get("locator", "")}]
    for record in records:
        if len(blocks) >= 4 or remaining <= 0:
            break
        excerpt = _window(record.get("excerpt"), [term, component], min(850, remaining))
        if not excerpt or excerpt in seen:
            continue
        seen.add(excerpt)
        remaining -= len(excerpt)
        blocks.append({
            "locator": _text(record.get("locator"), 100),
            "excerpt": excerpt,
        })
        evidence_id = str(record.get("evidence_id") or "")
        if evidence_id and evidence_id not in selected_ids:
            selected_ids.append(evidence_id)

    hints: list[str] = []
    hint_remaining = _HINT_BUDGET
    for observation in observations:
        # Deliberately omit parser kinds, confidence, and source-language labels.
        # They describe an imperfect parse, not linguistic authority.
        hint = " | ".join(filter(None, (
            _text(observation.get("term"), 60),
            _text(observation.get("raw_form") or observation.get("normalized_form"), 60),
            _text(observation.get("gloss"), 100),
        )))[:min(180, hint_remaining)]
        if hint and hint not in hints:
            hints.append(hint)
            hint_remaining -= len(hint)
        if len(hints) >= 6 or hint_remaining <= 0:
            break

    system = (
        "You are a local linguistic assistant building a multilingual word graph. "
        "Use retrieved book text plus your language knowledge. Return one JSON object. "
        "OCR and parser hints can be wrong; reinterpret them rather than copying them. "
        "A root carries the lexical core; a prefix or suffix modifies a base. "
        "Position at the start/end of a word does not by itself determine kind. "
        "A word uses a particular spelling of a component, not every variant in "
        "its root family. In term_components, give the actual surface in that word; "
        "keep other variants in family/history relations rather than a word split. "
        "The dictionary's title does not determine component kind either. "
        "Use language codes only in language properties, never language-label nodes. "
        "Keep useful partial findings; uncertainty belongs in confidence or warnings. "
        "Do not invent citations; the application attaches retrieved provenance."
    )
    sections = [
        "ONE SMALL GRAPH TASK: " + task,
        "FOCUS: " + json.dumps({"term": term, "component": component}, ensure_ascii=False),
        "RETRIEVED BOOK TEXT (primary input; OCR may be imperfect):",
        json.dumps(blocks, ensure_ascii=False),
        "TENTATIVE PARSER READING HINTS (not facts): " + json.dumps(hints, ensure_ascii=False),
        instruction,
        (
            "Aim for one to three useful connected claims on this visit. More can "
            "be explored on later visits. Do not fill unrelated arrays or repeat "
            "the whole word as its own component just to complete a template. "
            "Use a short English explanation, not the form itself, as meaning. "
            "Choose root, prefix, or suffix using your linguistic knowledge; "
            "do not copy an ambiguous parser label. Output this small shape:"
        ),
        json.dumps(shape, ensure_ascii=False),
    ]
    if task != "connect-components":
        sections.extend((
            "If that relationship is not supported, a useful word/component "
            "connection is preferable to invented history. You may instead return:",
            json.dumps(_TERM_SHAPE, ensure_ascii=False),
        ))
    sections.append(
        "Empty unrelated fields are unnecessary. Return whatever useful subset "
        "you can infer; do not reject a whole result because one detail is uncertain."
    )
    prompt = "\n\n".join(sections)
    trace = {
        "policy": MORPHOLOGY_ENRICHMENT_PROMPT_POLICY,
        "task": task,
        "selected_evidence_ids": selected_ids,
        "selected_records": len(blocks),
        "excerpt_characters": _EXCERPT_BUDGET - remaining,
        "parser_hint_characters": _HINT_BUDGET - hint_remaining,
        "source_excerpt_fallback": fallback and bool(blocks),
        "max_output_tokens": MORPHOLOGY_ENRICHMENT_MAX_TOKENS,
        "prompt_sha256": hashlib.sha256((system + "\n" + prompt).encode("utf-8")).hexdigest(),
    }
    return system, prompt, trace
