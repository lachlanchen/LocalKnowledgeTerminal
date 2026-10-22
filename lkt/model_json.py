"""Recover JSON syntax without generating linguistic content or evidence."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Iterator


class AtomicJsonOutputError(ValueError):
    """A parse failure with bounded model replies for local diagnosis."""

    def __init__(self, message: str, *, model: str, failures: list[dict[str, Any]]):
        super().__init__(message)
        self.model = model
        self.failures = tuple(dict(item) for item in failures[-2:])


def _objects(text: str) -> Iterator[str]:
    start = -1
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start, depth = index, 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]
                start = -1
    if start >= 0:
        yield text[start:]


def _close_containers(candidate: str) -> str:
    stack: list[str] = []
    output: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(candidate):
        if quote:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                raise ValueError("mismatched JSON containers")
        elif char == ",":
            remaining = candidate[index + 1:].lstrip()
            if not remaining or remaining[0] in "}]":
                continue
        output.append(char)
    if quote:
        raise ValueError("incomplete JSON string")
    return "".join(output).rstrip() + "".join(reversed(stack))


def _repair_evidence_arrays(candidate: str) -> str:
    """Supply omitted list brackets, without changing or inventing source IDs.

    Small local models sometimes emit ``"evidence_ids": "id1", "id2"``.
    Restrict this repair to the contract's evidence list, not arbitrary fields
    where an orphan string might instead mean a missing key or colon.
    """
    string = r'"(?:[^"\\]|\\.)*"'
    pattern = (
        rf'("evidence_ids"\s*:\s*)'
        rf'({string}(?:\s*,\s*{string})+)'
        rf'(?=\s*[,}}])'
    )
    return re.sub(pattern, lambda match: f"{match[1]}[{match[2]}]", candidate)


def parse_model_json(text: str) -> tuple[dict[str, Any], str]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Do not mistake an unfinished reasoning block for the final answer.
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|python)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    for candidate in _objects(cleaned):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value, "none"
        except (ValueError, RecursionError):
            pass
        try:
            repaired = _close_containers(candidate)
            try:
                value = json.loads(repaired)
                method = "container-repair"
            except ValueError:
                try:
                    value = json.loads(_repair_evidence_arrays(repaired))
                    method = "evidence-array-repair"
                except ValueError:
                    value = ast.literal_eval(repaired)
                    method = "literal-repair"
            if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
                continue
            # Restrict literal recovery to actual JSON types, never executable code.
            value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
            return value, method
        except (ValueError, SyntaxError, TypeError, RecursionError):
            continue
    raise ValueError("model response did not contain a JSON object")
