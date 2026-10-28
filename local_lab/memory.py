"""Private, owner-reviewed memory, separate from immutable imported chats.

All model/import output enters as a draft. Only an explicit owner review confirms
it; editing any entry invalidates that confirmation. Stored code and Mainbooks
are inert documents, never instructions to this store. SQLite is intentionally
local to the private Lab state, not a selected workspace or remote device.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

KINDS = {'note', 'claim', 'procedure', 'code', 'test', 'mainbook'}
STATES = {'draft', 'confirmed', 'archived', 'rejected'}
MAX_BODY = 64000
MAX_MAINBOOK = 4 * 1024 * 1024
SUMMARY_COLUMNS = "id,title,substr(body,1,240) AS body,kind,status,idea_id,tags,NULL AS mainbook,(mainbook IS NOT NULL) AS has_mainbook,revision,created_at,updated_at,confirmed_at"


def _text(value, name, maximum, empty=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('%s must be %s text (up to %d characters)' % (name, 'nonempty' if not empty else 'valid', maximum))
    return value


def _identifier(value, name='ID'):
    return _text(value, name, 128)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError('Content must be finite JSON data') from e


def _now():
    return time.time()


class MemoryStore:
    def __init__(self, state_dir):
        root = Path(state_dir)
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Memory requires a plain private state directory')
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Memory state directory must be private and owned by this account')
        self.path = root.resolve() / 'memory.sqlite3'
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Memory database must be a private regular file owned by this account')
        # SQLite opens these itself; refuse attacker-controlled existing sidecars.
        for suffix in ('-journal', '-wal', '-shm'):
            side = Path(str(self.path) + suffix)
            if side.exists() or side.is_symlink():
                s = side.lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
                    raise ValueError('Memory database sidecar is not private')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('memory_casefold', 1, lambda value: str(value).casefold(), deterministic=True)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS imports (
            digest TEXT PRIMARY KEY, provider TEXT NOT NULL, imported_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS ideas (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL,
            title TEXT NOT NULL, created_at REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(provider, source_id));
          CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            source_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
            parent_id TEXT, deleted INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id);
          CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, idea_id TEXT REFERENCES ideas(id),
            tags TEXT NOT NULL, mainbook TEXT, revision INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, confirmed_at REAL);
          CREATE INDEX IF NOT EXISTS entries_status ON entries(status, idea_id);
          CREATE TABLE IF NOT EXISTS sources (
            entry_id TEXT NOT NULL REFERENCES entries(id),
            message_id TEXT NOT NULL REFERENCES messages(id), PRIMARY KEY(entry_id,message_id));
          CREATE TABLE IF NOT EXISTS history (
            entry_id TEXT NOT NULL REFERENCES entries(id), revision INTEGER NOT NULL,
            action TEXT NOT NULL, at REAL NOT NULL, snapshot TEXT NOT NULL,
            PRIMARY KEY(entry_id,revision));
        ''')
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def _source_ids(self, ident):
        return [r[0] for r in self.db.execute('SELECT message_id FROM sources WHERE entry_id=? ORDER BY message_id', (ident,))]

    def _entry(self, row, summary=False):
        value = dict(row)
        value['tags'] = json.loads(value['tags'])
        value['mainbook'] = json.loads(value['mainbook']) if value['mainbook'] else None
        value['source_message_ids'] = self._source_ids(value['id'])
        value['has_mainbook'] = bool(value.get('has_mainbook', value['mainbook'] is not None))
        value['preview'] = value['body'][:240]
        if summary:
            del value['body'], value['mainbook']
        return value

    def _row(self, ident):
        _identifier(ident)
        row = self.db.execute('SELECT * FROM entries WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('Memory entry not found')
        return row

    def overview(self, offset=0, limit=200):
        """A bounded page of each collection, with total counts and continuation.

        Lists share the offset: once one collection is exhausted its page is
        empty, while the others continue. No imported chat disappears merely
        because an archive contains more than the first page's limit.
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10 ** 9:
            raise ValueError('Memory offset must be a nonnegative integer')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Memory page size must be between 1 and 1000')
        with self.lock:
            counts = {s: 0 for s in STATES}
            counts.update({r[0]: r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM entries GROUP BY status')})
            counts['entries'] = sum(counts[s] for s in STATES)
            counts['conversations'] = self.db.execute('SELECT COUNT(*) FROM conversations WHERE deleted=0').fetchone()[0]
            counts['messages'] = self.db.execute('SELECT COUNT(*) FROM messages WHERE deleted=0').fetchone()[0]
            counts['ideas'] = self.db.execute('SELECT COUNT(*) FROM ideas').fetchone()[0]
            more = offset + limit < max(counts['entries'], counts['conversations'], counts['ideas'])
            return {'ideas': [dict(r) for r in self.db.execute('SELECT * FROM ideas ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset))],
                    'conversations': [dict(r) for r in self.db.execute('SELECT c.*,COUNT(m.id) AS message_count '
                       'FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id '
                       'WHERE c.deleted=0 GROUP BY c.id ORDER BY c.created_at DESC,c.id DESC LIMIT ? OFFSET ?', (limit, offset))],
                    'entries': [self._entry(r, True) for r in self.db.execute('SELECT ' + SUMMARY_COLUMNS + ' FROM entries ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset))],
                    'counts': counts, 'offset': offset, 'limit': limit,
                    'next_offset': offset + limit if more else None}

    def create_idea(self, title):
        title = _text(title, 'Idea title', 200).strip()
        result = {'id': str(uuid.uuid4()), 'title': title, 'created_at': _now()}
        with self.lock, self.db:
            self.db.execute('INSERT INTO ideas VALUES (:id,:title,:created_at)', result)
        return result

    def import_chats(self, parsed):
        if not isinstance(parsed, dict):
            raise ValueError('Invalid parsed archive')
        digest = parsed.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Archive digest must be SHA-256')
        provider = _text(parsed.get('provider'), 'Provider', 80)
        conversations = parsed.get('conversations')
        if not isinstance(conversations, list) or len(conversations) > 100000:
            raise ValueError('Invalid conversation list')
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM imports WHERE digest=?', (digest,)).fetchone():
                return {'duplicate': True, 'conversations': 0, 'messages': 0}
            nc = nm = 0
            for c in conversations:
                if not isinstance(c, dict):
                    raise ValueError('Invalid conversation')
                p = _text(c.get('provider', provider), 'Provider', 80)
                sid = _text(c.get('source_id'), 'Source conversation ID', 512)
                title = _text(c.get('title') or 'Untitled chat', 'Conversation title', 2000)
                existing = self.db.execute('SELECT id,deleted FROM conversations WHERE provider=? AND source_id=?', (p, sid)).fetchone()
                if existing and existing['deleted']:
                    # Removing an archive is durable across later overlapping imports.
                    continue
                cid = existing['id'] if existing else str(uuid.uuid4())
                if not existing:
                    self.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,0)', (cid, p, sid, title, _now()))
                    nc += 1
                messages = c.get('messages')
                if not isinstance(messages, list) or len(messages) > 100000:
                    raise ValueError('Invalid message list')
                for m in messages:
                    if not isinstance(m, dict):
                        raise ValueError('Invalid message')
                    msid = _text(m.get('source_id'), 'Source message ID', 512)
                    role = _text(m.get('role'), 'Message role', 80)
                    text = _text(m.get('text'), 'Message', 2 * 1024 * 1024, empty=True)
                    parent = m.get('parent_id')
                    if parent is not None:
                        _text(parent, 'Parent message ID', 512, empty=True)
                    # Same upstream message with changed text is a separate immutable
                    # version. Repeated identical exports do not duplicate originals.
                    mid = hashlib.sha256(_json([p, sid, msid, role, text, parent]).encode()).hexdigest()
                    cur = self.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,0)', (mid, cid, msid, role, text, parent))
                    nm += cur.rowcount
            self.db.execute('INSERT INTO imports VALUES (?,?,?)', (digest, provider, _now()))
            return {'duplicate': False, 'conversations': nc, 'messages': nm}

    def conversation(self, ident):
        _identifier(ident)
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise ValueError('Conversation not found')
            result = dict(row)
            result['messages'] = [dict(r) for r in self.db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid', (ident,))]
            result['message_count'] = len(result['messages'])
            return result

    def messages(self, ids):
        """Explicitly selected original messages for a bounded model request."""
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
            raise ValueError('Select between 1 and 20 source messages')
        selected = list(dict.fromkeys(_identifier(i, 'Message ID') for i in ids))
        with self.lock:
            result = []
            for ident in selected:
                row = self.db.execute("SELECT m.*,c.title AS conversation_title,c.provider FROM messages m JOIN conversations c ON m.conversation_id=c.id WHERE m.id=?", (ident,)).fetchone()
                if row is None or row['deleted']:
                    raise ValueError('Source message not found or deleted')
                result.append(dict(row))
            return result

    def entry(self, ident):
        with self.lock:
            result = self._entry(self._row(ident))
            result['sources'] = [dict(r) for r in self.db.execute('''SELECT m.*,c.title AS conversation_title,c.provider
                  FROM sources s JOIN messages m ON s.message_id=m.id JOIN conversations c ON m.conversation_id=c.id
                  WHERE s.entry_id=? ORDER BY m.rowid''', (ident,))]
            result['history'] = [{'revision': r['revision'], 'action': r['action'], 'at': r['at'], 'snapshot': json.loads(r['snapshot'])}
                                 for r in self.db.execute('SELECT * FROM history WHERE entry_id=? ORDER BY revision DESC', (ident,))]
            return result

    def _history(self, ident, action):
        entry = self._entry(self._row(ident))
        self.db.execute('INSERT INTO history VALUES (?,?,?,?,?)', (ident, entry['revision'], action, entry['updated_at'], _json(entry)))

    def save_entry(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Memory entry must be an object')
        title = _text(payload.get('title'), 'Memory title', 200).strip()
        body = _text(payload.get('body', ''), 'Memory body', MAX_BODY, empty=True)
        kind = payload.get('kind', 'note')
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError('Unknown memory kind')
        tags = payload.get('tags', [])
        if not isinstance(tags, list) or len(tags) > 30:
            raise ValueError('Use at most 30 tags')
        tags = list(dict.fromkeys(_text(t, 'Tag', 80).strip() for t in tags))
        sources = payload.get('source_message_ids', [])
        if not isinstance(sources, list) or len(sources) > 100:
            raise ValueError('Use at most 100 source messages')
        sources = list(dict.fromkeys(_identifier(s, 'Source message ID') for s in sources))
        idea = payload.get('idea_id') or None
        if idea is not None:
            _identifier(idea, 'Idea ID')
        mainbook = payload.get('mainbook')
        if mainbook is not None:
            if not isinstance(mainbook, dict) or len(_json(mainbook).encode()) > MAX_MAINBOOK:
                raise ValueError('Mainbook snapshot must be an object of at most 4 MiB')
            from .graph import validate
            try:
                mainbook = validate(mainbook)
            except (TypeError, KeyError, RecursionError) as e:
                raise ValueError('Invalid Mainbook snapshot') from e
        ident = payload.get('id')
        with self.lock, self.db:
            old = self._row(ident) if ident else None
            if old is not None:
                self._check_revision(old, payload.get('expected_revision'))
            if idea and not self.db.execute('SELECT 1 FROM ideas WHERE id=?', (idea,)).fetchone():
                raise ValueError('Idea not found')
            for source in sources:
                row = self.db.execute('SELECT deleted FROM messages WHERE id=?', (source,)).fetchone()
                if row is None:
                    raise ValueError('Source message not found')
                if row['deleted'] and (not old or source not in self._source_ids(ident)):
                    raise ValueError('Cannot add a deleted source message')
            now = _now()
            ident = ident or str(uuid.uuid4())
            revision = old['revision'] + 1 if old is not None else 1
            values = (title, body, kind, 'draft', idea, _json(tags), _json(mainbook) if mainbook is not None else None, revision, now, None, ident)
            if old is not None:
                self.db.execute('''UPDATE entries SET title=?,body=?,kind=?,status=?,idea_id=?,tags=?,mainbook=?,revision=?,updated_at=?,confirmed_at=? WHERE id=?''', values)
            else:
                self.db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (ident, title, body, kind, 'draft', idea, _json(tags), _json(mainbook) if mainbook is not None else None, 1, now, now, None))
            self.db.execute('DELETE FROM sources WHERE entry_id=?', (ident,))
            self.db.executemany('INSERT INTO sources VALUES (?,?)', [(ident, s) for s in sources])
            self._history(ident, 'edit' if old is not None else 'create')
            return self.entry(ident)

    @staticmethod
    def _check_revision(row, expected):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != row['revision']:
            raise ValueError('Memory changed since it was opened. Reload it before saving or reviewing.')

    def review(self, ident, action, expected_revision):
        transitions = {'confirm': 'confirmed', 'reject': 'rejected', 'archive': 'archived', 'reopen': 'draft'}
        if not isinstance(action, str) or action not in transitions:
            raise ValueError('Unknown review action')
        with self.lock, self.db:
            row = self._row(ident)
            self._check_revision(row, expected_revision)
            if action in ('confirm', 'reject') and row['status'] != 'draft':
                raise ValueError('Only drafts can be confirmed or rejected; reopen first')
            now = _now()
            self.db.execute('UPDATE entries SET status=?,revision=revision+1,updated_at=?,confirmed_at=? WHERE id=?',
                            (transitions[action], now, now if action == 'confirm' else None, ident))
            self._history(ident, 'owner.' + action)
            return self.entry(ident)

    def search(self, query, idea_id=None):
        query = _text(query, 'Search query', 1000).strip()
        if idea_id is not None:
            _identifier(idea_id, 'Idea ID')
        terms = list(dict.fromkeys(re.findall(r'\w+', query.casefold(), flags=re.UNICODE)))[:20]
        if not terms:
            return []
        with self.lock:
            # Parameterized LIKE does not interpret user SQL or FTS operators.
            # Explicit wildcards are escaped. Only confirmed entries enter context.
            where = 'status=?'
            args = ['confirmed']
            if idea_id:
                where += ' AND idea_id=?'; args.append(idea_id)
            clauses = []
            for term in terms:
                clauses.append("memory_casefold(title || ' ' || body || ' ' || tags) LIKE ? ESCAPE '\\'")
                args.append('%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            where += ' AND (' + ' OR '.join(clauses) + ')'
            columns = SUMMARY_COLUMNS.replace('substr(body,1,240) AS body', 'body')
            rows = self.db.execute('SELECT ' + columns + ' FROM entries WHERE ' + where + ' ORDER BY updated_at DESC LIMIT 200', args)
            results = []
            for row in rows:
                value = self._entry(row, True)
                haystack = (row['title'] + ' ' + row['body'] + ' ' + row['tags']).casefold()
                value['score'] = sum(haystack.count(t) for t in terms) + 5 * sum(t in row['title'].casefold() for t in terms)
                value['body'] = row['body'][:8000]
                value['truncated'] = len(row['body']) > 8000
                results.append(value)
            return sorted(results, key=lambda x: (x['score'], x['updated_at']), reverse=True)[:20]

    def delete_conversation(self, ident):
        """Forget original text; keep tombstones so existing memory provenance is honest.

Reviewed memories and their revision snapshots remain owner-authored content.
The caller must explain that deleting a chat does not erase those excerpts.
"""
        with self.lock, self.db:
            self.conversation(ident)
            self.db.execute("UPDATE messages SET text='',role='deleted',deleted=1 WHERE conversation_id=?", (ident,))
            self.db.execute("UPDATE conversations SET title='Deleted source conversation',deleted=1 WHERE id=?", (ident,))
        return {'deleted': True, 'id': ident, 'memory_retained': True}

    def export(self):
        with self.lock:
            return {'schema': 'mainaiger.memory.v1', 'exported_at': _now(),
                    'ideas': [dict(r) for r in self.db.execute('SELECT * FROM ideas ORDER BY created_at')],
                    'conversations': [self.conversation(r[0]) for r in self.db.execute('SELECT id FROM conversations ORDER BY created_at')],
                    'entries': [self.entry(r[0]) for r in self.db.execute('SELECT id FROM entries ORDER BY created_at')]}
