from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


_PREFIX_MODES = {
    "ask": "chat",
    "chat": "chat",
    "answer": "answer",
    "question": "question",
    "word": "knowledge",
    "card": "knowledge",
    "origin": "word",
    "etymology": "word",
    "root": "root",
    "affix": "affix",
    "prefix": "affix",
    "suffix": "affix",
}
_PREFIX_PATTERN = re.compile(
    rf"^(?P<prefix>{'|'.join(sorted(_PREFIX_MODES, key=len, reverse=True))})\s*:\s*(?P<query>.*)$",
    flags=re.IGNORECASE,
)
_ENGLISH_WORD = re.compile(r"^[A-Za-z]+(?:['’-][A-Za-z]+)*$")


@dataclass(frozen=True)
class IntentRoute:
    mode: str
    query: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def route_intent(value: Any) -> IntentRoute:
    """Route one ambient-terminal inquiry without consulting a language model."""

    query = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    if not query:
        raise ValueError("enter a question or word")
    if len(query) > 2000:
        raise ValueError("inquiry is too long")

    explicit = _PREFIX_PATTERN.fullmatch(query)
    if explicit:
        prefix = explicit.group("prefix").casefold()
        routed_query = explicit.group("query").strip()
        if not routed_query:
            raise ValueError(f"enter text after {prefix}:")
        mode = _PREFIX_MODES[prefix]
        if mode != "chat" and len(routed_query) > 240:
            raise ValueError("card query is too long")
        return IntentRoute(mode, routed_query, f"explicit-{prefix}")

    if _ENGLISH_WORD.fullmatch(query):
        return IntentRoute("knowledge", query, "single-english-word")
    return IntentRoute("chat", query, "general-inquiry")
