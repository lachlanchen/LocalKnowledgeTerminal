from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .knowledge import KnowledgeStore


DISPLAY_LANGUAGES = ("en", "ja", "zh", "fr", "ar")


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
    ):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.source_fingerprint = source_fingerprint

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
            priority=priority,
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
            depends_on=(*language_jobs, grammar),
        )
        jobs["compose-word-card"] = word_card
        if include_origin:
            origin_card = self._job(
                "compose-origin-card",
                subject_key,
                term_id,
                priority=90,
                depends_on=(origin, *language_jobs),
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
        pronunciation = self._job(
            "prepare-pronunciation",
            subject_key,
            term_id,
            language=target_language,
            priority=60,
            depends_on=(translation,),
        )
        return PreparationPlan(
            term_id,
            subject_key,
            {
                f"translation:{target_language}": translation,
                f"pronunciation:{target_language}": pronunciation,
            },
        )

    def plan_pronunciation(
        self,
        text: str,
        target_language: str,
        *,
        source_language: str = "en",
    ) -> PreparationPlan:
        """Rebuild one reading from its accepted translation, then recompose."""

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

        # Recompose only when a complete Word Card already exists. The new
        # pronunciation job replaces the old dependency; every other accepted
        # atom is reused without another model call.
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
        required = [
            ("accepted-meaning", language),
            ("accepted-grammar-properties", language),
        ]
        required.extend(
            ("accepted-pronunciation", output_language)
            for output_language in dict.fromkeys(display_languages)
        )
        required.extend(
            ("accepted-translation", output_language)
            for output_language in dict.fromkeys(display_languages)
            if output_language != language
        )
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
        required.extend(
            ("accepted-pronunciation", output_language)
            for output_language in dict.fromkeys(display_languages)
        )
        required.extend(
            ("accepted-translation", output_language)
            for output_language in dict.fromkeys(display_languages)
            if output_language != language
        )
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

    def plan_lexical_history_repair(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
    ) -> PreparationPlan:
        """Rebuild failed lexical history while reusing accepted language atoms."""

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
                if translation is None:
                    missing.append(f"accepted-translation:{output_language}")
                else:
                    language_dependencies.append(str(translation["job_id"]))
            pronunciation = newest("accepted-pronunciation", output_language)
            if pronunciation is None:
                missing.append(f"accepted-pronunciation:{output_language}")
            else:
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
            depends_on=(origin, *language_dependencies),
        )
        jobs["expand-origin-branches"] = origin
        jobs["compose-origin-card"] = composition
        return PreparationPlan(term_id, subject_key, jobs)

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
