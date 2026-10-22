"""Compile enrichment membership claims without inventing complete word splits."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
import unicodedata

from .relation_support import reduce_relation_assertions, retire_relation_support, upsert_relation_support

ENRICHMENT_MEMBERSHIP_POLICY = "enrichment-membership-v2"
_REPAIR_KEY = "enrichment-membership-repair-v2"
_OWNER_TYPES = ("atomic-split", "relation-decision", "morphology-enrichment")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _letters(value: Any) -> str:
    return re.sub("[^a-z]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def enrichment_confidence(value: Any, default: float = 0.55) -> float:
    try:
        score = float(value)
        if not math.isfinite(score):
            return default
        return max(0.05, min(score, 0.8))
    except (TypeError, ValueError):
        return default


def classify_term_component_claims(items: list[Any]) -> list[dict[str, Any]]:
    """Keep family knowledge, selecting non-overlapping literal partial spans.

    These are displayable partial observations, not an authoritative partition.
    Ambiguous repeated surfaces and overlapping variants remain family links.
    No language, root, or affix is inferred from spelling by this function.
    """
    result = [
        {"enrichment_membership_policy": ENRICHMENT_MEMBERSHIP_POLICY,
         "analysis_scope": "family", "membership_reason": "no unique word surface"}
        for _ in items
    ]
    candidates: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        term, surface = _letters(item.get("term")), _letters(
            item.get("surface") or item.get("component_form"))
        kind = str(item.get("component_kind") or "").strip().casefold()
        kind = {"stem": "root", "prefixal": "prefix", "suffixal": "suffix"}.get(kind, kind)
        if not term or not surface or kind not in {"root", "prefix", "suffix"}:
            continue
        supplied = item.get("source_span")
        span = None
        if (isinstance(supplied, (tuple, list)) and len(supplied) == 2
                and all(type(value) is int for value in supplied)):
            start, end = supplied
            if 0 <= start < end <= len(term) and term[start:end] == surface:
                span = (start, end)
        if span is None:
            starts = [i for i in range(len(term) - len(surface) + 1)
                      if term.startswith(surface, i)]
            if len(starts) != 1:
                continue
            span = (starts[0], starts[0] + len(surface))
        form = str(item.get("component_form") or "").strip().casefold()
        language = str(item.get("component_language") or "en").strip().casefold()
        group = (term, str(item.get("term_language") or "en").strip().casefold())
        candidates.setdefault(group, []).append((
            -(span[1] - span[0]), -enrichment_confidence(item.get("confidence")),
            form, kind, language, index, span,
        ))
    for group in candidates.values():
        selected: list[tuple[tuple[int, int], tuple[str, str, str]]] = []
        for _, _, form, kind, language, index, span in sorted(group):
            identity = (form, kind, language)
            overlap = [
                (other_span, other_identity) for other_span, other_identity in selected
                if span[0] < other_span[1] and other_span[0] < span[1]
            ]
            if overlap and any(other != (span, identity) for other in overlap):
                result[index]["membership_reason"] = "overlapping family alternative"
                continue
            result[index].update({
                "analysis_scope": "partial", "source_span": list(span),
                "membership_reason": "literal partial observation, not a complete split",
            })
            selected.append((span, identity))
    return result


def _membership_rows(connection: Any, term_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT assertion.assertion_id, assertion.relation, assertion.target_entity_id,
                  term.text AS term, term.language AS term_language,
                  morpheme.form AS canonical_form, morpheme.kind, morpheme.language,
                  support.support_id, support.owner_type, support.owner_id,
                  support.owner_version, support.confidence, support.properties_json,
                  job.job_id
           FROM relation_assertions AS assertion
           JOIN terms AS term ON term.entity_id = assertion.source_entity_id
           JOIN entities AS term_entity ON term_entity.entity_id = term.entity_id
            AND term_entity.status = 'accepted'
           JOIN morphemes AS morpheme ON morpheme.entity_id = assertion.target_entity_id
           JOIN entities AS component ON component.entity_id = morpheme.entity_id
            AND component.status = 'accepted'
           JOIN relation_assertion_supports AS support
             ON support.assertion_id = assertion.assertion_id
            AND support.status = 'accepted' AND support.basis = 'model'
           JOIN preparation_jobs AS job
             ON job.job_id = CASE WHEN json_valid(support.properties_json)
                THEN json_extract(support.properties_json, '$.enrichment_job_id') END
            AND job.job_type = 'enrich-morphology-v3'
           WHERE assertion.subject_entity_id = ? AND assertion.source_entity_id = ?
             AND assertion.status = 'accepted'
             AND assertion.relation IN ('has-component', 'shares-component')
             AND support.owner_type IN ('atomic-split', 'relation-decision', 'morphology-enrichment')
           ORDER BY assertion.assertion_id, support.support_id""",
        (term_id, term_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _repair_word(store: Any, connection: Any, term_id: str) -> dict[str, int]:
    rows = _membership_rows(connection, term_id)
    specifications = []
    for row in rows:
        properties = _object(row["properties_json"])
        specifications.append({
            "term": row["term"], "term_language": row["term_language"],
            "component_form": row["canonical_form"], "component_kind": row["kind"],
            "component_language": row["language"], "confidence": row["confidence"],
            "surface": properties.get("surface") or row["canonical_form"],
            "source_span": properties.get("source_span"),
        })
    classifications = classify_term_component_claims(specifications)
    changed: set[str] = set()
    converted = 0
    for row, classification in zip(rows, classifications):
        old_properties = _object(row["properties_json"])
        properties = dict(old_properties)
        properties.pop("ordinal", None)
        properties.pop("source_span", None)
        properties.update(classification)
        properties["surface"] = str(old_properties.get("surface") or row["canonical_form"])
        score = enrichment_confidence(row["confidence"])
        old_id = str(row["assertion_id"])
        if row["relation"] == "has-component":
            new_id = store.accept_relation_assertion(
                term_id, term_id, str(row["target_entity_id"]), "shares-component",
                basis="model", confidence=score, properties=properties,
                context_evidence_ids=properties.get("context_evidence_ids", []),
                job_id=str(row["job_id"]), owner_type="morphology-enrichment",
                owner_id=str(row["job_id"]), owner_version=ENRICHMENT_MEMBERSHIP_POLICY,
            )
            if not new_id:
                continue
            # Only the attributed model support is retired. Other atomic, book,
            # and reviewed owners remain authoritative through the reducer.
            retire_relation_support(
                connection, assertion_id=old_id,
                owner_type=str(row["owner_type"]), owner_id=str(row["owner_id"]),
            )
            changed.update((old_id, new_id))
            converted += 1
        elif (old_properties != properties
              or abs(float(row["confidence"]) - score) > 1e-12):
            upsert_relation_support(
                connection, assertion_id=old_id,
                owner_type=str(row["owner_type"]), owner_id=str(row["owner_id"]),
                owner_version=ENRICHMENT_MEMBERSHIP_POLICY,
                basis="model", confidence=score, properties=properties,
            )
            changed.add(old_id)
    if changed:
        reduce_relation_assertions(connection, changed)
        store._advance_relation_graph_revision(connection)
        store._mark_projection_repairs_for_entities(connection, {term_id})
    return {"converted_supports": converted, "changed_assertions": len(changed)}


def repair_membership_batch(
    store: Any, connection: Any, *, limit: int = 8,
    term_entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run a bounded, resumable owner-aware upgrade in the caller's transaction."""
    limit = max(1, min(int(limit), 32))
    explicit = term_entity_ids is not None
    now = datetime.now(timezone.utc)
    state_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key=?", (_REPAIR_KEY,),
    ).fetchone()
    state = _object(state_row[0]) if state_row else {}
    if not explicit and state.get("complete") and str(state.get("next_scan_at", "")) > now.isoformat():
        return {"scanned": 0, "converted_supports": 0, "changed_assertions": 0,
                "complete": True, "scanned_total": state.get("scanned_total", 0)}
    if explicit:
        terms = sorted({str(item).strip() for item in term_entity_ids if str(item).strip()})[:limit]
    else:
        cursor = "" if state.get("complete") else str(state.get("cursor", ""))
        terms = [str(row[0]) for row in connection.execute(
            """SELECT DISTINCT assertion.source_entity_id
               FROM relation_assertions AS assertion
               JOIN terms AS term ON term.entity_id = assertion.source_entity_id
               JOIN entities AS entity ON entity.entity_id = term.entity_id
                AND entity.status = 'accepted'
               JOIN relation_assertion_supports AS support
                 ON support.assertion_id = assertion.assertion_id
                AND support.status = 'accepted' AND support.basis = 'model'
               JOIN preparation_jobs AS job
                 ON job.job_id = CASE WHEN json_valid(support.properties_json)
                    THEN json_extract(support.properties_json, '$.enrichment_job_id') END
                AND job.job_type = 'enrich-morphology-v3'
               WHERE assertion.subject_entity_id = assertion.source_entity_id
                 AND assertion.source_entity_id > ? AND assertion.status = 'accepted'
                 AND support.owner_type IN ('atomic-split', 'relation-decision', 'morphology-enrichment')
                 AND (
                     assertion.relation = 'has-component'
                     OR (assertion.relation = 'shares-component' AND
                         COALESCE(json_extract(support.properties_json,
                             '$.enrichment_membership_policy'), '') <> ?)
                 )
               ORDER BY assertion.source_entity_id LIMIT ?""",
            (cursor, ENRICHMENT_MEMBERSHIP_POLICY, limit),
        )]
    result = {"scanned": len(terms), "converted_supports": 0, "changed_assertions": 0}
    for term_id in terms:
        changes = _repair_word(store, connection, term_id)
        for key in changes:
            result[key] += changes[key]
    if not explicit:
        complete = len(terms) < limit
        previous_count = 0 if state.get("complete") else int(state.get("scanned_total", 0))
        state = {
            "policy": ENRICHMENT_MEMBERSHIP_POLICY,
            "cursor": terms[-1] if terms else str(state.get("cursor", "")),
            "complete": complete, "scanned_total": previous_count + len(terms),
            "next_scan_at": (now + timedelta(hours=1)).isoformat() if complete else "",
            "updated_at": now.isoformat(), "last_batch": dict(result),
        }
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_REPAIR_KEY, json.dumps(state, sort_keys=True)),
        )
        result.update({"complete": complete, "scanned_total": state["scanned_total"]})
    return result
