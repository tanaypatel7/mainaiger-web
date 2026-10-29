"""Event triggers: a Mainbook's Start can be fired from the outside.

A Start node may list *sources* (``graph.nodes[start].events``): a **timer** (every N seconds),
a **file** (something under the read-only workspace changed), a **chat** prompt (sent on the
Models page) or **manual** (the "Fire now" button). The rules:

* Nothing fires until the owner **arms** the Mainbook in a review dialog that lists exactly what
  may fire it. Arming lasts until the owner disarms it, the cells or arrows change, or the Lab
  stops; it is not saved, so a restarted Lab has nothing armed. Events never arm anything.
* Each firing is one ordinary run of the whole graph (the same approved path as "Run all"), with
  the event bound in the active session as ``event`` (a dict with ``kind``, ``at`` and the payload)
  and, for convenience, ``prompt``, ``path`` or ``now``. Runs go one at a time: an event that
  arrives during a run waits in a short queue (timer ticks are coalesced; the queue is capped at
  ``QUEUE_MAX``); an event with no running session is dropped and said so.
* Every firing, drop and queue overflow is a journal line, so "why did this run?" is in the Log.

The watcher is one daemon thread with a 0.5 s tick: it counts the timers down, polls the watched
paths' size and mtime every ``FILE_POLL`` seconds (standard library only; no inotify/FSEvents),
and starts queued runs when the Lab is free. It never touches the network.
"""
import os
import threading
import time
from pathlib import Path

EVENT_KINDS = ('manual', 'timer', 'file', 'chat')
TIMER_MIN, TIMER_MAX = 10, 86400
FILE_POLL = 2.0
TICK = 0.5
QUEUE_MAX = 10
MAX_SOURCES = 8
MAX_WATCH_ENTRIES = 2000
MAX_PROMPT = 20000
RECENT = 20


class EventError(ValueError):
    pass


def check_sources(sources):
    """Validate and normalize a Start node's event sources; raises EventError."""
    if sources in (None, []):
        return []
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise EventError('events must be a list of at most %d sources' % MAX_SOURCES)
    out, seen = [], set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict) or src.get('kind') not in EVENT_KINDS:
            raise EventError('event %d: kind must be one of %s' % (i, ', '.join(EVENT_KINDS)))
        kind = src['kind']
        clean = {'kind': kind}
        if kind == 'timer':
            every = src.get('every')
            if not isinstance(every, int) or isinstance(every, bool) or not TIMER_MIN <= every <= TIMER_MAX:
                raise EventError('event %d: a timer needs "every" between %d and %d seconds' % (i, TIMER_MIN, TIMER_MAX))
            if set(src) - {'kind', 'every'}:
                raise EventError('event %d: a timer has only "every"' % i)
            clean['every'] = every
        elif kind == 'file':
            path = src.get('path')
            if not isinstance(path, str) or not path.strip() or len(path) > 200 or path.startswith('/') or '\\' in path or '\x00' in path:
                raise EventError('event %d: a file source needs a path relative to the workspace' % i)
            parts = [p for p in path.strip().split('/') if p not in ('', '.')]
            if any(p == '..' for p in parts):
                raise EventError('event %d: the path may not leave the workspace' % i)
            if set(src) - {'kind', 'path'}:
                raise EventError('event %d: a file source has only "path"' % i)
            clean['path'] = '/'.join(parts)
        else:
            if set(src) - {'kind'}:
                raise EventError('event %d: a %s source has no settings' % (i, kind))
        key = (kind, clean.get('every'), clean.get('path'))
        if key in seen:
            raise EventError('event %d repeats another source' % i)
        seen.add(key)
        out.append(clean)
    return out


def describe(source, workspace=None):
    """One plain sentence for the arm review: what may fire the Mainbook."""
    kind = source['kind']
    if kind == 'timer':
        every = source['every']
        shown = '%d s' % every if every < 120 else ('%d min' % (every // 60) if every < 7200 else '%.1f h' % (every / 3600))
        return 'Every %s while armed (bound as now)' % shown
    if kind == 'file':
        where = ('%s/%s' % (workspace, source['path'])) if workspace else ('workspace/' + source['path'])
        return 'When anything at %s is added, changed or removed (checked every %d s; bound as path)' % (where if source['path'] else (workspace or 'the workspace'), int(FILE_POLL))
    if kind == 'chat':
        return 'When you send a chat prompt on the Models page (bound as prompt; the chat still answers as usual)'
    return 'When you press Fire now (payload bound as event)'


def bindings_for(kind, payload, at):
    """The variables a fired run starts with: `event`, plus a convenience name per kind."""
    payload = payload if isinstance(payload, dict) else {}
    event = dict(payload, kind=kind, at=at)
    out = {'event': event}
    if kind == 'timer':
        out['now'] = at
    elif kind == 'file':
        out['path'] = payload.get('path')
    elif kind == 'chat':
        out['prompt'] = payload.get('prompt')
    return out


def _snapshot(root, rel):
    """(relative path -> (size, mtime)) for a file or everything under a folder, bounded."""
    base = Path(root)
    target = (base / rel) if rel else base
    out = {}
    try:
        if target.is_file():
            st = target.stat()
            out[rel or target.name] = (st.st_size, st.st_mtime)
            return out
        if not target.is_dir():
            return out
        count = 0
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in sorted(dirnames) if not d.startswith('.')]
            for name in sorted(filenames):
                if name.startswith('.'):
                    continue
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                out[str(full.relative_to(base))] = (st.st_size, st.st_mtime)
                count += 1
                if count >= MAX_WATCH_ENTRIES:
                    return out
    except OSError:
        pass
    return out


class EventRegistry:
    """Arms one Mainbook at a time and fires its runs. ``start_run(bindings, reason)`` is the
    server's approved run path; ``running()`` says whether a run is in progress; ``workspace()``
    gives the read-only workspace folder (or None)."""

    def __init__(self, start_run, journal=None, running=lambda: False, workspace=lambda: None, clock=time.time):
        self.start_run, self.journal, self.running, self.workspace, self.clock = start_run, journal, running, workspace, clock
        self.lock = threading.Lock()
        self.armed = None          # {'sources', 'fingerprint', 'armed_at', 'title'}
        self.queue = []            # [{'kind', 'payload', 'at', 'origin'}]
        self.recent = []           # last RECENT firings/drops, newest first
        self.fired_count = 0
        self.dropped_count = 0
        self.timers = {}           # index -> next due time
        self.watched = {}          # index -> last snapshot
        self.next_poll = 0
        self.thread = None
        self.stop_flag = threading.Event()

    # ---- arming ---------------------------------------------------------------
    def arm(self, sources, fingerprint, title=''):
        sources = check_sources(sources)
        if not sources:
            raise EventError('This Mainbook\'s Start has no event sources; add a timer, a file, chat or manual source first')
        ws = self.workspace()
        if any(s['kind'] == 'file' for s in sources) and not ws:
            raise EventError('A file source needs a workspace folder (choose one on the Setup page)')
        with self.lock:
            self.armed = {'sources': sources, 'fingerprint': fingerprint, 'armed_at': self.clock(), 'title': title}
            self.queue = []
            now = self.clock()
            self.timers = {i: now + s['every'] for i, s in enumerate(sources) if s['kind'] == 'timer'}
            self.watched = {i: _snapshot(ws, s['path']) for i, s in enumerate(sources) if s['kind'] == 'file'}
            self.next_poll = now + FILE_POLL
            self._ensure_thread()
        self._log('events.arm', 'Armed %s: %s' % (title or 'the Mainbook', '; '.join(describe(s, ws) for s in sources)), {'sources': sources, 'title': title})
        return self.status()

    def disarm(self, reason='by the owner'):
        with self.lock:
            was = self.armed
            self.armed = None
            self.queue = []
            self.timers = {}
            self.watched = {}
        if was is not None:
            self._log('events.disarm', 'Disarmed %s (%s)' % (was.get('title') or 'the Mainbook', reason), {'reason': reason})
        return self.status()

    def graph_changed(self, fingerprint):
        """Called when the document is saved: an edit to cells, arrows or sources disarms."""
        with self.lock:
            armed = self.armed
        if armed is not None and armed['fingerprint'] != fingerprint:
            self.disarm('the cells, arrows or sources changed; arm again to continue')
            return True
        return False

    # ---- firing ---------------------------------------------------------------
    def has_source(self, kind):
        with self.lock:
            return self.armed is not None and any(s['kind'] == kind for s in self.armed['sources'])

    def fire(self, kind, payload=None, origin='api'):
        """Queue one event; returns what happened: queued | coalesced | dropped (not armed / queue full)."""
        if kind not in EVENT_KINDS:
            raise EventError('Unknown event kind')
        payload = dict(payload) if isinstance(payload, dict) else {}
        if kind == 'chat' and isinstance(payload.get('prompt'), str):
            payload['prompt'] = payload['prompt'][:MAX_PROMPT]
        at = self.clock()
        with self.lock:
            if self.armed is None:
                outcome = 'not armed'
            elif kind != 'manual' and not any(s['kind'] == kind for s in self.armed['sources']):
                outcome = 'no %s source' % kind
            elif kind == 'timer' and any(q['kind'] == 'timer' for q in self.queue):
                outcome = 'coalesced'
            elif len(self.queue) >= QUEUE_MAX:
                outcome = 'queue full'
            else:
                self.queue.append({'kind': kind, 'payload': payload, 'at': at, 'origin': origin})
                outcome = 'queued'
        if outcome in ('not armed', 'queue full') or outcome.startswith('no '):
            self.dropped_count += 1
            self._remember({'kind': kind, 'at': at, 'outcome': 'dropped: ' + outcome, 'origin': origin})
            self._log('event.dropped', 'Event %s dropped: %s' % (kind, outcome), {'kind': kind, 'outcome': outcome, 'origin': origin})
        elif outcome == 'coalesced':
            self._remember({'kind': kind, 'at': at, 'outcome': 'coalesced', 'origin': origin})
        self._ensure_thread()
        return outcome

    def _start_next(self):
        with self.lock:
            if self.armed is None or not self.queue or self.running():
                return False
            item = self.queue.pop(0)
            title = self.armed.get('title')
        bindings = bindings_for(item['kind'], item['payload'], item['at'])
        reason = 'fired by %s' % item['kind'] + ((' (%s)' % item['payload']['path']) if item['kind'] == 'file' and item['payload'].get('path') else '')
        try:
            snap = self.start_run(bindings, reason)
        except Exception as error:  # no session, a run started meanwhile, a bad document: say so, keep watching
            self.dropped_count += 1
            self._remember({'kind': item['kind'], 'at': item['at'], 'outcome': 'dropped: %s' % error, 'origin': item['origin']})
            self._log('event.dropped', 'Event %s could not start a run: %s' % (item['kind'], error), {'kind': item['kind'], 'error': str(error)})
            return True
        self.fired_count += 1
        self._remember({'kind': item['kind'], 'at': item['at'], 'outcome': 'run ' + str(snap.get('id') if isinstance(snap, dict) else snap), 'origin': item['origin'], 'run': snap.get('id') if isinstance(snap, dict) else None})
        self._log('event.fired', 'Event %s started a run of %s' % (item['kind'], title or 'the Mainbook'), {'kind': item['kind'], 'run': snap.get('id') if isinstance(snap, dict) else None, 'origin': item['origin'], 'payload': {k: v for k, v in item['payload'].items() if k != 'prompt'}})
        return True

    # ---- the watcher thread -----------------------------------------------------
    def _ensure_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._loop, name='mainaiger-events', daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as error:  # the watcher must survive anything
                self._log('events.error', 'The event watcher hit an error and continues: %s' % error, {'error': str(error)})
            self.stop_flag.wait(TICK)

    def tick(self):
        """One pass: due timers, changed files, then start a queued run if the Lab is free. Safe to call from tests."""
        now = self.clock()
        with self.lock:
            armed = self.armed
            due = [i for i, t in self.timers.items() if t <= now] if armed else []
            for i in due:
                self.timers[i] = now + armed['sources'][i]['every']
            poll = armed is not None and self.watched and now >= self.next_poll
            if poll:
                self.next_poll = now + FILE_POLL
        for _ in due:
            self.fire('timer', {}, origin='timer')
        if poll:
            ws = self.workspace()
            for i, previous in list(self.watched.items()):
                src = armed['sources'][i]
                current = _snapshot(ws, src['path']) if ws else {}
                if current != previous:
                    changed = sorted(set(previous) ^ set(current)) or sorted(p for p in current if current[p] != previous.get(p))
                    with self.lock:
                        if self.armed is armed:
                            self.watched[i] = current
                    self.fire('file', {'path': changed[0] if changed else src['path'], 'changes': changed[:20], 'watched': src['path']}, origin='file')
        self._start_next()

    def close(self):
        self.stop_flag.set()
        self.disarm('the Lab stopped')

    # ---- reporting -------------------------------------------------------------
    def _remember(self, item):
        with self.lock:
            self.recent.insert(0, item)
            del self.recent[RECENT:]

    def _log(self, kind, text, data=None):
        if self.journal is not None:
            try:
                self.journal.write(kind, text, data)
            except Exception:
                pass

    def status(self):
        with self.lock:
            armed = self.armed
            ws = self.workspace()
            return {'armed': armed is not None,
                    'sources': [dict(s, description=describe(s, ws)) for s in (armed['sources'] if armed else [])],
                    'title': armed.get('title') if armed else None, 'armed_at': armed.get('armed_at') if armed else None,
                    'queue': len(self.queue), 'fired': self.fired_count, 'dropped': self.dropped_count, 'recent': list(self.recent),
                    'watching': self.thread is not None and self.thread.is_alive() and armed is not None,
                    'next_timer': min(self.timers.values()) if self.timers else None}
