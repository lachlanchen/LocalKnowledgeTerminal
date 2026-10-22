from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .freedict import FreeDictRag
from .languages import DISPLAY_LANGUAGES, language_profile, normalize_language_code


WORDNET_LEXICONS = {
    code: language_profile(code).wordnet_specifier for code in DISPLAY_LANGUAGES
}


class LexiconRuntimeUnavailable(RuntimeError):
    pass


class WordnetRag:
    """Compact, sense-aligned multilingual dictionary evidence through Wn."""

    def __init__(self, data_directory: Path):
        try:
            import wn
        except ImportError as exc:  # pragma: no cover - Pi knowledge runtime
            raise LexiconRuntimeUnavailable(
                "Wn is not installed; run scripts/install_knowledge_runtime.sh"
            ) from exc
        self.wn = wn
        self.data_directory = Path(data_directory).resolve()
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.wn.config.data_directory = self.data_directory

    def _specifier(self, language: str) -> str:
        profile = language_profile(language)
        if profile.wordnet_specifier:
            return profile.wordnet_specifier
        language = profile.code
        primary = language.split("-", 1)[0]
        candidates: list[str] = []
        for lexicon in self.wn.lexicons():
            try:
                installed_language = normalize_language_code(str(lexicon.language))
            except ValueError:
                continue
            if installed_language not in {language, primary}:
                continue
            candidates.append(f"{lexicon.id}:{lexicon.version}")
        if not candidates:
            raise ValueError(f"no installed local dictionary for language: {language}")
        return sorted(candidates)[0]

    def _wordnet(self, language: str) -> tuple[Any, str]:
        language = normalize_language_code(language)
        specifier = self._specifier(language)
        expand = "" if language == "en" else WORDNET_LEXICONS["en"]
        return self.wn.Wordnet(specifier, expand=expand), specifier

    def search(
        self,
        query: str,
        *,
        source_language: str = "en",
        target_languages: Iterable[str] = ("ja", "zh", "fr", "ar"),
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise ValueError("lexicon query is empty")
        limit = max(1, min(int(limit), 20))
        source_language = normalize_language_code(source_language)
        try:
            source, source_specifier = self._wordnet(source_language)
        except ValueError:
            return []
        targets: dict[str, Any] = {}
        for raw_language in target_languages:
            language = normalize_language_code(raw_language)
            if language == source_language or language in targets:
                continue
            try:
                targets[language] = self._wordnet(language)[0]
            except ValueError:
                continue
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for word in source.words(query):
            for sense in word.senses():
                synset = sense.synset()
                key = (str(word.id), str(synset.id))
                if key in seen:
                    continue
                seen.add(key)
                translations: dict[str, list[str]] = {}
                ili = synset.ili
                if ili:
                    for language, wordnet in targets.items():
                        lemmas: list[str] = []
                        for translated in wordnet.synsets(ili=ili):
                            for lemma in translated.lemmas():
                                if lemma not in lemmas:
                                    lemmas.append(lemma)
                        translations[language] = lemmas[:8]
                results.append(
                    {
                        "entry_id": f"{source_specifier}:{sense.id}",
                        "headword": word.lemma(),
                        "part_of_speech": word.pos,
                        "definition": synset.definition() or "",
                        "forms": word.forms(),
                        "ili": str(ili or ""),
                        "translations": translations,
                        "corpus_id": source_specifier,
                        "source_language": source_language,
                        "source_title": "Open Multilingual Wordnet 2.0",
                        "kind": "dictionary-sense",
                        "license_locator": "lexicon metadata",
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def status(self) -> dict[str, Any]:
        installed = {
            f"{lexicon.id}:{lexicon.version}": {
                "language": str(lexicon.language),
                "label": str(lexicon.label),
            }
            for lexicon in self.wn.lexicons()
        }
        required = list(WORDNET_LEXICONS.values())
        return {
            "ready": all(specifier in installed for specifier in required),
            "data_directory": str(self.data_directory),
            "required": required,
            "installed": installed,
        }


class LocalLexiconRag:
    """Sense-aligned WordNet plus optional exact bilingual correction indexes."""

    def __init__(self, wordnet_directory: Path, freedict_database: Path):
        self.wordnet = WordnetRag(wordnet_directory)
        self.freedict = FreeDictRag(freedict_database)

    def search(
        self,
        query: str,
        *,
        source_language: str = "en",
        target_languages: Iterable[str] = ("ja", "zh", "fr", "ar"),
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        source_language = normalize_language_code(source_language)
        targets = tuple(
            dict.fromkeys(normalize_language_code(value) for value in target_languages)
        )
        records = self.wordnet.search(
            query,
            source_language=source_language,
            target_languages=targets,
            limit=limit,
        )
        if (
            source_language.split("-", 1)[0] == "en"
            and "ar" in targets
            and self.freedict.database.is_file()
        ):
            records.extend(self.freedict.search(query, limit=10))
        return records

    def status(self) -> dict[str, Any]:
        wordnet = self.wordnet.status()
        freedict = self.freedict.status()
        return {
            "ready": bool(wordnet.get("ready")) and bool(freedict.get("ready")),
            "wordnet": wordnet,
            "freedict_eng_ara": freedict,
        }
