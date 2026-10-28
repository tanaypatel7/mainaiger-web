"""Code map: a folder of code as nodes and edges, read with ``ast``, never executed.

Nodes: modules (one per ``.py`` file), classes, functions and methods, HTTP routes found in
request handlers, and UI files that call those routes. Edges:

- ``contains``  module → class/function, class → method
- ``imports``   module → module inside the same folder (``import a.b``, ``from .x import y``)
- ``calls``     function → function inside the folder, resolved statically: a bare name
                defined or imported in the same module, ``alias.name`` where ``alias`` is an
                imported module, ``self.method`` inside a class, ``Class.method``
- ``routes``    route → the handler function whose body compares ``path`` to that literal
                (``path=='/api/x'``, ``path in ('/a','/b')``, ``path.startswith('/api/x')``)
- ``ui``        UI file → route, from ``api('/api/x'…)`` / ``fetch('/api/x'…)`` / ``json('/api/x'…)``
                calls in ``.js`` and ``.html`` files

Everything static here is *advisory*, like data edges on the canvas: dynamic calls,
callbacks, reflection and generated code are not seen, and a resolved call means "this name
refers to that definition as written", not "this runs". The map says what it skipped.
"""
import ast
import os
import re
from pathlib import Path

MAX_FILES = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_NODES = 20000
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.mypy_cache', '.pytest_cache', 'dist', 'build', '.tox'}
UI_SUFFIXES = {'.js', '.mjs', '.html'}
UI_CALL = re.compile(r"""(?:\bapi|\bfetch|\bjson|\brequest)\(\s*(['"`])(/api/[A-Za-z0-9_./-]*)""")
HANDLER_NAMES = {'do_GET', 'do_POST', 'do_PUT', 'do_DELETE', 'do_PATCH'}


class MapError(ValueError):
    pass


def _rel(path, root):
    return str(path.relative_to(root)).replace(os.sep, '/')


def _doc(node):
    text = ast.get_docstring(node) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None
    return (text.strip().splitlines()[0][:160] if text else '')


def _module_name(rel):
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


class _Module:
    def __init__(self, rel, tree):
        self.rel, self.tree = rel, tree
        self.name = _module_name(rel)
        self.package = self.name.rsplit('.', 1)[0] if ('.' in self.name and not rel.endswith('__init__.py')) else (self.name if rel.endswith('__init__.py') else '')
        self.defs = {}          # qualname -> node id
        self.imports = {}       # local alias -> ('module', module_name) | ('name', module_name, name)
        self.classes = {}       # class name -> {method name}


def _resolve_relative(module, level, target):
    """Absolute dotted name for ``from .x import y`` style imports."""
    base = module.name.split('.')
    if not module.rel.endswith('__init__.py'):
        base = base[:-1]
    if level > 1:
        base = base[:len(base) - (level - 1)] if level - 1 <= len(base) else []
    parts = base + ([target] if target else [])
    return '.'.join(p for p in parts if p)


def scan(root, max_files=MAX_FILES):
    """Build the map of a folder. Returns {'nodes', 'edges', 'stats'}."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise MapError('Not a folder')
    nodes, edges, stats = [], [], {'files': 0, 'python': 0, 'ui': 0, 'skipped': [], 'unresolved_calls': 0, 'syntax_errors': 0}
    modules = {}            # module name -> _Module
    py_files, ui_files = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.') and not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if name.endswith('.py'):
                py_files.append(path)
            elif path.suffix in UI_SUFFIXES:
                ui_files.append(path)
            if len(py_files) + len(ui_files) > max_files:
                stats['skipped'].append('more than %d files: the rest was not read' % max_files)
                break
        if len(py_files) + len(ui_files) > max_files:
            break
    stats['files'] = len(py_files) + len(ui_files)
    # ---- modules and definitions
    for path in py_files:
        rel = _rel(path, root)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                stats['skipped'].append(rel + ': larger than 1 MB')
                continue
            source = path.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(source, filename=rel)
        except SyntaxError as error:
            stats['syntax_errors'] += 1
            stats['skipped'].append('%s: syntax error at line %s' % (rel, error.lineno))
            continue
        except OSError as error:
            stats['skipped'].append('%s: %s' % (rel, error))
            continue
        stats['python'] += 1
        module = _Module(rel, tree)
        modules[module.name] = module
        mod_id = 'mod:' + rel
        nodes.append({'id': mod_id, 'kind': 'module', 'name': module.name or rel, 'path': rel, 'line': 1, 'end_line': len(source.splitlines()) or 1, 'doc': _doc(tree), 'size': len(source)})
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ident = 'def:%s:%s' % (rel, item.name)
                module.defs[item.name] = ident
                nodes.append({'id': ident, 'kind': 'function', 'name': item.name, 'path': rel, 'line': item.lineno, 'end_line': item.end_lineno or item.lineno, 'doc': _doc(item), 'parent': mod_id, 'handler': item.name in HANDLER_NAMES})
                edges.append({'source': mod_id, 'target': ident, 'kind': 'contains'})
            elif isinstance(item, ast.ClassDef):
                cid = 'def:%s:%s' % (rel, item.name)
                module.defs[item.name] = cid
                module.classes[item.name] = set()
                nodes.append({'id': cid, 'kind': 'class', 'name': item.name, 'path': rel, 'line': item.lineno, 'end_line': item.end_lineno or item.lineno, 'doc': _doc(item), 'parent': mod_id})
                edges.append({'source': mod_id, 'target': cid, 'kind': 'contains'})
                for sub in item.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        mid = 'def:%s:%s.%s' % (rel, item.name, sub.name)
                        module.defs['%s.%s' % (item.name, sub.name)] = mid
                        module.classes[item.name].add(sub.name)
                        nodes.append({'id': mid, 'kind': 'method', 'name': '%s.%s' % (item.name, sub.name), 'path': rel, 'line': sub.lineno, 'end_line': sub.end_lineno or sub.lineno, 'doc': _doc(sub), 'parent': cid, 'handler': sub.name in HANDLER_NAMES})
                        edges.append({'source': cid, 'target': mid, 'kind': 'contains'})
        if len(nodes) > MAX_NODES:
            stats['skipped'].append('more than %d nodes: later files were not mapped' % MAX_NODES)
            break
    by_name = {m.name: m for m in modules.values()}
    # ---- imports (resolved inside the folder only)
    for module in modules.values():
        for item in ast.walk(module.tree):
            if isinstance(item, ast.Import):
                for alias in item.names:
                    if alias.name in by_name:
                        module.imports[alias.asname or alias.name.split('.')[0]] = ('module', alias.name)
                        edges.append({'source': 'mod:' + module.rel, 'target': 'mod:' + by_name[alias.name].rel, 'kind': 'imports'})
            elif isinstance(item, ast.ImportFrom):
                target = _resolve_relative(module, item.level, item.module) if item.level else (item.module or '')
                for alias in item.names:
                    full = (target + '.' + alias.name) if target else alias.name
                    if full in by_name:  # from pkg import submodule
                        module.imports[alias.asname or alias.name] = ('module', full)
                        edges.append({'source': 'mod:' + module.rel, 'target': 'mod:' + by_name[full].rel, 'kind': 'imports'})
                    elif target in by_name:  # from module import name
                        module.imports[alias.asname or alias.name] = ('name', target, alias.name)
                        edges.append({'source': 'mod:' + module.rel, 'target': 'mod:' + by_name[target].rel, 'kind': 'imports'})
    # de-duplicate import edges
    seen = set()
    edges = [e for e in edges if not ((e['source'], e['target'], e['kind']) in seen or seen.add((e['source'], e['target'], e['kind'])))]
    # ---- calls
    def resolve(module, call, scope_class):
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in module.defs:
                return module.defs[func.id]
            imp = module.imports.get(func.id)
            if imp and imp[0] == 'name' and imp[1] in by_name:
                return by_name[imp[1]].defs.get(imp[2])
            return None
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name):
                if base.id == 'self' and scope_class and func.attr in module.classes.get(scope_class, ()):
                    return module.defs.get('%s.%s' % (scope_class, func.attr))
                if base.id in module.classes and func.attr in module.classes[base.id]:
                    return module.defs.get('%s.%s' % (base.id, func.attr))
                imp = module.imports.get(base.id)
                if imp and imp[0] == 'module' and imp[1] in by_name:
                    return by_name[imp[1]].defs.get(func.attr)
        return None
    for module in modules.values():
        for item in module.tree.body:
            scopes = []
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((item, None, module.defs[item.name]))
            elif isinstance(item, ast.ClassDef):
                for sub in item.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        scopes.append((sub, item.name, module.defs['%s.%s' % (item.name, sub.name)]))
            for fn, cls, source_id in scopes:
                targets = set()
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Call):
                        target = resolve(module, sub, cls)
                        if target and target != source_id:
                            targets.add(target)
                        elif target is None:
                            stats['unresolved_calls'] += 1
                for target in sorted(targets):
                    edges.append({'source': source_id, 'target': target, 'kind': 'calls'})
    # ---- routes inside request handlers
    route_ids = {}
    for module in modules.values():
        for item in module.tree.body:
            if not isinstance(item, ast.ClassDef):
                continue
            for sub in item.body:
                if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) or sub.name not in HANDLER_NAMES:
                    continue
                method = sub.name[3:]
                handler_id = module.defs['%s.%s' % (item.name, sub.name)]
                for lit in _route_literals(sub):
                    rid = 'route:%s %s' % (method, lit)
                    if rid not in route_ids:
                        route_ids[rid] = {'id': rid, 'kind': 'route', 'name': '%s %s' % (method, lit), 'path': module.rel, 'line': sub.lineno, 'end_line': sub.end_lineno or sub.lineno, 'doc': '', 'method': method, 'route': lit}
                        nodes.append(route_ids[rid])
                    edges.append({'source': rid, 'target': handler_id, 'kind': 'routes'})
    # ---- UI files calling routes
    by_path = {}
    for rid, node in route_ids.items():
        by_path.setdefault(node['route'], []).append(rid)
    for path in ui_files:
        rel = _rel(path, root)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                stats['skipped'].append(rel + ': larger than 1 MB')
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError as error:
            stats['skipped'].append('%s: %s' % (rel, error))
            continue
        stats['ui'] += 1
        uid = 'ui:' + rel
        calls = sorted({m.group(2) for m in UI_CALL.finditer(text)})
        nodes.append({'id': uid, 'kind': 'ui', 'name': rel.split('/')[-1], 'path': rel, 'line': 1, 'end_line': len(text.splitlines()) or 1, 'doc': '', 'routes_called': calls})
        for route in calls:
            matched = by_path.get(route) or [rid for p, rids in by_path.items() for rid in rids if p.endswith('/') and route.startswith(p)]
            for rid in sorted(set(matched)):
                edges.append({'source': uid, 'target': rid, 'kind': 'ui'})
    stats['nodes'], stats['edges'] = len(nodes), len(edges)
    stats['skipped'] = stats['skipped'][:50]
    return {'root': str(root), 'nodes': nodes, 'edges': edges, 'stats': stats}


def _route_literals(fn):
    """String literals a handler compares `path` against: path=='/x', path in ('/a','/b'), path.startswith('/x')."""
    out = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name) and sub.left.id == 'path':
            for comp in sub.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    out.append(comp.value)
                elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    out.extend(e.value for e in comp.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == 'startswith' and isinstance(sub.func.value, ast.Name) and sub.func.value.id == 'path':
            for arg in sub.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value if arg.value.endswith('/') else arg.value + '/')
    return list(dict.fromkeys(out))


def source(root, rel, line, end_line, max_lines=400):
    """The lines of one node, read from disk; never follows symlinks or leaves the root."""
    root = Path(root).resolve()
    if not isinstance(rel, str) or '..' in Path(rel).parts or Path(rel).is_absolute():
        raise MapError('Paths stay inside the folder')
    target = root
    for part in Path(rel).parts:
        target = target / part
        if target.is_symlink():
            raise MapError('Symlinks are not followed')
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise MapError('Paths stay inside the folder')
    if not resolved.is_file():
        raise MapError('No such file')
    lines = resolved.read_text(encoding='utf-8', errors='replace').splitlines()
    start = max(1, int(line))
    end = min(len(lines), max(start, int(end_line)), start + max_lines - 1)
    return {'path': rel, 'line': start, 'end_line': end, 'text': '\n'.join(lines[start - 1:end]), 'truncated': end < int(end_line)}
