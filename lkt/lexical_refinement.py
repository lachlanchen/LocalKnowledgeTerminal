"""Bounded lexical reanalysis and subject-owned origin path revisions.

The local model supplies linguistic judgments. These helpers preserve surface
coverage, revision ownership, and scheduling fairness without inventing words,
ancestry links, or citations.
"""

from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from typing import Any


STRUCTURE_POLICY = "surface-canonical-v2"
ORIGIN_PATH_POLICY = "independent-origin-paths-v1"


def structure_review_due(artifacts: list[dict[str, Any]], epoch_number: int) -> bool:
    """Upgrade legacy model splits once, then review every fourth eligible visit.

    The caller still owns the durable lease, rotation, and exponential cooldown.
    This does not create an extra lane or bypass a subject's scheduling limits.
    """
    splits = [item for item in artifacts if item.get("stage") == "accepted-morpheme-split"]
    if not splits:
        return True
    payload = splits[-1].get("payload") or {}
    if payload.get("analysis_scope") == "partial" or payload.get("refinement_needed"):
        # Incomplete work returns on alternate eligible visits, still subject
        # to the caller's durable rotation, cooldown, and lease.
        return epoch_number > 0 and epoch_number % 2 == 0
    parts = payload.get("parts") or []
    if not parts:
        return True
    model_owned = any(part.get("basis") != "book" for part in parts)
    if not model_owned:
        return False
    return payload.get("analysis_policy") != STRUCTURE_POLICY or (
        epoch_number > 0 and epoch_number % 4 == 0
    )


def revise_origin_paths(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    focused_component_id: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Keep complete paths separate instead of joining unrelated list ends."""
    previous_by_id = {
        str(branch.get("component_id", "")): branch
        for branch in previous if isinstance(branch, dict)
    }
    result = deepcopy(current)
    revised: set[str] = set()
    for branch in result:
        component_id = str(branch.get("component_id", ""))
        old = previous_by_id.get(component_id, {})
        if component_id == focused_component_id and branch.get("steps"):
            revised.add(component_id)
        elif old.get("steps"):
            # An unvisited component, or an empty uncertain draft, must not
            # erase useful accepted history from a previous visit.
            branch["steps"] = deepcopy(old["steps"])
        if branch.get("steps"):
            branch["exploration_status"] = "history-found"
        elif component_id != focused_component_id and old.get("exploration_status"):
            branch["exploration_status"] = old["exploration_status"]
    return result, revised


def retire_replaced_model_origin_paths(
    store: Any,
    subject_entity_id: str,
    *,
    active_assertion_ids: set[str],
    current_component_ids: set[str],
    revised_component_ids: set[str],
) -> list[str]:
    """Retire only obsolete model transitions owned by this origin producer.

    Book/reviewed claims, other subjects, unvisited component paths, canonical
    entities, and prior artifacts are not removed. The store's retirement API
    maintains the assertion audit and projection invalidation contract.
    """
    def reconcile() -> list[str]:
        with closing(store._connect()) as connection:
            rows = connection.execute(
                """SELECT assertion_id,properties_json FROM relation_assertions
                   WHERE subject_entity_id=? AND status='accepted'
                     AND relation='developed-into' AND basis='model'""",
                (subject_entity_id,),
            ).fetchall()
        retired: list[str] = []
        for row in rows:
            assertion_id = str(row["assertion_id"])
            if assertion_id in active_assertion_ids:
                continue
            try:
                properties = json.loads(row["properties_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(properties, dict):
                continue
            if properties.get("origin_provenance_version") != 2:
                continue
            if str(properties.get("source_term_id", "")) != subject_entity_id:
                continue
            component_id = str(properties.get("component_id", ""))
            if not component_id:
                continue
            if component_id in current_component_ids and component_id not in revised_component_ids:
                continue
            if store.retire_relation_assertion(subject_entity_id, assertion_id):
                retired.append(assertion_id)
        return retired

    return store.atomic_semantic_write(subject_entity_id, reconcile)
