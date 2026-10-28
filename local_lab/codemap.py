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


def _params(func):
    """The parameters of a def, with literal defaults and simple annotations, for the form a cell shows."""
    args = func.args
    out = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    def one(arg, default, kind):
        item = {'name': arg.arg, 'kind': kind}
        ann = getattr(arg, 'annotation', None)
        if ann is not None:
            try:
                item['annotation'] = ast.unparse(ann)[:60]
            except Exception:
                pass
        if default is not None:
            item['has_default'] = True
            if isinstance(default, ast.Constant) and isinstance(default.value, (bool, int, float, str)):
                item['default'] = default.value
            elif isinstance(default, ast.Constant) and default.value is None:
                item['default_text'] = 'None'
            else:
                try:
                    item['default_text'] = ast.unparse(default)[:80]
                except Exception:
                    item['default_text'] = '…'
        return item
    for arg, default in zip(positional, defaults):
        out.append(one(arg, default, 'positional'))
    if args.vararg:
        out.append({'name': args.vararg.arg, 'kind': 'vararg'})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        out.append(one(arg, default, 'keyword'))
    if args.kwarg:
        out.append({'name': args.kwarg.arg, 'kind': 'kwarg'})
    return out


def _field_type(param):
    """A form field type for a parameter: from its default's type, else its annotation, else text."""
    default = param.get('default')
    if isinstance(default, bool):
        return 'flag'
    if isinstance(default, int):
        return 'integer'
    if isinstance(default, float):
        return 'number'
    if isinstance(default, str):
        return 'text'
    ann = (param.get('annotation') or '').strip()
    return {'bool': 'flag', 'int': 'integer', 'float': 'number', 'str': 'text'}.get(ann, 'text')


def cell_for(node, import_roots):
    """A cell that calls one function from the map: a form for its parameters in front of three lines of code.

    ``import_roots`` are the folders (inside the session, then on this device) that hold the
    package; the code adds the first one that exists to ``sys.path``. Parameters whose default or
    annotation gives a type become typed fields; the rest are text fields (strings). Methods get a
    cell that needs an instance, said plainly. Nothing here runs: it is text for a cell.
    """
    if node.get('kind') not in ('function', 'method'):
        raise MapError('Cells are made from functions and methods; routes and UI files are reached through them')
    params = node.get('params') or []
    module = node.get('module') or _module_name(node['path'])
    is_method = node['kind'] == 'method'
    cls, _, func = node['name'].partition('.') if is_method else ('', '', node['name'])
    skip = {'self', 'cls'} if is_method else set()
    fields, calls, notes, literal = [], [], [], []
    for param in params:
        if param['name'] in skip:
            continue
        if param['kind'] in ('vararg', 'kwarg'):
            notes.append('%s%s is not on the form; add it in the call if you need it.' % ('*' if param['kind'] == 'vararg' else '**', param['name']))
            continue
        field = {'name': param['name'], 'type': _field_type(param), 'label': param['name'] + ((' (%s)' % param['annotation']) if param.get('annotation') else '')}
        if 'default' in param:
            field['default'] = param['default']
        elif param.get('has_default'):
            notes.append('%s defaults to %s in the code; the form passes what you type.' % (param['name'], param.get('default_text', '…')))
        if field['type'] == 'text' and not isinstance(param.get('default'), str):
            literal.append(param['name'])  # unknown type: what is typed is read as a Python literal when it looks like one
        fields.append(field)
        calls.append('%s=%s' % (param['name'], param['name']) if param['kind'] == 'keyword' else param['name'])
    roots_literal = ''.join(repr(r) + ', ' for r in import_roots)
    lines = ['# From %s:%d — %s(%s)' % (node['path'], node['line'], node['name'], ', '.join(p['name'] for p in params)),
             '# Made by the Map page: the form above holds the parameters; edit anything.',
             'import os, sys',
             'for _root in (%s):' % roots_literal,
             '    if _root and os.path.isdir(_root) and _root not in sys.path:',
             '        sys.path.insert(0, _root); break',
             'from %s import %s' % (module, cls if is_method else func)]
    if literal:
        lines += ['def _literal(text):  # a text field with no known type: empty = None, a Python literal if it looks like one, else the text',
                  '    if text == "": return None',
                  '    try: return __import__("ast").literal_eval(text)',
                  '    except Exception: return text']
        lines += ['%s = _literal(%s)' % (name, name) for name in literal]
    if is_method:
        lines.append('obj = %s()  # an instance of %s: give it what its __init__ needs' % (cls, cls))
        lines.append('result = obj.%s(%s)' % (func, ', '.join(calls)))
    else:
        lines.append('result = %s(%s)' % (func, ', '.join(calls)))
    lines.append('print(result)')
    for note in notes:
        lines.insert(2, '# ' + note)
    doc = (node.get('doc') or '').strip().splitlines()
    title = node['name'] if len(node['name']) <= 60 else node['name'][:59] + '…'
    return {'title': title, 'source': '\n'.join(lines) + '\n', 'form': {'fields': fields} if fields else None,
            'note': doc[0] if doc else '', 'from': {'path': node['path'], 'line': node['line'], 'name': node['name'], 'module': module}}


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
                nodes.append({'id': ident, 'kind': 'function', 'name': item.name, 'path': rel, 'line': item.lineno, 'end_line': item.end_lineno or item.lineno, 'doc': _doc(item), 'parent': mod_id, 'handler': item.name in HANDLER_NAMES, 'params': _params(item), 'module': module.name})
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
                        nodes.append({'id': mid, 'kind': 'method', 'name': '%s.%s' % (item.name, sub.name), 'path': rel, 'line': sub.lineno, 'end_line': sub.end_lineno or sub.lineno, 'doc': _doc(sub), 'parent': cid, 'handler': sub.name in HANDLER_NAMES, 'params': _params(sub), 'module': module.name})
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


def _safe_file(root, rel):
    """The resolved file for a relative path: never a symlink, never outside the root."""
    root = Path(root).resolve()
    if not isinstance(rel, str) or not rel or '..' in Path(rel).parts or Path(rel).is_absolute():
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
    return resolved


def file_hash(text):
    """32-bit FNV-1a of a file's text as 8 hex chars: the editor sends it back so a write only lands on what was read."""
    value = 0x811c9dc5
    for byte in text.encode('utf-8'):
        value = ((value ^ byte) * 0x01000193) & 0xffffffff
    return '%08x' % value


def source(root, rel, line, end_line, max_lines=400):
    """The lines of one node, read from disk; never follows symlinks or leaves the root."""
    resolved = _safe_file(root, rel)
    text = resolved.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    start = max(1, int(line))
    end = min(len(lines), max(start, int(end_line)), start + max_lines - 1)
    return {'path': rel, 'line': start, 'end_line': end, 'text': '\n'.join(lines[start - 1:end]), 'truncated': end < int(end_line), 'file_hash': file_hash(text)}


MAX_EDIT_BYTES = 200000


def replace_lines(root, rel, line, end_line, text, expected_hash, write=False):
    """Replace lines line..end_line of one file with ``text``: a unified diff first (write=False), then the
    atomic write (write=True) — only if the file still hashes to ``expected_hash`` and, for a .py file, the
    whole new file still parses. Returns {diff, changed, file_hash, new_hash, written, lines}."""
    import difflib
    import tempfile
    resolved = _safe_file(root, rel)
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_EDIT_BYTES or '\x00' in text:
        raise MapError('The new text must be text of at most %d KB' % (MAX_EDIT_BYTES // 1000))
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise MapError('Files larger than 1 MB are not edited here')
    current = resolved.read_text(encoding='utf-8', errors='replace')
    if file_hash(current) != expected_hash:
        raise MapError('The file changed since it was read; reload the map and edit again')
    lines = current.splitlines(keepends=True)
    start, end = int(line), int(end_line)
    if not 1 <= start <= end <= max(1, len(lines)):
        raise MapError('The line range is outside the file')
    new_lines = text.splitlines(keepends=True)
    if new_lines and not new_lines[-1].endswith('\n') and (end < len(lines) or lines[end - 1].endswith('\n')):
        new_lines[-1] += '\n'  # keep the newline the replaced block ended with, so the next def and the file's end stay intact
    new_text = ''.join(lines[:start - 1] + new_lines + lines[end:])
    if resolved.suffix == '.py':
        try:
            ast.parse(new_text, filename=rel)
        except SyntaxError as error:
            raise MapError('The file would not parse after this change: line %s: %s' % (error.lineno, error.msg)) from None
    diff = ''.join(difflib.unified_diff(current.splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile=rel, tofile=rel, n=3))
    out = {'path': rel, 'diff': diff, 'changed': new_text != current, 'file_hash': expected_hash, 'new_hash': file_hash(new_text), 'written': False,
           'lines': {'before': end - start + 1, 'after': len(new_lines)}}
    if write and out['changed']:
        mode = resolved.stat().st_mode & 0o777
        fd, tmp = tempfile.mkstemp(prefix='.' + resolved.name + '.', suffix='.tmp', dir=str(resolved.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(new_text)
            os.chmod(tmp, mode)
            os.replace(tmp, resolved)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        out['written'] = True
    return out
