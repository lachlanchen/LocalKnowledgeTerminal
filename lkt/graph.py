from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .knowledge import KnowledgeStore


class GraphRuntimeUnavailable(RuntimeError):
    pass


def _remove_projection(path: Path, owner: Path) -> None:
    if path.parent.resolve() != owner.parent.resolve():
        raise ValueError("graph projection cleanup escaped its destination directory")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def rebuild_ladybug(
    store: KnowledgeStore,
    destination: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Build a replaceable LadybugDB projection from accepted SQLite knowledge."""

    try:
        import ladybug as lb
    except ImportError as exc:  # pragma: no cover - exercised on the Pi runtime
        raise GraphRuntimeUnavailable(
            "LadybugDB is not installed; run scripts/install_knowledge_runtime.sh"
        ) from exc

    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise FileExistsError(
            f"graph projection already exists: {destination}; pass --replace to rebuild"
        )
    snapshot = store.graph_snapshot()
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    temporary = destination.with_name(f"{destination.name}.build-{uuid.uuid4().hex}")
    previous = destination.with_name(f"{destination.name}.previous")
    try:
        database = lb.Database(str(temporary))
        connection = lb.Connection(database)
        connection.execute(
            """CREATE NODE TABLE Entity(
                   id STRING PRIMARY KEY,
                   entity_type STRING,
                   canonical_key STRING,
                   label STRING,
                   quality DOUBLE
               )"""
        )
        connection.execute(
            """CREATE REL TABLE KnowledgeEdge(
                   FROM Entity TO Entity,
                   edge_id STRING,
                   relation STRING,
                   basis STRING,
                   confidence DOUBLE,
                   properties STRING
               )"""
        )
        for node in snapshot["nodes"]:
            connection.execute(
                """CREATE (n:Entity {
                       id: $id,
                       entity_type: $entity_type,
                       canonical_key: $canonical_key,
                       label: $label,
                       quality: $quality
                   })""",
                {
                    "id": node["id"],
                    "entity_type": node["type"],
                    "canonical_key": node["key"],
                    "label": node["label"],
                    "quality": node["quality"],
                },
            )
        for edge in snapshot["edges"]:
            connection.execute(
                """MATCH (source:Entity {id: $source}),
                         (target:Entity {id: $target})
                   CREATE (source)-[:KnowledgeEdge {
                       edge_id: $edge_id,
                       relation: $relation,
                       basis: $basis,
                       confidence: $confidence,
                       properties: $properties
                   }]->(target)""",
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "edge_id": edge["id"],
                    "relation": edge["relation"],
                    "basis": edge["basis"],
                    "confidence": edge["confidence"],
                    "properties": json.dumps(
                        edge["properties"], ensure_ascii=False, sort_keys=True
                    ),
                },
            )
        connection.close()
        del connection
        del database
        if destination.exists():
            if previous.exists():
                _remove_projection(previous, destination)
            destination.replace(previous)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            _remove_projection(temporary, destination)
        raise
    return {
        "ready": True,
        "engine": "ladybug",
        "path": str(destination),
        "nodes": len(snapshot["nodes"]),
        "edges": len(snapshot["edges"]),
        "source_sha256": fingerprint,
        "previous": str(previous) if previous.exists() else "",
    }


def graph_counts(destination: Path) -> dict[str, int]:
    try:
        import ladybug as lb
    except ImportError as exc:  # pragma: no cover - exercised on the Pi runtime
        raise GraphRuntimeUnavailable(
            "LadybugDB is not installed; run scripts/install_knowledge_runtime.sh"
        ) from exc
    database = lb.Database(str(Path(destination).resolve()), read_only=True)
    connection = lb.Connection(database)
    nodes = connection.execute("MATCH (n:Entity) RETURN count(n)").get_next()[0]
    edges = connection.execute(
        "MATCH (:Entity)-[r:KnowledgeEdge]->(:Entity) RETURN count(r)"
    ).get_next()[0]
    connection.close()
    return {"nodes": int(nodes), "edges": int(edges)}
