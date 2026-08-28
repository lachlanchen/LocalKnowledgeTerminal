from __future__ import annotations

import json
from typing import Protocol

from .models import Card


class OutputUnavailable(RuntimeError):
    pass


class CardOutput(Protocol):
    """Stable boundary for GUI, e-ink, audio, and future renderers."""

    name: str
    media_type: str

    def render(self, card: Card) -> bytes:
        ...


class JsonOutput:
    name = "json"
    media_type = "application/json; charset=utf-8"

    def render(self, card: Card) -> bytes:
        return json.dumps(card.to_dict(), ensure_ascii=False).encode("utf-8")


class EinkOutput:
    name = "eink"
    media_type = "image/png"

    def render(self, card: Card) -> bytes:
        raise OutputUnavailable(
            "e-ink adapter is reserved until the panel model and color profile are known"
        )


class AudioOutput:
    name = "audio"
    media_type = "audio/wav"

    def render(self, card: Card) -> bytes:
        raise OutputUnavailable(
            "audio adapter is reserved; the card schema already exposes pronunciation text"
        )
