from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import lkt.markdown as markdown_module
from lkt.cli import command_ingest_markdown, command_search_markdown, parser
from lkt.markdown import MarkdownIndex, build_markdown_index, markdown_paths


SOURCE = """# Research note

The reviewed evidence keeps an exact source trail.

## 原始想法

成功有时会变成限制。See [[Concepts/Success Trap|the concept note]].
"""


class MarkdownIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.source = self.vault / "Research.md"
        self.source.write_text(SOURCE, encoding="utf-8")
        concepts = self.vault / "Concepts"
        concepts.mkdir()
        (concepts / "Success Trap.md").write_text(
            "# Success Trap\n\nA prior strength can constrain a later choice.\n",
            encoding="utf-8",
        )
        self.database = self.root / "markdown.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_search_resolves_exact_source_provenance_and_wikilink(self) -> None:
        source_before = self.source.read_bytes()
        result = build_markdown_index(self.vault, self.database)
        self.assertEqual(result["files"], 2)
        self.assertEqual(self.source.read_bytes(), source_before)

        match = MarkdownIndex(self.database).search("成功有时会变成限制")[0]
        self.assertEqual(match["path"], "Research.md")
        self.assertEqual(match["heading"], "原始想法")
        self.assertEqual(match["line_start"], 5)
        self.assertEqual(match["line_end"], 7)
        self.assertIn("成功有时会变成限制。", match["excerpt"])
        self.assertEqual(
            match["source_sha256"], hashlib.sha256(source_before).hexdigest()
        )
        self.assertEqual(
            match["wikilinks"],
            [{"target": "Concepts/Success Trap", "label": "the concept note"}],
        )
        self.assertEqual(
            MarkdownIndex(self.database).resolve(match["section_id"]), match
        )

    def test_short_chinese_keywords_find_the_exact_section(self) -> None:
        build_markdown_index(self.vault, self.database)
        index = MarkdownIndex(self.database)
        self.assertEqual(index.search("成功")[0]["heading"], "原始想法")
        self.assertEqual(index.search("限制")[0]["heading"], "原始想法")

    def test_excerpt_preserves_source_line_endings_and_whitespace(self) -> None:
        exact = "# Exact\r\n\r\n  leading and trailing  \r\n"
        source = self.vault / "Exact.md"
        source.write_bytes(exact.encode("utf-8"))
        build_markdown_index(self.vault, self.database)

        match = MarkdownIndex(self.database).search("leading trailing")[0]
        self.assertEqual(match["excerpt"], exact)
        self.assertEqual(match["line_start"], 1)
        self.assertEqual(match["line_end"], 3)

    def test_rebuild_is_equivalent_and_removes_deleted_note(self) -> None:
        first = build_markdown_index(self.vault, self.database)
        index = MarkdownIndex(self.database)
        first_results = index.search("strength constrain")
        first_fingerprint = index.metadata()["vault_fingerprint"]

        second = build_markdown_index(self.vault, self.database)
        self.assertEqual(second["vault_fingerprint"], first["vault_fingerprint"])
        self.assertEqual(MarkdownIndex(self.database).search("strength constrain"), first_results)

        (self.vault / "Concepts" / "Success Trap.md").unlink()
        third = build_markdown_index(self.vault, self.database)
        self.assertEqual(third["files"], 1)
        self.assertNotEqual(third["vault_fingerprint"], first_fingerprint)
        self.assertEqual(MarkdownIndex(self.database).search("strength constrain"), [])

    def test_hidden_generated_and_symlinked_content_is_not_indexed(self) -> None:
        hidden = self.vault / ".obsidian"
        hidden.mkdir()
        (hidden / "workspace.md").write_text("private layout state", encoding="utf-8")
        generated = self.vault / "generated"
        generated.mkdir()
        (generated / "answer.md").write_text("generated answer", encoding="utf-8")
        (self.vault / ".hidden.md").write_text("hidden note", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("outside source", encoding="utf-8")
        symlink = self.vault / "linked.md"
        try:
            symlink.symlink_to(outside)
        except OSError:
            pass

        paths = [path.relative_to(self.vault).as_posix() for path in markdown_paths(self.vault)]
        self.assertEqual(paths, ["Concepts/Success Trap.md", "Research.md"])
        build_markdown_index(self.vault, self.database)
        index = MarkdownIndex(self.database)
        self.assertEqual(index.search("private layout"), [])
        self.assertEqual(index.search("generated answer"), [])
        self.assertEqual(index.search("outside source"), [])

    def test_code_examples_do_not_become_wikilink_edges(self) -> None:
        (self.vault / "Syntax.md").write_text(
            """# Syntax examples

`[[Inline Example]]` is syntax, not a relationship.

```markdown
[[Fenced Example]]
```

The actual note points to [[Concepts/Success Trap]].
""",
            encoding="utf-8",
        )
        build_markdown_index(self.vault, self.database)
        targets = {edge["target"] for edge in MarkdownIndex(self.database).edges()}
        self.assertEqual(targets, {"Concepts/Success Trap"})

    def test_comments_escapes_and_indented_code_do_not_become_edges(self) -> None:
        (self.vault / "Literal.md").write_text(
            """# Literal examples

<!-- [[Commented Example]] -->
\\[[Escaped Example]]
    [[Indented Example]]

Visible [[Concepts/Success Trap]].
""",
            encoding="utf-8",
        )
        build_markdown_index(self.vault, self.database)
        targets = {edge["target"] for edge in MarkdownIndex(self.database).edges()}
        self.assertEqual(targets, {"Concepts/Success Trap"})

    def test_variable_fences_and_code_spans_do_not_become_edges(self) -> None:
        (self.vault / "Complex Syntax.md").write_text(
            """# Complex syntax

````markdown
```
[[Inside Four Backticks]]
```
````

``code with ` inside and [[Inside Double Backticks]]``

> ```markdown
> [[Inside Blockquoted Fence]]
> ```

```markdown
> ```
[[Still Inside Unquoted Fence]]
```

> ```markdown
> [[Inside Unclosed Blockquoted Fence]]
Visible [[Visible After Blockquote]].

Visible [[Concepts/Success Trap]].
""",
            encoding="utf-8",
        )
        build_markdown_index(self.vault, self.database)
        targets = {edge["target"] for edge in MarkdownIndex(self.database).edges()}
        self.assertEqual(
            targets, {"Concepts/Success Trap", "Visible After Blockquote"}
        )

    def test_database_cannot_be_written_inside_canonical_vault(self) -> None:
        unsafe = self.vault / "runtime" / "markdown.sqlite3"
        with self.assertRaisesRegex(ValueError, "outside the canonical vault"):
            build_markdown_index(self.vault, unsafe)
        self.assertFalse(unsafe.exists())

    def test_lock_symlink_cannot_modify_a_canonical_note(self) -> None:
        lock = self.database.with_name(self.database.name + ".lock")
        before = self.source.read_bytes()
        try:
            lock.symlink_to(self.source)
        except OSError:
            self.skipTest("symlinks are unavailable")

        with self.assertRaisesRegex(ValueError, "lock path"):
            build_markdown_index(self.vault, self.database)

        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.database.exists())

    def test_lock_hardlink_cannot_modify_a_canonical_note(self) -> None:
        lock = self.database.with_name(self.database.name + ".lock")
        before = self.source.read_bytes()
        try:
            lock.hardlink_to(self.source)
        except OSError:
            self.skipTest("hard links are unavailable")

        with self.assertRaisesRegex(ValueError, "lock path"):
            build_markdown_index(self.vault, self.database)

        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.database.exists())

    def test_failed_build_preserves_previous_index(self) -> None:
        build_markdown_index(self.vault, self.database)
        before = MarkdownIndex(self.database).metadata()
        (self.vault / "broken.md").write_bytes(b"\xff\xfe\x00")

        with self.assertRaises(UnicodeDecodeError):
            build_markdown_index(self.vault, self.database)

        self.assertEqual(MarkdownIndex(self.database).metadata(), before)
        self.assertEqual(list(self.root.glob(".markdown.sqlite3.building-*")), [])
        self.assertTrue(self.database.with_name(self.database.name + ".lock").is_file())

    def test_concurrent_build_is_rejected_without_damaging_index(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = []
        original_read_sources = markdown_module.read_sources

        def blocking_read_sources(vault: Path):
            if threading.current_thread().name == "first-build" and not entered.is_set():
                entered.set()
                release.wait(timeout=5)
            return original_read_sources(vault)

        def first_build() -> None:
            try:
                build_markdown_index(self.vault, self.database)
                finished.append("ok")
            except Exception as error:  # pragma: no cover - surfaced by assertion
                finished.append(type(error).__name__)

        with mock.patch.object(
            markdown_module, "read_sources", side_effect=blocking_read_sources
        ):
            worker = threading.Thread(target=first_build, name="first-build")
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                build_markdown_index(self.vault, self.database)
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(finished, ["ok"])
        self.assertEqual(MarkdownIndex(self.database).metadata()["file_count"], "2")
        self.assertEqual(list(self.root.glob(".markdown.sqlite3.building-*")), [])
        self.assertTrue(self.database.with_name(self.database.name + ".lock").is_file())
        self.assertEqual(build_markdown_index(self.vault, self.database)["files"], 2)

    def test_cli_builds_and_searches_the_explicit_database(self) -> None:
        build_args = SimpleNamespace(vault=str(self.vault), database=str(self.database))
        search_args = SimpleNamespace(
            query="source trail", limit=3, database=str(self.database)
        )
        build_output = io.StringIO()
        with contextlib.redirect_stdout(build_output):
            self.assertEqual(command_ingest_markdown(build_args), 0)
        search_output = io.StringIO()
        with contextlib.redirect_stdout(search_output):
            self.assertEqual(command_search_markdown(search_args), 0)
        self.assertEqual(json.loads(build_output.getvalue())["files"], 2)
        search_payload = json.loads(search_output.getvalue())
        self.assertEqual(search_payload[0]["path"], "Research.md")

        ingest = parser().parse_args(
            ["ingest-markdown", str(self.vault), "--database", str(self.database)]
        )
        search = parser().parse_args(
            ["search-markdown", "source", "--database", str(self.database)]
        )
        self.assertIs(ingest.handler, command_ingest_markdown)
        self.assertIs(search.handler, command_search_markdown)


if __name__ == "__main__":
    unittest.main()
