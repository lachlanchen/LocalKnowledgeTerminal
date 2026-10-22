from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from typing import Any


def is_arabic_script_text(value: str) -> bool:
    """Accept Arabic-script prose while rejecting mixed alphabetic leakage."""

    letters = [character for character in str(value) if character.isalpha()]
    return len(letters) >= 2 and all(
        "\u0600" <= character <= "\u06ff"
        or "\u0750" <= character <= "\u077f"
        or "\u08a0" <= character <= "\u08ff"
        for character in letters
    )


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([，。！？；：、,.!?;:])")
_ESPEAK_VOICES = {"en": "en-us", "fr": "fr-fr", "ar": "ar"}
_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")


def _language_code(value: str) -> str:
    code = str(value).strip().casefold().replace("_", "-")
    if not _LANGUAGE_CODE.fullmatch(code):
        raise ValueError(f"invalid language code: {value!r}")
    return code


def chinese_pinyin(text: str, fallback: str = "") -> str:
    """Return full tone-marked pinyin while retaining sentence punctuation.

    The import stays local so a development checkout can still inspect old cards
    without the pronunciation package. Production installation pins pypinyin.
    """

    if not text or not _CJK_RE.search(text):
        return fallback.strip()
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return fallback.strip()

    pieces = lazy_pinyin(text, style=Style.TONE, errors=lambda value: list(value))
    result = " ".join(part.strip() for part in pieces if part.strip())
    result = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", result)
    return re.sub(r"\s+", " ", result).strip()


def chinese_ruby_tokens(text: str, fallback: str = "") -> list[dict[str, str]]:
    """Pair each Han character with tone-marked pinyin for HTML ruby rendering."""

    if not text or not _CJK_RE.search(text):
        return []
    compound = (
        [{"t": text, "r": fallback.strip()}] if fallback.strip() else []
    )
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return compound

    readings = lazy_pinyin(text, style=Style.TONE, errors=lambda value: list(value))
    if len(readings) != len(text):
        return compound
    tokens: list[dict[str, str]] = []
    for character, reading in zip(text, readings, strict=True):
        token = {"t": character}
        if _CJK_RE.fullmatch(character) and reading != character:
            token["r"] = reading
        tokens.append(token)
    if any(
        _CJK_RE.search(token["t"]) and not token.get("r") for token in tokens
    ):
        return compound
    return tokens


class EspeakPronouncer:
    """Small offline IPA adapter with deterministic installed-voice selection."""

    def __init__(self, executable: str = ""):
        self.executable = executable or shutil.which("espeak-ng") or ""
        self._available_voice_codes: tuple[str, ...] | None = None

    def _available_voices(self) -> tuple[str, ...]:
        if self._available_voice_codes is not None:
            return self._available_voice_codes
        if not self.executable:
            raise RuntimeError("eSpeak NG is unavailable")
        result = subprocess.run(
            [self.executable, "--voices"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        voices: set[str] = set()
        for line in result.stdout.splitlines()[1:]:
            columns = line.split()
            if len(columns) < 2:
                continue
            try:
                voices.add(_language_code(columns[1]))
            except ValueError:
                continue
        if not voices:
            raise RuntimeError("eSpeak NG did not report any usable voices")
        self._available_voice_codes = tuple(sorted(voices))
        return self._available_voice_codes

    def _voice(self, language: str) -> tuple[str, str]:
        code = _language_code(language)
        base = code.split("-", 1)[0]
        available = self._available_voices()
        installed = set(available)
        preferred = tuple(
            dict.fromkeys(
                candidate
                for candidate in (
                    _ESPEAK_VOICES.get(code, ""),
                    code,
                    _ESPEAK_VOICES.get(base, ""),
                    base,
                )
                if candidate
            )
        )
        voice = next((candidate for candidate in preferred if candidate in installed), "")
        if not voice:
            voice = next(
                (candidate for candidate in available if candidate.startswith(f"{base}-")),
                "",
            )
        if not voice:
            raise RuntimeError(
                f"eSpeak NG has no installed voice for language {language!r}"
            )
        return code, voice

    def pronounce(self, text: str, language: str) -> dict[str, Any]:
        language, voice = self._voice(language)
        spoken_text = text
        normalization = ""
        if language == "ar":
            spoken_text = "".join(
                character
                for character in unicodedata.normalize("NFKD", text)
                if not unicodedata.combining(character)
            )
            normalization = "stripped-partial-diacritics"
        result = subprocess.run(
            [self.executable, "-q", "--ipa=3", "-v", voice, spoken_text],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        reading = re.sub(r"\s+", " ", result.stdout).strip()
        if not reading or len(reading) > 200:
            raise ValueError("eSpeak NG returned an empty or oversized IPA reading")
        version = subprocess.run(
            [self.executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        ).stdout.splitlines()[0].strip()
        return {
            "reading": reading,
            "system": "ipa",
            "dialect": voice,
            "segments": [
                {
                    "grapheme": text,
                    "phoneme": reading,
                    "color_key": "p0",
                    "features": {"engine": "espeak-ng", "voice": voice},
                }
            ],
            "source": {
                "engine": "espeak-ng",
                "version": version,
                "voice": voice,
                **({"input_normalization": normalization} if normalization else {}),
            },
        }
