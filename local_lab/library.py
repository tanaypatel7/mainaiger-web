"""Cell library: one global, owner-approved folder of reusable cells, usable from any Mainbook.

A library cell is one JSON file, ``<name>.cell.json``::

    {"format": "mainaiger-cell", "schema_version": 1, "name": "load-data", "title": "Load data",
     "source": "...python...", "inputs": [...], "outputs": [...], "tags": ["ml"],
     "description": "...", "version": 3, "updated_at": 1730000000.0}

Rules, the same ones Mainbooks follow:

* The folder lives next to the Mainbooks folder (``<parent of Mainbooks>/Library``), is shown
  with its full path and approved once; the choice is remembered in the private state
  directory (``library.json``). It must be a plain directory and must not overlap the Lab's
  private state.
* Names are letters, digits, dashes and underscores; files never escape the folder and
  symlinks are refused. Writes are atomic and never follow symlinks.
* ``version`` is an integer that goes up by one on every save of the same name. A cell that
  was inserted from the library keeps a link in its notebook metadata::

      metadata.mainaiger.library = {"name": "load-data", "version": 3, "source_hash": "9f2a..."}

  so the graph can say whether the library moved on (newer version) or the cell was edited
  locally (source hash differs), and update one place.
* The folder is a document store only: it is never mounted into a container.
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import graph as graph_module
from . import mainbooks

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$')
TAG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,30}$')
CONFIG = 'library.json'
SUFFIX = '.cell.json'
MAX_FILE = 512 * 1024
MAX_SOURCE = 200_000
FORMAT = 'mainaiger-cell'
SCHEMA_VERSION = 1
STARTERS = Path(__file__).resolve().parent.parent / 'examples' / 'library'


class LibraryError(ValueError):
    pass


# ---------------------------------------------------------------- folder


def default_folder(state_dir):
    """Next to the approved Mainbooks folder, or next to the default one when none is chosen yet."""
    books = mainbooks.folder(state_dir) or mainbooks.default_folder()
    return str(Path(books).parent / 'Library')


def _config_path(state_dir):
    return Path(state_dir).resolve(strict=True) / CONFIG


def folder(state_dir):
    path = _config_path(state_dir)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('folder')
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError):
        return None


def validate_folder(candidate, state_dir):
    try:
        resolved = mainbooks.validate_folder(candidate, state_dir)
    except mainbooks.MainbookError as error:
        raise LibraryError(str(error).replace('Mainbooks folder', 'Library folder'))
    books = mainbooks.folder(state_dir)
    if books and Path(books).resolve() == resolved:
        raise LibraryError('The Library folder must be different from the Mainbooks folder')
    return resolved


def configure(state_dir, candidate):
    resolved = validate_folder(candidate, state_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise LibraryError('The folder could not be created as a plain directory')
    config = _config_path(state_dir)
    fd = os.open(config.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump({'folder': str(resolved), 'chosen_at': time.time()}, out)
    os.replace(config.with_suffix('.tmp'), config)
    return status(state_dir)


def status(state_dir):
    chosen = folder(state_dir)
    exists = bool(chosen) and Path(chosen).is_dir() and not Path(chosen).is_symlink()
    return {'configured': bool(chosen), 'folder': chosen, 'default': default_folder(state_dir), 'exists': exists,
            'count': len(listing(state_dir)) if exists else 0, 'starters': [s['name'] for s in starters()]}


# ---------------------------------------------------------------- cell records


def _check_names(values, what):
    if not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) and graph_module.NAME.match(v) for v in values):
        raise LibraryError('%s must be a list of Python names' % what)
    return list(dict.fromkeys(values))


def _check_tags(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 20 or not all(isinstance(v, str) and TAG.match(v) for v in values):
        raise LibraryError('Tags are short words: letters, digits, dashes or underscores (up to 20 tags)')
    return sorted(dict.fromkeys(v.lower() for v in values))


def validate_record(record):
    """Normalise one library cell record; raises LibraryError when it is malformed."""
    if not isinstance(record, dict):
        raise LibraryError('A library cell must be an object')
    if record.get('format') != FORMAT:
        raise LibraryError('Expected format: ' + FORMAT)
    if record.get('schema_version') != SCHEMA_VERSION:
        raise LibraryError('Unsupported library cell schema version')
    name = record.get('name')
    if not isinstance(name, str) or not NAME.match(name):
        raise LibraryError('Use a name with letters, digits, dashes or underscores (up to 60 characters)')
    source = record.get('source', '')
    if isinstance(source, list):
        source = ''.join(source)
    if not isinstance(source, str) or len(source) > MAX_SOURCE:
        raise LibraryError('The cell source must be text up to 200 kB')
    title = record.get('title', name)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise LibraryError('The title must be text up to 120 characters')
    description = record.get('description', '')
    if not isinstance(description, str) or len(description) > 2000:
        raise LibraryError('The description must be text up to 2000 characters')
    version = record.get('version', 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or version > 10 ** 9:
        raise LibraryError('version must be a positive integer')
    cell_type = record.get('cell_type', 'code')
    if cell_type not in ('code', 'markdown'):
        raise LibraryError('cell_type must be code or markdown')
    out = {'format': FORMAT, 'schema_version': SCHEMA_VERSION, 'name': name, 'title': title.strip(), 'cell_type': cell_type,
           'source': source, 'inputs': _check_names(record.get('inputs', []), 'inputs'), 'outputs': _check_names(record.get('outputs', []), 'outputs'),
           'tags': _check_tags(record.get('tags')), 'description': description.strip(), 'version': version,
           'updated_at': float(record['updated_at']) if isinstance(record.get('updated_at'), (int, float)) else None}
    out['source_hash'] = graph_module.source_hash(source)
    return out


def record_from_cell(doc, node_id, name, tags=None, description=''):
    """Build a library record from a code cell of the current document (declared or analysed names)."""
    node = graph_module.node_by_id(doc, node_id)
    if not node or node['kind'] != 'code':
        raise LibraryError('Choose a code cell to save')
    cell = doc['notebook']['cells'][node['cell_index']]
    own = cell.get('metadata', {}).get('mainaiger', {})
    names = graph_module.declared(doc, node_id)
    return validate_record({'format': FORMAT, 'schema_version': SCHEMA_VERSION, 'name': name, 'title': own.get('title') or name,
                            'source': graph_module.source_text(cell), 'inputs': names['inputs'], 'outputs': names['outputs'],
                            'tags': tags, 'description': description or ''})


# ---------------------------------------------------------------- files


def _path_for(state_dir, name):
    if not isinstance(name, str) or not NAME.match(name):
        raise LibraryError('Use a name with letters, digits, dashes or underscores (up to 60 characters)')
    chosen = folder(state_dir)
    if not chosen or not Path(chosen).is_dir():
        raise LibraryError('Approve a Library folder first')
    root = Path(chosen)
    target = root / (name + SUFFIX)
    if target.is_symlink():
        raise LibraryError('That name points at a symlink; choose another name')
    if target.exists() and target.resolve().parent != root.resolve():
        raise LibraryError('That file is outside the Library folder')
    return root, target


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise LibraryError('No library cell at %s' % path.name)
    if path.stat().st_size > MAX_FILE:
        raise LibraryError('The library cell file is larger than 512 kB')
    try:
        return validate_record(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError) as error:
        raise LibraryError('The library cell could not be read: %s' % error)


def _write(root, target, record):
    payload = (json.dumps(record, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    if len(payload) > MAX_FILE:
        raise LibraryError('The library cell is larger than 512 kB')
    fd, tmp = tempfile.mkstemp(prefix='.' + record['name'] + '-', suffix='.part', dir=root)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return len(payload)


def load(state_dir, name):
    root, target = _path_for(state_dir, name)
    if not target.exists():
        raise LibraryError('No library cell named "%s"' % name)
    return _read(target)


def save(state_dir, record, overwrite=False):
    """Write a record. A new name starts at version 1; an existing name needs overwrite=True and
    becomes the stored version + 1 (whatever version the caller passed)."""
    record = validate_record(record)
    root, target = _path_for(state_dir, record['name'])
    if target.exists():
        if not overwrite:
            raise LibraryError('A library cell named "%s" already exists; choose Replace to save a new version' % record['name'])
        record['version'] = _read(target)['version'] + 1
    else:
        record['version'] = 1
    record['updated_at'] = time.time()
    size = _write(root, target, record)
    return {'record': record, 'path': str(target), 'bytes': size}


def listing(state_dir, query=''):
    """Summaries of every readable cell, optionally filtered by a case-insensitive query over
    name, title, tags, description, inputs and outputs. Sorted by name."""
    chosen = folder(state_dir)
    if not chosen or not Path(chosen).is_dir():
        return []
    words = [w for w in (query or '').lower().split() if w][:8]
    rows = []
    for file in sorted(Path(chosen).glob('*' + SUFFIX)):
        name = file.name[:-len(SUFFIX)]
        if file.is_symlink() or not file.is_file() or not NAME.match(name):
            continue
        try:
            record = _read(file)
        except LibraryError:
            rows.append({'name': name, 'title': '(unreadable)', 'version': None, 'tags': [], 'inputs': [], 'outputs': [], 'description': '', 'cell_type': 'code', 'updated_at': file.stat().st_mtime, 'source_hash': None, 'unreadable': True})
            continue
        if record['name'] != name:
            continue  # file name and record name must agree; otherwise it is not ours
        haystack = ' '.join([record['name'], record['title'], record['description'], ' '.join(record['tags']), ' '.join(record['inputs']), ' '.join(record['outputs'])]).lower()
        if words and not all(w in haystack for w in words):
            continue
        rows.append({k: record[k] for k in ('name', 'title', 'version', 'tags', 'inputs', 'outputs', 'description', 'cell_type', 'updated_at', 'source_hash')})
    return rows


# ---------------------------------------------------------------- document integration


def link_of(cell):
    own = cell.get('metadata', {}).get('mainaiger', {}) if isinstance(cell.get('metadata'), dict) else {}
    link = own.get('library')
    return link if isinstance(link, dict) and isinstance(link.get('name'), str) and isinstance(link.get('version'), int) else None


def _apply_record(cell, record):
    """Put a library record's content into a notebook cell and link it."""
    cell['source'] = record['source']
    own = cell.setdefault('metadata', {}).setdefault('mainaiger', {})
    own['title'] = record['title']
    if record['inputs']:
        own['inputs'] = list(record['inputs'])
    else:
        own.pop('inputs', None)
    if record['outputs']:
        own['outputs'] = list(record['outputs'])
    else:
        own.pop('outputs', None)
    own['library'] = {'name': record['name'], 'version': record['version'], 'source_hash': record['source_hash']}
    if cell.get('cell_type') == 'code':
        cell['outputs'] = []
        cell['execution_count'] = None
        own.pop('last_run', None)


def insert(doc, record, position, after=None):
    """Add a new node from a library record. ``position`` is {x, y}; ``after`` (a node id) also
    chains the new cell behind that node the way the + button does. Returns (doc, new node id)."""
    doc = graph_module.validate(doc)
    cells, nodes, edges = doc['notebook']['cells'], doc['graph']['nodes'], doc['graph']['edges']
    taken = {c['id'] for c in cells} | {n['id'] for n in nodes} | {e['id'] for e in edges}
    base = re.sub(r'[^A-Za-z0-9_-]', '-', record['name'])[:40] or 'cell'
    node_id, n = base, 1
    while node_id in taken:
        n += 1
        node_id = '%s-%d' % (base, n)
    kind = record['cell_type']
    cell = {'id': node_id, 'cell_type': kind, 'metadata': {'mainaiger': {}}, 'source': ''}
    if kind == 'code':
        cell['execution_count'] = None
        cell['outputs'] = []
    _apply_record(cell, record)
    cells.append(cell)
    nodes.append({'id': node_id, 'kind': kind, 'cell_index': len(cells) - 1})
    if not isinstance(position, dict) or not all(isinstance(position.get(k), (int, float)) and abs(position[k]) < 1e6 for k in ('x', 'y')):
        raise LibraryError('position must be {x, y}')
    doc['layout']['nodes'][node_id] = {'x': int(position['x']), 'y': int(position['y'])}
    if after is not None and kind == 'code':
        source_node = graph_module.node_by_id(doc, after)
        if not source_node or source_node['kind'] in ('finish', 'markdown'):
            raise LibraryError('A cell can only be chained after Start or a code cell')
        edge_ids = {e['id'] for e in edges}
        def fresh():
            k = 1
            while 'e%d' % k in edge_ids:
                k += 1
            edge_ids.add('e%d' % k)
            return 'e%d' % k
        finish = next((x for x in nodes if x['kind'] == 'finish'), None)
        to_finish = next((e for e in edges if e['kind'] == 'control' and e['source'] == after and finish and e['target'] == finish['id']), None)
        if to_finish:
            to_finish['target'] = node_id
            edges.append({'id': fresh(), 'kind': 'control', 'source': node_id, 'target': finish['id']})
        else:
            edges.append({'id': fresh(), 'kind': 'control', 'source': after, 'target': node_id})
    return graph_module.validate(doc), node_id


def pull(doc, node_id, record):
    """Replace a linked cell's content with the library record (the "Update from library" step)."""
    doc = graph_module.validate(doc)
    node = graph_module.node_by_id(doc, node_id)
    if not node or node['kind'] not in ('code', 'markdown'):
        raise LibraryError('Choose a cell to update')
    cell = doc['notebook']['cells'][node['cell_index']]
    link = link_of(cell)
    if not link or link['name'] != record['name']:
        raise LibraryError('That cell is not linked to library cell "%s"' % record['name'])
    _apply_record(cell, record)
    return graph_module.validate(doc)


def link_state(cell, records_by_name):
    """How a cell relates to the library: None (not linked), 'current', 'behind' (library is newer),
    'modified' (edited locally since it was inserted), or 'missing' (library cell gone)."""
    link = link_of(cell)
    if not link:
        return None
    record = records_by_name.get(link['name'])
    if record is None:
        return 'missing'
    edited = graph_module.source_hash(graph_module.source_text(cell)) != link.get('source_hash')
    if edited:
        return 'modified'
    return 'behind' if record['version'] > link['version'] else 'current'


# ---------------------------------------------------------------- starter cells


def starters():
    """The example cells shipped with the Lab (examples/library), as validated records."""
    rows = []
    if STARTERS.is_dir():
        for file in sorted(STARTERS.glob('*' + SUFFIX)):
            try:
                rows.append(validate_record(json.loads(file.read_text(encoding='utf-8'))))
            except (OSError, ValueError, LibraryError):
                continue
    return rows


def seed(state_dir, overwrite=False):
    """Copy the starter cells into the approved folder. Existing names are skipped unless overwrite."""
    added, skipped = [], []
    for record in starters():
        root, target = _path_for(state_dir, record['name'])
        if target.exists() and not overwrite:
            skipped.append(record['name'])
            continue
        save(state_dir, record, overwrite=overwrite)
        added.append(record['name'])
    return {'added': added, 'skipped': skipped}
