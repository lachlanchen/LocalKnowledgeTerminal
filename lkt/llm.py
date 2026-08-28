from __future__ import annotations

import json
import re
import time
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


WORD_ORIGIN_PROMPT = """You are the independent Word Origin engine in Local Knowledge Terminal.
Create a visually structured etymology from BOOK EVIDENCE plus your own reliable linguistic
knowledge. The book is the anchor. Never invent a quotation, record, or page. A graph node whose
claim is directly supported by the supplied excerpt must use basis "book"; a useful established
detail added from your own knowledge must use basis "model". Prefer accuracy over extra detail.
Return exactly one compact JSON object with no markdown or commentary.

Required JSON shape:
{
  "title": "short title, no more than 8 words",
  "subtitle": "one short line of orientation",
  "summary_en": "clear modern definition in one sentence",
  "origin_story": "one concise synthesis of the linguistic journey",
  "origin_graph": [
    {"stage": "language or era", "form": "historical form", "meaning": "at most 8 words", "basis": "book or model"}
  ],
  "key_points": ["at most 2 concise points"],
  "english": {"term": "", "pronunciation": "", "meaning": ""},
  "japanese": {"term": "established equivalent", "reading": "hiragana or katakana", "meaning": "short Japanese meaning"},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"},
  "memory_hook": "short memorable connection",
  "related_terms": []
}
Make origin_graph a chronological path of 3 to 5 non-overlapping stages, earliest first and
Modern English last. Use Unicode directly and established Japanese/Chinese equivalents instead
of phonetic imitations. Everything must fit one screen. Keep the response under 360 words. /no_think"""


WORD_CARD_PROMPT = """You are the independent multilingual Word Card engine in Local Knowledge
Terminal. Use the Word Origins excerpts as retrieved reference, then make one compact memory card.
The core learning object is the established equivalent in English, Japanese, Chinese, French, and
Arabic. Never invent a quotation, source, or page. Return exactly one JSON object with no markdown.

Required JSON shape:
{
  "title": "the English word",
  "subtitle": "one vivid orientation line",
  "summary_en": "one concise definition",
  "origin_story": "one short useful note grounded in the excerpts",
  "key_points": ["at most 2 concise points"],
  "english": {"term": "", "pronunciation": "IPA", "meaning": "short meaning"},
  "japanese": {"term": "established equivalent", "reading": "hiragana or katakana", "meaning": "short Japanese meaning"},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"},
  "french": {"term": "established equivalent", "pronunciation": "IPA if known", "meaning": "short French meaning"},
  "arabic": {"term": "established equivalent", "reading": "simple transliteration", "meaning": "short Arabic meaning"},
  "memory_hook": "one memorable cross-language connection",
  "related_terms": []
}
Prefer lexical equivalents over transliterations. Use Unicode directly. Keep every meaning to one
short phrase and the whole response under 300 words so it fits one screen. /no_think"""


ANSWER_PROMPT = """You are the independent Book Answer engine in Local Knowledge Terminal.
The selected answer and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. Add only one concise reflection. Return exactly one JSON object
with no markdown or commentary.

Required JSON shape:
{
  "title": "2 to 5 word title",
  "subtitle": "one short orientation line",
  "summary_en": "one concise sentence",
  "origin_story": "one concise reflection sentence",
  "key_points": ["at most 2 short points"],
  "english": {"term": "", "pronunciation": "", "meaning": "one short meaning"},
  "japanese": {"term": "", "reading": "", "meaning": "one short Japanese meaning"},
  "chinese": {
    "simplified": "",
    "traditional": "full traditional Chinese source sentence",
    "pinyin": "pinyin for the entire Chinese source sentence, with tone marks",
    "meaning": "one short Chinese meaning"
  },
  "memory_hook": "one memorable line",
  "related_terms": []
}
Use Unicode directly. Keep the entire response under 200 words. /no_think"""


QUESTION_PROMPT = """You are the independent Book Question engine in Local Knowledge Terminal.
The selected question and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. Add only one concise reflection prompt. Return exactly one JSON
object with no markdown or commentary.

Required JSON shape:
{
  "title": "2 to 5 word title",
  "subtitle": "one short orientation line",
  "summary_en": "one concise sentence",
  "origin_story": "one concise reflection sentence",
  "key_points": ["at most 2 short prompts"],
  "english": {"term": "", "pronunciation": "", "meaning": "one short meaning"},
  "japanese": {"term": "", "reading": "", "meaning": "one short Japanese meaning"},
  "chinese": {"simplified": "", "traditional": "", "pinyin": "full tone-marked pinyin", "meaning": "one short Chinese meaning"},
  "memory_hook": "one memorable line",
  "related_terms": []
}
Use Unicode directly. Keep the entire response under 200 words. /no_think"""


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

    def _request(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise ModelUnavailable(f"model HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelUnavailable(f"local model unavailable: {exc}") from exc
        if not isinstance(body, dict):
            raise ModelUnavailable("unexpected response from local model")
        return body, time.monotonic() - started

    @staticmethod
    def _content(body: dict[str, Any]) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable("unexpected response from local model") from exc
        return str(content)

    def chat(
        self, messages: list[dict[str, str]], context: str = ""
    ) -> dict[str, Any]:
        system_content = (
            "You are the local Qwen model inside Local Knowledge Terminal. "
            "Answer clearly and directly in the language used by the user. "
            "This is raw chat, so never imply that an answer is book-cited."
        )
        if context:
            system_content += (
                " The user is discussing the saved card below. Use its retrieved "
                "source excerpts as the only authority for historical or book claims.\n\n"
                f"CURRENT CARD\n{context}"
            )
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_content,
                },
                *messages[:-1],
                {
                    **messages[-1],
                    "content": f"{messages[-1]['content']}\n\n/no_think",
                },
            ],
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.2,
            "max_tokens": 640,
            "stream": False,
        }
        body, elapsed = self._request(payload)
        content = re.sub(
            r"<think>.*?</think>", "", self._content(body), flags=re.DOTALL
        ).strip()
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
        completion_tokens = int(usage.get("completion_tokens") or timings.get("predicted_n") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0)
        measured_rate = completion_tokens / elapsed if elapsed > 0 else 0.0
        generation_rate = float(timings.get("predicted_per_second") or measured_rate)
        return {
            "message": content,
            "model": self.model_name,
            "grounded": False,
            "contextual": bool(context),
            "metrics": {
                "elapsed_seconds": round(elapsed, 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tokens_per_second": round(generation_rate, 2),
            },
        }

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
        prompts = {
            "word": WORD_ORIGIN_PROMPT,
            "knowledge": WORD_CARD_PROMPT,
            "answer": ANSWER_PROMPT,
            "question": QUESTION_PROMPT,
        }
        token_budgets = {"word": 820, "knowledge": 760, "answer": 520, "question": 520}
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": prompts[mode],
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
            "max_tokens": token_budgets[mode],
            "stream": False,
        }
        body, _elapsed = self._request(payload)
        content = self._content(body)
        try:
            return _extract_json(str(content))
        except ValueError as exc:
            raise ModelUnavailable(str(exc)) from exc
