"""Graph documents, dependency analysis and scheduling for MainAIger Lab.

A graph document (``.ipymb``, schema version 2) wraps an ordinary Jupyter
notebook: the notebook holds the cells (code, outputs, metadata) and the graph
says how execution flows between them.

* **Nodes**: one ``start``, one ``finish``, and ``code``/``markdown`` nodes that
  each point at a notebook cell by ``cell_index``.
* **Control edges** are the triggers: ``A -> B`` means "when A finishes
  successfully, B may run". A node with several incoming control edges runs
  after all of them (join-all). Cycles are rejected in this version.
* **Data edges** are derived, not drawn by hand: a cell's *inputs* are the
  names it reads without defining; its *outputs* are the names it defines.
  The nearest upstream producer of each input is shown as a dashed data edge.

Execution is serial in one kernel session: the scheduler orders the nodes
topologically and runs them one at a time, so a branching picture never means
parallel execution. The analysis is advisory (static Python cannot see dynamic
names), and the user can override declared inputs/outputs per cell.
"""
import ast
import builtins
import copy
import re

SCHEMA_VERSION = 2
NODE_KINDS = {'start', 'finish', 'code', 'markdown'}
EDGE_KINDS = {'control', 'data'}
MAX_NODES, MAX_EDGES, MAX_SOURCE = 200, 1000, 100000
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
BUILTINS = set(dir(builtins))


class GraphError(ValueError):
    """The document is malformed or cannot be scheduled."""


# ---------------------------------------------------------------- documents
def new_document():
    """A minimal valid document: Start -> one empty code cell -> Finish."""
    return {
        'format': 'mainaiger', 'schema_version': SCHEMA_VERSION,
        'notebook': {'nbformat': 4, 'nbformat_minor': 5,
                     'metadata': {'kernelspec': {'name': 'python3', 'display_name': 'Python 3', 'language': 'python'}},
                     'cells': [{'id': 'cell-1', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'First cell'}},
                                'source': 'print("Hello from the graph")', 'execution_count': None, 'outputs': []}]},
        'graph': {'nodes': [{'id': 'start', 'kind': 'start'}, {'id': 'cell-1', 'kind': 'code', 'cell_index': 0},
                            {'id': 'finish', 'kind': 'finish'}],
                  'edges': [{'id': 'e1', 'kind': 'control', 'source': 'start', 'target': 'cell-1'},
                            {'id': 'e2', 'kind': 'control', 'source': 'cell-1', 'target': 'finish'}]},
        'layout': {'nodes': {'start': {'x': 360, 'y': 40}, 'cell-1': {'x': 220, 'y': 160}, 'finish': {'x': 360, 'y': 440}},
                   'viewport': {'x': 0, 'y': 0, 'zoom': 1}},
    }


def _require(condition, message):
    if not condition:
        raise GraphError(message)


def source_text(cell):
    source = cell.get('source', '')
    return ''.join(source) if isinstance(source, list) else source


def validate(document):
    """Check structure and references; return a normalized deep copy (schema 2)."""
    _require(isinstance(document, dict), 'Document must be an object')
    _require(document.get('format') == 'mainaiger', 'Expected format: mainaiger')
    _require(document.get('schema_version') in (1, 2), 'Unsupported schema_version; this Lab reads versions 1 and 2')
    doc = copy.deepcopy(document)
    doc['schema_version'] = SCHEMA_VERSION
    notebook = doc.get('notebook')
    _require(isinstance(notebook, dict) and notebook.get('nbformat') == 4, 'Notebook must be nbformat 4')
    cells = notebook.get('cells')
    _require(isinstance(cells, list) and len(cells) <= MAX_NODES, 'Notebook cells must be a list of at most %d' % MAX_NODES)
    cell_ids = set()
    for i, cell in enumerate(cells):
        _require(isinstance(cell, dict), 'Cell %d must be an object' % i)
        _require(cell.get('cell_type') in ('code', 'markdown', 'raw'), 'Cell %d has an unsupported cell_type' % i)
        source = cell.get('source', '')
        _require(isinstance(source, str) or (isinstance(source, list) and all(isinstance(s, str) for s in source)),
                 'Cell %d source must be text' % i)
        _require(len(source_text(cell).encode('utf-8')) <= MAX_SOURCE, 'Cell %d is too large' % i)
        if 'id' in cell:
            _require(isinstance(cell['id'], str) and 0 < len(cell['id']) <= 64 and cell['id'] not in cell_ids, 'Cell %d needs a unique id' % i)
            cell_ids.add(cell['id'])
        meta = cell.setdefault('metadata', {})
        _require(isinstance(meta, dict), 'Cell %d metadata must be an object' % i)
        own = meta.setdefault('mainaiger', {})
        _require(isinstance(own, dict), 'Cell %d mainaiger metadata must be an object' % i)
        for key in ('inputs', 'outputs'):
            if key in own:
                _require(isinstance(own[key], list) and all(isinstance(n, str) and NAME.match(n) for n in own[key]) and len(own[key]) <= 100,
                         'Cell %d declared %s must be a list of Python names' % (i, key))
        if 'title' in own:
            _require(isinstance(own['title'], str) and len(own['title']) <= 120, 'Cell %d title too long' % i)
        if not isinstance(cell.get('outputs'), list):
            cell['outputs'] = []
        if cell['cell_type'] == 'code' and not isinstance(cell.get('execution_count'), (int, type(None))):
            cell['execution_count'] = None
    graph = doc.get('graph')
    _require(isinstance(graph, dict), 'graph must be an object')
    nodes, edges = graph.get('nodes'), graph.get('edges')
    _require(isinstance(nodes, list) and isinstance(edges, list), 'graph.nodes and graph.edges must be lists')
    _require(len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES, 'Graph is too large')
    kinds, used_cells = {}, set()
    for node in nodes:
        _require(isinstance(node, dict), 'Every node must be an object')
        ident = node.get('id')
        _require(isinstance(ident, str) and 0 < len(ident) <= 64 and ident not in kinds, 'Node ids must be unique nonempty strings')
        kind = node.get('kind')
        _require(kind in NODE_KINDS, 'Node %s has an unsupported kind' % ident)
        kinds[ident] = kind
        if kind in ('code', 'markdown'):
            index = node.get('cell_index')
            _require(type(index) is int and 0 <= index < len(cells) and index not in used_cells, 'Node %s has an invalid cell_index' % ident)
            _require(cells[index]['cell_type'] == kind, 'Node %s kind does not match its cell' % ident)
            used_cells.add(index)
        else:
            _require('cell_index' not in node, 'Start and finish nodes have no cell')
    _require(sum(1 for k in kinds.values() if k == 'start') == 1, 'Exactly one start node is required')
    _require(sum(1 for k in kinds.values() if k == 'finish') == 1, 'Exactly one finish node is required')
    edge_ids, seen = set(), set()
    for edge in edges:
        _require(isinstance(edge, dict), 'Every edge must be an object')
        ident = edge.get('id')
        _require(isinstance(ident, str) and 0 < len(ident) <= 64 and ident not in edge_ids, 'Edge ids must be unique nonempty strings')
        edge_ids.add(ident)
        kind = edge.get('kind')
        _require(kind in EDGE_KINDS, 'Edge %s has an unsupported kind' % ident)
        source, target = edge.get('source'), edge.get('target')
        _require(source in kinds and target in kinds, 'Edge %s references an unknown node' % ident)
        _require(source != target, 'Edge %s connects a node to itself' % ident)
        if kind == 'control':
            _require(kinds[source] != 'finish', 'Edge %s: nothing follows finish' % ident)
            _require(kinds[target] != 'start', 'Edge %s: nothing leads into start' % ident)
            _require(kinds[source] != 'markdown' and kinds[target] != 'markdown', 'Edge %s: markdown nodes do not execute' % ident)
            key = ('control', source, target)
        else:
            name = edge.get('name')
            _require(isinstance(name, str) and NAME.match(name), 'Edge %s needs a Python name' % ident)
            _require(kinds[source] == 'code' and kinds[target] == 'code', 'Edge %s: data edges join code nodes' % ident)
            key = ('data', source, target, name)
        _require(key not in seen, 'Edge %s duplicates another edge' % ident)
        seen.add(key)
    layout = doc.setdefault('layout', {})
    _require(isinstance(layout, dict), 'layout must be an object')
    positions = layout.setdefault('nodes', {})
    _require(isinstance(positions, dict), 'layout.nodes must be an object')
    for ident, pos in list(positions.items()):
        if ident not in kinds or not isinstance(pos, dict) or not all(isinstance(pos.get(k), (int, float)) for k in ('x', 'y')):
            del positions[ident]
        else:
            positions[ident] = {'x': float(pos['x']), 'y': float(pos['y'])}
    layout.setdefault('viewport', {'x': 0, 'y': 0, 'zoom': 1})
    order(doc)  # raises on control cycles
    return doc


# ---------------------------------------------------------------- analysis
class _Names(ast.NodeVisitor):
    def __init__(self):
        self.loaded, self.bound = [], set()

    def _bind(self, target):
        if isinstance(target, ast.Name):
            self.bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element)
        elif isinstance(target, ast.Starred):
            self._bind(target.value)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            if node.id not in self.loaded:
                self.loaded.append(node.id)
        else:
            self.bound.add(node.id)

    def visit_Import(self, node):
        for alias in node.names:
            self.bound.add((alias.asname or alias.name).split('.')[0])
    visit_ImportFrom = visit_Import

    def visit_FunctionDef(self, node):
        self.bound.add(node.name)
        for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs + [a for a in (node.args.vararg, node.args.kwarg) if a]:
            self.bound.add(arg.arg)
        self.generic_visit(node)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Lambda(self, node):
        for arg in node.args.args + node.args.kwonlyargs:
            self.bound.add(arg.arg)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.generic_visit(node)


def analyze(source):
    """Advisory inputs/outputs of a cell: names read but not defined, names defined.

    Static analysis cannot see ``globals()[name]``, ``exec`` or ``from x import *``,
    so these lists are suggestions the user may override in the node's settings.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return {'inputs': [], 'outputs': [], 'error': 'SyntaxError: %s (line %s)' % (error.msg, error.lineno)}
    visitor = _Names()
    visitor.visit(tree)
    top_level = set()
    for statement in tree.body:
        collector = _Names()
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level.add(statement.name)
            continue
        collector.visit(statement)
        # Names bound anywhere in a top-level statement that is not a def/class
        # (assignment targets, loop variables, imports, walrus) become module globals.
        top_level.update(collector.bound - _comprehension_locals(statement))
    inputs = [n for n in visitor.loaded if n not in visitor.bound and n not in BUILTINS]
    outputs = sorted(top_level - {'_'})
    return {'inputs': inputs, 'outputs': outputs, 'error': None}


def _comprehension_locals(statement):
    names = set()
    for node in ast.walk(statement):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in node.generators:
                collector = _Names()
                collector._bind(generator.target)
                names |= collector.bound
        elif isinstance(node, ast.Lambda):
            names |= {a.arg for a in node.args.args + node.args.kwonlyargs}
    return names


def declared(doc, node_id):
    """Inputs/outputs for a code node: the cell's declaration if set, else the analysis."""
    node = node_by_id(doc, node_id)
    cell = doc['notebook']['cells'][node['cell_index']]
    own = cell.get('metadata', {}).get('mainaiger', {})
    result = analyze(source_text(cell))
    if 'inputs' in own:
        result['inputs'] = list(own['inputs'])
    if 'outputs' in own:
        result['outputs'] = list(own['outputs'])
    result['declared'] = 'inputs' in own or 'outputs' in own
    return result


def analysis(doc):
    return {node['id']: declared(doc, node['id']) for node in doc['graph']['nodes'] if node['kind'] == 'code'}


# ---------------------------------------------------------------- scheduling
def node_by_id(doc, node_id):
    for node in doc['graph']['nodes']:
        if node['id'] == node_id:
            return node
    raise GraphError('Unknown node %s' % node_id)


def control_edges(doc):
    return [e for e in doc['graph']['edges'] if e['kind'] == 'control']


def order(doc):
    """Topological order of all nodes by control edges (stable: document order breaks ties)."""
    nodes = [n['id'] for n in doc['graph']['nodes']]
    position = {ident: i for i, ident in enumerate(nodes)}
    incoming = {ident: 0 for ident in nodes}
    outgoing = {ident: [] for ident in nodes}
    for edge in control_edges(doc):
        incoming[edge['target']] += 1
        outgoing[edge['source']].append(edge['target'])
    ready = sorted((ident for ident in nodes if incoming[ident] == 0), key=position.get)
    result = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for nxt in outgoing[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=position.get)
    if len(result) != len(nodes):
        raise GraphError('Control edges form a cycle; loops are not supported yet')
    return result


def reachable(doc, roots, downstream=True):
    """Node ids reachable from roots along control edges (including the roots)."""
    adjacency = {}
    for edge in control_edges(doc):
        a, b = (edge['source'], edge['target']) if downstream else (edge['target'], edge['source'])
        adjacency.setdefault(a, []).append(b)
    seen, stack = set(), list(roots)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, []))
    return seen


def code_nodes(doc):
    return [n['id'] for n in doc['graph']['nodes'] if n['kind'] == 'code']


def plan(doc, mode, selected=()):
    """Ordered code nodes to execute for a run mode.

    all: every code node reachable from start. from: the selected nodes and
    everything downstream of them. one/selected: exactly the selected nodes.
    """
    codes = set(code_nodes(doc))
    selected = [s for s in selected if s in codes]
    if mode == 'all':
        start = next(n['id'] for n in doc['graph']['nodes'] if n['kind'] == 'start')
        chosen = reachable(doc, [start]) & codes
    elif mode == 'from':
        _require(selected, 'Select a code node to run from')
        chosen = reachable(doc, selected) & codes
    elif mode in ('one', 'selected'):
        _require(selected, 'Select a code node to run')
        chosen = set(selected)
    else:
        raise GraphError('Unknown run mode')
    ordered = [ident for ident in order(doc) if ident in chosen]
    return ordered


def disconnected(doc):
    start = next(n['id'] for n in doc['graph']['nodes'] if n['kind'] == 'start')
    return [ident for ident in code_nodes(doc) if ident not in reachable(doc, [start])]


def data_edges(doc):
    """Derived data edges: nearest upstream producer for each input of each code node."""
    info = analysis(doc)
    topo = [ident for ident in order(doc) if ident in info]
    edges = []
    for index, consumer in enumerate(topo):
        ancestors = reachable(doc, [consumer], downstream=False)
        for name in info[consumer]['inputs']:
            producer = None
            for candidate in reversed(topo[:index]):
                if name in info[candidate]['outputs'] and candidate in ancestors:
                    producer = candidate
                    break
            if producer is None:
                for candidate in reversed(topo[:index]):
                    if name in info[candidate]['outputs']:
                        producer = candidate
                        break
            if producer is not None:
                edges.append({'id': 'data:%s:%s:%s' % (producer, consumer, name), 'kind': 'data', 'source': producer,
                              'target': consumer, 'name': name, 'derived': True})
    return edges


def preflight(doc, plan_ids, present_names=()):
    """Which declared inputs would be missing if plan_ids ran now, and who could produce them."""
    info = analysis(doc)
    produced, missing, from_session = set(), [], []
    present = set(present_names)
    topo = [ident for ident in order(doc) if ident in info]
    for ident in plan_ids:
        for name in info[ident]['inputs']:
            if name in produced:
                continue
            if name in present:
                from_session.append({'node': ident, 'name': name})
                continue
            ancestors = reachable(doc, [ident], downstream=False)
            producers = [c for c in topo if c != ident and name in info[c]['outputs'] and c not in plan_ids]
            producers.sort(key=lambda c: (c not in ancestors, topo.index(c)))
            missing.append({'node': ident, 'name': name, 'producers': producers})
        produced.update(info[ident]['outputs'])
    prerequisites = set()
    for item in missing:
        if item['producers']:
            best = item['producers'][0]
            prerequisites |= reachable(doc, [best], downstream=False) & set(info)
    prerequisites -= set(plan_ids)
    return {'plan': list(plan_ids), 'missing': missing, 'from_session': from_session,
            'prerequisites': [ident for ident in topo if ident in prerequisites],
            'unresolvable': [m for m in missing if not m['producers']]}


def record_result(doc, node_id, result):
    """Store a cell's run result in the notebook the way Jupyter would."""
    node = node_by_id(doc, node_id)
    cell = doc['notebook']['cells'][node['cell_index']]
    cell['execution_count'] = result.get('count') or None
    outputs = []
    if result.get('output'):
        outputs.append({'output_type': 'stream', 'name': 'stdout', 'text': result['output']})
    error = result.get('error')
    if error:
        outputs.append({'output_type': 'error', 'ename': error.get('ename', ''), 'evalue': error.get('evalue', ''),
                        'traceback': list(error.get('traceback', []))})
    cell['outputs'] = outputs
    cell.setdefault('metadata', {}).setdefault('mainaiger', {})['last_run'] = {
        'status': result.get('status'), 'elapsed_ms': result.get('elapsed_ms'), 'count': result.get('count')}
