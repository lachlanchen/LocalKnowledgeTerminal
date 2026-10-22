"""Focused, source-scoped context for one local-model morpheme meaning."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .morphology import _is_index_only
from .morphology_enrichment_prompting import _window

from .semantic_contract import MORPHOLOGY_MEANING_PROMPT_POLICY


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _form_pattern(form: str) -> str:
    return r"(?<!\w)" + re.escape(form.strip("-* ")) + r"(?!\w)"


def _definition_forms(excerpt: str) -> list[str]:
    # Printed form/gloss pairs are hints, never automatic graph claims.
    candidates = re.findall(
        r"(?<!\w)([^\W\d_][\w'-]{0,40})\s*\(\s*=\s*",
        _normalized(excerpt),
    )
    forms = []
    for candidate in candidates:
        letters = [char for char in candidate if char.isalpha()]
        if letters and unicodedata.name(letters[-1], "").startswith("LATIN "):
            suffix = []
            for char in reversed(candidate):
                if char in "-'" or unicodedata.name(char, "").startswith("LATIN "):
                    suffix.append(char)
                else:
                    break
            candidate = "".join(reversed(suffix)).strip("-'")
        if candidate and candidate not in forms:
            forms.append(candidate)
    return forms[:6]

def build_morphology_meaning_prompt(
    *, entity_id: str, label: str, kind: str, language: str,
    language_name: str, context: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Prefer definitions; keep related-word context separate from target support."""
    focus = _text(label, 80)
    needle = _normalized(focus).strip("-* ")
    matcher = re.compile(_form_pattern(needle)) if needle else None
    definition_matcher = re.compile(
        _form_pattern(needle) +
        r"\s*(?:[-:]\s*|\(\s*=\s*|=\s*|\bmeans?\b\s+)"
    ) if needle else None
    entities = {
        str(item.get("id") or ""): item
        for item in context.get("entities", [])
        if isinstance(item, dict) and item.get("id") and item.get("label")
    }
    records = []
    omitted_references: list[str] = []
    for index, record in enumerate(context.get("evidence", [])):
        if not isinstance(record, dict):
            continue
        evidence_id = str(record.get("evidence_id") or "").strip()
        excerpt = " ".join(str(record.get("excerpt") or "").split())
        if not evidence_id or not excerpt:
            continue
        normalized = _normalized(excerpt)
        if _is_index_only(excerpt) or re.fullmatch(
            r"(?:see|cf\.?)\s+[\w\s'*-]+[.;]?", normalized
        ):
            omitted_references.append(evidence_id)
            continue
        owner_id = str(record.get("entity_id") or "")
        owner = entities.get(owner_id, {})
        about = _text(owner.get("label") or record.get("headword"), 48)
        scope = (
            "target" if owner_id == entity_id else
            "related-word" if owner.get("type") == "term" else
            "context"
        )
        forms = _definition_forms(excerpt)
        direct = bool(definition_matcher and definition_matcher.search(normalized))
        mentions = bool(matcher and matcher.search(normalized))
        morphology = bool(forms) or any(
            marker in str(record.get("corpus_id", "")).casefold()
            for marker in ("morphology", "root-dictionary", "affix-dictionary")
        )
        rank = 0 if direct else 1 if morphology else 2 if mentions else 3
        records.append((
            rank, index, evidence_id, excerpt, record, about, scope, forms, direct,
        ))
    passages = []
    aliases: dict[str, str] = {}
    scopes: dict[str, str] = {}
    definition_ids: list[str] = []
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for _, _, evidence_id, excerpt, record, about, scope, forms, direct in sorted(records):
        excerpt = _window(excerpt, [needle, focus, *forms], 360)
        if evidence_id in seen_ids or excerpt in seen_text:
            continue
        alias = f"E{len(passages) + 1}"
        aliases[alias] = evidence_id
        scopes[evidence_id] = scope
        passage = {
            "id": alias, "at": _text(record.get("locator"), 70),
            "scope": scope, "text": excerpt,
        }
        if about:
            passage["about"] = about
        if forms:
            passage["printed_forms"] = forms[:4]
        passages.append(passage)
        # Only context actually shown to the model can qualify as a definition.
        if direct and definition_matcher and definition_matcher.search(_normalized(excerpt)):
            definition_ids.append(evidence_id)
        seen_ids.add(evidence_id)
        seen_text.add(excerpt)
        if len(passages) == 3:
            break

    neighbors = []
    seen_neighbors: set[tuple[str, str, str]] = set()
    for edge in context.get("assertions", []):
        if not isinstance(edge, dict):
            continue
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source == entity_id and target != entity_id:
            neighbor, direction = entities.get(target, {}), "out"
        elif target == entity_id and source != entity_id:
            neighbor, direction = entities.get(source, {}), "in"
        else:
            continue
        form = _text(neighbor.get("label"), 72)
        relation = _text(edge.get("relation"), 40)
        identity = (form, relation, direction)
        if not form or not relation or identity in seen_neighbors:
            continue
        seen_neighbors.add(identity)
        neighbors.append({"form": form, "relation": relation, "direction": direction})
        if len(neighbors) == 4:
            break

    system = (
        "Explain one root or affix from local passages and linguistic knowledge. "
        "Existing graph roles may be wrong. Return one JSON object."
    )
    contract = (
        "evidence_ids: cite supplied IDs such as E1 only when they support your "
        "explanation; otherwise []. Never invent IDs."
        if passages else
        "evidence_ids: exactly []. No definition passage matched; give a cautious "
        "explanation without invented history or citations."
    )
    prompt = "\n".join((
        f"TARGET LANGUAGE: {language_name} ({language})",
        f"MORPHEME FORM: {focus}",
        f"MORPHEME KIND: {_text(kind, 20)} (graph hint)",
        "CONNECTED GRAPH HINTS (not proof): " + json.dumps(neighbors, ensure_ascii=False),
        "RETRIEVED PASSAGES (OCR may be imperfect):",
        json.dumps(passages, ensure_ascii=False),
        "Explain the target form, not its whole word. A word's meaning does not "
        "define every substring. Printed forms may contradict the graph hint; "
        "explain that uncertainty rather than defend a bad split.",
        f"Return meaning: one useful explanation in {language_name}, at most 24 words;",
        "confidence: number from 0 to 1; " + contract,
    ))
    trace = {
        "policy": MORPHOLOGY_MEANING_PROMPT_POLICY,
        "selected_evidence_ids": list(aliases.values()),
        "evidence_aliases": aliases,
        "evidence_scopes": scopes,
        "definition_evidence_ids": definition_ids,
        "omitted_reference_ids": list(dict.fromkeys(omitted_references)),
        "selected_records": len(passages),
        "excerpt_characters": sum(len(item["text"]) for item in passages),
        "graph_neighbors": len(neighbors),
        "prompt_characters": len(system) + len(prompt),
        "prompt_sha256": hashlib.sha256((system + "\n" + prompt).encode("utf-8")).hexdigest(),
        "max_output_tokens": 224,
    }
    return system, prompt, trace
