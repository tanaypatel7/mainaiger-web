"""Read-only file browsing for the FILES panel.

Three roots can be browsed: the **workspace** folder chosen on the Setup page (the one
mounted read-only into sessions), the **Mainbooks** folder and the **Library** folder.
Nothing here writes. Paths are relative, resolved against the root, and refused when they
leave it or pass through a symlink; listings skip symlinks; previews are text only and
bounded. This is the same stance as the session mount: the Lab may look, the owner decides
what is written where through the reviewed steps that exist for that purpose.
"""
import os
from pathlib import Path, PurePosixPath

from . import mainbooks, library

MAX_ENTRIES = 500
MAX_PREVIEW = 200 * 1024
TEXT_SUFFIXES = {'.py', '.ipymb', '.ipynb', '.json', '.md', '.txt', '.csv', '.tsv', '.yaml', '.yml', '.toml', '.cfg', '.ini',
                 '.js', '.ts', '.html', '.css', '.sh', '.rst', '.xml', '.svg', '.log', '.env.example'}


class FilesError(ValueError):
    pass


def roots(settings, state_dir):
    """Name -> absolute path for the folders the owner may browse right now."""
    out = {}
    if settings.get('workspace'):
        out['workspace'] = settings['workspace']
    books = mainbooks.folder(state_dir)
    if books:
        out['mainbooks'] = books
    lib = library.folder(state_dir)
    if lib:
        out['library'] = lib
    return out


def _resolve(root, relative):
    if not isinstance(relative, str) or len(relative) > 1024 or '\x00' in relative:
        raise FilesError('Invalid path')
    pure = PurePosixPath(relative.strip('/')) if relative.strip('/') else PurePosixPath('.')
    if pure.is_absolute() or any(part in ('..', '') for part in pure.parts if part != '.'):
        raise FilesError('Paths stay inside the folder')
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        raise FilesError('That folder is not available')
    target = base
    for part in pure.parts:
        if part == '.':
            continue
        target = target / part
        if target.is_symlink():
            raise FilesError('Symlinks are not followed')
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FilesError('No such file or folder')
    base_resolved = base.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise FilesError('Paths stay inside the folder')
    return target, str(pure) if str(pure) != '.' else ''


def listing(root, relative=''):
    target, rel = _resolve(root, relative)
    if not target.is_dir():
        raise FilesError('Not a folder')
    rows, skipped = [], 0
    try:
        names = sorted(os.listdir(target), key=lambda n: (not (target / n).is_dir(), n.lower()))
    except OSError as error:
        raise FilesError('The folder could not be read: %s' % error)
    for name in names:
        path = target / name
        if path.is_symlink():
            skipped += 1
            continue
        if len(rows) >= MAX_ENTRIES:
            skipped += 1
            continue
        try:
            st = path.stat()
        except OSError:
            skipped += 1
            continue
        kind = 'dir' if path.is_dir() else 'file'
        rows.append({'name': name, 'kind': kind, 'size': None if kind == 'dir' else st.st_size, 'modified': st.st_mtime,
                     'path': (rel + '/' + name) if rel else name, 'hidden': name.startswith('.'),
                     'text': kind == 'file' and (path.suffix.lower() in TEXT_SUFFIXES or st.st_size == 0)})
    return {'path': rel, 'entries': rows, 'skipped': skipped, 'truncated': len(rows) >= MAX_ENTRIES}


def read(root, relative):
    """A bounded text preview: {'path', 'size', 'text', 'truncated', 'binary'}."""
    target, rel = _resolve(root, relative)
    if not target.is_file():
        raise FilesError('Not a file')
    size = target.stat().st_size
    with open(target, 'rb') as handle:
        data = handle.read(MAX_PREVIEW + 1)
    truncated = len(data) > MAX_PREVIEW
    data = data[:MAX_PREVIEW]
    if b'\x00' in data:
        return {'path': rel, 'size': size, 'text': None, 'truncated': False, 'binary': True}
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = data.decode('utf-8', 'replace')
    return {'path': rel, 'size': size, 'text': text, 'truncated': truncated, 'binary': False}
