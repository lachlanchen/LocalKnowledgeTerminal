"""Exercise the production browser page builder without an external JS package."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser helper tests")
class CompactMorphologyPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "lkt/static/app.js").read_text(encoding="utf-8")
        start = source.index("function compactMorphologyBranchSlides(")
        end = source.index("\nfunction compactWordOriginFocusAreas(", start)
        cls.helper = source[start:end]

    def build(self, data, slides, capacity=3):
        script = self.helper + "\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        script += "const before = JSON.stringify(input);\n"
        script += "const pages = compactMorphologyBranchSlides(input.slides, input.data, input.capacity);\n"
        script += "if (JSON.stringify(input) !== before) throw Error('Input was mutated');\n"
        script += "process.stdout.write(JSON.stringify(pages));\n"
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=json.dumps({"data": data, "slides": slides, "capacity": capacity}),
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    @staticmethod
    def fixture():
        nodes = [{"id": "root", "type": "root"}, {"id": "word", "type": "word"}]
        nodes += [{"id": "prefix", "type": "prefix"}, {"id": "suffix", "type": "suffix"}]
        nodes += [{"id": f"history-{index}", "type": "historical"} for index in range(5)]
        pairs = [("root", "word"), ("prefix", "word"), ("suffix", "word"), ("root", "history-0")]
        pairs += [(f"history-{index}", f"history-{index + 1}") for index in range(4)]
        edges = [{"id": f"edge-{index}", "source": left, "target": right}
                 for index, (left, right) in enumerate(pairs)]
        data = {"center_id": "root", "nodes": nodes, "edges": edges}
        slides = [{"id": "branch", "focus_node_id": "root", "label": "Root family",
                   "node_ids": [node["id"] for node in nodes],
                   "edge_ids": [edge["id"] for edge in edges]}]
        return data, slides

    def assert_connected(self, page, data):
        ids = set(page["node_ids"])
        reached = {page["node_ids"][0]}
        edges = [edge for edge in data["edges"] if edge["id"] in page["edge_ids"]]
        for _ in ids:
            for edge in edges:
                self.assertTrue({edge["source"], edge["target"]} <= ids)
                if edge["source"] in reached or edge["target"] in reached:
                    reached.update([edge["source"], edge["target"]])
        self.assertEqual(reached, ids)

    def test_small_connected_pages_keep_every_history_edge_and_node(self):
        data, slides = self.fixture()
        pages = self.build(data, slides)
        self.assertGreater(len(pages), 1)
        self.assertEqual({node for page in pages for node in page["node_ids"]},
                         {node["id"] for node in data["nodes"]})
        self.assertEqual({edge for page in pages for edge in page["edge_ids"]},
                         {edge["id"] for edge in data["edges"]})
        for page in pages:
            self.assertLessEqual(len(page["node_ids"]), 3)
            self.assertIn(page["focus_node_id"], page["node_ids"])
            self.assert_connected(page, data)

    def test_first_page_keeps_a_derived_word_with_the_root(self):
        data, slides = self.fixture()
        pages = self.build(data, slides)
        self.assertTrue({"root", "word"} <= set(pages[0]["node_ids"]))

    def test_duplicate_source_slides_do_not_duplicate_pages(self):
        data, slides = self.fixture()
        self.assertEqual(self.build(data, slides), self.build(data, slides + slides))

    def test_disconnected_data_never_creates_a_dangling_context_node(self):
        data, slides = self.fixture()
        data["edges"] = [edge for edge in data["edges"] if edge["id"] != "edge-3"]
        pages = self.build(data, slides)
        for page in pages:
            self.assert_connected(page, data)

    def test_isolated_saved_node_is_preserved_without_a_fake_edge(self):
        data = {"center_id": "root", "nodes": [{"id": "root", "type": "root"}], "edges": []}
        pages = self.build(data, [{"node_ids": ["root"]}])
        self.assertEqual(pages[0]["node_ids"], ["root"])
        self.assertEqual(pages[0]["edge_ids"], [])

    def test_requested_edge_scope_is_preserved(self):
        data, slides = self.fixture()
        slides[0]["edge_ids"] = ["edge-0"]
        pages = self.build(data, slides)
        self.assertEqual({edge for page in pages for edge in page["edge_ids"]}, {"edge-0"})

    def test_two_node_pages_remain_complete_and_connected(self):
        data, slides = self.fixture()
        pages = self.build(data, slides, capacity=2)
        self.assertEqual({edge for page in pages for edge in page["edge_ids"]},
                         {edge["id"] for edge in data["edges"]})
        for page in pages:
            self.assertLessEqual(len(page["node_ids"]), 2)
            self.assert_connected(page, data)

    def test_affix_anchor_is_preferred_over_a_historical_focus(self):
        data, slides = self.fixture()
        data["nodes"][0]["type"] = "prefix"
        slides[0]["focus_node_id"] = "history-4"
        pages = self.build(data, slides)
        self.assertEqual(pages[0]["focus_node_id"], "root")
        self.assertIn("word", pages[0]["node_ids"])
