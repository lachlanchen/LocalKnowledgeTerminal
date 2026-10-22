"""Restart-safe, dependency-aware affinity for unfinished lexical publication."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from typing import Any, Iterable

from .knowledge import KnowledgeStore


PUBLICATION_FOCUS_KEY = "lexical_publication_focus_v1"
_REFRESH_TURN_KEY = "lexical_model_free_refresh_turn_v2"
_REFRESH_COUNT_KEY = "lexical_model_free_refresh_count_v2"
_REFRESH_KIND_KEY = "lexical_model_free_refresh_kind_v2"
CACHED_PUBLICATIONS_PER_TURN = 2
PUBLICATION_JOB_TYPES = (
    "retrieve-evidence", "prepare-meaning", "split-morphemes",
    "expand-origin-branches", "compose-origin-card",
)


def claim_publication_job(
    store: KnowledgeStore, allowed_job_types: Iterable[str]
) -> dict[str, Any] | None:
    """Finish an admitted plan before starting more work on other subjects.

    Affinity belongs to a particular publication job, not a word forever.
    Blocked plans yield to ready plans; completed plans release their affinity.
    Projection repair and semantic enrichment cannot borrow this reservation.
    """
    allowed = tuple(kind for kind in PUBLICATION_JOB_TYPES if kind in allowed_job_types)
    if not allowed:
        return None
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._fail_blocked_jobs(connection)
        saved = connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", (PUBLICATION_FOCUS_KEY,),
        ).fetchone()
        focus = str(saved["value"]) if saved else ""
        row = connection.execute(
            """SELECT job.*, publication.job_id AS publication_job_id
               FROM preparation_jobs AS job
               JOIN preparation_jobs AS publication
                 ON publication.subject_key=job.subject_key
                AND publication.prompt_version=job.prompt_version
                AND publication.source_fingerprint=job.source_fingerprint
                AND publication.job_type='compose-origin-card'
                AND publication.status='queued'
                AND publication.attempts<publication.max_attempts
               WHERE job.status='queued' AND job.attempts<job.max_attempts
                 AND job.subject_key LIKE 'term:%'
                 AND job.job_type IN (""" + ",".join("?" for _ in allowed) + """)
                 AND publication.prompt_version NOT LIKE 'continuous-origin-%'
                 AND publication.prompt_version NOT LIKE '%-projection-%'
                 AND job.prompt_version NOT LIKE 'semantic-enrichment:%'
                 AND NOT EXISTS (
                     SELECT 1 FROM lexical_projection_repairs AS repair
                     WHERE repair.compose_job_id=publication.job_id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM lexical_semantic_enrichments AS semantic
                     WHERE semantic.enrichment_job_id=job.job_id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM job_dependencies AS dependency
                     JOIN preparation_jobs AS prerequisite
                       ON prerequisite.job_id=dependency.depends_on_job_id
                     WHERE dependency.job_id=job.job_id
                       AND prerequisite.status<>'complete'
                       AND (job.job_type<>'compose-origin-card'
                            OR prerequisite.status<>'failed')
                 )
               ORDER BY CASE WHEN publication.job_id=? THEN 0 ELSE 1 END,
                        publication.created_at, publication.job_id,
                        CASE job.job_type
                            WHEN 'compose-origin-card' THEN 0
                            WHEN 'expand-origin-branches' THEN 1
                            WHEN 'split-morphemes' THEN 2
                            WHEN 'prepare-meaning' THEN 3 ELSE 4 END,
                        job.created_at, job.job_id
               LIMIT 1""",
            (*allowed, focus),
        ).fetchone()
        if row is None:
            connection.execute(
                """DELETE FROM schema_meta WHERE key=? AND NOT EXISTS (
                       SELECT 1 FROM preparation_jobs WHERE job_id=?
                         AND status IN ('queued','running'))""",
                (PUBLICATION_FOCUS_KEY, focus),
            )
            connection.commit()
            return None
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE preparation_jobs SET status='running',attempts=attempts+1,
                   locked_at=?,updated_at=? WHERE job_id=?""",
            (now, now, row["job_id"]),
        )
        connection.execute(
            """INSERT INTO schema_meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (PUBLICATION_FOCUS_KEY, str(row["publication_job_id"])),
        )
        result = dict(connection.execute(
            "SELECT * FROM preparation_jobs WHERE job_id=?", (row["job_id"],),
        ).fetchone())
        connection.commit()
        return result


def claim_bounded_composition_refresh(
    store: KnowledgeStore, allowed_job_types: Iterable[str], *, turn_key: str,
) -> dict[str, Any] | None:
    """Claim a bounded, fair batch of cached publications, never inference.

    Alternate Word Card and origin work when both are ready, oldest first
    within each kind. Priority-5 refreshes must not indefinitely overtake old
    cards. The quota and job lease commit together and survive worker restarts.
    """
    allowed = set(allowed_job_types)
    types = tuple(kind for kind in ("compose-origin-card", "compose-word-card") if kind in allowed)
    if not types:
        return None
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        turn = connection.execute("SELECT value FROM schema_meta WHERE key=?", (turn_key,)).fetchone()
        current = turn_key + ":" + (str(turn["value"]) if turn else "0")
        state = dict(connection.execute(
            "SELECT key,value FROM schema_meta WHERE key IN (?,?,?)",
            (_REFRESH_TURN_KEY, _REFRESH_COUNT_KEY, _REFRESH_KIND_KEY),
        ).fetchall())
        used = int(state.get(_REFRESH_COUNT_KEY, "0")) if state.get(_REFRESH_TURN_KEY) == current else 0
        if used >= CACHED_PUBLICATIONS_PER_TURN:
            connection.commit()
            return None
        ready = connection.execute(
            """SELECT job.* FROM preparation_jobs AS job
               WHERE job.status='queued' AND job.attempts<job.max_attempts
                 AND job.job_type IN (""" + ",".join("?" for _ in types) + """)
                 AND NOT EXISTS (
                     SELECT 1 FROM job_dependencies AS dependency
                     JOIN preparation_jobs AS prerequisite
                       ON prerequisite.job_id=dependency.depends_on_job_id
                     WHERE dependency.job_id=job.job_id
                       AND prerequisite.status NOT IN ('complete','failed')
                 )
               ORDER BY CASE WHEN job.job_type=? THEN 1 ELSE 0 END,
                        job.created_at,job.job_id
               LIMIT 1""", (*types, str(state.get(_REFRESH_KIND_KEY, ""))),
        ).fetchone()
        if ready is None:
            connection.commit()
            return None
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE preparation_jobs
               SET status='running',attempts=attempts+1,locked_at=?,updated_at=?
               WHERE job_id=?""", (now, now, ready["job_id"]),
        )
        connection.executemany(
            """INSERT INTO schema_meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            ((_REFRESH_TURN_KEY, current), (_REFRESH_COUNT_KEY, str(used + 1)),
             (_REFRESH_KIND_KEY, str(ready["job_type"]))),
        )
        result = dict(connection.execute(
            "SELECT * FROM preparation_jobs WHERE job_id=?", (ready["job_id"],),
        ).fetchone())
        connection.commit()
        return result
