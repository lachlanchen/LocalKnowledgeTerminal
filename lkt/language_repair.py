"""Bounded, durable language repair admission; inference stays in atomic.py."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime, timedelta

from .languages import DISPLAY_LANGUAGES, normalize_language_code
from .language_projection import accepted_language_artifacts

VERSION = "language-repair-v1"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def script_problem(language, text):
    primary = language.split("-", 1)[0]
    patterns = {"zh": r"[\u3400-\u9fff]", "ja": r"[\u3040-\u30ff\u3400-\u9fff]",
                "ar": r"[\u0600-\u06ff]"}
    return bool(text and primary in patterns and not re.search(patterns[primary], text))


def repair_request(store, job):
    with closing(store._connect()) as connection:
        row = connection.execute(
            """SELECT payload FROM job_artifacts WHERE job_id=?
               AND stage='language-repair-request' ORDER BY created_at DESC LIMIT 1""",
            (job["job_id"],),
        ).fetchone()
    return json.loads(row[0]) if row else {}


def restore_translation_checkpoint(store, job, previous, *, reading=None, reading_source=None):
    """Restore accepted facts atomically, preserving their original provenance."""
    source_id = str(job["subject_entity_id"])
    language = normalize_language_code(job["language"])
    required = ("translation_id", "source_meaning_id", "term", "meaning")
    if any(not str(previous.get(key, "")).strip() for key in required):
        raise ValueError("accepted translation checkpoint is incomplete")

    def persist():
        with closing(store._connect()) as connection:
            source_meaning = connection.execute(
                """SELECT m.subject_entity_id,e.status FROM meanings m
                   JOIN entities e USING(entity_id) WHERE m.entity_id=?""",
                (previous["source_meaning_id"],),
            ).fetchone()
            if (source_meaning is None or source_meaning["status"] != "accepted"
                    or source_meaning["subject_entity_id"] != source_id):
                raise ValueError("translation source sense is no longer accepted")
            latest = connection.execute(
                """SELECT a.payload FROM job_artifacts a JOIN preparation_jobs j USING(job_id)
                   WHERE j.subject_entity_id=? AND a.language=? AND a.stage='accepted-translation'
                     AND a.validation_state='accepted' ORDER BY a.created_at DESC LIMIT 1""",
                (source_id, language),
            ).fetchone()
            current = json.loads(latest[0]) if latest else {}
            if any(current.get(key) != previous.get(key) for key in required):
                raise ValueError("translation checkpoint changed during repair")
            existing = connection.execute(
                """SELECT t.entity_id,e.status FROM terms t JOIN entities e USING(entity_id)
                   WHERE t.language=? AND t.normalized=? AND t.kind='word'""",
                (language, _key(previous["term"])),
            ).fetchone()
            if existing is not None and existing["status"] in {"archived", "rejected"}:
                raise ValueError("automatic restoration cannot reactivate a quarantined term")
        value = dict(previous)
        provenance = dict(value.get("provenance", {}))
        confidence = float(value.get("confidence", 0.5))
        if reading is not None:
            value["reading"] = reading
            value["reading_provenance"] = dict(reading_source or {})
            provenance["reading_provenance"] = value["reading_provenance"]
        target = store.upsert_term(
            language, value["term"], status="accepted", quality_score=confidence,
        )
        value["target_term_id"] = target
        value["translation_id"] = store.add_translation(
            source_id, language, value["term"],
            source_meaning_id=value["source_meaning_id"], target_term_id=target,
            definition=value["meaning"], transliteration=value.get("reading", ""),
            usage_note=value.get("usage_note", ""), status="accepted",
            quality_score=confidence, confidence=confidence,
            basis=provenance.get("basis", "model"),
            evidence_ids=value.get("evidence_ids", []), metadata=provenance,
            definition_model=value.get("model", ""),
            definition_prompt_version=value.get("provenance", {}).get("prompt_version", ""),
        )
        value["provenance"] = provenance
        value["repair_method"] = "accepted-checkpoint-restore"
        with closing(store._connect()) as connection:
            target_meaning = connection.execute(
                """SELECT m.entity_id FROM meanings m JOIN entities e USING(entity_id)
                   JOIN entity_edges edge ON edge.source_entity_id=m.entity_id
                   WHERE m.subject_entity_id=? AND m.language=? AND e.status='accepted'
                     AND edge.relation='translated-from' AND edge.target_entity_id=?
                     AND edge.status='accepted' ORDER BY e.updated_at DESC LIMIT 1""",
                (target, language, value["source_meaning_id"]),
            ).fetchone()
        if target_meaning:
            value["target_meaning_id"] = target_meaning[0]
        store.record_revision(
            value["translation_id"], value, model="accepted-checkpoint-restore",
            prompt_version=job.get("prompt_version", ""),
            reason="restore accepted translation links or align a dictionary reading",
            accepted=True,
        )
        return store.save_job_artifact(
            job["job_id"], "accepted-translation", value, language=language,
            validation_state="accepted", quality_score=confidence,
        )

    return store.atomic_semantic_write(source_id, persist)


def align_translation_reading(store, job, translation, reading, method, evidence_ids):
    """Keep accepted translation, pronunciation and card readings consistent."""
    if (job.get("language") not in {"ja", "zh"}
            or method.get("engine") not in {"JMdict", "pypinyin"}
            or _key(translation.get("reading", "")) == _key(reading)
            or not translation.get("source_meaning_id")):
        return
    restore_translation_checkpoint(
        store, job, translation, reading=reading,
        reading_source={"method": method, "evidence_ids": list(evidence_ids)},
    )


def inspect_subject(store, subject_id, languages=DISPLAY_LANGUAGES):
    """Return reproducible observations; suspicion is not a rejection decision."""
    source = store.term_record(subject_id)
    artifacts = accepted_language_artifacts(store, subject_id)
    meanings = [a for a in artifacts if a["stage"] == "accepted-meaning"
                and a["language"] == source["language"]]
    if not meanings:
        return []
    meaning = meanings[-1]
    sense = meaning["payload"]
    sense_fingerprint = _fingerprint({
        key: sense.get(key, "") for key in ("meaning_id", "definition", "part_of_speech")
    })
    translations = {a["language"]: a for a in artifacts if a["stage"] == "accepted-translation"}
    pronunciations = {a["language"]: a for a in artifacts if a["stage"] == "accepted-pronunciation"}
    expected = list(dict.fromkeys([*languages, *translations]))
    observations = []
    with closing(store._connect()) as connection:
        source_status = connection.execute(
            "SELECT status FROM entities WHERE entity_id=?", (subject_id,),
        ).fetchone()
        if not source_status or source_status[0] != "accepted":
            return []
        for raw_language in expected:
            language = normalize_language_code(raw_language)
            if language == source["language"]:
                continue
            artifact = translations.get(language, {})
            value = artifact.get("payload", {})
            pronunciation = pronunciations.get(language, {}).get("payload", {})
            issues = []
            translation_row = None
            target_row = None
            if not value:
                issues.append("missing-translation")
            else:
                translation_row = connection.execute(
                    """SELECT tr.*,e.status FROM translations tr JOIN entities e USING(entity_id)
                       WHERE tr.entity_id=?""", (value.get("translation_id", ""),),
                ).fetchone()
                target_row = connection.execute(
                    """SELECT t.*,e.status FROM terms t JOIN entities e USING(entity_id)
                       WHERE t.entity_id=?""", (value.get("target_term_id", ""),),
                ).fetchone()
                if translation_row is None or translation_row["status"] != "accepted":
                    issues.append("missing-translation-record")
                if target_row is None or target_row["status"] != "accepted":
                    issues.append("missing-target-term")
                target_id = value.get("target_term_id", "")
                has_meaning = connection.execute(
                    """SELECT 1 FROM meanings m JOIN entities e USING(entity_id)
                       JOIN entity_edges edge ON edge.source_entity_id=m.entity_id
                         AND edge.target_entity_id=? AND edge.relation='translated-from'
                         AND edge.status='accepted'
                       WHERE m.subject_entity_id=? AND m.language=? AND e.status='accepted'
                       LIMIT 1""", (sense.get("meaning_id", ""), target_id, language),
                ).fetchone()
                if not has_meaning:
                    issues.append("missing-target-sense-link")
                for field in ("term", "meaning"):
                    text = str(value.get(field, "")).strip()
                    if not text:
                        issues.append("missing-" + field)
                    elif script_problem(language, text):
                        issues.append("unexpected-" + field + "-script")
                    if "\ufffd" in text or re.search(r"\\(?:begin|frac|textbf)\b", text):
                        issues.append("damaged-" + field)
                if _key(value.get("meaning", "")) == _key(value.get("term", "")):
                    issues.append("uninformative-definition")
                if any(str(w.get("code", "")) == "retained-unsupported-unchanged-language-form"
                       for w in value.get("warnings", []) if isinstance(w, dict)):
                    issues.append("unverified-unchanged-form")
                review = value.get("repair_review", {})
                if (value.get("dictionary_candidates")
                        and review.get("source_sense_fingerprint") != sense_fingerprint
                        and not set(value.get("evidence_ids", [])) & set(sense.get("evidence_ids", []))):
                    issues.append("dictionary-sense-needs-review")
                if (str(value.get("source_meaning_id", ""))
                        != str(sense.get("meaning_id", ""))):
                    issues.append("source-sense-changed")
            if value:
                same_form = _key(pronunciation.get("term", "")) == _key(value.get("term", ""))
                if not pronunciation or not same_form:
                    issues.append("missing-current-pronunciation")
                elif (language.split("-")[0] in {"ja", "zh"}
                      and pronunciation.get("system") in {"kana", "pinyin"}
                      and _key(pronunciation.get("reading", ""))
                      != _key(value.get("reading", ""))):
                    issues.append("reading-mismatch")
            jobs = connection.execute(
                """SELECT job_type,status,error FROM preparation_jobs
                   WHERE subject_entity_id=? AND language=?
                   AND job_type IN ('prepare-translation','prepare-pronunciation')
                   ORDER BY updated_at DESC LIMIT 3""", (subject_id, language),
            ).fetchall()
            issues = sorted(set(issues))
            snapshot = {
                "sense": sense_fingerprint, "language": language, "issues": issues,
                "translation": {k: value.get(k, "") for k in (
                    "term", "meaning", "reading", "source_meaning_id", "translation_id")},
                "pronunciation": {k: pronunciation.get(k, "") for k in ("term", "reading", "system")},
            }
            observations.append({
                "subject_entity_id": subject_id, "source_term": source["text"],
                "source_language": source["language"], "language": language,
                "source_sense_fingerprint": sense_fingerprint,
                "source_sense": {k: sense.get(k, "") for k in (
                    "meaning_id", "definition", "part_of_speech")},
                "meaning_job_id": meaning["job_id"], "issues": issues,
                "previous_translation": value, "fingerprint": _fingerprint(snapshot),
                "recent_errors": [j["error"] for j in jobs if j["error"]],
                "priority": 10 if "missing-translation" in issues else
                            20 if any(x.startswith("missing-") for x in issues) else 30,
            })
    return observations


class LanguageRepairSeeder:
    def __init__(self, store, *, model, languages=DISPLAY_LANGUAGES, scan_limit=8):
        self.store, self.model = store, model
        self.languages = tuple(languages)
        self.scan_limit = max(1, min(int(scan_limit), 64))
        with closing(store._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS lexical_language_health (
                    subject_entity_id TEXT NOT NULL REFERENCES terms(entity_id),
                    language TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    observation_json TEXT NOT NULL, priority INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_eligible_at TEXT NOT NULL DEFAULT '',
                    job_id TEXT REFERENCES preparation_jobs(job_id),
                    checked_at TEXT NOT NULL, PRIMARY KEY(subject_entity_id,language)
                );
                CREATE INDEX IF NOT EXISTS idx_language_health_due
                    ON lexical_language_health(status,next_eligible_at,priority);
                CREATE TABLE IF NOT EXISTS lexical_language_scan (
                    scanner TEXT PRIMARY KEY,cursor_entity_id TEXT NOT NULL DEFAULT '',
                    scanned_count INTEGER NOT NULL DEFAULT 0,cycles INTEGER NOT NULL DEFAULT 0
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO lexical_language_scan(scanner) VALUES(?)", (VERSION,),
            )
            connection.commit()

    def observe(self, subject_id):
        observations = inspect_subject(self.store, subject_id, self.languages)
        now = datetime.now(UTC)
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for observation in observations:
                key = (subject_id, observation["language"])
                previous = connection.execute(
                    """SELECT h.*,j.status AS job_status FROM lexical_language_health h
                       LEFT JOIN preparation_jobs j ON j.job_id=h.job_id
                       WHERE h.subject_entity_id=? AND h.language=?""", key,
                ).fetchone()
                status, eligible, attempts, job_id = "pending", "", 0, None
                if previous:
                    attempts, job_id = previous["attempts"], previous["job_id"]
                    eligible = previous["next_eligible_at"]
                    if previous["job_status"] in {"queued", "running"}:
                        status = "queued"
                    elif previous["status"] == "queued":
                        status = "cooldown"
                        hours = min(168, 1 * 2 ** min(attempts - 1, 8))
                        eligible = (now + timedelta(hours=hours)).isoformat()
                    elif eligible and eligible > now.isoformat():
                        status = "cooldown"
                if not observation["issues"] and status != "queued":
                    status, eligible = "healthy", ""
                connection.execute(
                    """INSERT INTO lexical_language_health(
                         subject_entity_id,language,fingerprint,observation_json,priority,
                         status,attempts,next_eligible_at,job_id,checked_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(subject_entity_id,language) DO UPDATE SET
                         fingerprint=excluded.fingerprint,observation_json=excluded.observation_json,
                         priority=excluded.priority,status=excluded.status,
                         next_eligible_at=excluded.next_eligible_at,checked_at=excluded.checked_at""",
                    (*key, observation["fingerprint"], json.dumps(observation, ensure_ascii=False),
                     observation["priority"], status, attempts, eligible, job_id, now.isoformat()),
                )
            connection.commit()
        return observations

    def scan(self):
        with closing(self.store._connect()) as connection:
            cursor = connection.execute(
                "SELECT cursor_entity_id FROM lexical_language_scan WHERE scanner=?", (VERSION,),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT t.entity_id FROM terms t JOIN entities e USING(entity_id)
                   WHERE e.status='accepted' AND t.entity_id>? AND EXISTS(
                     SELECT 1 FROM preparation_jobs j JOIN job_artifacts a USING(job_id)
                     WHERE j.subject_entity_id=t.entity_id AND a.stage='accepted-meaning'
                       AND a.validation_state='accepted')
                   ORDER BY t.entity_id LIMIT ?""", (cursor, self.scan_limit),
            ).fetchall()
        for row in rows:
            self.observe(row[0])
        with closing(self.store._connect()) as connection:
            connection.execute(
                """UPDATE lexical_language_scan SET cursor_entity_id=?,
                     scanned_count=scanned_count+?,cycles=cycles+? WHERE scanner=?""",
                (rows[-1][0] if rows else "", len(rows), 0 if rows else 1, VERSION),
            )
            connection.commit()
        return len(rows)

    def enqueue_next(self, *, preferred_subject_id=""):
        now = datetime.now(UTC).isoformat()
        with closing(self.store._connect()) as connection:
            active = connection.execute(
                """SELECT 1 FROM preparation_jobs WHERE prompt_version LIKE ?
                   AND status IN ('queued','running') LIMIT 1""", (VERSION + ":%",),
            ).fetchone()
            if active:
                return {"selected": False, "reason": "repair-in-flight"}
            latest = connection.execute(
                """SELECT MAX(updated_at) FROM preparation_jobs
                   WHERE prompt_version LIKE ? AND status IN ('complete','failed')""",
                (VERSION + ":%",),
            ).fetchone()[0]
            if latest:
                others = connection.execute(
                    """SELECT COUNT(*) FROM preparation_jobs WHERE prompt_version NOT LIKE ?
                       AND status IN ('complete','failed') AND updated_at>?""",
                    (VERSION + ":%", latest),
                ).fetchone()[0]
                if others < 6:
                    return {"selected": False, "reason": "preserving-new-knowledge-progress"}
            row = connection.execute(
                """SELECT h.* FROM lexical_language_health h JOIN entities e
                     ON e.entity_id=h.subject_entity_id
                   WHERE h.status IN ('pending','cooldown') AND e.status='accepted'
                     AND (h.next_eligible_at='' OR h.next_eligible_at<=?)
                   ORDER BY CASE WHEN h.subject_entity_id=? THEN 0 ELSE 1 END,
                     h.priority,h.attempts,h.checked_at,h.subject_entity_id,h.language LIMIT 1""",
                (now, preferred_subject_id),
            ).fetchone()
        if row is None:
            return {"selected": False, "reason": "no-due-language-repair"}
        observation = json.loads(row["observation_json"])
        subject_id = row["subject_entity_id"]

        def persist():
            with closing(self.store._connect()) as connection:
                current = connection.execute(
                    "SELECT * FROM lexical_language_health WHERE subject_entity_id=? AND language=?",
                    (subject_id, row["language"]),
                ).fetchone()
                active = connection.execute(
                    """SELECT 1 FROM preparation_jobs WHERE prompt_version LIKE ?
                       AND status IN ('queued','running') LIMIT 1""", (VERSION + ":%",),
                ).fetchone()
                if (active or current["fingerprint"] != row["fingerprint"]
                        or current["status"] not in {"pending", "cooldown"}):
                    return {"selected": False, "reason": "repair-state-changed"}
                epoch = int(current["attempts"]) + 1
                prompt = f"{VERSION}:{row['fingerprint'][:16]}:e{epoch}"
                kwargs = dict(subject_entity_id=subject_id, model=self.model,
                              prompt_version=prompt, max_attempts=2)
                subject_key = "term:" + subject_id
                issues = set(observation["issues"])
                reading_only = issues <= {"reading-mismatch", "missing-current-pronunciation"}
                structural = issues <= {
                    "missing-translation-record", "missing-target-term", "missing-target-sense-link",
                    "reading-mismatch", "missing-current-pronunciation",
                }
                observation["repair_mode"] = (
                    "pronunciation" if reading_only else
                    "restore-checkpoint" if structural else "local-model-review"
                )
                translation = ""
                dependencies = ()
                if not reading_only:
                    if not structural:
                        retrieval = self.store.enqueue_job(
                            "retrieve-evidence", subject_key,
                            language=observation["source_language"], priority=12, **kwargs,
                        )
                        dependencies = (retrieval,)
                    translation = self.store.enqueue_job(
                        "prepare-translation", subject_key, language=row["language"],
                        priority=15, depends_on=dependencies, **kwargs,
                    )
                    self.store.save_job_artifact(
                        translation, "language-repair-request", observation,
                        language=row["language"], reusable=False,
                    )
                    dependencies = (translation,)
                pronunciation = self.store.enqueue_job(
                    "prepare-pronunciation", subject_key, language=row["language"],
                    priority=16, depends_on=dependencies, **kwargs,
                )
                composition = self.store.enqueue_job(
                    "compose-word-card", subject_key, priority=17,
                    depends_on=(*dependencies, pronunciation), **kwargs,
                )
                connection.execute(
                    """UPDATE lexical_language_health SET status='queued',attempts=?,
                         job_id=?,checked_at=? WHERE subject_entity_id=? AND language=?""",
                    (epoch, composition, now, subject_id, row["language"]),
                )
                return {"selected": True, "action": "language-repair",
                        "subject_entity_id": subject_id, "term": observation["source_term"],
                        "language": row["language"], "issues": observation["issues"],
                        "repair_mode": observation["repair_mode"],
                        "epoch": epoch, "translation_job_id": translation,
                        "compose_job_id": composition}
        return self.store.atomic_semantic_write(subject_id, persist)

    def run_once(self, _seed=""):
        scanned = self.scan()
        return {"event": VERSION, "scanned": scanned, **self.enqueue_next()}
