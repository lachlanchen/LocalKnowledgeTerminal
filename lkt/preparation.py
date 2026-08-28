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
        )
        for prerequisite in depends_on:
            self.store.add_job_dependency(job_id, prerequisite)
        return job_id

    def plan_word(
        self,
        text: str,
        *,
        language: str = "en",
        display_languages: Iterable[str] = DISPLAY_LANGUAGES,
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
        origin_card = self._job(
            "compose-origin-card",
            subject_key,
            term_id,
            priority=90,
            depends_on=(origin, *language_jobs),
        )
        jobs["compose-word-card"] = word_card
        jobs["compose-origin-card"] = origin_card
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
