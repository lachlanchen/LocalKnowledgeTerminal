#!/usr/bin/env python3
"""Build a project-owned proof of the read-only Markdown index boundary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lkt.markdown import (
    MarkdownIndex,
    build_markdown_index,
    external_destination,
    read_sources,
)


DEFAULT_VAULT = ROOT / "examples/fixtures/markdown-vault"
DEFAULT_OUTPUT = ROOT / "examples/artifacts/markdown-vault-index.json"


def build_proof(vault: Path = DEFAULT_VAULT) -> dict[str, Any]:
    source_hashes = {
        source.relative_path: source.source_sha256 for source in read_sources(vault)
    }
    with tempfile.TemporaryDirectory(prefix="lkt-markdown-proof-") as temporary:
        database = Path(temporary) / "markdown.sqlite3"
        built = build_markdown_index(vault, database)
        index = MarkdownIndex(database)
        results = index.search("success choices", limit=4)
        chinese_results = index.search("成功有时会变成限制", limit=4)
        edges = index.edges()
    if source_hashes != {
        source.relative_path: source.source_sha256 for source in read_sources(vault)
    }:
        raise RuntimeError("fixture changed while its proof was built")
    return {
        "version": 1,
        "boundary": {
            "canonical_source": "project-owned Markdown fixture",
            "index": "disposable local SQLite FTS projection",
            "writes_to_vault": False,
            "automatic_concept_discovery": False,
            "semantic_or_vector_search": False,
            "whole_vault_question_answering": False,
        },
        "build": {
            key: value for key, value in built.items() if key != "database"
        },
        "source_hashes": source_hashes,
        "searches": {
            "english": results,
            "chinese": chinese_results,
        },
        "explicit_wikilink_edges": edges,
    }


def render_proof(vault: Path = DEFAULT_VAULT) -> bytes:
    return (
        json.dumps(build_proof(vault), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def write_proof(vault: Path, output: Path) -> Path:
    output = external_destination(vault, output, "Markdown proof output")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.building-", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(render_proof(vault))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        rendered = render_proof(args.vault)
        if not args.output.is_file() or args.output.read_bytes() != rendered:
            raise SystemExit("Markdown proof artifact is missing or stale")
        print(f"verified {args.output}")
        return 0
    output = write_proof(args.vault, args.output)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
