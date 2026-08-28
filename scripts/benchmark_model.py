#!/usr/bin/env python3
"""Run one bounded, reproducible local-model quality and speed probe."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MODEL_URL = os.environ.get(
    "LKT_LLM_URL", "http://127.0.0.1:8081/v1/chat/completions"
)
WEB_HEALTH_URL = os.environ.get(
    "LKT_HEALTH_URL", "http://127.0.0.1:8090/api/health"
)
PROMPT = """In no more than 120 words, make a precise vocabulary note for “ephemeral”.
Give its current English meaning, then an established Japanese equivalent with exact kana reading,
Chinese equivalent with tone-marked pinyin, French equivalent, and Modern Standard Arabic
equivalent with simple transliteration. Do not substitute the literal ancient meaning for the
current meaning. If unsure about an item, say uncertain instead of guessing. /no_think"""


def read_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected JSON response from {url}")
    return value


def service_memory() -> dict[str, int]:
    try:
        pid = int(
            subprocess.check_output(
                [
                    "systemctl",
                    "show",
                    "--property=MainPID",
                    "--value",
                    "lkt-llm.service",
                ],
                text=True,
                timeout=5,
            ).strip()
        )
        fields: dict[str, int] = {"pid": pid}
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "VmHWM", "VmSwap"}:
                fields[f"{key.lower()}_kib"] = int(value.strip().split()[0])
        return fields
    except (FileNotFoundError, OSError, ValueError, subprocess.SubprocessError):
        return {}


def main() -> None:
    health = read_json(WEB_HEALTH_URL)
    model = str((health.get("model") or {}).get("name") or "unknown")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful local language model benchmark. Answer directly, "
                    "preserve Unicode, and never invent a form when uncertain."
                ),
            },
            {"role": "user", "content": PROMPT},
        ],
        "temperature": 0.2,
        "top_p": 0.8,
        "top_k": 20,
        "max_tokens": 240,
        "stream": False,
    }
    started = time.monotonic()
    body = read_json(MODEL_URL, payload)
    wall_seconds = time.monotonic() - started
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
    try:
        response = str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("model response did not contain assistant content") from exc
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    completion_tokens = int(
        usage.get("completion_tokens") or timings.get("predicted_n") or 0
    )
    measured_rate = completion_tokens / wall_seconds if wall_seconds else 0.0
    result = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "model": model,
        "prompt_id": "multilingual-ephemeral-v1",
        "metrics": {
            "wall_seconds": round(wall_seconds, 2),
            "prompt_tokens": int(
                usage.get("prompt_tokens") or timings.get("prompt_n") or 0
            ),
            "completion_tokens": completion_tokens,
            "tokens_per_second": round(
                float(timings.get("predicted_per_second") or measured_rate), 2
            ),
        },
        "memory": service_memory(),
        "response": response,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

