"""Fair completion of admitted language jobs and model-free card refreshes.

No linguistic inference lives here. Existing atomic handlers consume the local
RAG context, and composition continues to use their accepted checkpoints.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, Iterable

from .knowledge import KnowledgeStore


LANGUAGE_JOB_TYPES = (
    "prepare-translation", "prepare-pronunciation", "prepare-grammar-properties",
)
LANGUAGE_SLOTS = (1, 3, 5, 7)
REFRESH_VERSION = "language-card-refresh-v1"
_LAST_LANGUAGE_PREFIX = "language_completion_last_v1:"
_LAST_DISPATCH = "language_completion_dispatch_v1"
_CARD_INPUT_STAGES = (
    "accepted-meaning", "accepted-translation", "accepted-pronunciation",
    "accepted-grammar-properties",
)


def claim_language_completion(
    store: KnowledgeStore, allowed_job_types: Iterable[str],
) -> dict[str, Any] | None:
    """Claim an exact ready job, including prerequisites of a language job.

    Published partial cards remain eligible. Missing checkpoints come before
    reruns, and durable per-language service times prevent one language or a
    repeated failure from occupying every reserved turn.
    """
    allowed = tuple(dict.fromkeys(allowed_job_types))
    roots = tuple(kind for kind in LANGUAGE_JOB_TYPES if kind in allowed)
    if not roots:
        return None
    root_marks = ",".join("?" for _ in roots)
    allowed_marks = ",".join("?" for _ in allowed)
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._fail_blocked_jobs(connection)
        row = connection.execute(
            """WITH RECURSIVE checkpoints AS (
                SELECT previous.subject_key,atom.language,atom.stage
                FROM job_artifacts atom
                JOIN preparation_jobs previous ON previous.job_id=atom.job_id
                WHERE atom.validation_state='accepted'
                  AND atom.stage IN ('accepted-translation','accepted-pronunciation',
                                     'accepted-grammar-properties','accepted-word-card')
                GROUP BY previous.subject_key,atom.language,atom.stage
              ), published AS (
                SELECT subject_key FROM checkpoints
                WHERE stage='accepted-word-card' GROUP BY subject_key
              ), roots AS (
                SELECT job.*,
                  COALESCE(served.value, '') AS last_served,
                  checkpoint.stage IS NOT NULL AS has_checkpoint,
                  published.subject_key IS NOT NULL AS has_word_card
                FROM preparation_jobs job
                JOIN entities source ON source.entity_id=job.subject_entity_id
                  AND source.status='accepted'
                LEFT JOIN schema_meta served ON served.key=? || job.language
                LEFT JOIN checkpoints checkpoint
                  ON checkpoint.subject_key=job.subject_key
                  AND checkpoint.language=job.language
                  AND checkpoint.stage=CASE job.job_type
                    WHEN 'prepare-translation' THEN 'accepted-translation'
                    WHEN 'prepare-pronunciation' THEN 'accepted-pronunciation'
                    ELSE 'accepted-grammar-properties' END
                LEFT JOIN published ON published.subject_key=job.subject_key
                WHERE job.status='queued' AND job.attempts<job.max_attempts
                  AND job.subject_key LIKE 'term:%'
                  AND job.job_type IN (""" + root_marks + """)
              ), needed(root_id, job_id) AS (
                SELECT job_id,job_id FROM roots
                UNION
                SELECT needed.root_id,dependency.depends_on_job_id
                FROM needed JOIN job_dependencies dependency
                  ON dependency.job_id=needed.job_id
              )
              SELECT job.*,root.language AS completion_language,
                     root.job_id AS completion_root_id
              FROM needed
              JOIN roots root ON root.job_id=needed.root_id
              JOIN preparation_jobs job ON job.job_id=needed.job_id
              WHERE job.status='queued' AND job.attempts<job.max_attempts
                AND job.job_type IN (""" + allowed_marks + """)
                AND NOT EXISTS (
                  SELECT 1 FROM job_dependencies dependency
                  JOIN preparation_jobs prerequisite
                    ON prerequisite.job_id=dependency.depends_on_job_id
                  WHERE dependency.job_id=job.job_id
                    AND prerequisite.status<>'complete'
                    AND (job.job_type NOT IN ('compose-word-card','compose-origin-card')
                         OR prerequisite.status<>'failed')
                )
              ORDER BY root.has_checkpoint,root.has_word_card DESC,
                       root.last_served,root.attempts,root.created_at,root.job_id,
                       job.priority,job.created_at,job.job_id
              LIMIT 1""",
            (_LAST_LANGUAGE_PREFIX, *roots, *allowed),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE preparation_jobs SET status='running',attempts=attempts+1,
                   locked_at=?,updated_at=? WHERE job_id=?""",
            (now, now, row["job_id"]),
        )
        diagnostic = {
            "at": now, "job_id": row["job_id"], "job_type": row["job_type"],
            "language": row["completion_language"],
            "language_job_id": row["completion_root_id"],
            "subject_key": row["subject_key"],
        }
        connection.executemany(
            """INSERT INTO schema_meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (
                (_LAST_LANGUAGE_PREFIX + row["completion_language"], now),
                (_LAST_DISPATCH, json.dumps(diagnostic)),
            ),
        )
        claimed = connection.execute(
            "SELECT * FROM preparation_jobs WHERE job_id=?", (row["job_id"],),
        ).fetchone()
        connection.commit()
        return dict(claimed)


def queue_language_card_refreshes(
    store: KnowledgeStore, *, model: str, limit: int = 8,
) -> int:
    """Republish accepted inputs missing from a previously published Word Card.

    Read accepted inputs and card manifests once, then compare sets by subject.
    Correlating an artifact-table scan with another scan for each input turns
    this inexpensive publication step into quadratic work on a real library.
    IDs, rather than publication timestamps, detect stale concurrent snapshots.
    """
    limit = max(0, min(int(limit), 32))
    if not limit:
        return 0
    marks = ",".join("?" for _ in _CARD_INPUT_STAGES)
    with closing(store._connect()) as connection:
        rows = connection.execute(
            """SELECT job.subject_key,job.subject_entity_id,
                      term.language AS source_language,
                      atom.artifact_id,atom.stage,atom.created_at,
                      CASE WHEN atom.stage='accepted-word-card'
                        THEN json_extract(
                          CASE WHEN json_valid(atom.payload) THEN atom.payload ELSE '{}' END,
                          '$.knowledge_artifact_ids')
                        ELSE NULL END AS included_json
               FROM job_artifacts atom
               JOIN preparation_jobs job ON job.job_id=atom.job_id
               JOIN entities source ON source.entity_id=job.subject_entity_id
                 AND source.status='accepted'
               JOIN terms term ON term.entity_id=job.subject_entity_id
               WHERE atom.validation_state='accepted'
                 AND atom.stage IN (""" + marks + """,'accepted-word-card')
                 AND job.subject_key LIKE 'term:%'""",
            _CARD_INPUT_STAGES,
        ).fetchall()
        refreshes = connection.execute(
            """SELECT subject_key,status,created_at FROM preparation_jobs
               WHERE job_type='compose-word-card' AND prompt_version=?
                 AND status IN ('queued','running','paused','failed')""",
            (REFRESH_VERSION,),
        ).fetchall()

    subjects: dict[str, dict[str, Any]] = {}
    for row in rows:
        subject = subjects.setdefault(row["subject_key"], {
            "entity_id": row["subject_entity_id"],
            "language": row["source_language"],
            "inputs": {}, "included": set(), "has_card": False, "has_meaning": False,
        })
        if row["stage"] == "accepted-word-card":
            subject["has_card"] = True
            try:
                included = json.loads(row["included_json"] or "[]")
            except (ValueError, TypeError):
                included = []
            if isinstance(included, list):
                subject["included"].update(item for item in included if isinstance(item, str))
        else:
            subject["inputs"][row["artifact_id"]] = row["created_at"]
            if row["stage"] == "accepted-meaning":
                subject["has_meaning"] = True

    pending = set()
    failed_after: dict[str, str] = {}
    for refresh in refreshes:
        subject_key = refresh["subject_key"]
        if refresh["status"] != "failed":
            pending.add(subject_key)
        else:
            failed_after[subject_key] = max(
                failed_after.get(subject_key, ""), refresh["created_at"],
            )

    candidates = []
    for subject_key, subject in subjects.items():
        if subject_key in pending or not subject["has_card"] or not subject["has_meaning"]:
            continue
        missing_dates = [
            created_at for artifact_id, created_at in subject["inputs"].items()
            if artifact_id not in subject["included"]
            and created_at > failed_after.get(subject_key, "")
        ]
        if missing_dates:
            candidates.append((min(missing_dates), subject_key))

    queued = 0
    for _, subject_key in sorted(candidates)[:limit]:
        subject = subjects[subject_key]
        inputs = sorted(subject["inputs"])
        fingerprint = hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
        store.enqueue_job(
            "compose-word-card", subject_key,
            subject_entity_id=subject["entity_id"], language=subject["language"],
            priority=5, model=model, prompt_version=REFRESH_VERSION,
            source_fingerprint=fingerprint, max_attempts=2,
        )
        queued += 1
    return queued


def language_completion_status(store: KnowledgeStore) -> dict[str, Any]:
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", (_LAST_DISPATCH,),
        ).fetchone()
        queue = connection.execute(
            """SELECT job_type,language,status,COUNT(*) AS jobs,
                      MIN(created_at) AS oldest
               FROM preparation_jobs
               WHERE job_type IN ('prepare-translation','prepare-pronunciation',
                                  'prepare-grammar-properties')
                 AND status IN ('queued','running')
               GROUP BY job_type,language,status""",
        ).fetchall()
    return {
        "reserved_slots": list(LANGUAGE_SLOTS),
        "last_dispatch": json.loads(row[0]) if row else {},
        "queue": [dict(item) for item in queue],
        "refresh_policy": REFRESH_VERSION,
    }
