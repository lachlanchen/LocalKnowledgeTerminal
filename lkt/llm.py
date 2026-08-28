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
Return exactly one compact JSON object with no markdown or commentary. Fill every required field
with real content; never return a blank template or placeholder.

Required JSON shape:
{
  "title": "the modern English word",
  "summary_en": "clear modern definition in one sentence",
  "origin_graph": [
    {"id": "short-unique-id", "parent": "id of the later form this feeds, or empty only for the modern root", "stage": "language or era", "form": "historical form or morpheme", "meaning": "at most 8 words", "basis": "book or model"}
  ],
  "english": {"term": "", "pronunciation": "", "meaning": ""},
  "japanese": {"term": "established equivalent", "reading": "exact hiragana or katakana", "meaning": "short meaning written in Japanese", "ruby_tokens": [{"t": "kanji or kana segment", "r": "exact kana reading; empty for plain kana"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"}
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
Fill every required English, Japanese, and Chinese field with real content; never return a blank
template or placeholder.

Required JSON shape:
{
  "title": "the English word",
  "english": {"term": "", "pronunciation": "IPA", "meaning": "short modern English meaning"},
  "japanese": {"term": "established equivalent", "reading": "exact kana reading", "meaning": "short meaning written in Japanese", "ruby_tokens": [{"t": "kanji or kana segment", "r": "exact kana reading; empty for plain kana"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short meaning written in Chinese"},
  "french": {"term": "established equivalent", "pronunciation": "IPA if known", "meaning": "short meaning written in French"},
  "arabic": {"term": "established modern Arabic equivalent", "reading": "simple transliteration", "meaning": "short meaning written in Arabic"}
}
All five term fields must express the same current everyday sense, not merely the literal ancient
root. Prefer common lexical equivalents over transliterations. Japanese ruby token text must
concatenate exactly to japanese.term. If you are not confident in a French or Arabic equivalent,
return its object with empty strings instead of guessing. Before returning, verify each reading
against its term and keep every meaning in its requested language. Use Unicode directly. Keep every
meaning to one short phrase and the whole response under 300 words so it fits one screen. /no_think"""


ANSWER_PROMPT = """You are the independent Book Answer engine in Local Knowledge Terminal.
The selected answer and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. The application attaches those translations itself. Return
exactly one compact JSON object with no markdown or commentary:

{"title": "2 to 5 word title", "origin_story": "one concise reflection sentence"}
Use Unicode directly. Keep the response under 40 words. /no_think"""


QUESTION_PROMPT = """You are the independent Book Question engine in Local Knowledge Terminal.
The selected question and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. The application attaches those translations itself. Return
exactly one compact JSON object with no markdown or commentary:

{"title": "2 to 5 word title", "origin_story": "one concise reflection prompt"}
Use Unicode directly. Keep the response under 40 words. /no_think"""


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


def _nonempty_path(value: dict[str, Any], *path: str) -> bool:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return isinstance(current, str) and bool(current.strip())


def _validate_card_draft(value: dict[str, Any], mode: str) -> None:
    required_paths = {
        "word": (
            ("title",),
            ("summary_en",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("japanese", "meaning"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
            ("chinese", "meaning"),
        ),
        "knowledge": (
            ("title",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("japanese", "meaning"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
            ("chinese", "meaning"),
        ),
        "answer": (("title",), ("origin_story",)),
        "question": (("title",), ("origin_story",)),
    }
    missing = [
        ".".join(path)
        for path in required_paths[mode]
        if not _nonempty_path(value, *path)
    ]
    if mode == "word":
        graph = value.get("origin_graph")
        if not isinstance(graph, list) or len(graph) < 3:
            missing.append("origin_graph[3+]")
        elif any(
            not isinstance(node, dict)
            or any(not str(node.get(key, "")).strip() for key in ("id", "stage", "form", "meaning"))
            for node in graph
        ):
            missing.append("origin_graph.complete_nodes")
    if missing:
        raise ValueError(f"model returned incomplete card fields: {', '.join(missing)}")


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
        token_budgets = {"word": 380, "knowledge": 300, "answer": 140, "question": 140}
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
        for attempt in range(2):
            body, _elapsed = self._request(payload)
            content = self._content(body)
            try:
                draft = _extract_json(str(content))
                _validate_card_draft(draft, mode)
                return draft
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
                                "Repair the previous response. Fill every required field with real, "
                                "non-empty content and return exactly one valid JSON object matching "
                                "the required shape, with no markdown. /no_think"
                            ),
                        },
                    ],
                }
        raise AssertionError("unreachable")
