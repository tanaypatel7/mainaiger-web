"""Meaningful persistence and export-safety checks; no external dependencies."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "tools" / "notebook_bridge.py"
SPEC = importlib.util.spec_from_file_location("notebook_bridge", MODULE)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def sample():
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {"custom": {"unicode": "α"}},
            "unknown_top_level": [1, {"keep": True}], "cells": [
                {"id": "intent", "cell_type": "markdown", "metadata": {}, "source": "# Plan"},
                {"id": "load", "cell_type": "code", "metadata": {"tags": ["input"]},
                 "source": ["x = [1, 2, 3]\n", "print(x)"], "execution_count": 7,
                 "outputs": [{"output_type": "stream", "name": "stdout", "text": ["[1, 2, 3]\n"]}],
                 "unknown_cell_field": {"preserve": None}},
                {"id": "sum", "cell_type": "code", "metadata": {}, "source": "total = sum(x)",
                 "execution_count": None, "outputs": []}]}


class BridgeTests(unittest.TestCase):
    def test_lossless_data_round_trip(self):
        original = sample()
        before = copy.deepcopy(original)
        document = bridge.import_notebook(original)
        self.assertEqual(bridge.export_notebook(document), original)
        self.assertEqual(original, before)
        self.assertIn("load", [node["id"] for node in document["graph"]["nodes"]])
        document["notebook"]["metadata"]["custom"]["unicode"] = "β"
        self.assertEqual(original, before, "Import must not alias mutable notebook structures")

    def test_missing_duplicate_and_reserved_ids_preserve_original(self):
        notebook = sample()
        del notebook["cells"][0]["id"]
        notebook["cells"][1]["id"] = "mainaiger-start"
        notebook["cells"][2]["id"] = "mainaiger-start"
        document = bridge.import_notebook(notebook)
        ids = [node["id"] for node in document["graph"]["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(bridge.export_notebook(document), notebook)

    def test_markdown_is_preserved_but_not_scheduled(self):
        document = bridge.import_notebook(sample())
        self.assertFalse(any("intent" in (e["source"], e["target"]) for e in document["graph"]["edges"]))
        self.assertTrue(bridge.is_sequential(document))

    def test_invalid_references_and_duplicate_nodes_fail(self):
        document = bridge.import_notebook(sample())
        document["graph"]["edges"][0]["target"] = "missing"
        with self.assertRaisesRegex(bridge.DocumentError, "unknown target"):
            bridge.validate_document(document)
        document = bridge.import_notebook(sample())
        document["graph"]["nodes"].append(copy.deepcopy(document["graph"]["nodes"][0]))
        with self.assertRaisesRegex(bridge.DocumentError, "Duplicate node"):
            bridge.validate_document(document)

    def test_versions_are_not_silently_upgraded(self):
        document = bridge.import_notebook(sample())
        document["schema_version"] = 3  # 1 and 2 are known; anything newer is refused
        with self.assertRaisesRegex(bridge.DocumentError, "schema_version"):
            bridge.validate_document(document)

    def test_malformed_types_fail_with_document_error(self):
        notebook = sample()
        notebook["cells"][0]["cell_type"] = {"not": "a cell type"}
        with self.assertRaises(bridge.DocumentError):
            bridge.import_notebook(notebook)
        document = bridge.import_notebook(sample())
        document["graph"]["edges"][0]["source"] = []
        with self.assertRaises(bridge.DocumentError):
            bridge.validate_document(document)

    def test_draft_saves_are_valid_but_export_requires_override(self):
        document = bridge.import_notebook(sample())
        document["graph"]["edges"] = []
        bridge.validate_document(document)
        with self.assertRaisesRegex(bridge.DocumentError, "allow-lossy"):
            bridge.export_notebook(document)
        self.assertEqual(bridge.export_notebook(document, allow_lossy=True), sample())

    def test_data_edges_and_trigger_configuration_require_override(self):
        document = bridge.import_notebook(sample())
        document["graph"]["edges"].append({"id": "data-x", "kind": "data", "source": "load", "target": "sum"})
        with self.assertRaises(bridge.DocumentError):
            bridge.export_notebook(document)
        document = bridge.import_notebook(sample())
        document["graph"]["nodes"][1]["trigger_policy"] = "any"
        with self.assertRaises(bridge.DocumentError):
            bridge.export_notebook(document)

    def test_cell_reordering_is_not_silently_exported(self):
        document = bridge.import_notebook(sample())
        for edge, pair in zip(document["graph"]["edges"], [("mainaiger-start", "sum"), ("sum", "load"), ("load", "mainaiger-finish")]):
            edge["source"], edge["target"] = pair
        with self.assertRaises(bridge.DocumentError):
            bridge.export_notebook(document)

    def test_output_protection_and_unicode_json(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "example.ipymb"
            document = bridge.import_notebook(sample())
            bridge._write(target, document, force=False)
            self.assertEqual(bridge._read(target), document)
            with self.assertRaisesRegex(bridge.DocumentError, "Output exists"):
                bridge._write(target, {}, force=False)


if __name__ == "__main__":
    unittest.main()
