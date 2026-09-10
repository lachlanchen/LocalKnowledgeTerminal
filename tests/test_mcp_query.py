from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lkt.knowledge import KnowledgeStore
from lkt.mcp_query import (
    MAX_EVIDENCE,
    MAX_EVIDENCE_PER_CLAIM,
    KnowledgeReader,
    PrivateKnowledgeError,
    _safe_locator,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/fixtures/pocketpolyglot-sample.json"


def make_knowledge_database(directory: Path) -> tuple[Path, dict[str, str]]:
    raw = FIXTURE.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    fixture = json.loads(raw)
    paragraph = fixture["sections"][0]["subsections"][0]["stories"][0][
        "paragraphs"
    ][0]
    unit = paragraph["units"][0]
    database = directory / "knowledge.sqlite3"
    store = KnowledgeStore(database)

    passage_id = store.upsert_content_item(
        "sentence", "zh", paragraph["source_text"], source_key=paragraph["id"]
    )
    japanese_text = "".join(
        str(token["t"]) for phrase in unit["ja"] for token in phrase
    )
    japanese_id = store.upsert_content_item(
        "sentence", "ja", japanese_text, source_key=f"{paragraph['id']}:ja:1"
    )
    evidence_id = store.add_evidence(
        "pocketpolyglot-public-sample",
        f"{paragraph['id']}-unit-1",
        source_hash=source_hash,
        locator=(
            "sections/spring/subsections/path/stories/sample-001/"
            "paragraphs/sample-001-p1/units/0"
        ),
        excerpt=unit["source_text"],
        payload={"fixture": "pocketpolyglot-sample.json", "unit_index": 0},
    )
    store.link_evidence(
        passage_id,
        evidence_id,
        claim="Exact project-owned source paragraph.",
        confidence=1.0,
    )
    store.link_evidence(
        japanese_id,
        evidence_id,
        claim="Reviewed Japanese rendering of the exact source unit.",
        confidence=1.0,
    )

    spring_id = store.upsert_term("en", "spring", quality_score=1.0)
    chinese_id = store.upsert_term("zh", "春天", quality_score=1.0)
    japanese_term_id = store.upsert_term("ja", "春", quality_score=1.0)
    light_id = store.upsert_term("zh", "轻", kind="quality", quality_score=1.0)
    store.add_meaning(spring_id, "en", "the season after winter")
    store.add_translation(
        spring_id, "zh", "春天", target_term_id=chinese_id, quality_score=1.0
    )
    store.add_translation(
        spring_id,
        "ja",
        "春",
        transliteration="はる",
        target_term_id=japanese_term_id,
        quality_score=1.0,
    )
    store.link_evidence(
        spring_id,
        evidence_id,
        claim="The concept appears in this reviewed source unit.",
        confidence=1.0,
    )
    claim_id = store.accept_relation_assertion(
        spring_id,
        spring_id,
        light_id,
        "appears-with-quality",
        basis="reviewed",
        confidence=1.0,
        properties={"modes": ["passage"]},
        evidence_ids=(evidence_id,),
    )

    hidden_id = store.upsert_term("en", "hidden-fixture-fact")
    hidden_target_id = store.upsert_term("en", "hidden-target")
    hidden_claim_id = store.accept_relation_assertion(
        hidden_id,
        hidden_id,
        hidden_target_id,
        "hidden-link",
        basis="reviewed",
        confidence=1.0,
        evidence_ids=(evidence_id,),
    )
    store.upsert_term("en", "hidden-fixture-fact", status="archived")
    rejected_id = store.upsert_term(
        "en", "rejected-fixture-fact", status="rejected"
    )
    rejected_claim_id = store.accept_relation_assertion(
        rejected_id,
        rejected_id,
        light_id,
        "rejected-link",
        basis="reviewed",
        confidence=1.0,
        evidence_ids=(evidence_id,),
    )
    model_claim_id = store.accept_relation_assertion(
        spring_id,
        spring_id,
        japanese_term_id,
        "model-only-link",
        basis="model",
        confidence=0.5,
    )

    return database, {
        "claim_id": claim_id,
        "evidence_id": evidence_id,
        "hidden_claim_id": hidden_claim_id,
        "rejected_claim_id": rejected_claim_id,
        "model_claim_id": model_claim_id,
        "source_hash": source_hash,
    }


class KnowledgeReaderTests(unittest.TestCase):
    def test_local_file_locators_are_withheld(self) -> None:
        self.assertEqual(_safe_locator("/home/private/book.pdf"), "[local path withheld]")
        self.assertEqual(_safe_locator(r"C:\\Private\\book.pdf"), "[local path withheld]")
        self.assertEqual(_safe_locator("file:///private/book.pdf"), "[local path withheld]")
        self.assertEqual(
            _safe_locator("page=1,path=/home/private/book.pdf"),
            "[local path withheld]",
        )
        self.assertEqual(_safe_locator("~/private/book.pdf"), "[local path withheld]")
        self.assertEqual(_safe_locator("books/../private.pdf"), "[local path withheld]")
        self.assertEqual(
            _safe_locator("%252fhome%252fprivate%252fbook.pdf"),
            "[local path withheld]",
        )
        self.assertEqual(
            _safe_locator("locator=(/home/alice/private.pdf)"),
            "[local path withheld]",
        )
        self.assertEqual(
            _safe_locator("path:/home/alice/private.pdf"),
            "[local path withheld]",
        )
        self.assertEqual(
            _safe_locator("%2525252fhome%2525252falice%2525252fprivate.pdf"),
            "[local path withheld]",
        )
        self.assertEqual(_safe_locator("book.pdf#page=17"), "book.pdf#page=17")

    def test_multilingual_queries_resolve_to_one_stable_grounded_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            reader = KnowledgeReader(database)

            chinese = reader.query_private_knowledge("春天", "zh", limit=1)
            japanese = reader.query_private_knowledge("春", "ja", limit=1)
            repeated = reader.query_private_knowledge("春天", "zh", limit=1)

            self.assertEqual(chinese, repeated)
            self.assertEqual(chinese["match_count"], 1)
            self.assertEqual(
                chinese["matches"][0]["subject"]["id"],
                japanese["matches"][0]["subject"]["id"],
            )
            self.assertEqual(
                chinese["matches"][0]["graph"]["claims"][0]["id"],
                expected["claim_id"],
            )
            claim_ids = {
                claim["id"]
                for claim in chinese["matches"][0]["graph"]["claims"]
            }
            self.assertNotIn(expected["model_claim_id"], claim_ids)
            translations = chinese["matches"][0]["facts"]["translations"]
            self.assertEqual([item["language"] for item in translations], ["zh", "ja"])

    def test_trace_keeps_exact_stable_provenance_without_paths_or_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            reader = KnowledgeReader(database)

            claim = reader.trace_private_claim(expected["claim_id"])
            evidence = reader.trace_private_claim(expected["evidence_id"])
            serialized = json.dumps(
                {"claim": claim, "evidence": evidence}, ensure_ascii=False
            )

            self.assertEqual(claim["claim"]["evidence_ids"], [expected["evidence_id"]])
            self.assertEqual(claim["evidence"][0]["source_hash"], expected["source_hash"])
            self.assertEqual(evidence["evidence"]["id"], expected["evidence_id"])
            self.assertIn(expected["claim_id"], serialized)
            self.assertNotIn(str(database), serialized)
            self.assertNotIn("fixture", serialized)
            self.assertNotIn("pocketpolyglot-public-sample", serialized)
            self.assertNotIn("sample-001-p1-unit-1", serialized)
            self.assertTrue(
                claim["evidence"][0]["collection_id"].startswith("collection-")
            )
            self.assertTrue(claim["evidence"][0]["source_id"].startswith("source-"))

    def test_private_provenance_identifiers_and_embedded_paths_are_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = make_knowledge_database(Path(temporary))
            reader = KnowledgeReader(database)
            subject_id = reader.query_private_knowledge("spring", limit=1)["matches"][
                0
            ]["subject"]["id"]
            store = KnowledgeStore(database)
            evidence_id = store.add_evidence(
                "/home/alice/private/secret-collection",
                "customer-api-key-secret-entry",
                source_hash="not-a-hash-private-secret",
                locator="page=1,path=/home/alice/private/book.pdf",
                excerpt="A deliberately public test excerpt.",
            )
            store.link_evidence(subject_id, evidence_id)

            trace = KnowledgeReader(database).trace_private_claim(evidence_id)
            status = KnowledgeReader(database).collections_status()
            serialized = json.dumps({"trace": trace, "status": status})

            self.assertNotIn("/home/alice", serialized)
            self.assertNotIn("customer-api-key", serialized)
            self.assertNotIn("not-a-hash-private-secret", serialized)
            self.assertEqual(trace["evidence"]["locator"], "[local path withheld]")
            self.assertEqual(trace["evidence"]["source_hash"], "")
            self.assertRegex(trace["evidence"]["collection_id"], r"^collection-[0-9a-f]{16}$")
            self.assertRegex(trace["evidence"]["source_id"], r"^source-[0-9a-f]{16}$")

    def test_provenance_ids_and_hashes_survive_independent_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            first_db, first_expected = make_knowledge_database(first)
            second_db, second_expected = make_knowledge_database(second)

            self.assertEqual(first_expected, second_expected)
            first_trace = KnowledgeReader(first_db).trace_private_claim(
                first_expected["claim_id"]
            )
            second_trace = KnowledgeReader(second_db).trace_private_claim(
                second_expected["claim_id"]
            )
            self.assertEqual(first_trace, second_trace)
            self.assertEqual(len(first_trace["result_hash"]), 64)

    def test_archived_entities_and_their_claims_are_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            reader = KnowledgeReader(database)

            result = reader.query_private_knowledge("hidden-fixture-fact")
            self.assertEqual(result["matches"], [])
            with self.assertRaisesRegex(
                PrivateKnowledgeError, "accepted claim or evidence was not found"
            ):
                reader.trace_private_claim(expected["hidden_claim_id"])
            self.assertEqual(
                reader.query_private_knowledge("rejected-fixture-fact")["matches"],
                [],
            )
            with self.assertRaisesRegex(
                PrivateKnowledgeError, "accepted claim or evidence was not found"
            ):
                reader.trace_private_claim(expected["rejected_claim_id"])
            with self.assertRaisesRegex(
                PrivateKnowledgeError, "accepted claim or evidence was not found"
            ):
                reader.trace_private_claim(expected["model_claim_id"])

    def test_model_or_rejected_subject_cannot_reroute_or_expose_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            store = KnowledgeStore(database)
            searched_id = store.upsert_term("en", "model-reroute-target")
            owner_id = store.upsert_term("en", "model-reroute-owner")
            other_id = store.upsert_term("en", "model-reroute-other")
            model_id = store.accept_relation_assertion(
                owner_id,
                searched_id,
                other_id,
                "model-reroute",
                basis="model",
            )
            rejected_subject_id = store.upsert_term("en", "rejected-owner")
            rejected_subject_claim = store.accept_relation_assertion(
                rejected_subject_id,
                searched_id,
                other_id,
                "rejected-owner-link",
                basis="reviewed",
                evidence_ids=(expected["evidence_id"],),
            )
            store.upsert_term("en", "rejected-owner", status="rejected")

            reader = KnowledgeReader(database)
            result = reader.query_private_knowledge("model-reroute-target", limit=1)
            self.assertEqual(result["matches"][0]["subject"]["id"], searched_id)
            for identifier in (model_id, rejected_subject_claim):
                with self.assertRaisesRegex(
                    PrivateKnowledgeError, "accepted claim or evidence was not found"
                ):
                    reader.trace_private_claim(identifier)
            linked = reader.trace_private_claim(expected["evidence_id"])[
                "linked_claims"
            ]
            self.assertNotIn(
                rejected_subject_claim, {claim["id"] for claim in linked}
            )

    def test_orphaned_evidence_link_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "DELETE FROM evidence_records WHERE evidence_id = ?",
                    (expected["evidence_id"],),
                )

            reader = KnowledgeReader(database)
            with self.assertRaisesRegex(
                PrivateKnowledgeError, "accepted claim or evidence was not found"
            ):
                reader.trace_private_claim(expected["claim_id"])
            result = reader.query_private_knowledge("spring", limit=1)
            self.assertEqual(result["matches"][0]["graph"]["claims"], [])
            self.assertEqual(reader.collections_status()["counts"]["accepted_claims"], 0)

    def test_evidence_identifiers_are_bounded_in_graph_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            store = KnowledgeStore(database)
            source_id = store.upsert_term("en", "bounded-evidence-source")
            target_id = store.upsert_term("en", "bounded-evidence-target")
            evidence_ids = [
                store.add_evidence(
                    "bounded-test",
                    f"entry-{index:02d}",
                    source_hash=expected["source_hash"],
                    locator=f"fixtures/entry-{index:02d}",
                    excerpt=f"Evidence {index}",
                )
                for index in range(MAX_EVIDENCE + 9)
            ]
            claim_id = store.accept_relation_assertion(
                source_id,
                source_id,
                target_id,
                "bounded-evidence-link",
                basis="reviewed",
                evidence_ids=evidence_ids,
            )

            reader = KnowledgeReader(database)
            trace = reader.trace_private_claim(claim_id)
            graph = reader.query_private_knowledge("bounded-evidence-source", limit=1)[
                "matches"
            ][0]["graph"]
            graph_claim = next(claim for claim in graph["claims"] if claim["id"] == claim_id)

            self.assertEqual(len(trace["claim"]["evidence_ids"]), MAX_EVIDENCE)
            self.assertEqual(len(trace["evidence"]), MAX_EVIDENCE)
            self.assertTrue(trace["claim"]["evidence_truncated"])
            self.assertEqual(
                len(graph_claim["evidence_ids"]), MAX_EVIDENCE_PER_CLAIM
            )
            self.assertTrue(graph_claim["evidence_truncated"])
            self.assertTrue(graph["truncated"])

    def test_status_is_read_only_bounded_and_contains_no_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            before = database.stat().st_mtime_ns
            reader = KnowledgeReader(database)
            status = reader.collections_status()
            after = database.stat().st_mtime_ns

            self.assertEqual(before, after)
            self.assertTrue(status["read_only"])
            self.assertFalse(status["model_invocation"])
            self.assertFalse(status["languages_truncated"])
            self.assertEqual(status["languages"], ["en", "ja", "zh"])
            self.assertEqual(
                status["collections"][0]["source_hashes"], [expected["source_hash"]]
            )
            self.assertEqual(status["counts"]["accepted_claims"], 1)
            self.assertFalse(status["collections_truncated"])
            self.assertRegex(
                status["collections"][0]["collection_id"],
                r"^collection-[0-9a-f]{16}$",
            )
            self.assertNotIn(str(database), json.dumps(status))
            self.assertNotIn("pocketpolyglot-public-sample", json.dumps(status))

    def test_invalid_inputs_are_rejected_before_querying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = make_knowledge_database(Path(temporary))
            reader = KnowledgeReader(database)
            with self.assertRaisesRegex(PrivateKnowledgeError, "at most 200"):
                reader.query_private_knowledge("x" * 201)
            with self.assertRaisesRegex(PrivateKnowledgeError, "limit"):
                reader.query_private_knowledge("spring", limit=6)
            with self.assertRaisesRegex(PrivateKnowledgeError, "limit"):
                reader.query_private_knowledge("spring", limit=True)
            with self.assertRaisesRegex(PrivateKnowledgeError, "max_depth"):
                reader.query_private_knowledge("spring", max_depth=5)

    def test_cli_keeps_http_on_loopback_and_stops_cleanly(self) -> None:
        from lkt.mcp_server import main

        with self.assertRaises(SystemExit), patch(
            "lkt.mcp_server.create_mcp_server"
        ) as create:
            main(["--transport", "streamable-http", "--host", "0.0.0.0"])
        create.assert_not_called()

        server = Mock()
        server.run.side_effect = KeyboardInterrupt
        with patch("lkt.mcp_server.create_mcp_server", return_value=server):
            self.assertEqual(main(["--transport", "stdio"]), 0)
        server.run.assert_called_once_with("stdio")


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP runtime not installed")
class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_lists_only_read_tools_and_calls_them(self) -> None:
        from mcp.client import Client

        from lkt.mcp_server import create_mcp_server

        with tempfile.TemporaryDirectory() as temporary:
            database, expected = make_knowledge_database(Path(temporary))
            server = create_mcp_server(database)
            async with Client(server) as client:
                tools = await client.list_tools()
                names = [tool.name for tool in tools.tools]
                self.assertEqual(
                    names, ["query_private_knowledge", "trace_private_claim"]
                )
                self.assertTrue(
                    all(tool.annotations.read_only_hint for tool in tools.tools)
                )
                self.assertTrue(
                    all(
                        tool.annotations.destructive_hint is False
                        for tool in tools.tools
                    )
                )
                query = await client.call_tool(
                    "query_private_knowledge",
                    {"query": "春", "response_language": "ja", "limit": 1},
                )
                self.assertFalse(query.is_error)
                self.assertEqual(query.structured_content["match_count"], 1)
                trace = await client.call_tool(
                    "trace_private_claim", {"identifier": expected["claim_id"]}
                )
                self.assertFalse(trace.is_error)
                self.assertEqual(
                    trace.structured_content["evidence"][0]["source_hash"],
                    expected["source_hash"],
                )

    async def test_protocol_exposes_collection_status_as_a_resource(self) -> None:
        from mcp.client import Client

        from lkt.mcp_server import create_mcp_server

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = make_knowledge_database(Path(temporary))
            server = create_mcp_server(database)
            async with Client(server) as client:
                resources = await client.list_resources()
                self.assertEqual(
                    [str(resource.uri) for resource in resources.resources],
                    ["lkt://collections/status"],
                )
                result = await client.read_resource("lkt://collections/status")
                value = json.loads(result.contents[0].text)
                self.assertTrue(value["read_only"])
                self.assertNotIn(str(database), result.contents[0].text)


if __name__ == "__main__":
    unittest.main()
