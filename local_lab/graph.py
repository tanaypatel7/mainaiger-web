"""Graph documents, dependency analysis and scheduling for MainAIger Lab.

A graph document (``.ipymb``, schema version 2) wraps an ordinary Jupyter
notebook: the notebook holds the cells (code, outputs, metadata) and the graph
says how execution flows between them.

* **Nodes**: one ``start``, one ``finish``, and ``code``/``markdown`` nodes that
  each point at a notebook cell by ``cell_index``.
* **Control edges** are the triggers: ``A -> B`` means "when A finishes
  successfully, the arrow fires and B runs". An edge may carry ``when`` (one
  Python expression the source cell's session answers; the arrow fires only if
  it is true) and a *loop* edge (``loop: true`` with a ``max_fires`` budget)
  leads back to an earlier cell so its body runs again. Cycles among forward
  (non-loop) edges are rejected. A node with several incoming edges runs once
  all of them are decided, if at least one fired (graph_run.py).
* **Data edges** are derived, not drawn by hand: a cell's *inputs* are the
  names it reads without defining; its *outputs* are the names it defines.
  The nearest upstream producer of each input is shown as a dashed data edge.

Execution is serial: the scheduler orders the nodes topologically along the
forward edges and runs them one at a time in their sessions, so a branching
picture never means parallel execution. The analysis is advisory (static Python cannot see dynamic
names), and the user can override declared inputs/outputs per cell.
"""
import ast
import builtins
import copy
import re

SCHEMA_VERSION = 2
NODE_KINDS = {'start', 'finish', 'code', 'markdown'}
EDGE_KINDS = {'control', 'data'}
MAX_CONDITION = 300         # characters of a trigger condition (one Python expression)
MAX_NESTING = 3             # a Mainbook inside a Mainbook inside a Mainbook, and no deeper
LOOP_DEFAULT_FIRES = 3      # a loop arrow drawn without a budget runs back this many times at most
LOOP_MAX_FIRES = 100
MAX_NODES, MAX_EDGES, MAX_SOURCE = 200, 1000, 100000
FORM_TYPES = ('text', 'number', 'integer', 'flag', 'choice')


def _form_value_ok(kind, value, options=None):
    if kind == 'text':
        return isinstance(value, str) and len(value) <= 4000
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < 1e300
    if kind == 'integer':
        return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 10 ** 15
    if kind == 'flag':
        return isinstance(value, bool)
    if kind == 'choice':
        return isinstance(value, str) and value in (options or [])
    return False


def form_config(cell):
    own = cell.get('metadata', {}).get('mainaiger', {}) if isinstance(cell.get('metadata'), dict) else {}
    form = own.get('form')
    return form if isinstance(form, dict) and isinstance(form.get('fields'), list) and form['fields'] else None


def form_bindings(form):
    """{name: value} the form binds when its cell runs: the saved value, else the default, else a type-appropriate empty value."""
    out = {}
    values = form.get('values') or {}
    for f in form['fields']:
        if f['name'] in values:
            out[f['name']] = values[f['name']]
        elif 'default' in f:
            out[f['name']] = f['default']
        else:
            out[f['name']] = {'text': '', 'number': 0.0, 'integer': 0, 'flag': False, 'choice': (f.get('options') or [''])[0]}[f['type']]
    return out


def form_prelude(form):
    """The assignment lines that bind a form's values, as Python; a JSON literal per value keeps it exact."""
    import json as _json
    return ''.join('%s = __import__("json").loads(%r)\n' % (name, _json.dumps(value)) for name, value in form_bindings(form).items())


MODEL_NAME = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,80}(:[a-z0-9][a-z0-9._-]{0,40})?$')
PLACEHOLDER = re.compile(r'\{\{\s*(var|output)\s*:\s*([^{}\n]+?)\s*\}\}')  # {{output:Cell title with spaces}} / {{var:name}}
SESSION_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,30}(/[A-Za-z0-9][A-Za-z0-9 _-]{0,30})?$')  # "label" or "device/label"
MAINBOOK_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,80}$')  # a saved Mainbook a cell may run (local_lab/mainbooks.py NAME)
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
                     'cells': [{'id': 'cell-1', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'Cell 1'}},
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
    nb_meta = notebook.setdefault('metadata', {})
    _require(isinstance(nb_meta, dict), 'Notebook metadata must be an object')
    nb_own = nb_meta.setdefault('mainaiger', {})
    _require(isinstance(nb_own, dict), 'Notebook mainaiger metadata must be an object')
    if 'environment' in nb_own:
        env = nb_own['environment']
        _require(isinstance(env, dict) and set(env) <= {'packages'}, 'environment must be {packages: [...]}')
        from . import environment as environment_module
        try:
            packages = environment_module.parse_packages(env.get('packages'))
        except environment_module.EnvironmentSpecError as error:
            raise GraphError('environment: %s' % error) from None
        if packages:
            nb_own['environment'] = {'packages': packages}
        else:
            del nb_own['environment']
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
        if 'model' in own:
            # A model-backed cell: the source is a prompt, not Python. The Lab asks the named local model and
            # binds the (optionally schema-checked) answer to `output` in the session. See docs/LOCAL_LAB_SETUP.md.
            cfg = own['model']
            if cfg in (None, False, {}):
                del own['model']
            else:
                _require(isinstance(cfg, dict) and set(cfg) <= {'model', 'output', 'schema', 'attempts'}, 'Cell %d model config must be {model, output, schema?, attempts?}' % i)
                _require(isinstance(cfg.get('model'), str) and MODEL_NAME.match(cfg['model']), 'Cell %d needs a model name like llama3.2:3b' % i)
                _require(isinstance(cfg.get('output'), str) and NAME.match(cfg['output']), 'Cell %d model output must be a Python name' % i)
                if cfg.get('schema') is not None:
                    from . import decide as decide_module
                    try:
                        cfg['schema'] = decide_module.check_schema(cfg['schema'])
                    except decide_module.DecisionError as error:
                        raise GraphError('Cell %d model schema: %s' % (i, error)) from None
                else:
                    cfg.pop('schema', None)
                if 'attempts' in cfg:
                    _require(isinstance(cfg['attempts'], int) and not isinstance(cfg['attempts'], bool) and 1 <= cfg['attempts'] <= 3, 'Cell %d model attempts must be 1-3' % i)
                _require(cell['cell_type'] == 'code', 'Cell %d: only code cells can ask a model' % i)
        if 'form' in own:
            # A form cell: fields the owner fills in on the card; their values are bound as variables
            # when the cell runs, before its own code (if any). See docs/LOCAL_LAB_SETUP.md "Form cells".
            form = own['form']
            if form in (None, False, {}):
                del own['form']
            else:
                _require(isinstance(form, dict) and set(form) <= {'fields', 'values'} and isinstance(form.get('fields'), list) and 0 < len(form['fields']) <= 12,
                         'Cell %d form must be {fields: [1..12 fields], values?}' % i)
                _require(cell['cell_type'] == 'code', 'Cell %d: only code cells can carry a form' % i)
                seen = set()
                fields = []
                for f in form['fields']:
                    _require(isinstance(f, dict) and set(f) <= {'name', 'type', 'label', 'options', 'default'} and isinstance(f.get('name'), str) and NAME.match(f['name']) and f['name'] not in seen,
                             'Cell %d: each field needs a unique Python name' % i)
                    seen.add(f['name'])
                    kind = f.get('type', 'text')
                    _require(kind in FORM_TYPES, 'Cell %d: field "%s" type must be one of %s' % (i, f['name'], ', '.join(FORM_TYPES)))
                    clean = {'name': f['name'], 'type': kind}
                    if 'label' in f:
                        _require(isinstance(f['label'], str) and len(f['label']) <= 80, 'Cell %d: field label too long' % i)
                        clean['label'] = f['label']
                    if kind == 'choice':
                        opts = f.get('options')
                        _require(isinstance(opts, list) and 0 < len(opts) <= 20 and all(isinstance(o, str) and 0 < len(o) <= 60 for o in opts), 'Cell %d: choice field "%s" needs 1-20 text options' % (i, f['name']))
                        clean['options'] = list(opts)
                    if 'default' in f and f['default'] is not None:
                        _require(_form_value_ok(kind, f['default'], clean.get('options')), 'Cell %d: default of "%s" does not fit its type' % (i, f['name']))
                        clean['default'] = f['default']
                    fields.append(clean)
                values = form.get('values') or {}
                _require(isinstance(values, dict), 'Cell %d form values must be an object' % i)
                clean_values = {}
                for f in fields:
                    if f['name'] in values and values[f['name']] is not None:
                        _require(_form_value_ok(f['type'], values[f['name']], f.get('options')), 'Cell %d: value of "%s" does not fit its type' % (i, f['name']))
                        clean_values[f['name']] = values[f['name']]
                own['form'] = {'fields': fields, 'values': clean_values}
        if 'mainbook' in own:
            # A nested Mainbook: the cell runs a saved Mainbook (all of it) in the same session; its code is ignored.
            book = own['mainbook']
            if book in (None, False, {}, ''):
                del own['mainbook']
            else:
                _require(isinstance(book, dict) and set(book) == {'name'} and isinstance(book['name'], str) and MAINBOOK_NAME.match(book['name']) and book['name'].strip() == book['name'],
                         'Cell %d mainbook must be {name: <a saved Mainbook>}' % i)
                _require(cell['cell_type'] == 'code', 'Cell %d: only code cells can run a Mainbook' % i)
                _require('model' not in own and 'form' not in own, 'Cell %d: a cell runs a Mainbook, asks a model or carries a form, not several' % i)
        if 'session' in own:
            # The label of the session this cell runs on; absent means "the active session".
            if own['session'] in (None, ''):
                del own['session']
            else:
                _require(isinstance(own['session'], str) and SESSION_LABEL.match(own['session']), 'Cell %d "run on" must be a session label' % i)
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
            if kind == 'start' and 'events' in node:
                # What may fire this Mainbook from the outside once the owner arms it (local_lab/events.py).
                from . import events as events_module
                try:
                    sources = events_module.check_sources(node['events'])
                except events_module.EventError as error:
                    raise GraphError('Start events: %s' % error) from None
                if sources:
                    node['events'] = sources
                else:
                    del node['events']
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
            # A condition: one Python expression the source cell's session answers after the cell finishes;
            # the arrow fires only when it is true. A loop arrow leads back to an earlier cell and carries a
            # budget (max_fires). See docs/LOCAL_LAB_SETUP.md "Loop arrows and conditions".
            when = edge.get('when')
            if when in (None, ''):
                edge.pop('when', None)
            else:
                _require(isinstance(when, str) and len(when) <= MAX_CONDITION and '\n' not in when, 'Edge %s: a condition is one line of at most %d characters' % (ident, MAX_CONDITION))
                try:
                    ast.parse(when.strip(), '<condition>', 'eval')
                except SyntaxError as error:
                    raise GraphError('Edge %s: the condition is not a Python expression (%s)' % (ident, error.msg)) from None
                edge['when'] = when.strip()
            if edge.get('loop'):
                _require(edge['loop'] is True, 'Edge %s: loop must be true or absent' % ident)
                _require(kinds[source] == 'code' and kinds[target] == 'code', 'Edge %s: a loop arrow joins two code cells' % ident)
                fires = edge.get('max_fires', LOOP_DEFAULT_FIRES)
                _require(isinstance(fires, int) and not isinstance(fires, bool) and 1 <= fires <= LOOP_MAX_FIRES, 'Edge %s: max_fires must be between 1 and %d' % (ident, LOOP_MAX_FIRES))
                edge['max_fires'] = fires
            else:
                edge.pop('loop', None)
                _require('max_fires' not in edge, 'Edge %s: only loop arrows have max_fires' % ident)
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
    order(doc)  # raises on cycles among forward (non-loop) control edges
    for edge in loop_edges(doc):
        # A loop arrow must lead back: its target is upstream of its source along the forward arrows,
        # so firing it re-runs a body that ends at the source.
        _require(edge['source'] in reachable(doc, [edge['target']]),
                 'Edge %s is marked as a loop but %s does not lead forward to %s; a loop arrow goes back to an earlier cell' % (edge['id'], edge['target'], edge['source']))
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


def model_config(cell):
    own = cell.get('metadata', {}).get('mainaiger', {}) if isinstance(cell.get('metadata'), dict) else {}
    cfg = own.get('model')
    return cfg if isinstance(cfg, dict) and cfg.get('model') and cfg.get('output') else None


def prompt_placeholders(text):
    """[(kind, name)] for every {{var:name}} / {{output:cell}} in a prompt, in order, de-duplicated."""
    return list(dict.fromkeys((m.group(1), m.group(2)) for m in PLACEHOLDER.finditer(text or '')))


def mainbook_config(cell):
    own = cell.get('metadata', {}).get('mainaiger', {}) or {}
    book = own.get('mainbook')
    return book if isinstance(book, dict) and book.get('name') else None


def _inner_names(inner, resolve, depth):
    """What a whole Mainbook reads from the session and defines in it, from its own analysis."""
    info = analysis(inner, resolve, depth + 1)
    produced, needed = set(), []
    for ident in order(inner):
        if ident not in info:
            continue
        for name in info[ident]['inputs']:
            if name not in produced and name not in needed:
                needed.append(name)
        produced.update(info[ident]['outputs'])
    return needed, sorted(produced)


def declared(doc, node_id, resolve=None, depth=0):
    """Inputs/outputs for a code node: the cell's declaration if set, else the analysis.
    ``resolve(name)`` loads a saved Mainbook (or returns None) so a nested-Mainbook cell reads as its contents."""
    node = node_by_id(doc, node_id)
    cell = doc['notebook']['cells'][node['cell_index']]
    own = cell.get('metadata', {}).get('mainaiger', {})
    book = mainbook_config(cell)
    if book:
        result = {'inputs': [], 'outputs': [], 'error': None, 'mainbook': book['name'], 'declared': False, 'resolved': False}
        inner = None
        if resolve is not None and depth < MAX_NESTING:
            try:
                inner = resolve(book['name'])
            except Exception as error:  # a missing or broken Mainbook is reported on the card, not raised
                result['error'] = 'Mainbook "%s": %s' % (book['name'], error)
        if inner is not None:
            result['inputs'], result['outputs'] = _inner_names(inner, resolve, depth)
            result['resolved'] = True
            result['cells'] = len(code_nodes(inner))
        elif result['error'] is None:
            result['error'] = None if resolve is None else 'Mainbook "%s" was not found' % book['name']
        if 'inputs' in own:
            result['inputs'] = list(own['inputs'])
        if 'outputs' in own:
            result['outputs'] = list(own['outputs'])
        result['declared'] = 'inputs' in own or 'outputs' in own
        return result
    cfg = model_config(cell)
    if cfg:
        # The source is a prompt: it reads the variables it mentions and defines the output name.
        result = {'inputs': [n for k, n in prompt_placeholders(source_text(cell)) if k == 'var' and NAME.match(n)], 'outputs': [cfg['output']], 'error': None, 'model': cfg['model']}
        if 'inputs' in own:
            result['inputs'] = list(own['inputs'])
        result['declared'] = True
        return result
    result = analyze(source_text(cell))
    form = form_config(cell)
    if form:
        names = [f['name'] for f in form['fields']]
        result['inputs'] = [n for n in result['inputs'] if n not in names]
        result['outputs'] = sorted(set(result['outputs']) | set(names))
        result['form'] = True
    if 'inputs' in own:
        result['inputs'] = list(own['inputs'])
    if 'outputs' in own:
        result['outputs'] = list(own['outputs'])
    result['declared'] = 'inputs' in own or 'outputs' in own
    return result


def analysis(doc, resolve=None, depth=0):
    return {node['id']: declared(doc, node['id'], resolve, depth) for node in doc['graph']['nodes'] if node['kind'] == 'code'}


def mainbook_cells(doc):
    """[(node id, Mainbook name)] for the cells that run another Mainbook."""
    out = []
    for node in doc['graph']['nodes']:
        if node['kind'] == 'code':
            book = mainbook_config(doc['notebook']['cells'][node['cell_index']])
            if book:
                out.append((node['id'], book['name']))
    return out


# ---------------------------------------------------------------- scheduling
def node_by_id(doc, node_id):
    for node in doc['graph']['nodes']:
        if node['id'] == node_id:
            return node
    raise GraphError('Unknown node %s' % node_id)


def control_edges(doc):
    return [e for e in doc['graph']['edges'] if e['kind'] == 'control']


def forward_edges(doc):
    """Control edges that are not loops: the acyclic picture that gives the order."""
    return [e for e in doc['graph']['edges'] if e['kind'] == 'control' and not e.get('loop')]


def loop_edges(doc):
    return [e for e in doc['graph']['edges'] if e['kind'] == 'control' and e.get('loop')]


def order(doc):
    """Topological order of all nodes by forward control edges (stable: document order breaks ties)."""
    nodes = [n['id'] for n in doc['graph']['nodes']]
    position = {ident: i for i, ident in enumerate(nodes)}
    incoming = {ident: 0 for ident in nodes}
    outgoing = {ident: [] for ident in nodes}
    for edge in forward_edges(doc):
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
        raise GraphError('Control edges form a cycle; mark the arrow that goes back as a loop (it then carries a budget)')
    return result


def reachable(doc, roots, downstream=True):
    """Node ids reachable from roots along forward control edges (including the roots)."""
    adjacency = {}
    for edge in forward_edges(doc):
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


def start_events(doc):
    """The event sources on the Start node (what may fire this Mainbook once armed)."""
    start = next(n for n in doc['graph']['nodes'] if n['kind'] == 'start')
    return list(start.get('events') or [])


def fingerprint(doc):
    """A hash of what a run depends on: cell sources and settings, nodes and edges. Layout, outputs and
    counts are left out, so moving a card or running does not count as a change."""
    import json
    cells = [{'t': c.get('cell_type'), 's': source_text(c), 'm': {k: v for k, v in (c.get('metadata', {}).get('mainaiger', {}) or {}).items() if k != 'last_run'}} for c in doc['notebook']['cells']]
    graph = {'nodes': doc['graph']['nodes'], 'edges': doc['graph']['edges']}
    return source_hash(json.dumps([cells, graph], sort_keys=True, default=str))


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


def session_of(doc, node_id):
    """The session label a code node names, or None for the active session."""
    node = node_by_id(doc, node_id)
    if not node or node['kind'] != 'code':
        return None
    own = doc['notebook']['cells'][node['cell_index']].get('metadata', {}).get('mainaiger', {})
    return own.get('session') or None


def cross_session(doc, plan_ids):
    """Data edges inside the plan whose producer and consumer run in different sessions.
    Sessions are separate processes: the variable will not be there. Advisory, like data edges."""
    chosen = set(plan_ids)
    out = []
    for edge in data_edges(doc):
        if edge['source'] in chosen and edge['target'] in chosen:
            a, b = session_of(doc, edge['source']), session_of(doc, edge['target'])
            if a != b:
                out.append({'name': edge['name'], 'producer': edge['source'], 'consumer': edge['target'],
                            'producer_session': a, 'consumer_session': b})
    return out


def data_edges(doc, resolve=None):
    """Derived data edges: nearest upstream producer for each input of each code node."""
    info = analysis(doc, resolve)
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


def preflight(doc, plan_ids, present_names=(), resolve=None):
    """Which declared inputs would be missing if plan_ids ran now, and who could produce them."""
    info = analysis(doc, resolve)
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


def source_hash(text):
    """32-bit FNV-1a of the source, as 8 hex chars; the browser computes the same to flag stale outputs."""
    value = 0x811c9dc5
    for byte in text.encode('utf-8'):
        value = ((value ^ byte) * 0x01000193) & 0xffffffff
    return '%08x' % value


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
        'status': result.get('status'), 'elapsed_ms': result.get('elapsed_ms'), 'count': result.get('count'),
        'source_hash': source_hash(source_text(cell))}


# ---------------------------------------------------------------- notebook import
RISKY = (('import *', 'from … import * hides which names are defined'), ('exec(', 'exec() can define names invisibly'),
         ('globals()', 'globals() access is invisible to the analysis'), ('get_ipython', 'IPython magics are not available in this kernel'))


def _cell_title(cell, index, number, heading):
    own = cell.get('metadata', {}).get('mainaiger', {}) if isinstance(cell.get('metadata'), dict) else {}
    if isinstance(own.get('title'), str) and own['title'].strip():
        return own['title'].strip()[:120]
    if cell['cell_type'] == 'markdown':
        first = source_text(cell).strip().splitlines()
        return (first[0].lstrip('# ').strip()[:120] if first else 'Note') or 'Note'
    first = source_text(cell).strip().splitlines()
    if first and first[0].startswith('#') and len(first[0]) > 2:
        return first[0].lstrip('# ').strip()[:120]
    return heading[:100] + (' (%d)' % number) if heading else 'Cell %d' % number


def from_notebook(notebook):
    """Convert an nbformat-4 notebook into a schema-2 document plus an import report.

    Notebook order is the execution truth, so code cells form one chain
    Start -> cell -> ... -> Finish. Markdown cells become notes (not executed).
    Existing ids, outputs and execution counts are kept. The report lists what a
    reader should know: inputs no earlier cell produces (hidden-state
    candidates), constructs the static analysis cannot see, and magics.
    """
    _require(isinstance(notebook, dict) and notebook.get('nbformat') == 4 and isinstance(notebook.get('cells'), list),
             'Choose an nbformat 4 notebook (.ipynb)')
    _require(len(notebook['cells']) <= MAX_NODES, 'The notebook has more than %d cells' % MAX_NODES)
    used, cells, nodes, edges = set(), [], [{'id': 'start', 'kind': 'start'}], []
    previous, number, heading = 'start', 0, ''
    report = {'code_cells': 0, 'notes': 0, 'dropped': 0, 'warnings': []}
    for index, raw in enumerate(notebook['cells']):
        if not isinstance(raw, dict) or raw.get('cell_type') not in ('code', 'markdown', 'raw'):
            report['dropped'] += 1
            continue
        kind = 'markdown' if raw['cell_type'] == 'raw' else raw['cell_type']
        ident = raw.get('id') if isinstance(raw.get('id'), str) and 0 < len(raw.get('id', '')) <= 64 else None
        if not ident or ident in used or ident in ('start', 'finish'):
            base = 'cell-%d' % (index + 1); ident = base; n = 1
            while ident in used or ident in ('start', 'finish'):
                n += 1; ident = '%s-%d' % (base, n)
        used.add(ident)
        source = source_text(raw)
        _require(len(source.encode('utf-8')) <= MAX_SOURCE, 'Cell %d is too large' % (index + 1))
        cell = {'id': ident, 'cell_type': kind, 'metadata': copy.deepcopy(raw.get('metadata')) if isinstance(raw.get('metadata'), dict) else {}, 'source': source}
        if 'attachments' in raw and isinstance(raw['attachments'], dict):
            cell['attachments'] = copy.deepcopy(raw['attachments'])
        own = cell['metadata'].setdefault('mainaiger', {})
        if not isinstance(own, dict):
            own = cell['metadata']['mainaiger'] = {}
        if kind == 'code':
            number += 1
            report['code_cells'] += 1
            cell['outputs'] = [o for o in raw.get('outputs', []) if isinstance(o, dict)] if isinstance(raw.get('outputs'), list) else []
            cell['execution_count'] = raw.get('execution_count') if isinstance(raw.get('execution_count'), int) else None
            own['title'] = _cell_title(cell, index, number, heading)
            for needle, why in RISKY:
                if needle in source:
                    report['warnings'].append({'cell': ident, 'kind': 'opaque', 'detail': why})
            if source.lstrip().startswith(('%', '!')) or '\n%' in source or '\n!' in source:
                report['warnings'].append({'cell': ident, 'kind': 'magic', 'detail': 'IPython magic or shell line (%/!) will fail in this kernel'})
            nodes.append({'id': ident, 'kind': 'code', 'cell_index': len(cells)})
            edges.append({'id': 'e%d' % (len(edges) + 1), 'kind': 'control', 'source': previous, 'target': ident})
            previous = ident
        else:
            report['notes'] += 1
            own['title'] = _cell_title(cell, index, number, heading)
            first = source.strip().splitlines()
            if first and first[0].startswith('#'):
                heading = first[0].lstrip('# ').strip()
            nodes.append({'id': ident, 'kind': 'markdown', 'cell_index': len(cells)})
        cells.append(cell)
    nodes.append({'id': 'finish', 'kind': 'finish'})
    edges.append({'id': 'e%d' % (len(edges) + 1), 'kind': 'control', 'source': previous, 'target': 'finish'})
    metadata = copy.deepcopy(notebook.get('metadata')) if isinstance(notebook.get('metadata'), dict) else {}
    document = {'format': 'mainaiger', 'schema_version': SCHEMA_VERSION,
                'notebook': {'nbformat': 4, 'nbformat_minor': notebook.get('nbformat_minor', 5) if isinstance(notebook.get('nbformat_minor'), int) else 5,
                             'metadata': metadata, 'cells': cells},
                'graph': {'nodes': nodes, 'edges': edges}, 'layout': {'nodes': {}, 'viewport': {'x': 40, 'y': 30, 'zoom': 1}}}
    document = validate(document)
    # Hidden-state candidates: inputs that no earlier code cell defines.
    info = analysis(document)
    produced = set()
    for ident in [n['id'] for n in document['graph']['nodes'] if n['kind'] == 'code']:
        for name in info[ident]['inputs']:
            if name not in produced:
                report['warnings'].append({'cell': ident, 'kind': 'input', 'detail': 'reads "%s" which no earlier cell defines' % name})
        produced.update(info[ident]['outputs'])
    report['unresolved_inputs'] = sorted({w['detail'].split('"')[1] for w in report['warnings'] if w['kind'] == 'input'})
    return document, report


def to_notebook(document):
    """The embedded notebook with cells in trigger order; a lossy view of a branching graph."""
    doc = validate(document)
    ordered = [n for n in order(doc)]
    cells = []
    for ident in ordered:
        node = node_by_id(doc, ident)
        if node['kind'] in ('code', 'markdown'):
            cell = copy.deepcopy(doc['notebook']['cells'][node['cell_index']])
            form = form_config(cell)
            if form:
                cell['source'] = '# MainAIger form values\n' + form_prelude(form) + source_text(cell)
                cell.get('metadata', {}).get('mainaiger', {}).pop('form', None)
            cfg = model_config(cell)
            if cfg:
                # A prompt is not Python: export it as a raw cell that says what it was.
                cell = {'id': cell['id'], 'cell_type': 'raw', 'metadata': cell.get('metadata', {}),
                        'source': '# MainAIger model cell (%s -> %s); the Lab asks the local model with this prompt:\n%s' % (cfg['model'], cfg['output'], source_text(cell))}
            cells.append(cell)
    sequential = all(n['kind'] != 'code' or len([e for e in control_edges(doc) if e['target'] == n['id']]) <= 1 for n in doc['graph']['nodes'])
    return {**copy.deepcopy(doc['notebook']), 'cells': cells}, {'lossy': not sequential or len(control_edges(doc)) != len([n for n in doc['graph']['nodes'] if n['kind'] == 'code']) + 1}
