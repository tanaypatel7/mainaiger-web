"""The Lab's journal: one append-only line per thing that happened.

Sessions starting and ending, runs approved and finished, each cell's result, environment
builds, library saves: everything the owner might later ask "why did that happen?" about
is one JSON line in ``<state dir>/journal.jsonl``::

    {"seq": 41, "t": 1761600000.1, "kind": "run.finished", "text": "Run all finished: 6/6 ok", "data": {...}}

``seq`` only grows, so the browser can ask "everything after 41". The file is kept small:
past ``MAX_BYTES`` the oldest half is dropped (the retained lines keep their ``seq``). This
is the Lab's own record on this device; when runs span devices (roadmap C3) their journals
are joined by ``seq`` and device name, not merged into one file.
"""
import json
import os
import threading
import time
from pathlib import Path

FILE = 'journal.jsonl'
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 500
MAX_DATA = 4000


class Journal:
    def __init__(self, state_dir):
        self.path = Path(state_dir).resolve(strict=True) / FILE
        self.lock = threading.Lock()
        self.seq = 0
        self._load()

    def _load(self):
        if not self.path.is_file() or self.path.is_symlink():
            return
        try:
            for line in self.path.read_text(encoding='utf-8').splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get('seq'), int):
                    self.seq = max(self.seq, entry['seq'])
        except OSError:
            pass

    def write(self, kind, text, data=None):
        """Append one entry and return it. Never raises: the journal must not break the Lab."""
        if not isinstance(kind, str) or not kind:
            kind = 'note'
        text = (text if isinstance(text, str) else str(text))[:MAX_TEXT]
        payload = data if isinstance(data, dict) else {}
        try:
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > MAX_DATA:
                payload = {'truncated': True}
        except (TypeError, ValueError):
            payload = {'unserializable': True}
        with self.lock:
            self.seq += 1
            entry = {'seq': self.seq, 't': time.time(), 'kind': kind, 'text': text, 'data': payload}
            try:
                self._rotate_if_needed()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as out:
                    out.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except OSError:
                pass
            return entry

    def _rotate_if_needed(self):
        try:
            if self.path.is_file() and self.path.stat().st_size > MAX_BYTES:
                lines = self.path.read_text(encoding='utf-8').splitlines()
                keep = lines[len(lines) // 2:]
                tmp = self.path.with_suffix('.tmp')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    out.write('\n'.join(keep) + ('\n' if keep else ''))
                os.replace(tmp, self.path)
        except OSError:
            pass

    def tail(self, after=0, limit=200):
        """Entries with seq > after, oldest first, at most `limit` (the newest ones when more exist)."""
        limit = max(1, min(int(limit), 1000))
        after = max(0, int(after))
        if not self.path.is_file() or self.path.is_symlink():
            return []
        rows = []
        try:
            for line in self.path.read_text(encoding='utf-8').splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get('seq'), int) and entry['seq'] > after:
                    rows.append(entry)
        except OSError:
            return []
        return rows[-limit:]
