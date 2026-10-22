from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from .knowledge import KnowledgeStore
from .languages import (
    DISPLAY_LANGUAGES,
    normalize_language_code,
    ordered_language_codes,
    supports_english_morphology,
)


from .semantic_contract import (
    SEMANTIC_JOB_PROMPT_MARKER as _SEMANTIC_JOB_PROMPT_MARKER,
    SemanticContextUnavailable,
)


def _language_code(value: str) -> str:
    return normalize_language_code(value)


@dataclass(frozen=True)
class PreparationPlan:
    subject_entity_id: str
    subject_key: str
    jobs: dict[str, str]


class PreparationPlanner:
    """Create small sequential jobs; execution is handled by a separate worker."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        model: str,
        prompt_version: str = "atomic-v1",
        source_fingerprint: str = "",
        priority_offset: int = 0,
    ):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.source_fingerprint = source_fingerprint
        self.priority_offset = int(priority_offset)

    def _job(
        self,
        job_type: str,
        subject_key: str,
        subject_entity_id: str,
        *,
        language: str = "",
        priority: int,
        depends_on: Iterable[str] = (),
        max_attempts: int = 2,
    ) -> str:
        job_id = self.store.enqueue_job(
            job_type,
            subject_key,
            subject_entity_id=subject_entity_id,
            language=language,
            priority=priority + self.priority_offset,
            model=self.model,
            prompt_version=self.prompt_version,
            source_fingerprint=self.source_fingerprint,
            max_attempts=max_attempts,
            depends_on=depends_on,
        )
        return job_id

    def plan_word(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        return self._plan_word(
            text,
            language=language,
            display_languages=display_languages,
            include_origin=True,
        )

    def plan_word_card(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        """Prepare only the multilingual Word Card requested by the user.

        Origin decomposition is an independent product path. Keeping it out of
        a linked vocabulary click makes that click faster and prevents a weak
        historical branch from blocking an otherwise valid Word Card.
        """

        return self._plan_word(
            text,
            language=language,
            display_languages=display_languages,
            include_origin=False,
        )

    def _plan_word(
        self,
        text: str,
        *,
        language: str,
        display_languages: Iterable[str],
        include_origin: bool,
    ) -> PreparationPlan:
        language = _language_code(language)
        display_languages = ordered_language_codes(language, display_languages)
        if include_origin and not supports_english_morphology(language):
            raise ValueError(
                "origin/root/affix analysis currently requires an English source term"
            )
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        jobs: dict[str, str] = {}
        retrieval = self._job(
            "retrieve-evidence", subject_key, term_id, language=language, priority=10
        )
        jobs["retrieve-evidence"] = retrieval
        meaning = self._job(
            "prepare-meaning",
            subject_key,
            term_id,
            language=language,
            priority=20,
            depends_on=(retrieval,),
        )
        jobs[f"meaning:{language}"] = meaning
        origin = ""
        if include_origin:
            morphology = self._job(
                "split-morphemes",
                subject_key,
                term_id,
                language=language,
                priority=30,
                depends_on=(retrieval, meaning),
            )
            jobs["split-morphemes"] = morphology
            origin = self._job(
                "expand-origin-branches",
                subject_key,
                term_id,
                language=language,
                priority=40,
                depends_on=(morphology,),
                max_attempts=3,
            )
            jobs["expand-origin-branches"] = origin

        language_jobs: list[str] = []
        for output_language in dict.fromkeys(display_languages):
            if output_language == language:
                translation_job = meaning
            else:
                translation_job = self._job(
                    "prepare-translation",
                    subject_key,
                    term_id,
                    language=output_language,
                    priority=50,
                    depends_on=(meaning,),
                )
                jobs[f"translation:{output_language}"] = translation_job
            pronunciation = self._job(
                "prepare-pronunciation",
                subject_key,
                term_id,
                language=output_language,
                priority=60,
                depends_on=(translation_job,),
            )
            jobs[f"pronunciation:{output_language}"] = pronunciation
            language_jobs.extend((translation_job, pronunciation))

        grammar = self._job(
            "prepare-grammar-properties",
            subject_key,
            term_id,
            language=language,
            priority=70,
            depends_on=(meaning,),
        )
        jobs["grammar-properties"] = grammar
        word_card = self._job(
            "compose-word-card",
            subject_key,
            term_id,
            priority=90,
            # Lower-priority language jobs run first, but a failed optional
            # language must not block composition from last-good atoms.
            depends_on=(grammar,),
        )
        jobs["compose-word-card"] = word_card
        if include_origin:
            origin_card = self._job(
                "compose-origin-card",
                subject_key,
                term_id,
                priority=90,
                depends_on=(origin,),
            )
            jobs["compose-origin-card"] = origin_card
        return PreparationPlan(term_id, subject_key, jobs)

    def plan_translation(
        self,
        text: str,
        target_language: str,
        *,
        source_language: str = "en",
    ) -> PreparationPlan:
        """Revisit one language atom without duplicating the complete word plan."""
        source_language = _language_code(source_language)
        target_language = _language_code(target_language)
        term_id = self.store.upsert_term(source_language, text, status="draft")
        subject_key = f"term:{term_id}"
        meanings = self.store.artifacts_for_subject(
            subject_key,
            stage="accepted-meaning",
            validation_state="accepted",
        )
        if not meanings:
            raise ValueError(f"no accepted meaning is available for {text!r}")
        translation = self._job(
            "prepare-translation",
            subject_key,
            term_id,
            language=target_language,
            priority=50,
            depends_on=(str(meanings[-1]["job_id"]),),
        )
        return PreparationPlan(
            term_id,
            subject_key,
            {f"translation:{target_language}": translation},
        )

    def plan_language(
        self,
        text: str,
        target_language: str,
        *,
        source_language: str = "en",
    ) -> PreparationPlan:
        """Rebuild one translation and its dependent pronunciation only."""

        source_language = _language_code(source_language)
        target_language = _language_code(target_language)
        if target_language == source_language:
            raise ValueError("source language is not a translation target")
        term_id = self.store.upsert_term(source_language, text, status="draft")
        subject_key = f"term:{term_id}"
        artifacts = self.store.artifacts_for_subject(
            subject_key, validation_state="accepted"
        )
        meanings = [
            item for item in artifacts
            if item["stage"] == "accepted-meaning"
            and item["language"] == source_language
        ]
        if not meanings:
            raise ValueError(f"no accepted meaning is available for {text!r}")
        translation = self._job(
            "prepare-translation",
            subject_key,
            term_id,
            language=target_language,
            priority=50,
            depends_on=(str(meanings[-1]["job_id"]),),
        )
        pronunciation = self._job(
            "prepare-pronunciation",
            subject_key,
            term_id,
            language=target_language,
            priority=60,
            depends_on=(translation,),
        )
        jobs = {
            f"translation:{target_language}": translation,
            f"pronunciation:{target_language}": pronunciation,
        }
        required = [
            ("accepted-grammar-properties", source_language),
            *(
                ("accepted-translation", language)
                for language in DISPLAY_LANGUAGES
                if language not in {source_language, target_language}
            ),
            *(
                ("accepted-pronunciation", language)
                for language in DISPLAY_LANGUAGES
                if language != target_language
            ),
        ]
        dependencies = [translation, pronunciation]
        for stage, language in required:
            matches = [
                item for item in artifacts
                if item["stage"] == stage and item["language"] == language
            ]
            if not matches:
                break
            dependencies.append(str(matches[-1]["job_id"]))
        else:
            jobs["compose-word-card"] = self._job(
                "compose-word-card",
                subject_key,
                term_id,
                priority=90,
                depends_on=dependencies,
            )
        return PreparationPlan(
            term_id,
            subject_key,
            jobs,
        )

    def plan_pronunciation(
        self,
        text: str,
        target_language: str,
        *,
        source_language: str = "en",
    ) -> PreparationPlan:
        """Rebuild one reading from its accepted translation, then recompose."""

        source_language = _language_code(source_language)
        target_language = _language_code(target_language)
        term_id = self.store.upsert_term(source_language, text, status="draft")
        subject_key = f"term:{term_id}"
        artifacts = self.store.artifacts_for_subject(
            subject_key, validation_state="accepted"
        )
        source_stage = (
            "accepted-meaning"
            if target_language == source_language
            else "accepted-translation"
        )
        sources = [
            item
            for item in artifacts
            if item["stage"] == source_stage and item["language"] == target_language
        ]
        if not sources:
            source_label = "meaning" if target_language == source_language else "translation"
            raise ValueError(
                f"no accepted {target_language} {source_label} is available for {text!r}"
            )
        pronunciation = self._job(
            "prepare-pronunciation",
            subject_key,
            term_id,
            language=target_language,
            priority=60,
            depends_on=(str(sources[-1]["job_id"]),),
        )
        jobs = {f"pronunciation:{target_language}": pronunciation}

        # An isolated repair recomposes only an already complete core card.
        # First publication remains partial-capable in ``_plan_word``.
        required = [
            ("accepted-meaning", source_language),
            ("accepted-grammar-properties", source_language),
            *(
                ("accepted-translation", language)
                for language in DISPLAY_LANGUAGES
                if language != source_language
            ),
            *(
                ("accepted-pronunciation", language)
                for language in DISPLAY_LANGUAGES
                if language != target_language
            ),
        ]
        dependencies = [pronunciation]
        missing = False
        for stage, language in required:
            matches = [
                item
                for item in artifacts
                if item["stage"] == stage and item["language"] == language
            ]
            if not matches:
                missing = True
                break
            dependencies.append(str(matches[-1]["job_id"]))
        if not missing:
            jobs["compose-word-card"] = self._job(
                "compose-word-card",
                subject_key,
                term_id,
                priority=90,
                depends_on=dependencies,
            )
        return PreparationPlan(term_id, subject_key, jobs)

    def plan_word_card_view(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        """Recompose a Word Card from the newest accepted language atoms."""

        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        dependencies: list[str] = []
        language = _language_code(language)
        display_languages = ordered_language_codes(language, display_languages)
        required = [("accepted-meaning", language)]
        missing: list[str] = []
        for stage, output_language in required:
            artifacts = [
                artifact
                for artifact in self.store.artifacts_for_subject(
                    subject_key,
                    stage=stage,
                    validation_state="accepted",
                )
                if artifact["language"] == output_language
            ]
            if not artifacts:
                missing.append(f"{stage}:{output_language}")
                continue
            dependencies.append(str(artifacts[-1]["job_id"]))
        if missing:
            raise ValueError(
                f"accepted Word Card atoms are missing for {text!r}: {', '.join(missing)}"
            )
        composition = self._job(
            "compose-word-card",
            subject_key,
            term_id,
            priority=90,
            depends_on=dependencies,
        )
        return PreparationPlan(
            term_id,
            subject_key,
            {"compose-word-card": composition},
        )

    def plan_evidence(
        self,
        text: str,
        *,
        language: str = "en",
    ) -> PreparationPlan:
        """Refresh retrieval alone after a source or lexical-filter revision."""
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        retrieval = self._job(
            "retrieve-evidence",
            subject_key,
            term_id,
            language=language,
            priority=10,
        )
        return PreparationPlan(
            term_id,
            subject_key,
            {"retrieve-evidence": retrieval},
        )

    def plan_morphemes(
        self,
        text: str,
        *,
        language: str = "en",
    ) -> PreparationPlan:
        """Revisit only the bounded surface decomposition."""
        language = _language_code(language)
        if not supports_english_morphology(language):
            raise ValueError("morpheme splitting currently requires English")
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        evidence = self.store.artifacts_for_subject(
            subject_key,
            stage="retrieved-evidence",
            validation_state="candidate",
        )
        meanings = self.store.artifacts_for_subject(
            subject_key,
            stage="accepted-meaning",
            validation_state="accepted",
        )
        if not evidence or not meanings:
            raise ValueError(f"current evidence or accepted meaning is missing for {text!r}")
        split = self._job(
            "split-morphemes",
            subject_key,
            term_id,
            language=language,
            priority=30,
            depends_on=(str(evidence[-1]["job_id"]), str(meanings[-1]["job_id"])),
        )
        return PreparationPlan(term_id, subject_key, {"split-morphemes": split})

    def plan_origin(
        self,
        text: str,
        *,
        language: str = "en",
    ) -> PreparationPlan:
        """Revisit only historical branches from the accepted decomposition."""
        language = _language_code(language)
        if not supports_english_morphology(language):
            raise ValueError("origin expansion currently requires English morphology")
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        splits = self.store.artifacts_for_subject(
            subject_key,
            stage="accepted-morpheme-split",
            validation_state="accepted",
        )
        if not splits:
            raise ValueError(f"no accepted morpheme split is available for {text!r}")
        origin = self._job(
            "expand-origin-branches",
            subject_key,
            term_id,
            language=language,
            priority=40,
            depends_on=(str(splits[-1]["job_id"]),),
            max_attempts=3,
        )
        return PreparationPlan(term_id, subject_key, {"expand-origin-branches": origin})

    def plan_origin_card(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        """Compose the visible origin view from already accepted atoms."""
        language = _language_code(language)
        if not supports_english_morphology(language):
            raise ValueError("Origin Card composition currently requires English morphology")
        display_languages = ordered_language_codes(language, display_languages)
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"
        artifacts = self.store.artifacts_for_subject(
            subject_key, validation_state="accepted"
        )
        origins = [
            item for item in artifacts if item["stage"] == "accepted-origin-branches"
        ]
        if not origins:
            raise ValueError(f"no accepted origin branches are available for {text!r}")
        required = [("accepted-meaning", language)]
        dependencies = [str(origins[-1]["job_id"])]
        missing: list[str] = []
        for stage, output_language in required:
            matches = [
                item
                for item in artifacts
                if item["stage"] == stage and item["language"] == output_language
            ]
            if not matches:
                missing.append(f"{stage}:{output_language}")
                continue
            dependencies.append(str(matches[-1]["job_id"]))
        if missing:
            raise ValueError(
                f"accepted Origin Card atoms are missing for {text!r}: {', '.join(missing)}"
            )
        composition = self._job(
            "compose-origin-card",
            subject_key,
            term_id,
            priority=90,
            depends_on=dependencies,
        )
        return PreparationPlan(term_id, subject_key, {"compose-origin-card": composition})

    def plan_projection_repair(
        self,
        term_entity_id: str,
        desired_fingerprint: str,
        *,
        failure_count: int = 0,
    ) -> PreparationPlan:
        """Queue one subject-local composition from accepted checkpoints only.

        Projection repair is deliberately not a lexical preparation plan.  It
        must never upsert a caller-supplied word, retrieve evidence, invoke the
        model, or enqueue language work.  The desired fingerprint and bounded
        failure epoch make the one compose job deterministic across a crash
        between enqueue and durable repair binding, while allowing a genuinely
        failed fingerprint to retry after its cooldown.
        """

        term_entity_id = str(term_entity_id).strip()
        desired_fingerprint = str(desired_fingerprint).strip().casefold()
        if not term_entity_id:
            raise ValueError("projection repair requires an existing term subject")
        if (
            len(desired_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in desired_fingerprint)
        ):
            raise ValueError("projection repair requires a SHA-256 local fingerprint")
        record = self.store.term_record(term_entity_id)
        language = _language_code(str(record.get("language", "")))
        if not supports_english_morphology(language):
            raise ValueError("projection repair currently requires English morphology")
        if str(record.get("status", "")) != "accepted":
            raise ValueError("projection repair requires an accepted term subject")

        subject_key = f"term:{term_entity_id}"
        artifacts = self.store.artifacts_for_subject(
            subject_key, validation_state="accepted"
        )
        dependencies: list[str] = []
        missing: list[str] = []
        for stage in (
            "accepted-meaning",
            "accepted-morpheme-split",
            "accepted-origin-branches",
        ):
            matches = [item for item in artifacts if item["stage"] == stage]
            if not matches:
                missing.append(stage)
                continue
            dependencies.append(str(matches[-1]["job_id"]))
        if missing:
            raise ValueError(
                "accepted projection checkpoints are missing for "
                f"{term_entity_id!r}: {', '.join(missing)}"
            )

        failure_epoch = max(0, int(failure_count))
        prompt_version = (
            f"{self.prompt_version}-projection-local-"
            f"p{desired_fingerprint}-f{failure_epoch}"
        )
        composition = self.store.enqueue_job(
            "compose-origin-card",
            subject_key,
            subject_entity_id=term_entity_id,
            priority=5 + self.priority_offset,
            model=self.model,
            prompt_version=prompt_version,
            source_fingerprint=desired_fingerprint,
            max_attempts=2,
            depends_on=tuple(dict.fromkeys(dependencies)),
        )
        return PreparationPlan(
            term_entity_id,
            subject_key,
            {"compose-origin-card": composition},
        )

    def plan_lexical_history_repair(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        """Rebuild failed lexical history while reusing accepted language atoms."""

        language = _language_code(language)
        if not supports_english_morphology(language):
            raise ValueError("lexical history repair currently requires English morphology")
        display_languages = ordered_language_codes(language, display_languages)
        term_id = self.store.upsert_term(language, text, status="draft")
        subject_key = f"term:{term_id}"

        def newest(stage: str, output_language: str = "") -> dict[str, object] | None:
            artifacts = self.store.artifacts_for_subject(
                subject_key,
                stage=stage,
                validation_state=(
                    "candidate" if stage == "retrieved-evidence" else "accepted"
                ),
            )
            matches = [
                artifact
                for artifact in artifacts
                if not output_language or artifact["language"] == output_language
            ]
            return matches[-1] if matches else None

        retrieval = newest("retrieved-evidence")
        meaning = newest("accepted-meaning", language)
        missing: list[str] = []
        if retrieval is None:
            missing.append("retrieved-evidence")
        if meaning is None:
            missing.append(f"accepted-meaning:{language}")

        language_dependencies: list[str] = []
        for output_language in dict.fromkeys(display_languages):
            if output_language != language:
                translation = newest("accepted-translation", output_language)
                if translation is not None:
                    language_dependencies.append(str(translation["job_id"]))
            pronunciation = newest("accepted-pronunciation", output_language)
            if pronunciation is not None:
                language_dependencies.append(str(pronunciation["job_id"]))
        if missing:
            raise ValueError(
                f"accepted lexical repair atoms are missing for {text!r}: "
                + ", ".join(missing)
            )
        assert retrieval is not None and meaning is not None

        accepted_split = newest("accepted-morpheme-split", language)
        jobs: dict[str, str] = {}
        if accepted_split is None:
            split = self._job(
                "split-morphemes",
                subject_key,
                term_id,
                language=language,
                priority=30,
                depends_on=(str(retrieval["job_id"]), str(meaning["job_id"])),
            )
            jobs["split-morphemes"] = split
        else:
            # Lexical structure is already accepted knowledge. A prompt/worker
            # repair must not spend another model call rediscovering it.
            split = str(accepted_split["job_id"])
        origin = self._job(
            "expand-origin-branches",
            subject_key,
            term_id,
            language=language,
            priority=40,
            depends_on=(split,),
            max_attempts=3,
        )
        composition = self._job(
            "compose-origin-card",
            subject_key,
            term_id,
            priority=90,
            depends_on=(origin,),
        )
        jobs["expand-origin-branches"] = origin
        jobs["compose-origin-card"] = composition
        return PreparationPlan(term_id, subject_key, jobs)

    def plan_semantic_enrichment(
        self,
        claim: dict[str, Any],
        context: dict[str, Any],
    ) -> PreparationPlan:
        """Queue exactly the job type reserved by a semantic-growth claim."""

        entity_id = str(claim.get("entity_id", "")).strip()
        language = _language_code(str(claim.get("language", "en")))
        entity_type = str(claim.get("entity_type", "")).strip()
        job_type = str(claim.get("job_type", "")).strip()
        desired_fingerprint = str(claim.get("desired_fingerprint", "")).strip()
        if not entity_id or not desired_fingerprint:
            raise ValueError("semantic enrichment claim is incomplete")
        if desired_fingerprint != self.source_fingerprint:
            raise ValueError("semantic enrichment fingerprint does not match planner")
        if any(
            str(context.get(key, "")).strip() != expected
            for key, expected in (
                ("entity_id", entity_id),
                ("entity_type", entity_type),
                ("job_type", job_type),
                ("desired_fingerprint", desired_fingerprint),
            )
        ):
            raise ValueError("semantic enrichment context does not match claim")
        if bool(context.get("has_accepted_meaning", False)) and not bool(
            context.get("needs_semantic_review", False)
        ):
            raise ValueError("semantic enrichment subject already has a meaning")

        if entity_type == "morpheme":
            if job_type != "prepare-entity-meaning" or str(
                context.get("entity_kind", "")
            ) not in {"root", "prefix", "suffix"}:
                raise ValueError("semantic enrichment morpheme is not eligible")
            subject_key = f"semantic:{entity_id}:{language}"
            meaning = self._job(
                "prepare-entity-meaning",
                subject_key,
                entity_id,
                language=language,
                priority=7,
            )
            return PreparationPlan(
                entity_id,
                subject_key,
                {"prepare-entity-meaning": meaning},
            )

        if entity_type != "term" or job_type != "prepare-meaning":
            raise ValueError("semantic enrichment entity type is not supported")
        term = self.store.term_record(entity_id)
        if str(term.get("status", "")) != "accepted":
            raise ValueError("semantic enrichment requires an accepted derivative term")
        if _language_code(str(term.get("language", ""))) != language:
            raise ValueError("semantic enrichment term language does not match claim")

        subject_key = f"term:{entity_id}"
        existing_jobs = {
            str(job["job_id"]): job
            for job in self.store.jobs_for_subject(subject_key)
        }
        retrieved_artifacts = self.store.artifacts_for_subject(
            subject_key,
            stage="retrieved-evidence",
            validation_state="candidate",
        )
        reusable_retrievals = [
            artifact
            for artifact in retrieved_artifacts
            if (
                str(artifact.get("job_id", "")) in existing_jobs
                and str(
                    existing_jobs[str(artifact["job_id"])].get("job_type", "")
                )
                == "retrieve-evidence"
                and str(
                    existing_jobs[str(artifact["job_id"])].get("status", "")
                )
                == "complete"
                and str(
                    existing_jobs[str(artifact["job_id"])].get(
                        "source_fingerprint", ""
                    )
                )
                == desired_fingerprint
            )
        ]
        jobs: dict[str, str] = {}
        if reusable_retrievals:
            retrieval = str(reusable_retrievals[-1]["job_id"])
        else:
            retrieval = self._job(
                "retrieve-evidence",
                subject_key,
                entity_id,
                language=language,
                priority=6,
            )
            jobs["retrieve-evidence"] = retrieval
        meaning = self._job(
            "prepare-meaning",
            subject_key,
            entity_id,
            language=language,
            priority=7,
            depends_on=(retrieval,),
        )
        jobs["prepare-meaning"] = meaning
        return PreparationPlan(entity_id, subject_key, jobs)

    def plan_content(
        self,
        kind: str,
        text: str,
        *,
        language: str = "en",
        source_key: str = "",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        entity_id = self.store.upsert_content_item(
            kind, language, text, source_key=source_key, status="draft"
        )
        subject_key = f"content:{entity_id}"
        jobs: dict[str, str] = {}
        retrieval = self._job(
            "retrieve-evidence", subject_key, entity_id, language=language, priority=10
        )
        jobs["retrieve-evidence"] = retrieval
        investigate = self._job(
            "extract-investigation-terms",
            subject_key,
            entity_id,
            language=language,
            priority=20,
            depends_on=(retrieval,),
        )
        grammar = self._job(
            "prepare-grammar-parts",
            subject_key,
            entity_id,
            language=language,
            priority=30,
            depends_on=(retrieval,),
        )
        jobs["extract-investigation-terms"] = investigate
        jobs["grammar-parts"] = grammar
        translations: list[str] = []
        for output_language in dict.fromkeys(display_languages):
            translation = self._job(
                "prepare-content-translation",
                subject_key,
                entity_id,
                language=output_language,
                priority=40,
                depends_on=(retrieval,),
            )
            jobs[f"translation:{output_language}"] = translation
            translations.append(translation)
        compose = self._job(
            f"compose-{kind}-card",
            subject_key,
            entity_id,
            priority=90,
            depends_on=(grammar, *translations),
        )
        jobs[f"compose-{kind}-card"] = compose
        return PreparationPlan(entity_id, subject_key, jobs)

    def plan_card_investigations(self, card_id: str) -> PreparationPlan:
        """Select reusable words from one already-acquired book card."""

        content = self.store.content_for_card(card_id, "en")
        if content is None:
            raise ValueError(f"no acquired English content is available for card {card_id!r}")
        entity_id = str(content["entity_id"])
        subject_key = f"content:{entity_id}"
        extraction = self._job(
            "extract-investigation-terms",
            subject_key,
            entity_id,
            language="en",
            priority=20,
        )
        return PreparationPlan(
            entity_id,
            subject_key,
            {"extract-investigation-terms": extraction},
        )

    def plan_card_enrichment(
        self,
        card_id: str,
        *,
        include_investigation: bool = True,
        missing_only: bool = False,
    ) -> PreparationPlan:
        """Queue independent vocabulary and EN/JA/ZH grammar tasks for a card."""

        contents = {
            language: self.store.content_for_card(card_id, language)
            for language in ("en", "ja", "zh")
        }
        if any(content is None for content in contents.values()):
            raise ValueError(
                f"reviewed EN/JA/ZH content is unavailable for card {card_id!r}"
            )
        english = contents["en"]
        assert english is not None
        jobs: dict[str, str] = {}
        if include_investigation:
            extraction = self._job(
                "extract-investigation-terms",
                f"content:{english['entity_id']}",
                str(english["entity_id"]),
                language="en",
                priority=20,
            )
            jobs["extract-investigation-terms"] = extraction
        for language, content in contents.items():
            assert content is not None
            subject_key = f"content:{content['entity_id']}"
            if missing_only:
                accepted = self.store.grammar_for_content(str(content["entity_id"]))
                active = any(
                    job["job_type"] == "prepare-grammar-parts"
                    and job["language"] == language
                    and job["status"] in {"queued", "running"}
                    for job in self.store.jobs_for_subject(subject_key)
                )
                if accepted is not None or active:
                    continue
            jobs[f"grammar:{language}"] = self._job(
                "prepare-grammar-parts",
                subject_key,
                str(content["entity_id"]),
                language=language,
                priority=30,
            )
        return PreparationPlan(
            str(english["entity_id"]),
            f"content:{english['entity_id']}",
            jobs,
        )


class SemanticEnrichmentSeeder:
    """Admit one durable semantic subject while the serial worker stays bounded."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        model: str,
        prompt_version: str = "autonomous-semantic-v1",
        lease_seconds: int = 900,
        bootstrap_limit: int = 64,
    ):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version.strip() or "autonomous-semantic-v1"
        self.lease_seconds = max(30, min(int(lease_seconds), 86_400))
        self.bootstrap_limit = max(1, min(int(bootstrap_limit), 256))

    def _cancel_unbound_jobs(
        self,
        entity_id: str,
        language: str,
        fingerprint: str,
        prompt_version: str,
        reason: str,
    ) -> None:
        """Make every job from one failed semantic plan terminal atomically."""

        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE preparation_jobs
                   SET status = 'failed', attempts = max_attempts, error = ?,
                       locked_at = '', updated_at = ?
                   WHERE subject_entity_id = ? AND language = ?
                     AND source_fingerprint = ? AND prompt_version = ?
                     AND status IN ('queued', 'running')""",
                (
                    reason[:2000],
                    datetime.now(UTC).isoformat(),
                    entity_id,
                    language,
                    fingerprint,
                    prompt_version,
                ),
            )
            self.store._fail_blocked_jobs(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def run_once(self, _seed: str = "") -> dict[str, Any]:
        claim = self.store.claim_lexical_semantic_enrichment(
            lease_seconds=self.lease_seconds,
            bootstrap_limit=self.bootstrap_limit,
        )
        if claim is None:
            return {
                "selected": False,
                "action": "semantic-enrichment",
                "reason": "one semantic subject is active or no semantic gap is due",
            }

        entity_id = str(claim["entity_id"])
        language = _language_code(str(claim.get("language", "en")))
        fingerprint = str(claim["desired_fingerprint"])
        lease_token = str(claim["lease_token"])
        semantic_prompt_version = ""
        try:
            try:
                context = self.store.semantic_enrichment_context(entity_id, language)
            except SemanticContextUnavailable:
                context = {}
            if (
                any(
                    str(context.get(key, "")).strip() != str(claim.get(key, "")).strip()
                    for key in ("entity_id", "entity_type", "job_type", "desired_fingerprint")
                )
                or (bool(context.get("has_accepted_meaning", False))
                    and not bool(context.get("needs_semantic_review", False)))
            ):
                # Corpus/identity changes after claiming are normal stale work,
                # not a reason to crash the autonomous worker.
                self.store.release_lexical_semantic_enrichment(
                    entity_id, fingerprint, lease_token, language=language
                )
                self.store.refresh_lexical_semantic_enrichment(
                    entity_id, language=language
                )
                return {
                    "selected": False,
                    "action": "semantic-enrichment",
                    "reason": "semantic inputs changed before admission",
                    "entity_id": entity_id,
                }
            attempt = max(1, int(claim.get("attempt_count", 1)))
            semantic_prompt_version = (
                f"{_SEMANTIC_JOB_PROMPT_MARKER}{self.prompt_version}-a{attempt}"
            )
            planner = PreparationPlanner(
                self.store,
                model=self.model,
                prompt_version=semantic_prompt_version,
                source_fingerprint=fingerprint,
            )
            plan = planner.plan_semantic_enrichment(claim, context)
            terminal_job_id = str(plan.jobs[str(claim["job_type"])])
            bound = self.store.bind_lexical_semantic_enrichment(
                entity_id,
                fingerprint,
                lease_token,
                terminal_job_id,
                language=language,
            )
        except Exception:
            if semantic_prompt_version:
                self._cancel_unbound_jobs(
                    entity_id,
                    language,
                    fingerprint,
                    semantic_prompt_version,
                    "semantic plan failed before its durable binding",
                )
            self.store.release_lexical_semantic_enrichment(
                entity_id,
                fingerprint,
                lease_token,
                language=language,
            )
            raise
        if not bound:
            self._cancel_unbound_jobs(
                entity_id,
                language,
                fingerprint,
                semantic_prompt_version,
                "semantic claim changed before its job could be bound",
            )
            self.store.release_lexical_semantic_enrichment(
                entity_id,
                fingerprint,
                lease_token,
                language=language,
            )
            return {
                "selected": False,
                "action": "semantic-enrichment",
                "reason": "semantic claim changed before its job could be bound",
            }
        return {
            "selected": True,
            "action": "semantic-enrichment",
            "entity_id": entity_id,
            "entity_type": str(claim["entity_type"]),
            "entity_kind": str(claim["entity_kind"]),
            "label": str(claim.get("label", "")),
            "job_type": str(claim["job_type"]),
            "job_id": terminal_job_id,
            "jobs": dict(plan.jobs),
            "desired_fingerprint": fingerprint,
            "attempt_count": int(claim.get("attempt_count", 1)),
        }
