#!/usr/bin/env python3
"""Lossless notebook storage inside a versioned MainAIger graph document.

This bridge does not execute code or serialize live Python state. Notebook
source/outputs remain canonical in ``notebook.cells``; graph nodes reference
their cell indices. JSON whitespace may change, but notebook data is preserved.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile


class DocumentError(ValueError):
    """The document is malformed or cannot be exported safely."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DocumentError(message)


def validate_notebook(notebook: dict) -> None:
    _require(isinstance(notebook, dict), "Notebook must be a JSON object.")
    _require(type(notebook.get("nbformat")) is int and notebook["nbformat"] == 4,
             "Only nbformat 4 notebooks are supported.")
    _require(isinstance(notebook.get("cells"), list), "Notebook cells must be an array.")
    for i, cell in enumerate(notebook["cells"]):
        _require(isinstance(cell, dict), f"Cell {i} must be an object.")
        _require(isinstance(cell.get("cell_type"), str) and cell["cell_type"] in {"code", "markdown", "raw"},
                 f"Cell {i} has an unsupported cell_type.")
        source = cell.get("source")
        _require(isinstance(source, str) or
                 (isinstance(source, list) and all(isinstance(s, str) for s in source)),
                 f"Cell {i} source must be text or an array of strings.")


def import_notebook(notebook: dict) -> dict:
    """Create a sequential graph without changing the supplied notebook."""
    validate_notebook(notebook)
    supplied = [c.get("id") for c in notebook["cells"]]
    used = {value for value in supplied if isinstance(value, str) and value}

    def unique(stem: str) -> str:
        candidate, suffix = stem, 1
        while candidate in used:
            candidate = f"{stem}-{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    start, finish = unique("mainaiger-start"), unique("mainaiger-finish")
    nodes = [{"id": start, "kind": "start"}]
    positions = {start: {"x": 320, "y": 24}}
    code_ids = []
    for i, cell in enumerate(notebook["cells"]):
        original_id = cell.get("id")
        cell_id = (original_id if isinstance(original_id, str) and original_id
                   and supplied.count(original_id) == 1 else unique(f"cell-{i + 1}"))
        nodes.append({"id": cell_id, "kind": cell["cell_type"], "cell_index": i})
        positions[cell_id] = {"x": 320, "y": 120 + i * 220}
        if cell["cell_type"] == "code":
            code_ids.append(cell_id)
    nodes.append({"id": finish, "kind": "finish"})
    positions[finish] = {"x": 320, "y": 120 + len(notebook["cells"]) * 220}
    chain = [start, *code_ids, finish]
    edges = [{"id": f"control-{i + 1}", "kind": "control", "source": a, "target": b}
             for i, (a, b) in enumerate(zip(chain, chain[1:]))]
    document = {"format": "mainaiger", "schema_version": 1,
                "notebook": copy.deepcopy(notebook), "graph": {"nodes": nodes, "edges": edges},
                "layout": {"nodes": positions, "viewport": {"x": 0, "y": 0, "zoom": 1}}}
    validate_document(document)
    return document


def validate_document(document: dict) -> None:
    """Validate structure and references; incomplete draft graphs remain valid."""
    _require(isinstance(document, dict), "MainAIger document must be an object.")
    _require(document.get("format") == "mainaiger", "Expected format: mainaiger.")
    _require(type(document.get("schema_version")) is int and document["schema_version"] == 1,
             "Unsupported schema_version; this bridge supports version 1.")
    validate_notebook(document.get("notebook"))
    graph = document.get("graph")
    _require(isinstance(graph, dict), "graph must be an object.")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    _require(isinstance(nodes, list) and isinstance(edges, list),
             "graph.nodes and graph.edges must be arrays.")
    ids, cell_indices = set(), set()
    cells = document["notebook"]["cells"]
    for node in nodes:
        _require(isinstance(node, dict), "Every graph node must be an object.")
        ident = node.get("id")
        _require(isinstance(ident, str) and bool(ident), "Node IDs must be nonempty strings.")
        _require(ident not in ids, f"Duplicate node ID: {ident}.")
        ids.add(ident)
        kind = node.get("kind")
        _require(isinstance(kind, str) and bool(kind), f"Node {ident} needs a kind.")
        if "cell_index" in node:
            index = node["cell_index"]
            _require(type(index) is int and 0 <= index < len(cells),
                     f"Node {ident} has an invalid cell_index.")
            _require(index not in cell_indices, f"Cell {index} has duplicate graph nodes.")
            _require(kind == cells[index]["cell_type"], f"Node {ident} kind does not match its cell.")
            cell_indices.add(index)
        elif kind in {"code", "markdown", "raw"}:
            raise DocumentError(f"Node {ident} needs a cell_index.")
    edge_ids = set()
    for edge in edges:
        _require(isinstance(edge, dict), "Every edge must be an object.")
        ident = edge.get("id")
        _require(isinstance(ident, str) and bool(ident), "Edge IDs must be nonempty strings.")
        _require(ident not in edge_ids, f"Duplicate edge ID: {ident}.")
        edge_ids.add(ident)
        _require(isinstance(edge.get("kind"), str) and bool(edge["kind"]),
                 f"Edge {ident} needs a kind.")
        for side in ("source", "target"):
            endpoint = edge.get(side)
            _require(isinstance(endpoint, str) and endpoint in ids,
                     f"Edge {ident} has an unknown {side} node.")
    _require(isinstance(document.get("layout", {}), dict), "layout must be an object.")


def is_sequential(document: dict) -> bool:
    """Whether notebook order fully represents this graph's execution semantics."""
    validate_document(document)
    graph = document["graph"]
    if set(graph) != {"nodes", "edges"}:
        return False
    nodes, edges = graph["nodes"], graph["edges"]
    starts = [n for n in nodes if n["kind"] == "start"]
    finishes = [n for n in nodes if n["kind"] == "finish"]
    cell_nodes = [n for n in nodes if "cell_index" in n]
    if len(starts) != 1 or len(finishes) != 1:
        return False
    if len(nodes) != len(cell_nodes) + 2 or len(cell_nodes) != len(document["notebook"]["cells"]):
        return False
    if any(set(n) - {"id", "kind", "cell_index", "title", "description"} for n in nodes):
        return False
    if any(set(e) - {"id", "kind", "source", "target"} or e["kind"] != "control" for e in edges):
        return False
    ordered = sorted((n for n in cell_nodes if n["kind"] == "code"), key=lambda n: n["cell_index"])
    chain = [starts[0]["id"], *(n["id"] for n in ordered), finishes[0]["id"]]
    expected = set(zip(chain, chain[1:]))
    actual = {(e["source"], e["target"]) for e in edges}
    return actual == expected and len(edges) == len(expected)


def export_notebook(document: dict, *, allow_lossy: bool = False) -> dict:
    validate_document(document)
    if not is_sequential(document) and not allow_lossy:
        raise DocumentError("Graph is incomplete or has nonsequential/advanced semantics. "
                            "Export would keep notebook order only; use --allow-lossy explicitly.")
    return copy.deepcopy(document["notebook"])


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentError(f"Cannot read {path}: {error}") from error


def _write(path: Path, value: dict, *, force: bool) -> None:
    _require(force or not path.exists(), f"Output exists: {path}; use --force to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "export", "validate"):
        child = commands.add_parser(command)
        child.add_argument("source", type=Path)
        if command != "validate":
            child.add_argument("destination", type=Path)
            child.add_argument("--force", action="store_true", help="Replace an existing output file")
        if command == "export":
            child.add_argument("--allow-lossy", action="store_true", help="Discard graph execution semantics")
    args = parser.parse_args(argv)
    try:
        source = _read(args.source)
        if args.command == "validate":
            validate_document(source)
            print("Valid MainAIger v1 document (structural check; not an execution-readiness check).")
            return 0
        _require(args.source.resolve() != args.destination.resolve(), "Source and destination must differ.")
        if args.command == "import":
            result = import_notebook(source)
        else:
            result = export_notebook(source, allow_lossy=args.allow_lossy)
            if not is_sequential(source):
                print("WARNING: Graph execution semantics are omitted; exported cells retain notebook order.", file=sys.stderr)
        _write(args.destination, result, force=args.force)
        print(f"Wrote {args.destination}. No code was executed.")
        return 0
    except (DocumentError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
