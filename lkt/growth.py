"""Bounded local-RAG history growth independent of the discovery backlog.

This module schedules existing atomic handlers. It never infers a linguistic
claim, generates a citation, or makes an additional concurrent model request.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, Iterable

from .knowledge import KnowledgeStore
from .lexical_refinement import structure_review_due
from .language_completion import (
    LANGUAGE_SLOTS, claim_language_completion, language_completion_status,
    queue_language_card_refreshes,
)
from .publication_queue import (
    PUBLICATION_FOCUS_KEY, claim_bounded_composition_refresh, claim_publication_job,
)
from .store import _ANNOTATION_WARNINGS


_PREFIX = "continuous-origin-v1"
_TURN_KEY = "continuous_origin_worker_turn_v1"
_RECOVERY_KEY = "continuous_origin_annotation_recovery_v1"
_ERROR_KEY = "continuous_origin_worker_error_v1"
_HISTORY_JOBS = (
    "retrieve-evidence", "prepare-meaning", "split-morphemes",
    "expand-origin-branches", "compose-origin-card",
)
_LOG = logging.getLogger(__name__)


def _annotation_only_failure(error: str) -> bool:
    prefix = "origin views failed prepublication validation: "
    if not error.startswith(prefix):
        return False
    details = re.sub(r"(?:word|root|affix):\d+:\s*", "", error[len(prefix):])
    messages = [part.strip() for part in re.split(r"[;,]", details) if part.strip()]
    return bool(messages) and all(message in _ANNOTATION_WARNINGS for message in messages)


def _recover_annotation_publications(store: KnowledgeStore) -> None:
    """Release only the latest annotation-only failures, once per policy version."""
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM schema_meta WHERE key=?", (_RECOVERY_KEY,)
        ).fetchone():
            connection.commit()
            return
        rows = connection.execute(
            """SELECT job.* FROM preparation_jobs AS job
               WHERE job.job_type='compose-origin-card' AND job.status='failed'
                 AND NOT EXISTS (
                     SELECT 1 FROM preparation_jobs AS newer
                     WHERE newer.subject_key=job.subject_key
                       AND newer.job_type=job.job_type
                       AND (newer.created_at > job.created_at OR
                            (newer.created_at=job.created_at AND newer.job_id>job.job_id))
                 )"""
        ).fetchall()
        recovered = 0
        now = datetime.now(UTC).isoformat()
        for row in rows:
            if not _annotation_only_failure(str(row["error"])):
                continue
            repair = connection.execute(
                "SELECT * FROM lexical_projection_repairs WHERE compose_job_id=?",
                (row["job_id"],),
            ).fetchone()
            if repair is not None:
                # Preserve the failed job as an audit record. The existing
                # projection planner will issue a new fingerprint-bound retry.
                connection.execute(
                    """UPDATE lexical_projection_repairs
                       SET status='pending', next_eligible_at='', last_error='',
                           lease_token='', lease_expires_at='', compose_job_id=NULL,
                           bound_fingerprint='', updated_at=?
                       WHERE compose_job_id=?
                         AND desired_fingerprint<>applied_fingerprint""",
                    (now, row["job_id"]),
                )
            else:
                connection.execute(
                    """UPDATE preparation_jobs SET status='queued', attempts=0,
                           error='', locked_at='', updated_at=? WHERE job_id=?""",
                    (now, row["job_id"]),
                )
            recovered += 1
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES (?,?)",
            (_RECOVERY_KEY, json.dumps({"recovered": recovered, "at": now})),
        )
        connection.commit()


def _finish_history_epochs(store: KnowledgeStore) -> bool:
    """Reconcile this lane by its own jobs, not unrelated queued translations."""
    with closing(store._connect()) as connection:
        epochs = connection.execute(
            """SELECT epoch_id,prompt_version,status,lease_expires_at FROM lexical_revisit_epochs
               WHERE prompt_version LIKE ? AND status IN ('claimed','planned')""",
            (_PREFIX + "-revisit-%",),
        ).fetchall()
        outcomes = []
        active = False
        now = datetime.now(UTC).isoformat()
        for epoch in epochs:
            jobs = connection.execute(
                "SELECT status FROM preparation_jobs WHERE prompt_version=?",
                (epoch["prompt_version"],),
            ).fetchall()
            expired = str(epoch["lease_expires_at"] or "") <= now
            running = any(row["status"] == "running" for row in jobs)
            if expired and not running and (epoch["status"] == "claimed" or not jobs):
                # A process can die after leasing a subject but before binding
                # all its jobs. Retire that partial plan before selecting more
                # work; otherwise the unbound lease would block this lane forever.
                connection.execute(
                    """UPDATE preparation_jobs SET status='failed',error=?,updated_at=?
                       WHERE prompt_version=? AND status IN ('queued','paused')""",
                    ("History planning lease expired before binding", now, epoch["prompt_version"]),
                )
                connection.commit()
                outcomes.append((str(epoch["epoch_id"]), "failed"))
                continue
            if not jobs or any(row["status"] in {"queued", "running", "paused"} for row in jobs):
                active = True
                continue
            outcomes.append((
                str(epoch["epoch_id"]),
                "failed" if any(row["status"] == "failed" for row in jobs) else "complete",
            ))
    for epoch_id, outcome in outcomes:
        store.complete_lexical_revisit_epoch(
            epoch_id, outcome=outcome,
            result_summary="Continuous history lane finished its own atomic jobs.",
        )
    return active


def _plan_history(store: KnowledgeStore, model: str) -> None:
    if _finish_history_epochs(store):
        return
    # Reuse the existing durable candidate rotation, graph fingerprint, lease,
    # and bounded exponential cooldown. Non-history language queues no longer
    # prevent a word from receiving its first origin analysis or a revisit.
    claim = store.claim_lexical_revisit_candidate(
        language="en", prompt_prefix=_PREFIX, scan_limit=64, history_only=True,
    )
    if claim is None:
        return
    epoch_id = str(claim["epoch_id"])
    prompt = str(claim["prompt_version"])
    term_id = str(claim["term_id"])
    subject = "term:" + term_id

    def enqueue(kind: str, priority: int, dependencies: Iterable[str] = ()) -> str:
        return store.enqueue_job(
            kind, subject, subject_entity_id=term_id, language="en",
            priority=priority, model=model, prompt_version=prompt,
            source_fingerprint=str(claim["fingerprint"]),
            depends_on=tuple(dependencies), max_attempts=3,
        )

    try:
        artifacts = store.artifacts_for_subject(subject, validation_state="accepted")
        stages = {
            str(item["stage"]) for item in artifacts
            if str(item.get("language", "")) in {"", "en"}
        }
        # Always refresh evidence. Reuse meaning and trusted book splits, but
        # periodically let the local model correct its own structural analysis.
        # The existing subject lease and cooldown still bound these revisits.
        retrieval = enqueue("retrieve-evidence", 10)
        dependencies = [retrieval]
        if "accepted-meaning" not in stages:
            dependencies.append(enqueue("prepare-meaning", 20, (retrieval,)))
        if structure_review_due(artifacts, int(claim.get("epoch_number", 1))):
            dependencies.append(enqueue("split-morphemes", 30, dependencies))
        origin = enqueue("expand-origin-branches", 40, dependencies)
        enqueue("compose-origin-card", 50, (origin,))
        if not store.bind_lexical_revisit_epoch(epoch_id, job_subject_key=subject):
            raise RuntimeError("History growth lease changed before binding")
    except Exception as exc:
        with closing(store._connect()) as connection:
            connection.execute(
                """UPDATE preparation_jobs SET status='failed',error=?,updated_at=?
                   WHERE prompt_version=? AND status='queued'""",
                ("Unbound history plan: " + str(exc)[:400], datetime.now(UTC).isoformat(), prompt),
            )
            connection.commit()
        store.complete_lexical_revisit_epoch(epoch_id, outcome="failed", result_summary=str(exc)[:500])
        raise


def _claim_history_job(store: KnowledgeStore, allowed: tuple[str, ...]) -> dict[str, Any] | None:
    types = tuple(kind for kind in _HISTORY_JOBS if kind in allowed)
    if not types:
        return None
    placeholders = ",".join("?" for _ in types)
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._fail_blocked_jobs(connection)
        row = connection.execute(
            """SELECT job.* FROM preparation_jobs AS job
               WHERE job.status='queued' AND job.attempts<job.max_attempts
                 AND job.prompt_version LIKE ?
                 AND job.job_type IN (""" + placeholders + """)
                 AND NOT EXISTS (
                     SELECT 1 FROM job_dependencies AS dependency
                     JOIN preparation_jobs AS prerequisite
                       ON prerequisite.job_id=dependency.depends_on_job_id
                     WHERE dependency.job_id=job.job_id
                       AND prerequisite.status<>'complete'
                       AND (job.job_type<>'compose-origin-card' OR prerequisite.status<>'failed')
                 )
               ORDER BY job.priority,job.created_at,job.job_id LIMIT 1""",
            (_PREFIX + "-revisit-%", *types),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE preparation_jobs SET status='running',attempts=attempts+1,
                   locked_at=?,updated_at=? WHERE job_id=?""", (now, now, row["job_id"]),
        )
        result = dict(connection.execute(
            "SELECT * FROM preparation_jobs WHERE job_id=?", (row["job_id"],)
        ).fetchone())
        connection.commit()
        return result


def claim_growth_work(
    store: KnowledgeStore, *, model: str, allowed_job_types: Iterable[str]
) -> dict[str, Any] | None:
    """Protect eight dispatches for lexical publication and language completion."""
    allowed = tuple(allowed_job_types)
    if not any(kind in allowed for kind in (
        "prepare-meaning", "split-morphemes", "expand-origin-branches", "enrich-morphology-v3"
    )):
        return None
    try:
        _recover_annotation_publications(store)
        if "compose-word-card" in allowed:
            queue_language_card_refreshes(store, model=model)
        # All ready publications share a bounded, age-fair cache lane. Giving
        # fresh origin plans an unbounded bypass would strand Word Cards and
        # let a replenishing stream postpone inference forever.
        ready = claim_bounded_composition_refresh(store, allowed, turn_key=_TURN_KEY)
        if ready is not None:
            return ready
        with closing(store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM schema_meta WHERE key=?", (_TURN_KEY,)).fetchone()
            turn = int(row["value"]) if row else 0
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_TURN_KEY, str(turn + 1)),
            )
            connection.commit()
        slot = turn % 12
        if slot in LANGUAGE_SLOTS:
            job = claim_language_completion(store, allowed)
            if job is not None:
                return job
        if slot < 8:
            job = claim_publication_job(store, allowed)
            if job is not None:
                return job
            # When no tracked publication can advance, preserve the ordinary
            # request turns instead of lending all of them to background work.
            # Morphology enrichment owns its separate reservation below.
            if slot % 2:
                ordinary = tuple(kind for kind in allowed if kind != "enrich-morphology-v3")
                if ordinary:
                    job = store.claim_next_job(ordinary)
                    if job is not None:
                        return job
        if slot == 8:
            _plan_history(store, model)
            job = _claim_history_job(store, allowed)
            if job is not None:
                return job
        if slot == 10 and "enrich-morphology-v3" in allowed:
            job = store.claim_next_job(("enrich-morphology-v3",))
            if job is not None:
                return job
        if slot in {9, 11}:
            # The existing worker owns bounded semantic/language/publication
            # maintenance turns. It cannot preempt the eight protected turns.
            return None
        # Borrow an empty reservation rather than idling a healthy local model.
        job = claim_publication_job(store, allowed)
        if job is not None:
            return job
        _plan_history(store, model)
        return _claim_history_job(store, allowed)
    except Exception as exc:
        # A maintenance failure must not abandon an already running model job
        # or halt the remaining queue. Surface it in health instead of hiding it.
        _LOG.exception("Continuous lexical growth scheduling failed")
        try:
            with closing(store._connect()) as connection:
                connection.execute(
                    "INSERT INTO schema_meta(key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_ERROR_KEY, json.dumps({"error": str(exc)[:500], "at": datetime.now(UTC).isoformat()})),
                )
                connection.commit()
        except Exception:
            _LOG.exception("Could not record lexical growth scheduling failure")
    return None


def growth_status(store: KnowledgeStore) -> dict[str, Any]:
    """Read-only health diagnostics; this never schedules inference."""
    with closing(store._connect()) as connection:
        jobs = connection.execute(
            """SELECT job_type,status,count(*) AS jobs,max(updated_at) AS updated_at
               FROM preparation_jobs WHERE prompt_version LIKE ?
               GROUP BY job_type,status""", (_PREFIX + "-revisit-%",),
        ).fetchall()
        meta = dict(connection.execute(
            "SELECT key,value FROM schema_meta WHERE key IN (?,?,?,?)",
            (_TURN_KEY, _RECOVERY_KEY, _ERROR_KEY, PUBLICATION_FOCUS_KEY),
        ).fetchall())
    return {
        "policy": _PREFIX, "worker_turns": int(meta.get(_TURN_KEY, "0")),
        "jobs": [dict(row) for row in jobs],
        "annotation_recovery": json.loads(meta.get(_RECOVERY_KEY, "{}")),
        "last_error": json.loads(meta.get(_ERROR_KEY, "{}")),
        "local_model_only": True,
        "language_completion": language_completion_status(store),
        "dispatch": {
            "policy": "publication-first-v2", "cycle": 12,
            "publication_turns": 8, "history_turns": 1,
            "morphology_turns": 1, "maintenance_turns": 2,
            "focused_publication_job": meta.get(PUBLICATION_FOCUS_KEY, ""),
            "ready_publication": "model-free-priority",
            "cached_refresh_limit": "two-per-normal-dispatch",
            "cached_refresh_order": "alternate-word-origin-oldest-ready",
            "language_slots_within_publication_turns": list(LANGUAGE_SLOTS),
        },
    }
