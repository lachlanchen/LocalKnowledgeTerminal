"""Subject-scoped display of supported, explicitly partial morphology.

These helpers never promote a proposal into a complete positional partition or
merge canonical entities. Book/review ownership remains a storage concern.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .morphology_contract import canonical_morpheme_occurrence_identity


# Internal SQL fragment for queries whose relation table is named `assertion`.
# Rendering, repair eligibility, and change propagation must agree on which
# source memberships are live. Generic cross-word derivatives are not parts.
LIVE_SOURCE_COMPONENT_SQL = """(
    assertion.status = 'accepted'
    AND assertion.subject_entity_id = assertion.source_entity_id
    AND (
        assertion.relation = 'has-component'
        OR (assertion.relation = 'shares-component'
            AND CASE WHEN json_valid(assertion.properties_json)
                THEN json_extract(assertion.properties_json, '$.analysis_scope')
                ELSE '' END = 'partial')
    )
    AND EXISTS (
        SELECT 1 FROM relation_assertion_supports AS component_support
        WHERE component_support.assertion_id = assertion.assertion_id
          AND component_support.status = 'accepted'
    )
)"""


def _form(component: dict[str, Any]) -> str:
    return unicodedata.normalize("NFKC", str(
        component.get("canonical_form") or component.get("base")
        or component.get("surface", "")
    )).strip().strip("-").casefold()


def _provided_span(term: str, component: dict[str, Any]) -> tuple[int, int] | None:
    span = component.get("source_span")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    if any(type(value) is not int for value in span):
        return None
    letters = re.sub("[^a-z]", "", term.casefold())
    surface = re.sub("[^a-z]", "", str(
        component.get("surface") or _form(component)
    ).casefold())
    start, end = span
    if 0 <= start < end <= len(letters) and letters[start:end] == surface:
        return start, end
    return None


def component_projection_occurrence(
    term: str, component: dict[str, Any]
) -> tuple[str, ...]:
    """Retain partial spans, including a suffix before an unresolved ending."""
    span = _provided_span(term, component)
    if span is not None:
        return "span", str(span[0]), str(span[1])
    return canonical_morpheme_occurrence_identity(
        term, component.get("language", ""),
        component.get("canonical_form") or component.get("base")
        or component.get("surface", ""),
        component.get("kind", ""), component.get("ordinal", -1),
    )


def _visible_span(term: str, component: dict[str, Any]) -> tuple[int, int] | None:
    supplied = _provided_span(term, component)
    if supplied is not None:
        return supplied
    try:
        occurrence = component_projection_occurrence(term, component)
    except ValueError:
        return None
    if occurrence[0] == "span":
        return int(occurrence[1]), int(occurrence[2])
    return None


def _authority(component: dict[str, Any]) -> tuple[int, float]:
    ranks = {"derived": 0, "model": 1, "book": 2, "reviewed": 3}
    rank = max((ranks.get(str(basis), 0) for basis in
                component.get("support_bases", [])),
               default=ranks.get(str(component.get("basis", "model")), 1))
    if any("review" in str(owner) or "curator" in str(owner)
           for owner in component.get("support_owner_types", [])):
        rank = 3
    return rank, float(component.get("confidence", 0) or 0)


def _add_display_alias(winner: dict[str, Any], other: dict[str, Any]) -> None:
    # Only an unknown language can alias a known language. A different role or
    # two different known languages must never acquire the other's history.
    languages = {str(item.get("language") or "und").casefold()
                 for item in (winner, other)}
    if winner.get("kind") != other.get("kind"):
        return
    if len(languages) > 1 and "und" not in languages:
        return
    winner["projection_alias_ids"] = sorted({
        str(value) for value in [
            *winner.get("projection_alias_ids", []),
            *other.get("projection_alias_ids", []), other.get("morpheme_id", ""),
        ] if str(value)
    })


def merge_partial_component_projection(
    term: str, components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add live partial memberships, preferring authority at the same span.

Only equal forms at the same actual occurrence shadow one another. Disjoint
partial components remain visible; no new ordinals or global aliases are saved.
"""
    selected = [dict(item) for item in components
                if item.get("projection_relation") != "shares-component"]
    partials = [dict(item) for item in components
                if item.get("projection_relation") == "shares-component"]
    partials.sort(key=lambda item: (_authority(item), str(item.get("morpheme_id", ""))),
                  reverse=True)
    for component in partials:
        span = _visible_span(term, component)
        conflicts = [item for item in selected
                     if span is not None and _visible_span(term, item) == span
                     and _form(item) == _form(component)]
        if not conflicts:
            selected.append(component)
            continue
        winner = max(conflicts, key=_authority)
        if _authority(winner) >= _authority(component):
            _add_display_alias(winner, component)
            continue
        for other in conflicts:
            _add_display_alias(component, other)
            selected.remove(other)
        selected.append(component)
    selected.sort(key=lambda item: (
        _visible_span(term, item) or (len(term) + 1, len(term) + 1),
        int(item.get("ordinal", 0) or 0), str(item.get("morpheme_id", "")),
    ))
    return selected
