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
    {"id": "short-unique-id", "parent": "id of the later form this feeds, or empty only for the modern root", "stage": "language or era", "form": "historical form or morpheme", "meaning": "at most 8 words", "basis": "book or model"}
  ],
  "key_points": ["at most 2 concise points"],
  "english": {"term": "", "pronunciation": "", "meaning": ""},
  "japanese": {"term": "established equivalent", "reading": "exact hiragana or katakana", "meaning": "short meaning written in Japanese", "ruby_tokens": [{"t": "kanji or kana segment", "r": "exact kana reading; empty for plain kana"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"},
  "memory_hook": "short memorable connection",
  "related_terms": []
}
Make origin_graph a directed ancestry graph of 3 to 7 nodes like a compact dictionary etymology
tree. Put the modern English word first as the single root with an empty parent. Its ancestors or
component morphemes point to the later descendant using parent, so two roots/components may branch
into one word. For a compound, use exactly this topology: modern-word parent "";
earlier-compound parent modern-word; component-a parent earlier-compound; component-b parent
earlier-compound. Components are siblings and must never parent one another unless one is actually
derived from the other. Do not force a linear timeline when the word has multiple components.
Before returning, check that every parent is the later form receiving that node. Use Unicode
directly and established Japanese/Chinese equivalents instead of phonetic imitations. Everything
must fit one screen. Japanese ruby token text must concatenate exactly to japanese.term. Keep the
response under 360 words. /no_think"""


WORD_CARD_PROMPT = """You are the independent multilingual Word Card engine in Local Knowledge
Terminal. Use the Word Origins excerpts as retrieved reference, then make one compact memory card.
The core learning object is the established equivalent in English, Japanese, Chinese, French, and
Arabic. Never invent a quotation, source, or page. Return exactly one JSON object with no markdown.

Required JSON shape:
{
  "title": "the English word",
  "subtitle": "one vivid orientation line",
  "summary_en": "one concise modern definition, not the literal ancient origin",
  "origin_story": "one short useful note grounded in the excerpts",
  "key_points": ["at most 2 concise points"],
  "english": {"term": "", "pronunciation": "IPA", "meaning": "short modern English meaning"},
  "japanese": {"term": "established equivalent", "reading": "exact kana reading", "meaning": "short meaning written in Japanese", "ruby_tokens": [{"t": "kanji or kana segment", "r": "exact kana reading; empty for plain kana"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short meaning written in Chinese"},
  "french": {"term": "established equivalent", "pronunciation": "IPA if known", "meaning": "short meaning written in French"},
  "arabic": {"term": "established modern Arabic equivalent", "reading": "simple transliteration", "meaning": "short meaning written in Arabic"},
  "memory_hook": "one memorable cross-language connection",
  "related_terms": []
}
All five term fields must express the same current everyday sense, not merely the literal ancient
root. Prefer common lexical equivalents over transliterations. Japanese ruby token text must
concatenate exactly to japanese.term. If you are not confident in a French or Arabic equivalent,
return its object with empty strings instead of guessing. Before returning, verify each reading
against its term and keep every meaning in its requested language. Use Unicode directly. Keep every
meaning to one short phrase and the whole response under 300 words so it fits one screen. /no_think"""


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


def _text_schema(max_length: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_RUBY_SCHEMA = {
    "type": "array",
    "maxItems": 24,
    "items": _strict_object({"t": _text_schema(16), "r": _text_schema(32)}),
}
_ENGLISH_SCHEMA = _strict_object(
    {
        "term": _text_schema(100),
        "pronunciation": _text_schema(100),
        "meaning": _text_schema(180),
    }
)
_JAPANESE_SCHEMA = _strict_object(
    {
        "term": _text_schema(100),
        "reading": _text_schema(160),
        "meaning": _text_schema(160),
        "ruby_tokens": _RUBY_SCHEMA,
    }
)
_PLAIN_JAPANESE_SCHEMA = _strict_object(
    {
        "term": _text_schema(3000),
        "reading": _text_schema(160),
        "meaning": _text_schema(160),
    }
)
_CHINESE_SCHEMA = _strict_object(
    {
        "simplified": _text_schema(3000),
        "traditional": _text_schema(3000),
        "pinyin": _text_schema(4000),
        "meaning": _text_schema(160),
    }
)
_KEY_POINTS_SCHEMA = {
    "type": "array",
    "maxItems": 2,
    "items": _text_schema(180),
}
_RELATED_SCHEMA = {
    "type": "array",
    "maxItems": 4,
    "items": _strict_object(
        {"term": _text_schema(100), "note": _text_schema(180)}
    ),
}


def _base_card_schema(japanese: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _text_schema(120),
        "subtitle": _text_schema(180),
        "summary_en": _text_schema(300),
        "origin_story": _text_schema(640),
        "key_points": _KEY_POINTS_SCHEMA,
        "english": _ENGLISH_SCHEMA,
        "japanese": japanese,
        "chinese": _CHINESE_SCHEMA,
        "memory_hook": _text_schema(300),
        "related_terms": _RELATED_SCHEMA,
    }


_ORIGIN_NODE_SCHEMA = _strict_object(
    {
        "id": _text_schema(48),
        "parent": _text_schema(48),
        "stage": _text_schema(80),
        "form": _text_schema(80),
        "meaning": _text_schema(100),
        "basis": {"type": "string", "enum": ["book", "model"]},
    }
)
_ALTERNATE_LANGUAGE_SCHEMA = _strict_object(
    {
        "term": _text_schema(100),
        "pronunciation": _text_schema(100),
        "meaning": _text_schema(160),
    }
)
_ARABIC_SCHEMA = _strict_object(
    {
        "term": _text_schema(100),
        "reading": _text_schema(160),
        "meaning": _text_schema(160),
    }
)


CARD_JSON_SCHEMAS = {
    "word": _strict_object(
        {
            **_base_card_schema(_JAPANESE_SCHEMA),
            "origin_graph": {
                "type": "array",
                "minItems": 2,
                "maxItems": 7,
                "items": _ORIGIN_NODE_SCHEMA,
            },
        }
    ),
    "knowledge": _strict_object(
        {
            **_base_card_schema(_JAPANESE_SCHEMA),
            "french": _ALTERNATE_LANGUAGE_SCHEMA,
            "arabic": _ARABIC_SCHEMA,
        }
    ),
    "answer": _strict_object(_base_card_schema(_PLAIN_JAPANESE_SCHEMA)),
    "question": _strict_object(_base_card_schema(_PLAIN_JAPANESE_SCHEMA)),
}


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
        token_budgets = {"word": 480, "knowledge": 420, "answer": 320, "question": 320}
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
            "response_format": {
                "type": "json_object",
                "schema": CARD_JSON_SCHEMAS[mode],
            },
        }
        for attempt in range(2):
            body, _elapsed = self._request(payload)
            content = self._content(body)
            try:
                return _extract_json(str(content))
            except ValueError as exc:
                if attempt:
                    raise ModelUnavailable(
                        "model response was not valid JSON after one repair attempt"
                    ) from exc
                payload = {
                    **payload,
                    "temperature": 0.0,
                    "presence_penalty": 0.0,
                    "messages": [
                        *payload["messages"],
                        {"role": "assistant", "content": str(content)[:6000]},
                        {
                            "role": "user",
                            "content": (
                                "Repair the previous response. Return exactly one valid JSON "
                                "object matching the required shape, with no markdown. /no_think"
                            ),
                        },
                    ],
                }
        raise AssertionError("unreachable")
