from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable


_LANGUAGE_TAG = re.compile(
    r"(?:[a-zA-Z]{2,8}|[xX])(?:-[a-zA-Z0-9]{1,8})*"
)


def normalize_language_code(value: object) -> str:
    """Return one stable, lower-case BCP47 language tag.

    Storage uses lower case deliberately so equivalent user spellings do not
    create parallel graph entities. Presentation casing belongs in the label.
    Private-use tags such as ``x-lkt-test`` are accepted for local extensions.
    """

    code = str(value).strip().replace("_", "-")
    if not code or not _LANGUAGE_TAG.fullmatch(code):
        raise ValueError(f"invalid language code: {value!r}")
    return code.casefold()


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    label: str
    card_key: str
    direction: str = "ltr"
    reading_system: str = "ipa"
    translation_reading_required: bool = False
    english_morphology: bool = False
    wordnet_specifier: str = ""
    display_order: int = 1000

    def __post_init__(self) -> None:
        normalized = normalize_language_code(self.code)
        if normalized != self.code:
            raise ValueError("LanguageProfile.code must already be normalized")
        if not self.label.strip() or not self.card_key.strip():
            raise ValueError("language label and card key are required")
        if self.direction not in {"ltr", "rtl"}:
            raise ValueError("language direction must be ltr or rtl")


class LanguageRegistry:
    """Small runtime-extensible registry with safe BCP47 variant fallback."""

    def __init__(self, profiles: Iterable[LanguageProfile] = ()):
        self._profiles: dict[str, LanguageProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: LanguageProfile, *, replace_existing: bool = False) -> None:
        code = normalize_language_code(profile.code)
        if code in self._profiles and not replace_existing:
            raise ValueError(f"language profile already registered: {code}")
        self._profiles[code] = profile

    @staticmethod
    def _variant_label(base: LanguageProfile, code: str) -> str:
        suffixes = code.split("-")[1:]
        if not suffixes:
            return base.label
        labels = [
            suffix.title() if len(suffix) == 4 and suffix.isalpha() else suffix.upper()
            for suffix in suffixes
        ]
        return f"{base.label} ({', '.join(labels)})"

    def resolve(self, value: object) -> LanguageProfile:
        code = normalize_language_code(value)
        exact = self._profiles.get(code)
        if exact is not None:
            return exact
        primary = code.split("-", 1)[0]
        base = self._profiles.get(primary)
        if base is not None:
            return replace(
                base,
                code=code,
                label=self._variant_label(base, code),
                card_key=code.replace("-", "_"),
                display_order=base.display_order + 1,
            )
        label = code.upper() if "-" not in code else code
        return LanguageProfile(
            code=code,
            label=label,
            card_key=code.replace("-", "_"),
        )

    def profiles(self) -> tuple[LanguageProfile, ...]:
        return tuple(
            sorted(
                self._profiles.values(),
                key=lambda item: (item.display_order, item.code),
            )
        )


LANGUAGES = LanguageRegistry(
    (
        LanguageProfile(
            "en", "English", "english", english_morphology=True,
            wordnet_specifier="omw-en:2.0", display_order=10,
        ),
        LanguageProfile(
            "ja", "Japanese", "japanese", reading_system="kana",
            translation_reading_required=True, wordnet_specifier="omw-ja:2.0",
            display_order=20,
        ),
        LanguageProfile(
            "zh", "Simplified Chinese", "chinese", reading_system="pinyin",
            translation_reading_required=True, wordnet_specifier="omw-cmn:2.0",
            display_order=30,
        ),
        LanguageProfile(
            "fr", "French", "french", wordnet_specifier="omw-fr:2.0",
            display_order=40,
        ),
        LanguageProfile(
            "ar", "Arabic", "arabic", direction="rtl",
            translation_reading_required=True, wordnet_specifier="omw-arb:2.0",
            display_order=50,
        ),
    )
)

DISPLAY_LANGUAGES = tuple(profile.code for profile in LANGUAGES.profiles())


def language_profile(value: object) -> LanguageProfile:
    return LANGUAGES.resolve(value)


def language_name(value: object) -> str:
    return language_profile(value).label


def supports_english_morphology(value: object) -> bool:
    return language_profile(value).english_morphology


def ordered_language_codes(
    source_language: object,
    languages: Iterable[object] = DISPLAY_LANGUAGES,
) -> tuple[str, ...]:
    source = normalize_language_code(source_language)
    requested = [normalize_language_code(value) for value in languages]
    return tuple(dict.fromkeys((source, *requested)))


def register_language(
    profile: LanguageProfile, *, replace_existing: bool = False
) -> None:
    LANGUAGES.register(profile, replace_existing=replace_existing)
