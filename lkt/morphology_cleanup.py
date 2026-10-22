from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from .deck import AutonomousMorphologySeeder


_CARD_MODES = frozenset(("root", "affix"))
_COMPONENT_KINDS = {
    "root": frozenset(("root",)),
    "affix": frozenset(("prefix", "suffix")),
}
_DECLARATION_STOP = re.compile(
    r"(?:\b(?:comes?|deriv(?:e|es|ed|ation)|from|means?)\b|来自|源自|意为)",
    re.IGNORECASE,
)
_DECLARED_FORM = re.compile(r"(?<![A-Za-z])[-]?[A-Za-z][A-Za-z'\-]{1,24}-?")
_DECLARATION_WORDS = frozenset(
    ("affix", "prefix", "root", "suffix", "word", "words")
)


@dataclass(frozen=True)
class MorphologyPseudoCardCandidate:
    card_id: str
    mode: str
    query: str
    subject_entity_id: str
    focus_morpheme_id: str
    source_mode: str
    source_record_id: str
    source_headword: str
    source_page: int
    source_excerpt: str
    checkpoint_artifact_id: str
    projection_nodes: int
    projection_edges: int
    history_nodes: int
    proposal_branches: int
    replacement_record_id: str = ""
    replacement_term: str = ""
    replacement_page: int = 0


@dataclass(frozen=True)
class MorphologyPseudoCardCleanupReport:
    dry_run: bool
    scanned_cards: int
    checkpoint_linked_cards: int
    candidates: tuple[MorphologyPseudoCardCandidate, ...]
    cards_archived: int = 0
    checkpoints_superseded: int = 0
    checkpoints_retained_as_mixed: int = 0
    replacements_queued: int = 0
    replacement_jobs_queued: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_count"] = len(self.candidates)
        value["cards_to_archive"] = len(self.candidates)
        value["replacements_to_queue"] = sum(
            bool(candidate.replacement_term) for candidate in self.candidates
        )
        return value


def _normalize(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _family_signature(value: Any) -> str:
    letters = "".join(character for character in _normalize(value) if character.isalpha())
    if len(letters) >= 3 and letters[-1:] in {"c", "k", "q"}:
        return letters[:-1] + "k"
    return letters


def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _source_reference(
    source_fingerprint: str, morphology: Mapping[str, dict[str, Any]]
) -> tuple[str, str] | None:
    for mode in sorted(morphology):
        marker = f":{mode}:"
        if marker not in source_fingerprint:
            continue
        fingerprint, remainder = source_fingerprint.rsplit(marker, 1)
        record_id = remainder.split(":", 1)[0].strip()
        expected = str(morphology[mode].get("source_sha256", "")).strip()
        if expected and fingerprint == expected and record_id:
            return mode, record_id
    return None


def _declared_forms(mode: str, headword: str, excerpt: str) -> tuple[str, ...]:
    declaration = AutonomousMorphologySeeder._DECLARATION_PATTERNS[mode].search(
        excerpt
    )
    if declaration is None:
        return (headword.strip(),) if headword.strip() else ()
    header = excerpt[declaration.start() : declaration.start() + 240]
    stop = _DECLARATION_STOP.search(header)
    if stop is not None:
        header = header[: stop.start()]
    forms = [headword.strip()]
    forms.extend(match.group(0).strip("-") for match in _DECLARED_FORM.finditer(header))
    return tuple(
        dict.fromkeys(
            form
            for form in forms
            if form
            and _normalize(form) not in _DECLARATION_WORDS
            and 2 <= len(_family_signature(form)) <= 24
        )
    )


def _load_morphology_sources(
    morphology_databases: Mapping[str, Path],
) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for raw_mode, raw_path in morphology_databases.items():
        mode = str(raw_mode).strip().casefold()
        if mode not in _CARD_MODES:
            continue
        path = Path(raw_path).resolve()
        with sqlite3.connect(path, timeout=30) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            rows = [
                {
                    "record_id": str(row[0]),
                    "headword": str(row[1]),
                    "source_page": int(row[2]),
                    "content": str(row[3]),
                }
                for row in connection.execute(
                    "SELECT record_id, headword, source_page, content FROM records"
                )
            ]
        loaded[mode] = {
            "database": path,
            "source_sha256": str(metadata.get("source_sha256", "")),
            "records": {row["record_id"]: row for row in rows},
            "eligible": tuple(
                row
                for row in rows
                if AutonomousMorphologySeeder._eligible_evidence(
                    mode,
                    SimpleNamespace(
                        headword=row["headword"], excerpt=row["content"]
                    ),
                )
            ),
        }
    return loaded


def _nearest_declaration(
    source_mode: str,
    source: dict[str, Any],
    morphology: Mapping[str, dict[str, Any]],
) -> tuple[str, str, int]:
    source_form = _normalize(source["headword"])
    source_signature = _family_signature(source_form)
    if len(source_signature) < 3:
        return "", "", 0
    matches: list[tuple[int, int, str, str, int]] = []
    for row in morphology[source_mode]["eligible"]:
        page_distance = abs(int(row["source_page"]) - int(source["source_page"]))
        if page_distance > 8:
            continue
        for form in _declared_forms(source_mode, row["headword"], row["content"]):
            normalized = _normalize(form)
            if _family_signature(normalized) != source_signature:
                continue
            distance = _edit_distance(source_form, normalized)
            matches.append(
                (
                    distance,
                    page_distance,
                    str(row["record_id"]),
                    normalized,
                    int(row["source_page"]),
                )
            )
    if not matches:
        return "", "", 0
    _distance, _page_distance, record_id, term, page = min(matches)
    return record_id, term, page


def _card_projection(payload: dict[str, Any]) -> tuple[int, int, str]:
    extensions = payload.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    lexical_view = extensions.get("lexical_view")
    lexical_view = lexical_view if isinstance(lexical_view, dict) else {}
    graph = extensions.get("morphology_graph")
    graph = graph if isinstance(graph, dict) else {}
    projection = lexical_view if isinstance(lexical_view.get("nodes"), list) else graph
    nodes = projection.get("nodes")
    edges = projection.get("edges")
    nodes = nodes if isinstance(nodes, list) else []
    edges = edges if isinstance(edges, list) else []
    focus_ids = lexical_view.get("focus_entity_ids")
    focus_ids = focus_ids if isinstance(focus_ids, list) else []
    focus_id = next((str(value).strip() for value in focus_ids if str(value).strip()), "")
    if not focus_id:
        focus_id = str(graph.get("center_id", "")).strip()
    return len(nodes), len(edges), focus_id


def _checkpoint_rows(knowledge_database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(knowledge_database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT artifact.artifact_id, artifact.payload,
                      artifact.validation_state, job.subject_entity_id,
                      job.source_fingerprint, artifact.created_at
               FROM job_artifacts AS artifact
               JOIN preparation_jobs AS job ON job.job_id = artifact.job_id
               WHERE artifact.stage = 'accepted-origin-card'
                 AND artifact.validation_state = 'accepted'
                 AND job.prompt_version LIKE 'autonomous-morphology-%'
               ORDER BY artifact.created_at"""
        ).fetchall()
    checkpoints: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except (TypeError, json.JSONDecodeError):
            continue
        derived = payload.get("derived_card_ids")
        derived = derived if isinstance(derived, dict) else {}
        output_ids = {
            str(value).strip()
            for value in (payload.get("card_id"), *derived.values())
            if str(value).strip()
        }
        for card_id in derived.values():
            card_id = str(card_id).strip()
            if card_id:
                checkpoints.append(
                    {
                        "artifact_id": str(row["artifact_id"]),
                        "card_id": card_id,
                        "output_ids": output_ids,
                        "subject_entity_id": str(row["subject_entity_id"]),
                        "source_fingerprint": str(row["source_fingerprint"]),
                        "created_at": str(row["created_at"]),
                    }
                )
    return checkpoints


def _component_integrity(
    connection: sqlite3.Connection,
    *,
    mode: str,
    subject_entity_id: str,
    focus_morpheme_id: str,
    source_fingerprint: str,
) -> tuple[bool, int, int]:
    memberships = connection.execute(
        """SELECT member.term_id, term.normalized AS term_normalized,
                  morpheme.normalized AS morpheme_normalized,
                  morpheme.kind AS morpheme_kind
           FROM term_morphemes AS member
           JOIN terms AS term ON term.entity_id = member.term_id
           JOIN morphemes AS morpheme ON morpheme.entity_id = member.morpheme_id
           WHERE member.morpheme_id = ?""",
        (focus_morpheme_id,),
    ).fetchall()
    same_form_only = (
        len(memberships) == 1
        and str(memberships[0]["term_id"]) == subject_entity_id
        and str(memberships[0]["term_normalized"])
        == str(memberships[0]["morpheme_normalized"])
        and str(memberships[0]["morpheme_kind"]) in _COMPONENT_KINDS[mode]
    )

    history_nodes = int(
        connection.execute(
            """SELECT COUNT(DISTINCT entity.entity_id)
               FROM relation_assertions AS assertion
               JOIN entities AS entity
                 ON entity.entity_id IN (
                    assertion.source_entity_id, assertion.target_entity_id
                 )
               WHERE assertion.status = 'accepted'
                 AND entity.entity_type IN ('historical-form', 'history-event')
                 AND (
                    assertion.source_entity_id = ?
                    OR assertion.target_entity_id = ?
                    OR json_extract(assertion.properties_json, '$.component_id') = ?
                    OR json_extract(assertion.properties_json, '$.component_entity_id') = ?
                    OR json_extract(assertion.properties_json, '$.morpheme_id') = ?
                 )""",
            (focus_morpheme_id,) * 5,
        ).fetchone()[0]
    )

    branch_rows = connection.execute(
        """SELECT assertion.source_entity_id, assertion.target_entity_id,
                  assertion.basis, assertion.properties_json,
                  COUNT(evidence.evidence_id) AS evidence_count
           FROM relation_assertions AS assertion
           LEFT JOIN assertion_evidence AS evidence
             ON evidence.assertion_id = assertion.assertion_id
           WHERE assertion.status = 'accepted'
             AND assertion.relation = 'shares-component'
             AND (
                assertion.source_entity_id = ? OR assertion.target_entity_id = ?
             )
           GROUP BY assertion.assertion_id""",
        (focus_morpheme_id, focus_morpheme_id),
    ).fetchall()
    proposal_only = bool(branch_rows)
    for row in branch_rows:
        try:
            properties = json.loads(str(row["properties_json"]))
        except (TypeError, json.JSONDecodeError):
            properties = {}
        other_id = (
            str(row["target_entity_id"])
            if str(row["source_entity_id"]) == focus_morpheme_id
            else str(row["source_entity_id"])
        )
        canonical_member = connection.execute(
            """SELECT 1 FROM term_morphemes
               WHERE term_id = ? AND morpheme_id = ? LIMIT 1""",
            (other_id, focus_morpheme_id),
        ).fetchone()
        if (
            str(row["basis"]) != "model"
            or properties.get("proposal") is not True
            or int(row["evidence_count"])
            or canonical_member is not None
        ):
            proposal_only = False
            break

    branch_checkpoint_found = False
    branch_history_steps = 0
    artifacts = connection.execute(
        """SELECT artifact.payload
           FROM job_artifacts AS artifact
           JOIN preparation_jobs AS job ON job.job_id = artifact.job_id
           WHERE job.subject_entity_id = ?
             AND job.source_fingerprint = ?
             AND artifact.stage = 'accepted-origin-branches'
             AND artifact.validation_state = 'accepted'
           ORDER BY artifact.created_at DESC""",
        (subject_entity_id, source_fingerprint),
    ).fetchall()
    for artifact in artifacts:
        try:
            payload = json.loads(str(artifact["payload"]))
        except (TypeError, json.JSONDecodeError):
            continue
        branches = payload.get("branches")
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            if str(branch.get("component_id", "")) != focus_morpheme_id:
                continue
            branch_checkpoint_found = True
            steps = branch.get("steps")
            branch_history_steps = max(
                branch_history_steps, len(steps) if isinstance(steps, list) else 0
            )

    eligible = (
        same_form_only
        and history_nodes == 0
        and branch_checkpoint_found
        and branch_history_steps == 0
        and proposal_only
    )
    return eligible, history_nodes, len(branch_rows)


def audit_morphology_pseudocards(
    cards_database: Path,
    knowledge_database: Path,
    morphology_databases: Mapping[str, Path],
) -> MorphologyPseudoCardCleanupReport:
    """Find visible pseudo-cards admitted from non-declaration source rows.

    A failed source eligibility check alone is never enough. The visible card
    must also be a same-form wrapper with a two-node/one-edge projection, have
    an explicit no-history checkpoint, and expose only uncited model proposal
    branches. Canonical graph entities and assertions are deliberately outside
    this maintenance boundary.
    """

    cards_database = Path(cards_database).resolve()
    knowledge_database = Path(knowledge_database).resolve()
    morphology = _load_morphology_sources(morphology_databases)
    checkpoints = _checkpoint_rows(knowledge_database)
    checkpoint_by_card = {row["card_id"]: row for row in checkpoints}

    with sqlite3.connect(cards_database, timeout=30) as cards:
        cards.row_factory = sqlite3.Row
        active_rows = cards.execute(
            """SELECT card_id, mode, query, payload
               FROM cards
               WHERE mode IN ('root', 'affix')
                 AND status = 'active'
                 AND validation_state = 'accepted'
               ORDER BY mode, query, card_id"""
        ).fetchall()

    candidates: list[MorphologyPseudoCardCandidate] = []
    with sqlite3.connect(knowledge_database, timeout=30) as knowledge:
        knowledge.row_factory = sqlite3.Row
        for row in active_rows:
            checkpoint = checkpoint_by_card.get(str(row["card_id"]))
            if checkpoint is None:
                continue
            source_reference = _source_reference(
                str(checkpoint["source_fingerprint"]), morphology
            )
            if source_reference is None:
                continue
            source_mode, source_record_id = source_reference
            source = morphology[source_mode]["records"].get(source_record_id)
            if source is None:
                continue
            evidence = SimpleNamespace(
                headword=source["headword"], excerpt=source["content"]
            )
            if AutonomousMorphologySeeder._eligible_evidence(source_mode, evidence):
                continue
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            nodes, edges, focus_morpheme_id = _card_projection(payload)
            if nodes > 2 or edges > 1 or not focus_morpheme_id:
                continue
            extensions = payload.get("extensions")
            extensions = extensions if isinstance(extensions, dict) else {}
            subject_key = str(extensions.get("knowledge_subject", ""))
            subject_entity_id = (
                subject_key.split(":", 1)[1]
                if subject_key.startswith("term:")
                else str(checkpoint["subject_entity_id"])
            )
            if subject_entity_id != str(checkpoint["subject_entity_id"]):
                continue
            integrity, history_nodes, proposal_branches = _component_integrity(
                knowledge,
                mode=str(row["mode"]),
                subject_entity_id=subject_entity_id,
                focus_morpheme_id=focus_morpheme_id,
                source_fingerprint=str(checkpoint["source_fingerprint"]),
            )
            if not integrity:
                continue
            replacement_record_id, replacement_term, replacement_page = (
                _nearest_declaration(source_mode, source, morphology)
            )
            candidates.append(
                MorphologyPseudoCardCandidate(
                    card_id=str(row["card_id"]),
                    mode=str(row["mode"]),
                    query=str(row["query"]),
                    subject_entity_id=subject_entity_id,
                    focus_morpheme_id=focus_morpheme_id,
                    source_mode=source_mode,
                    source_record_id=source_record_id,
                    source_headword=str(source["headword"]),
                    source_page=int(source["source_page"]),
                    source_excerpt=str(source["content"])[:320],
                    checkpoint_artifact_id=str(checkpoint["artifact_id"]),
                    projection_nodes=nodes,
                    projection_edges=edges,
                    history_nodes=history_nodes,
                    proposal_branches=proposal_branches,
                    replacement_record_id=replacement_record_id,
                    replacement_term=replacement_term,
                    replacement_page=replacement_page,
                )
            )
    return MorphologyPseudoCardCleanupReport(
        dry_run=True,
        scanned_cards=len(active_rows),
        checkpoint_linked_cards=sum(
            str(row["card_id"]) in checkpoint_by_card for row in active_rows
        ),
        candidates=tuple(candidates),
    )


def repair_morphology_pseudocards(
    cards_database: Path,
    knowledge_database: Path,
    morphology_databases: Mapping[str, Path],
    *,
    dry_run: bool = True,
    model: str = "",
    queue_replacements: bool = True,
) -> MorphologyPseudoCardCleanupReport:
    """Archive exact bad visible revisions and optionally queue local repair.

    Replacement planning only persists ordinary preparation jobs. It never
    invokes a model in this call and therefore cannot introduce an online-model
    dependency. Mixed compose checkpoints are retained because their primary
    Word card may still be independently useful.
    """

    audit = audit_morphology_pseudocards(
        cards_database, knowledge_database, morphology_databases
    )
    if dry_run or not audit.candidates:
        return audit

    timestamp = datetime.now(UTC).isoformat()
    candidate_ids = {candidate.card_id for candidate in audit.candidates}
    checkpoint_rows = _checkpoint_rows(Path(knowledge_database).resolve())
    checkpoints = {
        row["artifact_id"]: row
        for row in checkpoint_rows
        if row["card_id"] in candidate_ids
    }
    cards_archived = 0
    with sqlite3.connect(Path(cards_database).resolve(), timeout=30) as cards:
        cards.execute("BEGIN IMMEDIATE")
        for candidate in audit.candidates:
            cursor = cards.execute(
                """UPDATE cards
                   SET status = 'archived', updated_at = ?,
                       review_note = CASE
                         WHEN review_note = '' THEN ?
                         ELSE review_note || '; ' || ?
                       END
                   WHERE card_id = ? AND status = 'active'
                     AND validation_state = 'accepted'""",
                (
                    timestamp,
                    "archived: source row is not a morphology declaration",
                    "archived: source row is not a morphology declaration",
                    candidate.card_id,
                ),
            )
            cards_archived += int(cursor.rowcount)
        cards.commit()

    checkpoints_superseded = 0
    checkpoints_retained_as_mixed = 0
    with sqlite3.connect(Path(knowledge_database).resolve(), timeout=30) as knowledge:
        knowledge.execute("BEGIN IMMEDIATE")
        for artifact_id, checkpoint in checkpoints.items():
            if set(checkpoint["output_ids"]).issubset(candidate_ids):
                cursor = knowledge.execute(
                    """UPDATE job_artifacts SET validation_state = 'superseded'
                       WHERE artifact_id = ? AND validation_state = 'accepted'""",
                    (artifact_id,),
                )
                checkpoints_superseded += int(cursor.rowcount)
            else:
                checkpoints_retained_as_mixed += 1
        knowledge.commit()

    replacements_queued = 0
    replacement_jobs_queued = 0
    if queue_replacements and model.strip():
        from .knowledge import KnowledgeStore
        from .preparation import PreparationPlanner

        morphology = _load_morphology_sources(morphology_databases)
        knowledge_store = KnowledgeStore(Path(knowledge_database))
        seen_replacements: set[tuple[str, str, str]] = set()
        for candidate in audit.candidates:
            replacement_key = (
                candidate.source_mode,
                candidate.replacement_record_id,
                candidate.replacement_term,
            )
            if not all(replacement_key) or replacement_key in seen_replacements:
                continue
            seen_replacements.add(replacement_key)
            source_sha = str(
                morphology[candidate.source_mode].get("source_sha256", "")
            )
            plan = PreparationPlanner(
                knowledge_store,
                model=model.strip(),
                prompt_version="morphology-pseudocard-repair-v1",
                source_fingerprint=(
                    f"{source_sha}:{candidate.source_mode}:"
                    f"{candidate.replacement_record_id}:cleanup"
                ),
            ).plan_word(candidate.replacement_term)
            statuses = {
                str(job["job_id"]): str(job["status"])
                for job in knowledge_store.jobs_for_subject(plan.subject_key)
            }
            queued = sum(
                statuses.get(str(job_id)) in {"queued", "running"}
                for job_id in plan.jobs.values()
            )
            replacements_queued += bool(queued)
            replacement_jobs_queued += queued

    return MorphologyPseudoCardCleanupReport(
        dry_run=False,
        scanned_cards=audit.scanned_cards,
        checkpoint_linked_cards=audit.checkpoint_linked_cards,
        candidates=audit.candidates,
        cards_archived=cards_archived,
        checkpoints_superseded=checkpoints_superseded,
        checkpoints_retained_as_mixed=checkpoints_retained_as_mixed,
        replacements_queued=replacements_queued,
        replacement_jobs_queued=replacement_jobs_queued,
    )
