from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


_LINE_BREAK_HYPHEN = re.compile(
    r"([A-Za-z\u00c0-\u024f]{5,})-[ \t]*\n[ \t]*"
    r"([a-z\u00df-\u024f]{2,})"
)
_PAGE_NUMBER = re.compile(r"(?:page|p\.)?[ \t]*\d+(?:[ \t]*/[ \t]*\d+)?", re.I)
_TEX_WRAPPER = re.compile(
    r"\\(?:boldsymbol|mathbf|mathit|mathrm|mathsf|mathtt|mbox|operatorname|"
    r"text|textbf|textit|textrm|underline|overline)[ \t]*\{([^{}]*)\}"
)
_TEX_FRACTION = re.compile(
    r"\\(?:dfrac|frac|tfrac)[ \t]*\{([^{}]*)\}[ \t]*\{([^{}]*)\}"
)
_TEX_SQRT = re.compile(r"\\sqrt[ \t]*\{([^{}]*)\}")
_TEX_SCRIPT = re.compile(r"[_^][ \t]*\{[ \t]*([^{}]*?)[ \t]*\}")
_TEX_ENVIRONMENT = re.compile(r"\\(?:begin|end)[ \t]*\{[^{}]*\}")
_TEX_COMMAND = re.compile(r"\\([A-Za-z]+)")
_CJK_GAP = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])\s+"
    r"(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
)

_TEX_SYMBOLS = {
    "alpha": "\u03b1",
    "beta": "\u03b2",
    "cdot": "\u00b7",
    "chi": "\u03c7",
    "delta": "\u03b4",
    "epsilon": "\u03b5",
    "eta": "\u03b7",
    "gamma": "\u03b3",
    "ge": "\u2265",
    "infty": "\u221e",
    "kappa": "\u03ba",
    "lambda": "\u03bb",
    "langle": "<",
    "le": "\u2264",
    "mu": "\u03bc",
    "neq": "\u2260",
    "nu": "\u03bd",
    "omega": "\u03c9",
    "phi": "\u03c6",
    "pi": "\u03c0",
    "pm": "\u00b1",
    "psi": "\u03c8",
    "rangle": ">",
    "rho": "\u03c1",
    "sigma": "\u03c3",
    "tau": "\u03c4",
    "theta": "\u03b8",
    "times": "\u00d7",
    "upsilon": "\u03c5",
    "xi": "\u03be",
    "zeta": "\u03b6",
}
_TEX_LAYOUT_COMMANDS = {
    "displaystyle",
    "hfill",
    "left",
    "limits",
    "mathstrut",
    "nolimits",
    "phantom",
    "qquad",
    "quad",
    "right",
    "scriptstyle",
    "textstyle",
    "vphantom",
}


def _looks_like_running_header(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    if _PAGE_NUMBER.fullmatch(value):
        return True
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and len(value.split()) <= 10 and all(
        not character.islower() for character in letters
    )


def _clean_extracted_lines(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    counts = Counter(line.casefold() for line in lines)
    kept: list[str] = []
    for index, line in enumerate(lines):
        key = line.casefold()
        boundary = index < 2 or index >= len(lines) - 2
        if boundary and _PAGE_NUMBER.fullmatch(line):
            continue
        if kept and kept[-1].casefold() == key:
            continue
        if counts[key] > 1 and _looks_like_running_header(line):
            continue
        kept.append(line)
    return " ".join(kept)


def _flatten_tex(value: str) -> str:
    had_tex = bool(re.search(r"\\(?:[A-Za-z]+|[()[\]])|[_^][ \t]*\{", value))
    text = value
    for delimiter in (r"\(", r"\)", r"\[", r"\]"):
        text = text.replace(delimiter, " ")
    text = _TEX_ENVIRONMENT.sub(" ", text)
    for _ in range(8):
        updated = _TEX_FRACTION.sub(r"\1/\2", text)
        updated = _TEX_SQRT.sub(r"sqrt(\1)", updated)
        updated = _TEX_WRAPPER.sub(r"\1", updated)
        updated = _TEX_SCRIPT.sub(r"\1", updated)
        if updated == text:
            break
        text = updated

    text = re.sub(r"\\([#$%&_{}])", r"\1", text)

    def replace_command(match: re.Match[str]) -> str:
        name = match.group(1)
        lowered = name.casefold()
        if lowered in _TEX_SYMBOLS:
            return f" {_TEX_SYMBOLS[lowered]} "
        if lowered in _TEX_LAYOUT_COMMANDS:
            return " "
        return f" {name} "

    text = _TEX_COMMAND.sub(replace_command, text)
    text = re.sub(r"\\[,;:!][ \t]*", " ", text)
    text = text.replace("~", " ")
    if text.count("$") >= 2 and text.count("$") % 2 == 0:
        text = text.replace("$", " ")
    if had_tex:
        text = text.translate(str.maketrans({"{": " ", "}": " "}))
        text = re.sub(r"(?<!\w)[_^](?!\w)", " ", text)
    return text


def normalize_display_excerpt(value: str) -> str:
    """Make extraction markup readable without changing the stored source text."""

    if not isinstance(value, str) or not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\u00ad", "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
    text = text.replace("\\\\", "\n")
    text = _clean_extracted_lines(text)
    text = _flatten_tex(text)
    text = _CJK_GAP.sub("", text)
    text = re.sub(r"\s+([,.;:!?%\)\]，。！？；：、])", r"\1", text)
    text = re.sub(r"([\(\[（])\s+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def card_evidence_values(raw_excerpt: str) -> dict[str, str]:
    raw = raw_excerpt if isinstance(raw_excerpt, str) else str(raw_excerpt or "")
    display = normalize_display_excerpt(raw)
    return {
        "raw_excerpt": raw,
        "display_excerpt": display,
        "excerpt": display,
    }


def normalize_card_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        return result
    evidence: list[Any] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            evidence.append(raw_item)
            continue
        item = dict(raw_item)
        raw_excerpt = item.get("raw_excerpt", item.get("excerpt", ""))
        item.update(card_evidence_values(raw_excerpt))
        evidence.append(item)
    result["evidence"] = evidence
    return result


def backfill_card_database(database: Path, *, status: str | None = "active") -> dict[str, int]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if status is None:
            rows = connection.execute("SELECT card_id, payload FROM cards").fetchall()
        else:
            rows = connection.execute(
                "SELECT card_id, payload FROM cards WHERE status = ?", (status,)
            ).fetchall()
        updated = 0
        evidence_items = 0
        normalized_items = 0
        skipped = 0
        for card_id, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                skipped += 1
                continue
            if not isinstance(payload, dict):
                skipped += 1
                continue
            original_evidence = payload.get("evidence")
            if isinstance(original_evidence, list):
                for item in original_evidence:
                    if not isinstance(item, dict):
                        continue
                    evidence_items += 1
                    raw_excerpt = item.get("raw_excerpt", item.get("excerpt", ""))
                    if normalize_display_excerpt(str(raw_excerpt or "")) != str(
                        raw_excerpt or ""
                    ):
                        normalized_items += 1
            normalized = normalize_card_payload(payload)
            if normalized == payload:
                continue
            connection.execute(
                "UPDATE cards SET payload = ? WHERE card_id = ?",
                (json.dumps(normalized, ensure_ascii=False), card_id),
            )
            updated += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "cards_scanned": len(rows),
        "cards_updated": updated,
        "evidence_items": evidence_items,
        "normalized_items": normalized_items,
        "cards_skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill clean display excerpts while preserving raw card evidence."
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--status", default="active")
    parser.add_argument("--all", action="store_true")
    arguments = parser.parse_args()
    result = backfill_card_database(
        arguments.database, status=None if arguments.all else arguments.status
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
