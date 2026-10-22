"""Retain useful local-model morphology without asserting a complete partition."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .morpheme_prompting import morpheme_structure_error


PARTIAL_MORPHOLOGY_POLICY = "explicit-partial-morphology-v2"


def _letters(value: Any) -> str:
    return re.sub(r"[^A-Za-z]", "", str(value or "")).casefold()


def finalize_morpheme_analysis(
    candidate: Any, term: str, previous_parts: list[dict[str, Any]],
    required_shape: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep model claims that have a real non-overlapping occurrence in the word.

    Gaps remain gaps: no missing component, canonical form, role or meaning is
    invented. Previous complete multi-part structure is not replaced by a
    weaker partial proposal. Partial visits merge non-conflicting earlier parts.
    """
    letters = _letters(term)
    value = deepcopy(candidate) if isinstance(candidate, dict) else {}
    if not morpheme_structure_error(value, letters, required_shape):
        return {**value, "analysis_scope": "complete", "refinement_needed": False,
                "unresolved_spans": [], "unresolved_proposals": [],
                "partial_policy": PARTIAL_MORPHOLOGY_POLICY}

    selected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    cursor = 0
    raw_parts = value.get("parts")
    raw_parts = raw_parts if isinstance(raw_parts, list) else []

    def take(item: Any, *, previous: bool = False, index: int = 0) -> None:
        nonlocal cursor
        if not isinstance(item, dict):
            if not previous:
                pending.append({"reason": "non-object proposal", "proposal": deepcopy(item)})
            return
        part = deepcopy(item)
        surface = _letters(part.get("surface"))
        kind = str(part.get("kind") or "").strip().casefold()
        canonical = part.get("canonical_form")
        reason = ""
        if surface == letters:
            reason = "whole-word description is not an additional morpheme"
        elif not surface or kind not in {"root", "free", "prefix", "suffix"}:
            reason = "surface or typed component role is missing"
        elif not isinstance(canonical, str) or not canonical.strip():
            reason = "canonical form still needs refinement"
        elif len(selected) >= 8:
            reason = "additional component proposal retained for a later visit"
        start = letters.find(surface, 0 if previous else cursor) if surface and not reason else -1
        while start >= 0 and any(start < end and start + len(surface) > begin for begin, end in occupied):
            start = letters.find(surface, start + 1) if previous else -1
        if not reason and start < 0:
            reason = "no unoccupied ordered surface occurrence"
        if reason:
            if not previous:
                pending.append({"index": index, "reason": reason, "proposal": deepcopy(item)})
            return
        end = start + len(surface)
        part["surface"] = str(item["surface"]).strip()
        part["kind"] = kind
        part["source_span"] = [start, end]
        part["retained_from_previous_analysis"] = previous
        selected.append(part)
        occupied.append((start, end))
        if not previous:
            cursor = end

    for index, part in enumerate(raw_parts):
        take(part, index=index)

    previous_complete = bool(previous_parts) and not morpheme_structure_error(
        {"parts": previous_parts}, letters, required_shape,
    )
    if previous_complete and (len(previous_parts) > 1 or not selected):
        return {
            **value, "parts": deepcopy(previous_parts), "analysis_scope": "complete",
            "refinement_needed": True, "unresolved_spans": [],
            "unresolved_proposals": deepcopy(raw_parts),
            "retained_previous_structure": True,
            "partial_policy": PARTIAL_MORPHOLOGY_POLICY,
        }

    for part in previous_parts:
        take(part, previous=True)
    selected.sort(key=lambda part: tuple(part["source_span"]))
    gaps: list[dict[str, Any]] = []
    position = 0
    for part in selected:
        start, end = part["source_span"]
        if start > position:
            gaps.append({"surface": letters[position:start], "start": position, "end": start})
        position = end
    if position < len(letters):
        gaps.append({"surface": letters[position:], "start": position, "end": len(letters)})
    complete = not morpheme_structure_error({"parts": selected}, letters, required_shape)
    return {
        **value, "parts": selected, "analysis_scope": "complete" if complete else "partial",
        "refinement_needed": not complete or bool(pending),
        "unresolved_spans": gaps, "unresolved_proposals": pending,
        "partial_policy": PARTIAL_MORPHOLOGY_POLICY,
    }


def word_history_anchor(source: dict[str, Any]) -> dict[str, Any]:
    """A transient reference to an existing word, never a fabricated morpheme."""
    return {
        "morpheme_id": str(source["entity_id"]),
        "surface": str(source["text"]), "canonical_form": str(source["text"]),
        "kind": "free", "language": str(source.get("language") or "en"),
        "meaning": "", "basis": "source-word", "evidence_ids": [],
        "word_anchor": True,
    }
