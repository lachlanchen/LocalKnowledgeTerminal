from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from typing import Any


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([，。！？；：、,.!?;:])")
_ESPEAK_VOICES = {"en": "en-us", "fr": "fr-fr", "ar": "ar"}


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


def chinese_ruby_tokens(text: str) -> list[dict[str, str]]:
    """Pair each Han character with tone-marked pinyin for HTML ruby rendering."""

    if not text or not _CJK_RE.search(text):
        return []
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return []

    readings = lazy_pinyin(text, style=Style.TONE, errors=lambda value: list(value))
    if len(readings) != len(text):
        return []
    tokens: list[dict[str, str]] = []
    for character, reading in zip(text, readings, strict=True):
        token = {"t": character}
        if _CJK_RE.fullmatch(character) and reading != character:
            token["r"] = reading
        tokens.append(token)
    return tokens


class EspeakPronouncer:
    """Small offline IPA adapter with a fixed language-to-voice policy."""

    def __init__(self, executable: str = ""):
        self.executable = executable or shutil.which("espeak-ng") or ""

    def pronounce(self, text: str, language: str) -> dict[str, Any]:
        voice = _ESPEAK_VOICES.get(language, "")
        if not self.executable or not voice:
            raise RuntimeError(f"eSpeak NG is unavailable for language {language!r}")
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
