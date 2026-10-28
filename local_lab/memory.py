"""Memory: an owner-approved folder of Markdown notes, Obsidian-compatible, in two tiers.

``<folder>/notes/``  persistent memory: notes you or the Lab wrote (ideas, facts, procedures,
                     snippets, answers, rules, Mainbook pointers), each with a status that grows
                     from draft to stable.
``<folder>/chats/``  chat memory: imported conversations, one note per chat, never rewritten by
                     the Lab after import (raw material with provenance).

A note is a file: YAML-like front matter between ``---`` lines (plain ``key: value`` and
``key: [a, b]`` lists; no YAML library needed) followed by Markdown. Links are ``[[Title]]`` or
``[[id]]`` in the body, as Obsidian writes them, so the vault opens in Obsidian or any editor.
Search is an SQLite FTS5 index in the private state folder (standard library; rebuilt from file
times, never the source of truth); when FTS5 is missing the search falls back to plain scanning.

Rules: the folder is approved once like Mainbooks (full path shown, never the home folder, never
the private state); names never escape the folder (symlinks refused); writes are atomic; every
change is a journal line (the server does that); chats are only written by the importer.
"""
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

from . import mainbooks

CONFIG = 'memory.json'
INDEX = 'memory-index.sqlite'
TIERS = ('notes', 'chats')
KINDS = ('idea', 'fact', 'procedure', 'snippet', 'answer', 'rule', 'mainbook', 'chat')
STATUSES = ('draft', 'growing', 'stable')
VISIBILITIES = ('private', 'shareable')
MAX_NOTE = 2 * 1024 * 1024
MAX_NOTES = 20000
MAX_TITLE = 120
LINK = re.compile(r'\[\[([^\[\]|#]+)(?:[#|][^\[\]]*)?\]\]')
SLUG_KEEP = re.compile(r'[^a-z0-9]+')
FIELDS = ('id', 'title', 'kind', 'status', 'tier', 'tags', 'sources', 'visibility', 'created', 'updated', 'mainbook', 'source')


class MemoryError(ValueError):
    pass


# ---------------------------------------------------------------- folder
def default_folder():
    return str(Path.home() / 'Documents' / 'MainAIger Lab' / 'Memory')


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
        raise MemoryError(str(error).replace('Mainbooks folder', 'Memory folder'))
    for other in (mainbooks.folder(state_dir),):
        if other and Path(other).resolve() == resolved:
            raise MemoryError('The Memory folder must be different from the Mainbooks folder')
    return resolved


def configure(state_dir, candidate):
    resolved = validate_folder(candidate, state_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    for tier in TIERS:
        (resolved / tier).mkdir(exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise MemoryError('The folder could not be created as a plain directory')
    config = _config_path(state_dir)
    fd = os.open(config.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump({'folder': str(resolved), 'chosen_at': time.time()}, out)
    os.replace(config.with_suffix('.tmp'), config)
    return status(state_dir)


def status(state_dir):
    chosen = folder(state_dir)
    exists = bool(chosen) and Path(chosen).is_dir() and not Path(chosen).is_symlink()
    counts = {'notes': 0, 'chats': 0}
    if exists:
        for tier in TIERS:
            counts[tier] = sum(1 for _ in _files(Path(chosen), tier))
    return {'configured': bool(chosen), 'folder': chosen, 'default': default_folder(), 'exists': exists, 'counts': counts,
            'search': 'fts5' if fts_available() else 'scan', 'kinds': list(KINDS), 'statuses': list(STATUSES)}


def _root(state_dir):
    chosen = folder(state_dir)
    if not chosen or not Path(chosen).is_dir():
        raise MemoryError('Choose a Memory folder first')
    return Path(chosen).resolve()


# ---------------------------------------------------------------- note format
def _format_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(_quote(str(v)) for v in value) + ']'
    return _quote(str(value))


def _quote(text):
    if text == '' or any(c in text for c in ':#[]{},"\n') or text.strip() != text or text.lower() in ('true', 'false', 'null'):
        return json.dumps(text, ensure_ascii=False)
    return text


def _parse_value(text):
    text = text.strip()
    if text.startswith('[') and text.endswith(']'):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items, buf, quoted, out = [], '', False, []
        # split on commas outside quotes
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == '"':
                end = i + 1
                while end < len(inner) and inner[end] != '"':
                    end += 2 if inner[end] == '\\' else 1
                items.append(json.loads(inner[i:end + 1]) if end < len(inner) else inner[i + 1:])
                i = end + 1
            elif c == ',':
                i += 1
            elif c == ' ':
                i += 1
            else:
                end = inner.find(',', i)
                end = len(inner) if end == -1 else end
                items.append(inner[i:end].strip())
                i = end
        return [str(v) for v in items if str(v) != '']
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    if text in ('true', 'false'):
        return text == 'true'
    try:
        return int(text) if re.match(r'^-?\d+$', text) else float(text) if re.match(r'^-?\d+\.\d+$', text) else text
    except ValueError:
        return text


def parse(text):
    """(front matter dict, body) from a note's text; a note without front matter is all body."""
    if not text.startswith('---\n'):
        return {}, text
    end = text.find('\n---\n', 4)
    if end == -1:
        return {}, text
    front = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition(':')
        if not sep or not re.match(r'^[A-Za-z_][A-Za-z0-9_-]*$', key.strip()):
            continue
        front[key.strip()] = _parse_value(value)
    return front, text[end + 5:]


def render(front, body):
    lines = ['---']
    for key in FIELDS:
        if key in front and front[key] not in (None, ''):
            lines.append('%s: %s' % (key, _format_value(front[key])))
    for key, value in front.items():
        if key not in FIELDS and value not in (None, ''):
            lines.append('%s: %s' % (key, _format_value(value)))
    lines.append('---')
    return '\n'.join(lines) + '\n' + (body if body.endswith('\n') or not body else body + '\n')


def links_in(body):
    """Link targets written as [[Title]] / [[id]] (the part before # or |), in order, unique."""
    seen, out = set(), []
    for match in LINK.finditer(body or ''):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def slug(title):
    base = SLUG_KEEP.sub('-', (title or '').lower()).strip('-')[:60]
    return base or 'note'


def new_id():
    return time.strftime('%Y%m%d') + '-' + secrets.token_hex(3)


# ---------------------------------------------------------------- files
def _files(root, tier):
    base = root / tier
    if not base.is_dir():
        return
    for path in sorted(base.rglob('*.md')):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part.startswith('.') for part in path.relative_to(root).parts):
            continue
        yield path


def _safe(root, rel):
    if not isinstance(rel, str) or not rel.endswith('.md') or '..' in Path(rel).parts or Path(rel).is_absolute() or '\x00' in rel:
        raise MemoryError('Paths stay inside the Memory folder')
    parts = Path(rel).parts
    if not parts or parts[0] not in TIERS:
        raise MemoryError('A note lives under notes/ or chats/')
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise MemoryError('Symlinks are not followed')
    resolved = target.resolve() if target.exists() else target
    if root not in resolved.parents:
        raise MemoryError('Paths stay inside the Memory folder')
    return target


def _hash(text):
    value = 0x811c9dc5
    for byte in text.encode('utf-8'):
        value = ((value ^ byte) * 0x01000193) & 0xffffffff
    return '%08x' % value


def _summary(path, root, text=None):
    text = path.read_text(encoding='utf-8', errors='replace') if text is None else text
    front, body = parse(text)
    rel = path.relative_to(root).as_posix()
    tier = rel.split('/')[0]
    preview = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith('#')), '')[:160]
    st = path.stat()
    return {'path': rel, 'tier': tier, 'id': str(front.get('id') or ''), 'title': str(front.get('title') or path.stem), 'kind': str(front.get('kind') or ('chat' if tier == 'chats' else 'fact')),
            'status': str(front.get('status') or 'stable'), 'tags': [str(t) for t in front.get('tags', [])] if isinstance(front.get('tags'), list) else [],
            'visibility': str(front.get('visibility') or 'private'), 'updated': st.st_mtime, 'bytes': st.st_size, 'preview': preview,
            'links': links_in(body), 'source': str(front.get('source') or ''), 'mainbook': str(front.get('mainbook') or '')}


def listing(state_dir, tier=None, kind=None, status_=None, tag=None, limit=500):
    root = _root(state_dir)
    rows = []
    for t in (TIERS if tier is None else (tier,)):
        if t not in TIERS:
            raise MemoryError('tier must be notes or chats')
        for path in _files(root, t):
            try:
                row = _summary(path, root)
            except OSError:
                continue
            if kind and row['kind'] != kind:
                continue
            if status_ and row['status'] != status_:
                continue
            if tag and tag not in row['tags']:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    rows.sort(key=lambda r: -r['updated'])
    return rows


def read(state_dir, rel):
    root = _root(state_dir)
    target = _safe(root, rel)
    if not target.is_file():
        raise MemoryError('No such note')
    if target.stat().st_size > MAX_NOTE:
        raise MemoryError('The note is larger than 2 MB')
    text = target.read_text(encoding='utf-8', errors='replace')
    front, body = parse(text)
    out = _summary(target, root, text)
    out.update(front=front, body=body, hash=_hash(text), sources=[str(s) for s in front.get('sources', [])] if isinstance(front.get('sources'), list) else [])
    return out


def _write_text(target, text):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_front(front, tier):
    out = dict(front)
    if tier == 'chats':
        out['kind'] = 'chat'
    kind = out.get('kind', 'fact')
    if kind not in KINDS:
        raise MemoryError('kind must be one of %s' % ', '.join(KINDS))
    if out.get('status', 'draft') not in STATUSES:
        raise MemoryError('status must be one of %s' % ', '.join(STATUSES))
    if out.get('visibility', 'private') not in VISIBILITIES:
        raise MemoryError('visibility must be private or shareable')
    title = str(out.get('title') or '').strip()
    if not title or len(title) > MAX_TITLE or any(c in title for c in '\n\r\x00/\\'):
        raise MemoryError('A note needs a title of up to %d characters without slashes' % MAX_TITLE)
    out['title'] = title
    tags = out.get('tags', [])
    if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags):
        raise MemoryError('tags is a list of up to 30 short words')
    out['tags'] = [t.strip().lower() for t in tags if t.strip()]
    sources = out.get('sources', [])
    if not isinstance(sources, list) or len(sources) > 50 or not all(isinstance(t, str) and len(t) <= 300 for t in sources):
        raise MemoryError('sources is a list of up to 50 short references')
    if kind == 'mainbook' and not (isinstance(out.get('mainbook'), str) and mainbooks.NAME.match(out['mainbook'])):
        raise MemoryError('a mainbook note names the saved Mainbook it runs (mainbook: <name>)')
    return out


def create(state_dir, title, body='', kind='fact', tags=(), sources=(), status_='draft', visibility='private', tier='notes', subfolder='', extra=None):
    """A new note file; returns its summary. Chats are only created by the importer (tier='chats')."""
    root = _root(state_dir)
    if tier not in TIERS:
        raise MemoryError('tier must be notes or chats')
    if not isinstance(body, str) or len(body.encode('utf-8')) > MAX_NOTE:
        raise MemoryError('The body must be text of at most 2 MB')
    if sum(1 for _ in _files(root, 'notes')) + sum(1 for _ in _files(root, 'chats')) >= MAX_NOTES:
        raise MemoryError('The Memory folder holds at most %d notes' % MAX_NOTES)
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    front = _check_front(dict(extra or {}, id=new_id(), title=title, kind=kind, status=status_, tier=tier, tags=list(tags), sources=list(sources), visibility=visibility, created=now, updated=now), tier)
    sub = ''
    if subfolder:
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$', subfolder):
            raise MemoryError('subfolder must be a short plain name')
        sub = subfolder + '/'
    base = slug(front['title'])
    rel = '%s/%s%s.md' % (tier, sub, base)
    target = _safe(root, rel)
    n = 2
    while target.exists():
        rel = '%s/%s%s-%d.md' % (tier, sub, base, n)
        target = _safe(root, rel)
        n += 1
    _write_text(target, render(front, body))
    return _summary(target, root)


def update(state_dir, rel, body=None, front_changes=None, expected_hash=None):
    """Change a note's body and/or front matter fields (title, kind, status, tags, sources, visibility, mainbook).
    Chats are never updated. ``expected_hash`` must match the note as last read."""
    root = _root(state_dir)
    target = _safe(root, rel)
    if not target.is_file():
        raise MemoryError('No such note')
    if rel.startswith('chats/'):
        raise MemoryError('Chat notes are raw material and are not edited; write what you learned as a note instead')
    text = target.read_text(encoding='utf-8', errors='replace')
    if expected_hash is not None and _hash(text) != expected_hash:
        raise MemoryError('The note changed since it was read; open it again')
    front, old_body = parse(text)
    changes = dict(front_changes or {})
    for key in ('id', 'created', 'tier'):
        changes.pop(key, None)
    allowed = {'title', 'kind', 'status', 'tags', 'sources', 'visibility', 'mainbook'}
    if set(changes) - allowed:
        raise MemoryError('Only %s can be changed' % ', '.join(sorted(allowed)))
    front.update(changes)
    front.setdefault('tier', 'notes')
    new_body = old_body if body is None else body
    if not isinstance(new_body, str) or len(new_body.encode('utf-8')) > MAX_NOTE:
        raise MemoryError('The body must be text of at most 2 MB')
    front = _check_front(front, 'notes')
    front['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    _write_text(target, render(front, new_body))
    return read(state_dir, rel)


def append(state_dir, rel, heading, text):
    """Grow a note: a dated section at the end (ideas and answers accumulate this way)."""
    root = _root(state_dir)
    target = _safe(root, rel)
    if not target.is_file():
        raise MemoryError('No such note')
    if rel.startswith('chats/'):
        raise MemoryError('Chat notes are not edited')
    if not isinstance(text, str) or not text.strip() or len(text) > 200000:
        raise MemoryError('Give some text to add (up to 200 KB)')
    if not isinstance(heading, str) or len(heading) > MAX_TITLE or '\n' in heading:
        raise MemoryError('The heading is one short line')
    current = target.read_text(encoding='utf-8', errors='replace')
    front, body = parse(current)
    stamp = time.strftime('%Y-%m-%d %H:%M')
    section = '\n## %s · %s\n\n%s\n' % (heading.strip() or 'Added', stamp, text.strip())
    front['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    if front.get('status', 'draft') == 'draft':
        front['status'] = 'growing'
    front = _check_front(dict(front, title=front.get('title') or target.stem), 'notes')
    _write_text(target, render(front, body.rstrip('\n') + '\n' + section))
    return read(state_dir, rel)


def delete(state_dir, rel):
    root = _root(state_dir)
    target = _safe(root, rel)
    if not target.is_file():
        raise MemoryError('No such note')
    target.unlink()
    return True


# ---------------------------------------------------------------- graph
def graph(state_dir, tier=None):
    """Notes as nodes, [[links]] as edges (resolved by title or id, case-insensitive); unresolved links are listed."""
    rows = listing(state_dir, tier=tier, limit=MAX_NOTES)
    by_key = {}
    for row in rows:
        by_key[row['title'].lower()] = row['path']
        if row['id']:
            by_key[row['id'].lower()] = row['path']
    edges, unresolved = [], []
    for row in rows:
        for target in row['links']:
            dest = by_key.get(target.lower())
            if dest and dest != row['path']:
                edges.append({'source': row['path'], 'target': dest, 'label': target})
            elif not dest:
                unresolved.append({'source': row['path'], 'target': target})
    return {'nodes': [{k: row[k] for k in ('path', 'tier', 'id', 'title', 'kind', 'status', 'tags', 'updated')} for row in rows], 'edges': edges, 'unresolved': unresolved[:200]}


# ---------------------------------------------------------------- search
_FTS = []


def fts_available():
    if not _FTS:
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('create virtual table t using fts5(a)')
            _FTS.append(True)
        except sqlite3.Error:
            _FTS.append(False)
        finally:
            conn.close()
    return _FTS[0]


def _index_path(state_dir):
    return Path(state_dir).resolve(strict=True) / INDEX


def reindex(state_dir):
    """Bring the FTS index in line with the files (by path and mtime). Returns counts."""
    root = _root(state_dir)
    if not fts_available():
        return {'indexed': 0, 'removed': 0, 'search': 'scan'}
    path = _index_path(state_dir)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path))
    try:
        if fresh:
            os.chmod(path, 0o600)
        conn.execute('create table if not exists files (path text primary key, mtime real)')
        conn.execute('create virtual table if not exists notes using fts5(path unindexed, title, tags, body, tokenize="porter unicode61")')
        known = dict(conn.execute('select path, mtime from files').fetchall())
        seen, indexed = set(), 0
        for tier in TIERS:
            for file in _files(root, tier):
                rel = file.relative_to(root).as_posix()
                seen.add(rel)
                mtime = file.stat().st_mtime
                if known.get(rel) == mtime:
                    continue
                try:
                    text = file.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                front, body = parse(text)
                conn.execute('delete from notes where path = ?', (rel,))
                conn.execute('insert into notes (path, title, tags, body) values (?, ?, ?, ?)', (rel, str(front.get('title') or file.stem), ' '.join(str(t) for t in front.get('tags', [])) if isinstance(front.get('tags'), list) else '', body[:500000]))
                conn.execute('insert or replace into files (path, mtime) values (?, ?)', (rel, mtime))
                indexed += 1
        removed = 0
        for rel in set(known) - seen:
            conn.execute('delete from notes where path = ?', (rel,))
            conn.execute('delete from files where path = ?', (rel,))
            removed += 1
        conn.commit()
        return {'indexed': indexed, 'removed': removed, 'search': 'fts5'}
    finally:
        conn.close()


def search(state_dir, query, tier=None, limit=20):
    """Ranked notes for a query: FTS5 (porter stemming) when available, else a word scan. Each hit has a snippet."""
    root = _root(state_dir)
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise MemoryError('Enter a few words to search for')
    words = [w for w in re.findall(r'[\w-]+', query.lower()) if len(w) > 1][:12]
    if not words:
        return []
    hits = []
    if fts_available():
        reindex(state_dir)
        conn = sqlite3.connect(str(_index_path(state_dir)))
        try:
            match = ' OR '.join('"%s"' % w.replace('"', '') for w in words)
            rows = conn.execute('select path, title, snippet(notes, 3, "[", "]", "…", 18), bm25(notes, 0, 4.0, 2.0, 1.0) as score from notes where notes match ? order by score limit ?', (match, limit * 2)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for rel, title, snippet, score in rows:
            if tier and not rel.startswith(tier + '/'):
                continue
            file = root / rel
            if not file.is_file():
                continue
            try:
                text = file.read_text(encoding='utf-8', errors='replace')
                row = _summary(file, root, text)
            except OSError:
                continue
            row.update(snippet=snippet, score=_score(words, row['title'], text, -score))
            hits.append(row)
        hits.sort(key=lambda r: -r['score'])
        return hits[:limit]
    for row in listing(state_dir, tier=tier, limit=MAX_NOTES):
        try:
            text = (root / row['path']).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        score = _score(words, row['title'], text, 0.0)
        if score:
            low = text.lower()
            pos = min((low.find(w) for w in words if w in low), default=0)
            row.update(snippet=text[max(0, pos - 60):pos + 120].replace('\n', ' '), score=score)
            hits.append(row)
    hits.sort(key=lambda r: -r['score'])
    return hits[:limit]


def _score(words, title, text, rank_hint=0.0):
    """A plain relevance score that works for a vault of three notes as well as three thousand: each distinct query
    word counts 3 when it is in the title and 1 when it is in the text; the FTS rank only breaks ties."""
    low_title, low_text = title.lower(), text.lower()
    score = 0.0
    for w in words:
        if w in low_title:
            score += 3.0
        if w in low_text:
            score += 1.0
    return round(score + min(0.5, max(0.0, rank_hint) * 1000.0 if abs(rank_hint) < 0.001 else min(0.5, abs(rank_hint) / 20.0)), 3)
