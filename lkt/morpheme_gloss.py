"""Optional morphology glosses: retain structure while meanings are refined."""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any


MORPHEME_GLOSS_POLICY = "optional-morpheme-gloss-v1"

_PLACEHOLDER = re.compile(
    r"(?:unknown|uncertain|unspecified|unavailable|none|null|n/?a|tbd|"
    r"not (?:known|available|specified|provided)|"
    r"(?:meaning|definition|gloss)(?: is|:)? (?:unknown|uncertain|unavailable|pending)|"
    r"(?:the )?(?:basic|base|root|original|whole-word|free) form"
    r"(?: of (?:the |a )?(?:word|term|verb|noun|adjective|morpheme)\b.*)?)",
    re.IGNORECASE,
)


def prepare_morpheme_gloss(proposed: Any, cleaned: str) -> dict[str, Any]:
    """Punctuation is text, not a reason to reject an otherwise useful split."""
    text = str(cleaned or "").strip()
    string_input = isinstance(proposed, str) or (
        isinstance(proposed, (list, tuple))
        and bool(proposed) and all(isinstance(item, str) for item in proposed)
    )
    normalized = unicodedata.normalize("NFKC", text).casefold().strip(" .:;")
    raw_values = ([proposed] if isinstance(proposed, str) else proposed) if string_input else []
    raw_texts = [
        unicodedata.normalize("NFKC", value).casefold().strip(" \t\r\n.:;")
        for value in raw_values if value.strip()
    ]
    raw_placeholder = bool(raw_texts) and all(
        _PLACEHOLDER.fullmatch(value) for value in raw_texts
    )
    issue = ""
    if not string_input:
        issue = "missing-or-nontext-gloss"
    elif raw_placeholder or _PLACEHOLDER.fullmatch(normalized):
        issue = "placeholder-gloss"
    elif not text:
        issue = "missing-or-nontext-gloss"
    elif (
        not any(character.isalpha() for character in text)
        or any(
            character.isalpha() and "LATIN" not in unicodedata.name(character, "")
            for character in text
        )
    ):
        issue = "english-gloss-needs-refinement"
    elif (
        "\ufffd" in text or any(character in "<>{}[]\\\\" for character in text)
        or any(unicodedata.category(character).startswith("C") for character in text)
    ):
        issue = "damaged-or-structured-gloss"
    if issue:
        return {
            "meaning": "", "meaning_pending": True,
            "meaning_issue": issue, "meaning_proposal": deepcopy(proposed),
        }
    return {"meaning": text, "meaning_pending": False}
