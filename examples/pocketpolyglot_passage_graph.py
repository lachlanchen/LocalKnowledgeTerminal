#!/usr/bin/env python3
"""Build a hand-reviewed passage-to-provenance-graph proof.

This example intentionally demonstrates a bounded transformation, not automatic
concept extraction or full-book ingestion.  It uses the original public
PocketPolyglot sample and Local Knowledge Terminal's real knowledge APIs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lkt.knowledge import KnowledgeStore


DEFAULT_FIXTURE = ROOT / "examples/fixtures/pocketpolyglot-sample.json"
DEFAULT_OUTPUT = ROOT / "examples/artifacts/pocketpolyglot-passage-graph.json"
SOURCE_REPOSITORY = "https://github.com/lachlanchen/PocketPolyglot"
SOURCE_COMMIT = "a437cbfa62aee4cb147bc6dea2188aea12791752"
SOURCE_PATH = "data/interlinear/sample.json"
SOURCE_SHA256 = "d544f1c97d353373b2ac86ef730c7e7a00f52f8ca79fbf34f7eaa039f5266f2f"
SOURCE_NOTE = (
    "Small original sample for validating the public PocketPolyglot layout "
    "and strict ruby/pinyin token rules."
)
PARAGRAPH_ID = "sample-001-p1"
PARAGRAPH_TEXT = "春天来了，风很轻。我在小径上慢慢走。花开得安静。"
UNIT_TEXTS = (
    "春天来了，风很轻。",
    "我在小径上慢慢走。",
    "花开得安静。",
)
LOCATOR_PREFIX = (
    "sections/spring/subsections/path/stories/sample-001/"
    "paragraphs/sample-001-p1"
)

# Each concept and relation below was selected by review from an exact unit.
CONCEPTS = (
    ("春天", "season", 0),
    ("风", "weather", 0),
    ("轻", "quality", 0),
    ("我", "speaker", 1),
    ("小径", "place", 1),
    ("花", "plant", 2),
    ("安静", "quality", 2),
)
RELATIONS = (
    ("__passage__", "春天", "mentions-concept", 0, "春天来了"),
    ("__passage__", "风", "mentions-concept", 0, "风很轻"),
    ("风", "轻", "has-quality", 0, "风很轻"),
    ("__passage__", "我", "mentions-concept", 1, "我在小径上慢慢走"),
    ("__passage__", "小径", "mentions-concept", 1, "我在小径上慢慢走"),
    ("我", "小径", "walks-on", 1, "我在小径上慢慢走"),
    ("__passage__", "花", "mentions-concept", 2, "花开得安静"),
    ("花", "安静", "blooms-with-quality", 2, "花开得安静"),
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _one_with_id(values: list[dict[str, Any]], identifier: str) -> dict[str, Any]:
    matches = [value for value in values if value.get("id") == identifier]
    if len(matches) != 1:
        raise ValueError(f"expected one {identifier!r} record, found {len(matches)}")
    return matches[0]


def load_source(fixture: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = fixture.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != SOURCE_SHA256:
        raise ValueError(
            f"fixture hash changed: expected {SOURCE_SHA256}, found {source_hash}"
        )
    data = json.loads(raw)
    if data.get("source", {}).get("note") != SOURCE_NOTE:
        raise ValueError("fixture no longer carries the reviewed original-sample note")

    section = _one_with_id(data.get("sections", []), "spring")
    subsection = _one_with_id(section.get("subsections", []), "path")
    story = _one_with_id(subsection.get("stories", []), "sample-001")
    paragraph = _one_with_id(story.get("paragraphs", []), PARAGRAPH_ID)
    units = paragraph.get("units", [])
    if paragraph.get("source_text") != PARAGRAPH_TEXT:
        raise ValueError("reviewed paragraph text changed")
    if tuple(unit.get("source_text") for unit in units) != UNIT_TEXTS:
        raise ValueError("reviewed unit text or order changed")
    if "".join(UNIT_TEXTS) != PARAGRAPH_TEXT:
        raise ValueError("reviewed units do not reconstruct the paragraph")
    for index, unit in enumerate(units):
        rebuilt = "".join(str(token.get("t", "")) for token in unit.get("zh", []))
        if rebuilt != UNIT_TEXTS[index]:
            raise ValueError(f"unit {index} tokens do not reconstruct its source text")
    return data, paragraph


def _canonical_graph(projection: dict[str, Any]) -> dict[str, Any]:
    """Remove volatile SQLite timestamps before hashing the public projection."""

    stable_edges = [
        {
            key: value
            for key, value in edge.items()
            if key not in {"created_at", "updated_at"}
        }
        for edge in projection["edges"]
    ]
    graph = {
        "subject_entity_id": projection["subject_entity_id"],
        "mode": projection["mode"],
        "limits": projection["limits"],
        "truncated": projection["truncated"],
        "nodes": projection["nodes"],
        "edges": stable_edges,
        "graph_revision": projection["graph_revision"],
    }
    graph["projection_hash"] = _canonical_sha256(graph)
    return graph


def build_proof(fixture: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    _, paragraph = load_source(fixture)
    units = paragraph["units"]

    with tempfile.TemporaryDirectory(prefix="lkt-passage-proof-") as temporary:
        store = KnowledgeStore(Path(temporary) / "knowledge.sqlite3")
        passage_id = store.upsert_content_item(
            "sentence", "zh", PARAGRAPH_TEXT, source_key=PARAGRAPH_ID
        )

        evidence_ids: list[str] = []
        for unit_index, unit in enumerate(units):
            evidence_id = store.add_evidence(
                "pocketpolyglot-public-sample",
                f"{PARAGRAPH_ID}-unit-{unit_index + 1}",
                source_hash=SOURCE_SHA256,
                locator=f"{LOCATOR_PREFIX}/units/{unit_index}",
                excerpt=unit["source_text"],
                payload={
                    "language": "zh",
                    "source_commit": SOURCE_COMMIT,
                    "source_note": SOURCE_NOTE,
                    "source_path": SOURCE_PATH,
                    "source_repository": SOURCE_REPOSITORY,
                    "unit_index": unit_index,
                },
            )
            evidence_ids.append(evidence_id)
            store.link_evidence(
                passage_id,
                evidence_id,
                claim="Exact unit within the reviewed source paragraph.",
                confidence=1.0,
            )

        entity_ids = {"__passage__": passage_id}
        for text, role, unit_index in CONCEPTS:
            entity_id = store.upsert_term(
                "zh",
                text,
                kind="concept",
                quality_score=1.0,
                payload={
                    "concept_role": role,
                    "review_status": "hand-reviewed",
                },
            )
            entity_ids[text] = entity_id
            store.link_evidence(
                entity_id,
                evidence_ids[unit_index],
                claim="Concept appears in this exact reviewed unit.",
                confidence=1.0,
            )

        for source, target, relation, unit_index, claim in RELATIONS:
            store.accept_relation_assertion(
                passage_id,
                entity_ids[source],
                entity_ids[target],
                relation,
                basis="reviewed",
                confidence=1.0,
                properties={
                    "claim_zh": claim,
                    "modes": ["passage"],
                    "review_status": "hand-reviewed",
                },
                evidence_ids=(evidence_ids[unit_index],),
            )

        graph = _canonical_graph(
            store.lexical_subgraph(
                passage_id,
                "passage",
                {"nodes": 16, "edges": 16, "depth": 4},
            )
        )
        referenced_evidence = sorted(
            {
                evidence_id
                for edge in graph["edges"]
                for evidence_id in edge["evidence_ids"]
            }
        )
        evidence = store.evidence_records(referenced_evidence)

    return {
        "schema_version": "1.0",
        "proof": {
            "description": (
                "Hand-reviewed transformation of one project-authored aligned "
                "passage into a small provenance-bearing concept graph."
            ),
            "not_claimed": [
                "automatic concept extraction",
                "full-book ingestion",
                "customer result",
                "translation accuracy benchmark",
            ],
            "review_status": "hand-reviewed",
        },
        "source": {
            "commit": SOURCE_COMMIT,
            "fixture_path": "examples/fixtures/pocketpolyglot-sample.json",
            "note": SOURCE_NOTE,
            "paragraph_id": PARAGRAPH_ID,
            "paragraph_text": PARAGRAPH_TEXT,
            "path": SOURCE_PATH,
            "repository": SOURCE_REPOSITORY,
            "sha256": SOURCE_SHA256,
            "unit_count": len(UNIT_TEXTS),
        },
        "graph": graph,
        "evidence": evidence,
    }


def validate_proof(proof: dict[str, Any]) -> None:
    graph = proof["graph"]
    evidence = {record["evidence_id"]: record for record in proof["evidence"]}
    if graph["truncated"]:
        raise ValueError("proof graph was unexpectedly truncated")
    if graph["projection_hash"] != _canonical_sha256(
        {key: value for key, value in graph.items() if key != "projection_hash"}
    ):
        raise ValueError("proof projection hash does not match its graph")
    for edge in graph["edges"]:
        if edge["basis"] != "reviewed" or not edge["evidence_ids"]:
            raise ValueError(f"edge {edge['id']} is not reviewed and evidenced")
        for evidence_id in edge["evidence_ids"]:
            if evidence_id not in evidence:
                raise ValueError(f"edge {edge['id']} has unresolved evidence")
    expected = {
        f"{LOCATOR_PREFIX}/units/{index}": excerpt
        for index, excerpt in enumerate(UNIT_TEXTS)
    }
    actual = {record["locator"]: record["excerpt"] for record in evidence.values()}
    if actual != expected:
        raise ValueError("evidence locators or excerpts changed")
    if any(record["source_hash"] != SOURCE_SHA256 for record in evidence.values()):
        raise ValueError("evidence is not pinned to the reviewed source hash")


def render_proof(fixture: Path = DEFAULT_FIXTURE) -> bytes:
    proof = build_proof(fixture)
    validate_proof(proof)
    return _json_bytes(proof)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that the committed artifact matches without writing it",
    )
    args = parser.parse_args()
    rendered = render_proof(args.fixture)
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != rendered:
            raise SystemExit(f"artifact is stale: {args.output}")
        print(f"ok: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered)
    proof = json.loads(rendered)
    print(f"wrote {args.output}")
    print(f"projection {proof['graph']['projection_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
