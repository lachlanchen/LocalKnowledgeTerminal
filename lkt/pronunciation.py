from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([，。！？；：、,.!?;:])")


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
