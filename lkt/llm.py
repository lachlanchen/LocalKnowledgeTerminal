from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import Evidence


class ModelUnavailable(RuntimeError):
    pass


class CardModel(Protocol):
    model_name: str

    def generate(self, query: str, mode: str, evidence: list[Evidence]) -> dict[str, Any]:
        ...


def _evidence_context(evidence: list[Evidence]) -> str:
    blocks = []
    for index, item in enumerate(evidence, 1):
        pages = ", ".join(str(page) for page in item.pages) or "not applicable"
        translations = (
            json.dumps(item.translations, ensure_ascii=False)
            if item.translations
            else "not supplied"
        )
        blocks.append(
            f"SOURCE {index}\n"
            f"Book: {item.source_title}\n"
            f"Record ID: {item.entry_id}\n"
            f"Record kind: {item.kind}\n"
            f"Headword: {item.headword}\n"
            f"Section: {item.section or 'unknown'}\n"
            f"Date label: {item.date_label or 'unknown'}\n"
            f"Book pages: {pages}\n"
            f"Locator: {item.locator or 'not applicable'}\n"
            f"Authoritative source text: {item.excerpt}\n"
            f"Reviewed translations: {translations}"
        )
    return "\n\n".join(blocks)


SYSTEM_PROMPT = """You create compact, accurate learning cards for Local Knowledge Terminal.
Use the supplied book records as the only authority for quoted text, etymology, and historical claims.
If the excerpts do not support a detail, say it is not available; never invent a source or page.
Reviewed translations are authoritative and must not be rewritten. Other translations, pinyin,
explanations, and memory aids may be your own, but must not contradict the evidence.
Return exactly one JSON object, with no markdown and no commentary.

Required JSON shape:
{
  "title": "short title",
  "subtitle": "one-line orientation",
  "summary_en": "clear English definition or answer",
  "origin_story": "book-grounded etymology or explanation",
  "key_points": ["2 to 4 concise points"],
  "english": {"term": "", "pronunciation": "", "meaning": ""},
  "japanese": {"term": "", "reading": "hiragana/katakana reading", "meaning": "Japanese explanation"},
  "chinese": {"simplified": "", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "Chinese explanation"},
  "memory_hook": "short memorable connection",
  "related_terms": [{"term": "", "note": ""}]
}
Use Unicode characters directly. Keep the total response under 900 words."""


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model response did not contain a JSON object")


class LlamaCppClient:
    def __init__(self, url: str, model_name: str, timeout: int = 240):
        self.url = url
        self.model_name = model_name
        self.timeout = timeout

    def health(self, timeout: float = 2.0) -> bool:
        health_url = self.url.split("/v1/", 1)[0].rstrip("/") + "/health"
        try:
            with urllib.request.urlopen(health_url, timeout=timeout) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False

    def generate(self, query: str, mode: str, evidence: list[Evidence]) -> dict[str, Any]:
        instructions = {
            "word": "Create a WORD ORIGIN card grounded in the retrieved dictionary entry",
            "knowledge": "Create a WORD CARD grounded in the retrieved Word Origins entries",
            "answer": (
                "Create a BOOK ANSWER card. Preserve the selected answer and its reviewed "
                "translations exactly; add a thoughtful reflection, not a prediction"
            ),
            "question": (
                "Create a BOOK QUESTION card. Preserve the selected question and its reviewed "
                "translations exactly; add prompts for thoughtful reflection"
            ),
        }
        instruction = instructions.get(mode, "Create a grounded learning card")
        user_prompt = (
            f"{instruction} for this request: {query}\n\n"
            f"BOOK EVIDENCE\n{_evidence_context(evidence)}\n\n"
            "/no_think"
        )
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
            "max_tokens": 1400,
            "stream": False,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise ModelUnavailable(f"model HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelUnavailable(f"local model unavailable: {exc}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable("unexpected response from local model") from exc
        try:
            return _extract_json(str(content))
        except ValueError as exc:
            raise ModelUnavailable(str(exc)) from exc
