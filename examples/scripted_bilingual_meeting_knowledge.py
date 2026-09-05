#!/usr/bin/env python3
"""Build a reviewed, project-owned bilingual meeting knowledge example.

The source is an authored transcript with scripted timestamps.  This example
does not run ASR, diarization, or automatic extraction and is not a customer
deployment or an accuracy benchmark.
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


DEFAULT_FIXTURE = ROOT / "examples/fixtures/scripted-bilingual-meeting.json"
DEFAULT_OUTPUT = ROOT / "examples/artifacts/scripted-bilingual-meeting-knowledge.json"
SOURCE_SHA256 = "4087a4626f719379ff78971865c9a5e931c1024aea8a24d77116f7f54f956d74"
CORPUS_ID = "lkt-project-owned-scripted-meeting"
KNOWLEDGE_TYPES = (
    ("customer-requirement", "customer requirement"),
    ("market-signal", "market signal"),
    ("product-suggestion", "product suggestion"),
    ("technical-issue", "technical issue"),
    ("decision-rationale", "decision/rationale"),
    ("risk", "risk"),
    ("item-requiring-verification", "item requiring verification"),
    ("new-opportunity", "new opportunity"),
    ("competitor-information", "competitor information"),
    ("commitment-action", "commitment/action"),
)
TYPE_LABELS = dict(KNOWLEDGE_TYPES)
NOT_CLAIMED = (
    "automatic speech recognition or diarization",
    "automatic knowledge extraction",
    "ASR, diarization, extraction, or translation accuracy benchmark",
    "customer deployment or customer result",
    "production readiness or enterprise scale",
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


def load_fixture(fixture: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    raw = fixture.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != SOURCE_SHA256:
        raise ValueError(
            f"fixture hash changed: expected {SOURCE_SHA256}, found {source_hash}"
        )
    data = json.loads(raw)
    meeting = data.get("meeting", {})
    utterances = data.get("utterances", [])
    if meeting.get("scripted") is not True or meeting.get("recording_exists") is not False:
        raise ValueError("fixture must remain a scripted example without a recording")
    if meeting.get("review_status") != "manually reviewed":
        raise ValueError("fixture must retain its manual-review label")
    if len(utterances) != len(KNOWLEDGE_TYPES):
        raise ValueError("fixture must contain exactly ten knowledge utterances")

    observed_types: list[str] = []
    previous_end = -1
    seen_ids: set[str] = set()
    for utterance in utterances:
        utterance_id = str(utterance.get("utterance_id", ""))
        text = str(utterance.get("text", ""))
        language = str(utterance.get("language", ""))
        start_ms = utterance.get("start_ms")
        end_ms = utterance.get("end_ms")
        unit = utterance.get("knowledge_unit", {})
        unit_id = str(unit.get("unit_id", ""))
        unit_type = str(unit.get("type", ""))
        if not utterance_id or not unit_id or utterance_id in seen_ids or unit_id in seen_ids:
            raise ValueError("utterance and unit IDs must be present and unique")
        seen_ids.update((utterance_id, unit_id))
        if not text or not str(utterance.get("speaker", "")):
            raise ValueError(f"{utterance_id} is missing text or speaker")
        if language not in {"en", "zh"}:
            raise ValueError(f"{utterance_id} has an unsupported fixture language")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int):
            raise ValueError(f"{utterance_id} timestamps must be integer milliseconds")
        if start_ms < 0 or start_ms >= end_ms or start_ms < previous_end:
            raise ValueError(f"{utterance_id} timestamps overlap or run backwards")
        if not str(unit.get("summary", "")):
            raise ValueError(f"{unit_id} has no reviewed summary")
        if unit_type not in TYPE_LABELS:
            raise ValueError(f"{unit_id} has an unknown knowledge type")
        observed_types.append(unit_type)
        previous_end = end_ms
    if tuple(observed_types) != tuple(slug for slug, _ in KNOWLEDGE_TYPES):
        raise ValueError("knowledge types or their reviewed order changed")
    return data


def _transcript_and_spans(
    utterances: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, int]]]:
    lines: list[str] = []
    spans: dict[str, dict[str, int]] = {}
    cursor = 0
    for utterance in utterances:
        speaker = str(utterance["speaker"])
        text = str(utterance["text"])
        line = f"{speaker}: {text}"
        text_start = cursor + len(speaker) + 2
        text_end = text_start + len(text)
        spans[str(utterance["utterance_id"])] = {
            "transcript_start_char": text_start,
            "transcript_end_char": text_end,
            "utterance_start_char": 0,
            "utterance_end_char": len(text),
        }
        lines.append(line)
        cursor += len(line) + 1
    return "\n".join(lines), spans


def _canonical_graph(projection: dict[str, Any]) -> dict[str, Any]:
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


def _save_review_lifecycle(
    store: KnowledgeStore,
    meeting_id: str,
    unit_entity_id: str,
    utterance: dict[str, Any],
    evidence_id: str,
) -> list[dict[str, Any]]:
    unit = utterance["knowledge_unit"]
    subject_key = f"meeting:{meeting_id}:{unit['unit_id']}"
    revisions: list[tuple[int, str, str]] = []
    if unit.get("superseded_summary"):
        revisions.append(
            (1, str(unit["superseded_summary"]), "superseded by manual correction")
        )
    revisions.append(
        (
            len(revisions) + 1,
            str(unit["summary"]),
            "manually reviewed against the exact transcript span",
        )
    )
    for revision, summary, review_note in revisions:
        job_id = store.enqueue_job(
            "review-meeting-knowledge-unit",
            subject_key,
            subject_entity_id=unit_entity_id,
            language=str(utterance["language"]),
            prompt_version=f"manual-review-v{revision}",
            source_fingerprint=SOURCE_SHA256,
        )
        store.save_job_artifact(
            job_id,
            "reviewed-meeting-knowledge-unit",
            {
                "evidence_id": evidence_id,
                "knowledge_type": unit["type"],
                "review_method": "manual",
                "review_note": review_note,
                "revision": revision,
                "summary": summary,
                "unit_id": unit["unit_id"],
            },
            language=str(utterance["language"]),
            validation_state="accepted",
            quality_score=1.0,
        )
        store.finish_job(job_id)

    artifacts = store.artifacts_for_subject(
        subject_key,
        stage="reviewed-meeting-knowledge-unit",
    )
    return sorted(
        (
            {
                "knowledge_type": artifact["payload"]["knowledge_type"],
                "review_method": artifact["payload"]["review_method"],
                "review_note": artifact["payload"]["review_note"],
                "revision": artifact["payload"]["revision"],
                "status": artifact["validation_state"],
                "summary": artifact["payload"]["summary"],
                "unit_id": artifact["payload"]["unit_id"],
            }
            for artifact in artifacts
        ),
        key=lambda item: item["revision"],
    )


def build_example(fixture: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    data = load_fixture(fixture)
    meeting = data["meeting"]
    utterances = data["utterances"]
    transcript, spans = _transcript_and_spans(utterances)

    with tempfile.TemporaryDirectory(prefix="lkt-scripted-meeting-") as temporary:
        store = KnowledgeStore(Path(temporary) / "knowledge.sqlite3")
        meeting_entity_id = store.upsert_content_item(
            "sentence",
            "mul",
            transcript,
            source_key=str(meeting["meeting_id"]),
        )
        store.set_property(meeting_entity_id, "scripted", True)
        store.set_property(meeting_entity_id, "project_owned", True)
        store.set_property(meeting_entity_id, "review_status", "manually reviewed")

        unit_records: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        lifecycle_records: list[dict[str, Any]] = []
        for utterance in utterances:
            utterance_id = str(utterance["utterance_id"])
            unit = utterance["knowledge_unit"]
            span = spans[utterance_id]
            evidence_id = store.add_evidence(
                CORPUS_ID,
                utterance_id,
                source_hash=SOURCE_SHA256,
                locator=(
                    f"meetings/{meeting['meeting_id']}/utterances/{utterance_id}"
                    f"#t={utterance['start_ms']},{utterance['end_ms']}"
                    f"&chars={span['transcript_start_char']},{span['transcript_end_char']}"
                ),
                excerpt=str(utterance["text"]),
                payload={
                    "language": utterance["language"],
                    "speaker": utterance["speaker"],
                    "start_ms": utterance["start_ms"],
                    "end_ms": utterance["end_ms"],
                    **span,
                    "timing_basis": "scripted fixture timing, not measured from audio",
                },
            )
            evidence_ids.append(evidence_id)
            store.link_evidence(
                meeting_entity_id,
                evidence_id,
                claim="Exact authored utterance in the project-owned meeting transcript.",
                confidence=1.0,
            )

            unit_entity_id = store.upsert_term(
                "mul",
                str(unit["summary"]),
                kind=f"meeting-{unit['type']}",
                quality_score=1.0,
                payload={
                    "knowledge_type": unit["type"],
                    "review_status": "manually reviewed",
                    "unit_id": unit["unit_id"],
                },
            )
            store.link_evidence(
                unit_entity_id,
                evidence_id,
                claim="Reviewed knowledge unit resolves to this exact transcript span.",
                confidence=1.0,
            )
            store.accept_relation_assertion(
                meeting_entity_id,
                meeting_entity_id,
                unit_entity_id,
                f"has-{unit['type']}",
                basis="reviewed",
                confidence=1.0,
                properties={
                    "knowledge_type": unit["type"],
                    "modes": ["meeting"],
                    "review_status": "manually reviewed",
                    "type_label": TYPE_LABELS[unit["type"]],
                    "unit_id": unit["unit_id"],
                },
                evidence_ids=(evidence_id,),
            )
            lifecycle = _save_review_lifecycle(
                store,
                meeting_entity_id,
                unit_entity_id,
                utterance,
                evidence_id,
            )
            lifecycle_records.extend(lifecycle)
            resolved_evidence = store.evidence_for_entity(unit_entity_id)
            if len(resolved_evidence) != 1:
                raise ValueError(f"{unit['unit_id']} did not resolve to one evidence record")
            unit_records.append(
                {
                    "entity_id": unit_entity_id,
                    "evidence_id": evidence_id,
                    "review_status": "manually reviewed",
                    "source_span": {
                        "language": utterance["language"],
                        "speaker": utterance["speaker"],
                        "source_hash": SOURCE_SHA256,
                        "start_ms": utterance["start_ms"],
                        "end_ms": utterance["end_ms"],
                        "utterance_id": utterance_id,
                        **span,
                        "text": utterance["text"],
                    },
                    "summary": unit["summary"],
                    "type": unit["type"],
                    "type_label": TYPE_LABELS[unit["type"]],
                    "unit_id": unit["unit_id"],
                }
            )

        graph = _canonical_graph(
            store.lexical_subgraph(
                meeting_entity_id,
                "meeting",
                {"nodes": 16, "edges": 16, "depth": 2},
            )
        )
        evidence = store.evidence_records(evidence_ids)

    return {
        "schema_version": "1.0",
        "example": {
            "description": (
                "A deterministic transformation of one project-owned scripted "
                "English/Mandarin meeting into ten manually reviewed knowledge units."
            ),
            "not_claimed": list(NOT_CLAIMED),
            "ownership": "Local Knowledge Terminal project-owned fixture",
            "review_status": "manually reviewed",
            "scripted": True,
        },
        "source": {
            "duration_ms": utterances[-1]["end_ms"],
            "fixture_path": "examples/fixtures/scripted-bilingual-meeting.json",
            "meeting_id": meeting["meeting_id"],
            "note": meeting["note"],
            "recording_exists": False,
            "sha256": SOURCE_SHA256,
            "timing_basis": meeting["timing_basis"],
            "title": meeting["title"],
            "transcript": transcript,
            "utterance_count": len(utterances),
        },
        "knowledge_units": unit_records,
        "review_lifecycle": lifecycle_records,
        "graph": graph,
        "evidence": evidence,
    }


def validate_example(example: dict[str, Any]) -> None:
    if example["example"]["scripted"] is not True:
        raise ValueError("example lost its scripted label")
    if example["example"]["review_status"] != "manually reviewed":
        raise ValueError("example lost its manual-review label")
    if tuple(example["example"]["not_claimed"]) != NOT_CLAIMED:
        raise ValueError("example claim boundary changed")
    if example["source"]["recording_exists"] is not False:
        raise ValueError("example must not imply that a recording exists")
    if example["source"]["sha256"] != SOURCE_SHA256:
        raise ValueError("example source is not pinned to the fixture hash")

    transcript = example["source"]["transcript"]
    units = example["knowledge_units"]
    if tuple(unit["type"] for unit in units) != tuple(
        slug for slug, _ in KNOWLEDGE_TYPES
    ):
        raise ValueError("example does not contain the ten reviewed knowledge types")
    evidence = {record["evidence_id"]: record for record in example["evidence"]}
    if len(evidence) != len(KNOWLEDGE_TYPES):
        raise ValueError("example must have one evidence record per knowledge unit")
    for unit in units:
        span = unit["source_span"]
        excerpt = transcript[
            span["transcript_start_char"] : span["transcript_end_char"]
        ]
        if excerpt != span["text"]:
            raise ValueError(f"{unit['unit_id']} transcript span does not resolve")
        record = evidence.get(unit["evidence_id"])
        if record is None or record["excerpt"] != excerpt:
            raise ValueError(f"{unit['unit_id']} evidence does not resolve")
        if (
            span["source_hash"] != SOURCE_SHA256
            or record["source_hash"] != SOURCE_SHA256
        ):
            raise ValueError(f"{unit['unit_id']} evidence is not source pinned")
        for field in (
            "speaker",
            "start_ms",
            "end_ms",
            "transcript_start_char",
            "transcript_end_char",
        ):
            if record["payload"].get(field) != span[field]:
                raise ValueError(f"{unit['unit_id']} evidence lost {field}")

    graph = example["graph"]
    if graph["truncated"] or len(graph["edges"]) != len(KNOWLEDGE_TYPES):
        raise ValueError("reviewed graph is incomplete")
    if graph["projection_hash"] != _canonical_sha256(
        {key: value for key, value in graph.items() if key != "projection_hash"}
    ):
        raise ValueError("reviewed graph hash does not match its projection")
    unit_by_entity = {unit["entity_id"]: unit for unit in units}
    for edge in graph["edges"]:
        unit = unit_by_entity.get(edge["target"])
        if unit is None:
            raise ValueError(f"graph edge {edge['id']} has an unknown knowledge target")
        if edge["basis"] != "reviewed" or edge["evidence_ids"] != [unit["evidence_id"]]:
            raise ValueError(f"graph edge {edge['id']} is not reviewed and evidenced")
        if edge["relation"] != f"has-{unit['type']}":
            raise ValueError(f"graph edge {edge['id']} has the wrong knowledge type")

    lifecycle: dict[str, list[dict[str, Any]]] = {}
    for record in example["review_lifecycle"]:
        lifecycle.setdefault(record["unit_id"], []).append(record)
        if record["review_method"] != "manual":
            raise ValueError("review lifecycle contains a non-manual record")
    if set(lifecycle) != {unit["unit_id"] for unit in units}:
        raise ValueError("review lifecycle does not cover every knowledge unit")
    if sum(
        record["status"] == "superseded"
        for records in lifecycle.values()
        for record in records
    ) != 1:
        raise ValueError("review lifecycle must contain exactly one superseded revision")
    for unit in units:
        records = lifecycle[unit["unit_id"]]
        if records[-1]["status"] != "accepted" or records[-1]["summary"] != unit["summary"]:
            raise ValueError(f"{unit['unit_id']} has no accepted final review")


def render_example(fixture: Path = DEFAULT_FIXTURE) -> bytes:
    example = build_example(fixture)
    validate_example(example)
    return _json_bytes(example)


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
    rendered = render_example(args.fixture)
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != rendered:
            raise SystemExit(f"artifact is stale: {args.output}")
        print(f"ok: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered)
    example = json.loads(rendered)
    print(f"wrote {args.output}")
    print(f"projection {example['graph']['projection_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
