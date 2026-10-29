"""Saved Mainbooks: one owner-approved folder the service may write .ipymb files to.

This is the first place the Lab writes user documents to disk. Rules:

* The folder is chosen once, shown with its full path, and approved explicitly;
  the default is ~/Documents/MainAIger Lab/Mainbooks. The choice is remembered in
  the private state directory. It must be absolute, must not be `/` or the home
  folder itself, and must not overlap the Lab's private state.
* Saved files are named by the owner (letters, digits, space, dash, underscore)
  and always end in .ipymb; names never escape the folder (symlinks refused).
* Writes are atomic (temp file + rename) and never follow symlinks.
* The folder is a document store only: it is never mounted into a container.
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import graph as graph_module

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,80}$')
CONFIG = 'mainbooks.json'
MAX_FILE = 4 * 1024 * 1024


class MainbookError(ValueError):
    pass


def default_folder():
    return str(Path.home() / 'Documents' / 'MainAIger Lab' / 'Mainbooks')


def _config_path(state_dir):
    return Path(state_dir).resolve(strict=True) / CONFIG


def folder(state_dir):
    """The approved folder, or None when the owner has not chosen one yet."""
    path = _config_path(state_dir)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        value = data.get('folder')
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError):
        return None


def validate_folder(candidate, state_dir):
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 1024 or any(c in candidate for c in '\n\r\x00'):
        raise MainbookError('Enter a folder path')
    path = Path(candidate.strip()).expanduser()
    if not path.is_absolute():
        raise MainbookError('The folder path must be absolute (start with /)')
    home = Path.home().resolve()
    state = Path(state_dir).resolve(strict=True)
    # Resolve the existing part of the path so symlinked ancestors (/var -> /private/var) compare correctly.
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    resolved = existing.resolve() / path.relative_to(existing)
    if resolved == Path('/') or resolved == home:
        raise MainbookError('Choose a specific folder, not / or your home folder')
    if resolved == state or state in resolved.parents or resolved in state.parents:
        raise MainbookError('The Mainbooks folder must not overlap the private Lab state')
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise MainbookError('That path exists and is not a plain folder')
    return resolved


def configure(state_dir, candidate):
    """Create (if needed) and remember the approved folder. Returns status()."""
    resolved = validate_folder(candidate, state_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise MainbookError('The folder could not be created as a plain directory')
    config = _config_path(state_dir)
    fd = os.open(config.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump({'folder': str(resolved), 'chosen_at': time.time()}, out)
    os.replace(config.with_suffix('.tmp'), config)
    return status(state_dir)


def status(state_dir):
    chosen = folder(state_dir)
    exists = bool(chosen) and Path(chosen).is_dir() and not Path(chosen).is_symlink()
    return {'configured': bool(chosen), 'folder': chosen, 'default': default_folder(), 'exists': exists,
            'count': len(listing(state_dir)) if exists else 0}


def _path_for(state_dir, name):
    if not isinstance(name, str) or not NAME.match(name.strip()) or name.strip() != name:
        raise MainbookError('Use a name with letters, digits, spaces, dashes or underscores (up to 80 characters)')
    chosen = folder(state_dir)
    if not chosen or not Path(chosen).is_dir():
        raise MainbookError('Choose a Mainbooks folder first')
    root = Path(chosen)
    target = root / (name + '.ipymb')
    if target.is_symlink():
        raise MainbookError('That name points at a symlink; choose another name')
    if target.exists() and target.resolve().parent != root.resolve():
        raise MainbookError('That file is outside the Mainbooks folder')
    return root, target


def listing(state_dir):
    chosen = folder(state_dir)
    if not chosen or not Path(chosen).is_dir():
        return []
    rows = []
    for file in sorted(Path(chosen).glob('*.ipymb')):
        if file.is_symlink() or not file.is_file() or not NAME.match(file.stem):
            continue
        row = {'name': file.stem, 'modified': file.stat().st_mtime, 'bytes': file.stat().st_size, 'cells': None, 'title': None}
        try:
            if file.stat().st_size <= MAX_FILE:
                doc = json.loads(file.read_text(encoding='utf-8'))
                cells = doc.get('notebook', {}).get('cells', [])
                row['cells'] = len(cells)
                for cell in cells:
                    text = graph_module.source_text(cell).strip() if isinstance(cell, dict) else ''
                    if cell.get('cell_type') == 'markdown' and text:
                        row['title'] = text.splitlines()[0].lstrip('# ')[:80]
                        break
        except (OSError, ValueError, AttributeError):
            row['title'] = '(unreadable)'
        rows.append(row)
    return rows


def save(state_dir, name, document, overwrite=False):
    doc = graph_module.validate(document)
    root, target = _path_for(state_dir, name)
    if target.exists() and not overwrite:
        raise MainbookError('A Mainbook named "%s" already exists; choose Replace to overwrite it' % name)
    doc['notebook'].setdefault('metadata', {}).setdefault('mainaiger', {})['name'] = name
    doc['notebook']['metadata']['mainaiger']['saved_at'] = time.time()
    payload = (json.dumps(doc, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    if len(payload) > MAX_FILE:
        raise MainbookError('The Mainbook is larger than 4 MB')
    fd, tmp = tempfile.mkstemp(prefix='.' + name + '-', suffix='.part', dir=root)
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
    return {'name': name, 'path': str(target), 'bytes': len(payload), 'document': doc}


def load(state_dir, name):
    root, target = _path_for(state_dir, name)
    if not target.is_file():
        raise MainbookError('No Mainbook named "%s"' % name)
    if target.stat().st_size > MAX_FILE:
        raise MainbookError('The Mainbook is larger than 4 MB')
    try:
        document = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise MainbookError('The Mainbook could not be read: %s' % error)
    return graph_module.validate(document)
