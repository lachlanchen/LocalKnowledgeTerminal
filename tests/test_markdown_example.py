from __future__ import annotations

import hashlib
import unittest

from examples.markdown_vault_index import (
    DEFAULT_OUTPUT,
    DEFAULT_VAULT,
    build_proof,
    render_proof,
    write_proof,
)


class MarkdownVaultExampleTests(unittest.TestCase):
    def test_proof_resolves_results_and_edges_to_unchanged_sources(self) -> None:
        proof = build_proof()
        self.assertFalse(proof["boundary"]["writes_to_vault"])
        self.assertFalse(proof["boundary"]["semantic_or_vector_search"])
        self.assertEqual(proof["build"]["files"], 2)
        self.assertEqual(len(proof["explicit_wikilink_edges"]), 1)
        for results in proof["searches"].values():
            self.assertTrue(results)
            for result in results:
                source = DEFAULT_VAULT / result["path"]
                self.assertEqual(
                    hashlib.sha256(source.read_bytes()).hexdigest(),
                    result["source_sha256"],
                )
                self.assertIn(result["excerpt"], source.read_text(encoding="utf-8"))

    def test_committed_artifact_is_byte_stable(self) -> None:
        self.assertEqual(DEFAULT_OUTPUT.read_bytes(), render_proof())

    def test_proof_output_cannot_overwrite_the_canonical_vault(self) -> None:
        source = DEFAULT_VAULT / "Inbox.md"
        before = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "outside the canonical vault"):
            write_proof(DEFAULT_VAULT, source)
        self.assertEqual(source.read_bytes(), before)

    def test_proof_output_symlink_cannot_target_the_canonical_vault(self) -> None:
        source = DEFAULT_VAULT / "Inbox.md"
        with self.subTest("symlinks supported"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "proof.json"
                try:
                    output.symlink_to(source)
                except OSError:
                    self.skipTest("symlinks are unavailable")
                before = source.read_bytes()
                with self.assertRaisesRegex(ValueError, "outside the canonical vault"):
                    write_proof(DEFAULT_VAULT, output)
                self.assertEqual(source.read_bytes(), before)

    def test_proof_output_hardlink_is_replaced_without_touching_the_vault(self) -> None:
        import tempfile
        from pathlib import Path

        source = DEFAULT_VAULT / "Inbox.md"
        before = source.read_bytes()
        with tempfile.TemporaryDirectory(
            prefix="markdown-proof-hardlink-", dir=DEFAULT_VAULT.parent.parent
        ) as temporary:
            output = Path(temporary) / "proof.json"
            try:
                output.hardlink_to(source)
            except OSError:
                self.skipTest("hard links are unavailable")
            write_proof(DEFAULT_VAULT, output)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(output.read_bytes(), render_proof())
            self.assertNotEqual(output.stat().st_ino, source.stat().st_ino)


if __name__ == "__main__":
    unittest.main()
