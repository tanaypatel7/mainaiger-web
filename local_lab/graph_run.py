"""Execute a planned set of graph nodes in their sessions, serially, driven by the arrows.

The rules, in plain words (docs/LOCAL_LAB_SETUP.md "Loop arrows and conditions"):

* Cells are visited in trigger order (the topological order of the forward arrows), one at a time.
* When a cell finishes ``ok``, each arrow leaving it is decided: an arrow without a condition
  *fires*; an arrow with a condition (one Python expression) fires only if the cell's session
  answers true. Deciding never runs a cell: the kernel's ``evaluate`` op answers the expression
  without advancing the execution count.
* A cell runs when at least one arrow into it fired (or it is a seed: the first cell of the plan,
  or the cell a loop arrow rewound to). A cell none of whose arrows fired is marked ``idle`` and
  its own arrows do not fire, so the cells behind an untaken branch stay idle too. A cell with
  arrows from two branches runs when either fired, once both have been decided.
* A *loop arrow* leads back to an earlier cell and carries a budget (``max_fires``). When it fires,
  its body — the loop's target and everything forward of it in the plan — is rewound to pending and
  runs again; the arrows leaving the body fire again when the body finishes for the last time.
  Once the budget is spent the arrow is ``exhausted`` and the run carries on past the loop. Every
  run also has a hard cap, ``MAX_STEPS`` cell executions, whatever the budgets say.
* A cell that fails (error, timeout, killed, interrupted) stops the run; the rest is ``skipped``. A
  condition that raises stops the run too, naming the arrow.

Nothing runs in parallel; a branching picture is still one cell at a time, each in the session its
cell names (or the active one). What the canvas animates — fired arrows, loop counts, idle cells —
is the ``snapshot``.
"""
import json
import secrets
import threading
import time

from .kernel_session import KernelError
from . import graph as graph_module

MAX_STEPS = 500  # cell executions per run, counting every loop pass and every nested Mainbook: the stop under any budget mistake
MAX_DEPTH = 3    # nested Mainbooks: a cell runs a Mainbook whose cell runs a Mainbook, and no deeper
SETTLED = ('fired', 'not_fired', 'idle', 'blocked', 'exhausted', 'error')


class ConditionError(Exception):
    """A trigger condition could not be answered; carries the edge and the kernel's error."""

    def __init__(self, edge, message, detail=None):
        super().__init__(message)
        self.edge, self.detail = edge, detail


class GraphRun:
    def __init__(self, document, plan_ids, kernel, mode='all', on_result=None, kernel_for=None, on_finish=None, ask_model=None, on_edge=None, bindings=None, reason=None,
                 load_mainbook=None, on_inner=None, depth=0, ancestry=(), budget=None):
        self.id = 'run-' + secrets.token_hex(4)
        self.document, self.plan, self.kernel, self.mode = document, list(plan_ids), kernel, mode
        self.on_result = on_result
        # Nested Mainbooks: load_mainbook(name) returns a validated document; on_inner(parent_ident, name, ident, result)
        # hears every inner cell result (for the journal). The step budget is shared down the nesting.
        self.load_mainbook, self.on_inner, self.depth, self.ancestry = load_mainbook, on_inner, depth, tuple(ancestry)
        self.budget = budget if budget is not None else {'steps': 0}
        self.inner = None
        # bindings: variables an event binds in the active session before the first cell (local_lab/events.py);
        # reason: why this run started ("fired by timer"), shown in the snapshot and the journal.
        self.bindings = dict(bindings) if bindings else None
        self.reason = reason
        # kernel_for(label) returns the session a cell named (metadata.mainaiger.session), or the
        # active one for None; it raises KernelError when that session is missing or dead.
        self.kernel_for = kernel_for
        self.on_finish = on_finish
        # ask_model(config, prompt) -> {'ok', 'value', 'raw', 'error', 'elapsed_ms', 'attempts', 'model'}; the server
        # supplies it (local_lab/decide.py or models.chat). Model cells run on the host, never inside the container.
        self.ask_model = ask_model
        # on_edge(edge, info) is told each time an arrow is decided (fired, not fired, exhausted, blocked, error).
        self.on_edge = on_edge
        self.current_kernel = None
        self.state = 'pending'
        self.current = None
        self.nodes = {ident: {'status': 'pending'} for ident in self.plan}
        self.fired = []          # edge ids in the order they fired; a loop arrow appears once per firing
        self.steps = 0           # cell executions so far (every loop pass counts)
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.cancel_requested = False
        self.thread = None
        self.lock = threading.Lock()
        # ---- the arrows this run decides ------------------------------------------------------
        doc = self.document
        self.plan_set = set(self.plan)
        self.start_id = next(n['id'] for n in doc['graph']['nodes'] if n['kind'] == 'start')
        sources = self.plan_set | ({self.start_id} if mode == 'all' else set())
        self.control = [e for e in graph_module.control_edges(doc) if e['source'] in sources]
        self.incoming = {ident: [e for e in self.control if e['target'] == ident and not e.get('loop')] for ident in self.plan}
        self.edges = {}
        for edge in self.control:
            info = {'state': None, 'fires': 0, 'source': edge['source'], 'target': edge['target'], 'loop': bool(edge.get('loop'))}
            if edge.get('when'):
                info['when'] = edge['when']
            if edge.get('loop'):
                info['max_fires'] = edge.get('max_fires', graph_module.LOOP_DEFAULT_FIRES)
            self.edges[edge['id']] = info
        # Seeds run without waiting for an arrow: the plan's first cells (nothing in the plan points at
        # them) and, later, the cell a loop arrow rewinds to. In "all" mode Start fires instead.
        self.seeds = {ident for ident in self.plan if not self.incoming[ident]}

    # ---- control ------------------------------------------------------
    def start(self):
        self.state, self.started_at = 'running', time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def cancel(self):
        """Stop after the current cell: interrupt it and skip the rest (a nested Mainbook is cancelled too)."""
        self.cancel_requested = True
        inner = self.inner
        if inner is not None:
            inner.cancel()
        if self.state == 'running':
            kernel = self.current_kernel or self.kernel
            if kernel is not None:
                kernel.interrupt()
        return True

    def run_inline(self):
        """Run on the calling thread (a nested Mainbook runs inside its parent's cell)."""
        self.state, self.started_at = 'running', time.time()
        self._run()
        return self.state

    def wait(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout)
        return self.state

    # ---- execution ----------------------------------------------------
    def _source(self, ident):
        node = graph_module.node_by_id(self.document, ident)
        return graph_module.source_text(self.document['notebook']['cells'][node['cell_index']])

    def _cell(self, ident):
        node = graph_module.node_by_id(self.document, ident)
        return self.document['notebook']['cells'][node['cell_index']]

    def _title(self, ident):
        if ident == self.start_id:
            return 'Start'
        node = next((n for n in self.document['graph']['nodes'] if n['id'] == ident), None)
        if node is None or node['kind'] not in ('code', 'markdown'):
            return ident
        return self._cell(ident).get('metadata', {}).get('mainaiger', {}).get('title') or ident

    def _session_kernel(self, ident):
        label = graph_module.session_of(self.document, ident) if ident != self.start_id else None
        return self.kernel_for(label) if self.kernel_for else self.kernel

    def _recorded_output(self, ref):
        """The stdout a cell printed: from this run when it ran here, else from the document."""
        target = next((n for n in self.document['graph']['nodes'] if n['id'] == ref), None)
        if target is None:
            for node in self.document['graph']['nodes']:
                if node['kind'] == 'code' and (self._cell(node['id']).get('metadata', {}).get('mainaiger', {}).get('title') == ref):
                    target = node
                    break
        if target is None or target['kind'] != 'code':
            return None
        with self.lock:
            ran = self.nodes.get(target['id'])
        if ran and 'output' in ran:
            return ran.get('output') or ''
        text = ''
        for out in self._cell(target['id']).get('outputs') or []:
            if out.get('output_type') == 'stream':
                text += ''.join(out['text']) if isinstance(out.get('text'), list) else str(out.get('text', ''))
        return text

    def _render_prompt(self, source, kernel):
        """Fill {{output:cell}} and {{var:name}}; returns (prompt, problem)."""
        summaries = None
        def fill(match):
            nonlocal summaries
            kind, name = match.group(1), match.group(2)
            if kind == 'output':
                text = self._recorded_output(name)
                if text is None:
                    raise KeyError('no code cell named "%s" for {{output:%s}}' % (name, name))
                return text
            if summaries is None:
                summaries = {v['name']: v for v in (kernel.variables() if kernel is not None else [])}
            if name not in summaries:
                raise KeyError('"%s" is not defined in the session for {{var:%s}}' % (name, name))
            return '%s (%s) = %s' % (name, summaries[name]['type'], summaries[name]['summary'])
        try:
            return graph_module.PLACEHOLDER.sub(fill, source), None
        except KeyError as error:
            return None, str(error).strip('\'"')

    def _model_cell(self, ident, cfg, source, label):
        """Ask the model on the host, then bind the answer to cfg['output'] in the cell's session."""
        started = time.time()
        def failed(message, kind='ModelError'):
            return {'status': 'error', 'count': None, 'output': '', 'truncated': False, 'elapsed_ms': int((time.time() - started) * 1000),
                    'error': {'ename': kind, 'evalue': message, 'traceback': []}, 'model': cfg['model']}
        if self.ask_model is None:
            return failed('This Lab has no model runner configured')
        try:
            kernel = self.kernel_for(label) if self.kernel_for else self.kernel
        except KernelError as error:
            return failed(str(error), 'KernelError')
        prompt, problem = self._render_prompt(source, kernel)
        if problem:
            return failed(problem, 'PromptError')
        if not prompt.strip():
            return failed('The prompt is empty')
        try:
            answer = self.ask_model(cfg, prompt)
        except Exception as error:  # a provider bug must not kill the run thread
            return failed('The model runner failed: %s' % error)
        if not answer.get('ok'):
            return failed(answer.get('error') or 'The model did not answer')
        value = answer.get('value')
        literal = json.dumps(value, ensure_ascii=False)
        code = '%s = __import__("json").loads(%r)\n' % (cfg['output'], literal)
        try:
            bound = kernel.execute(code)
        except KernelError as error:
            return failed('The answer could not be stored in the session: %s' % error, 'KernelError')
        if bound.get('status') != 'ok':
            return failed('The answer could not be stored in the session: %s' % (bound.get('error') or {}).get('evalue', bound.get('status')), 'KernelError')
        shown = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False)
        return {'status': 'ok', 'count': bound.get('count'), 'output': shown, 'truncated': False, 'error': None,
                'elapsed_ms': int((time.time() - started) * 1000), 'model': answer.get('model', cfg['model']), 'attempts': answer.get('attempts'),
                'note': 'Bound to %s in the session (model %s%s).' % (cfg['output'], answer.get('model', cfg['model']), ', schema-checked' if cfg.get('schema') else '')}

    def _mainbook_cell(self, ident, book):
        """Run a saved Mainbook, whole, inside this cell: same sessions, shared step budget, its own loops and arrows."""
        started = time.time()
        name = book['name']
        def failed(message, kind='MainbookError', status='error'):
            return {'status': status, 'count': None, 'output': '', 'truncated': False, 'elapsed_ms': int((time.time() - started) * 1000),
                    'error': {'ename': kind, 'evalue': message, 'traceback': []}, 'mainbook': name}
        if self.load_mainbook is None:
            return failed('This Lab cannot load Mainbooks here')
        if name in self.ancestry:
            return failed('Mainbook "%s" would run itself (%s); a Mainbook cannot include itself' % (name, ' → '.join(self.ancestry + (name,))))
        if self.depth + 1 >= MAX_DEPTH:
            return failed('Mainbooks nest at most %d deep' % MAX_DEPTH)
        try:
            inner_doc = self.load_mainbook(name)
            plan = graph_module.plan(inner_doc, 'all')
        except Exception as error:
            return failed('Mainbook "%s" could not be loaded: %s' % (name, error))
        if not plan:
            return failed('Mainbook "%s" has no code cells reachable from Start' % name)
        inner = GraphRun(inner_doc, plan, self.kernel, mode='all', kernel_for=self.kernel_for, ask_model=self.ask_model, on_edge=self.on_edge,
                         on_result=(lambda i, r: self.on_inner(ident, name, i, r)) if self.on_inner else None,
                         load_mainbook=self.load_mainbook, on_inner=self.on_inner, depth=self.depth + 1, ancestry=self.ancestry + (name,), budget=self.budget)
        self.inner = inner
        try:
            inner.run_inline()
        finally:
            self.inner = None
        snap = inner.snapshot()
        lines = []
        for inner_ident in inner.plan:
            entry = snap['nodes'].get(inner_ident) or {}
            first = (entry.get('output') or '').strip().splitlines()
            lines.append('%s: %s%s%s' % (inner._title(inner_ident), entry.get('status', 'pending'), (' · %d ms' % entry['elapsed_ms']) if entry.get('elapsed_ms') is not None else '', (' · ' + first[0][:120]) if first else ''))
        ok = sum(1 for n in snap['nodes'].values() if n.get('status') == 'ok')
        summary = 'Mainbook "%s": %s · %d/%d ok · %d step%s%s' % (name, snap['state'], ok, len(inner.plan), snap['steps'], '' if snap['steps'] == 1 else 's', (' · ' + snap['error']) if snap.get('error') else '')
        output = summary + '\n' + '\n'.join(lines) + '\n'
        compact = {'name': name, 'state': snap['state'], 'steps': snap['steps'], 'error': snap.get('error'), 'edges': snap['edges'], 'fired': snap['fired'],
                   'nodes': {i: {k: (v[:300] if k == 'output' and isinstance(v, str) else v) for k, v in n.items() if k in ('status', 'elapsed_ms', 'output', 'passes')} for i, n in snap['nodes'].items()}}
        if snap['state'] == 'finished':
            return {'status': 'ok', 'count': None, 'output': output, 'truncated': False, 'error': None, 'elapsed_ms': int((time.time() - started) * 1000), 'mainbook': name, 'sub': compact,
                    'note': 'Ran Mainbook "%s" in this session (%d cells).' % (name, len(inner.plan))}
        status = 'interrupted' if snap['state'] == 'cancelled' else 'error'
        result = failed(snap.get('error') or 'Mainbook "%s" %s' % (name, snap['state']), status=status)
        result['output'] = output
        result['sub'] = compact
        return result

    def _execute(self, ident):
        """Run one cell (code, form, model or nested Mainbook) in its session; returns the result dict."""
        source = self._source(ident)
        label = graph_module.session_of(self.document, ident)
        cfg = graph_module.model_config(self._cell(ident))
        form = graph_module.form_config(self._cell(ident))
        book = graph_module.mainbook_config(self._cell(ident))
        if form:
            # Bind the form's values first; the cell's own code (if any) runs after them in the same request.
            source = graph_module.form_prelude(form) + source
        if book:
            result = self._mainbook_cell(ident, book)
        elif cfg:
            result = self._model_cell(ident, cfg, source, label)
        elif not source.strip():
            result = {'status': 'ok', 'count': None, 'output': '', 'truncated': False, 'error': None, 'elapsed_ms': 0,
                      'note': 'Empty cell; nothing to run.'}
        else:
            try:
                kernel = self.kernel_for(label) if self.kernel_for else self.kernel
                self.current_kernel = kernel
                result = self.kernel.execute(source) if kernel is None else kernel.execute(source)
            except KernelError as error:
                result = {'status': 'killed', 'count': None, 'output': '', 'truncated': False, 'elapsed_ms': 0,
                          'error': {'ename': 'KernelError', 'evalue': str(error), 'traceback': []}}
            finally:
                self.current_kernel = None
        if label:
            result['session'] = label
        if form and result.get('status') == 'ok':
            bound = graph_module.form_bindings(form)
            result['note'] = ((result.get('note') + ' ') if result.get('note') else '') + 'Form values bound: ' + ', '.join('%s=%s' % (k, json.dumps(v)) for k, v in bound.items())
        return result

    # ---- arrows ---------------------------------------------------------
    def _triggered(self, ident):
        with self.lock:
            return ident in self.seeds or any(self.edges[e['id']]['state'] == 'fired' for e in self.incoming[ident])

    def _tell(self, edge, info):
        if self.on_edge:
            try:
                self.on_edge(edge, dict(info, source_title=self._title(edge['source']), target_title=self._title(edge['target'])))
            except Exception:  # reporting must never break the run
                pass

    def _decide(self, edge, kernel):
        """True when the arrow fires: no condition, or its expression answers true in the session."""
        when = edge.get('when')
        if not when:
            return True, None
        if kernel is None:
            raise ConditionError(edge, 'no session can answer the condition')
        try:
            answer = kernel.evaluate(when)
        except (KernelError, ValueError) as error:
            raise ConditionError(edge, str(error))
        if answer.get('status') != 'ok':
            err = answer.get('error') or {}
            what = ('%s: %s' % (err.get('ename', ''), err.get('evalue', ''))).strip(': ') or answer.get('status', 'error')
            raise ConditionError(edge, what, err)
        return bool(answer.get('truth')), answer.get('value')

    def _fire_from(self, ident):
        """Decide every arrow leaving ident (after it finished ok, or Start at the beginning)."""
        leaving = [e for e in self.control if e['source'] == ident]
        if not leaving:
            return
        kernel = None
        if any(e.get('when') for e in leaving):
            try:
                kernel = self._session_kernel(ident)
            except KernelError as error:
                raise ConditionError(next(e for e in leaving if e.get('when')), str(error))
        rewinds = []
        for edge in leaving:
            info = self.edges[edge['id']]
            truth, value = self._decide(edge, kernel)
            with self.lock:
                if value is not None:
                    info['value'] = value
                if not truth:
                    info['state'] = 'not_fired'
                elif not edge.get('loop'):
                    info['state'] = 'fired'; info['fires'] += 1; self.fired.append(edge['id'])
                elif edge['target'] not in self.plan_set:
                    info['state'] = 'blocked'  # the loop leads to a cell outside this run (run "from" a cell inside the loop)
                elif info['fires'] >= info['max_fires']:
                    info['state'] = 'exhausted'
                else:
                    info['state'] = 'fired'; info['fires'] += 1; self.fired.append(edge['id']); rewinds.append(edge)
            if edge.get('when') or edge.get('loop'):
                self._tell(edge, info)
        for edge in rewinds:
            self._rewind(edge)

    def _rewind(self, edge):
        """A loop arrow fired: its body (the target and everything forward of it in the plan) runs again."""
        body = graph_module.reachable(self.document, [edge['target']]) & self.plan_set
        with self.lock:
            for ident in body:
                previous = self.nodes.get(ident) or {}
                self.nodes[ident] = {'status': 'pending', 'passes': previous.get('passes', 0) + (1 if previous.get('status') not in (None, 'pending', 'idle') else 0)}
            for e in self.control:
                if e['source'] in body:
                    self.edges[e['id']]['state'] = None
            self.seeds.add(edge['target'])

    def _settle_idle(self, ident):
        with self.lock:
            self.nodes[ident] = {'status': 'idle', 'note': 'No arrow into this cell fired.'}
            for e in self.control:
                if e['source'] == ident:
                    self.edges[e['id']]['state'] = 'idle'

    def _next_pending(self):
        with self.lock:
            return next((ident for ident in self.plan if self.nodes[ident].get('status') == 'pending'), None)

    # ---- the engine -----------------------------------------------------
    def _bind_event(self):
        """An event's payload becomes variables in the active session, like a form in front of the graph."""
        kernel = self.kernel_for(None) if self.kernel_for else self.kernel
        if kernel is None:
            raise KernelError('No session to bind the event in')
        code = ''.join('%s = __import__("json").loads(%r)\n' % (name, json.dumps(value, ensure_ascii=False)) for name, value in self.bindings.items() if graph_module.NAME.match(name))
        result = kernel.execute(code)
        if result.get('status') != 'ok':
            raise KernelError('The event could not be bound in the session: %s' % ((result.get('error') or {}).get('evalue') or result.get('status')))

    def _run(self):
        try:
            if self.bindings:
                try:
                    self._bind_event()
                except KernelError as error:
                    self._skip_pending('skipped')
                    self.state = 'failed'
                    self.error = str(error)
                    return
            if self.mode == 'all':
                self._fire_from(self.start_id)
            while True:
                if self.cancel_requested:
                    self._skip_pending('cancelled')
                    self.state = 'cancelled'
                    return
                ident = self._next_pending()
                if ident is None:
                    break
                if not self._triggered(ident):
                    self._settle_idle(ident)
                    continue
                self.seeds.discard(ident)
                if self.budget['steps'] >= MAX_STEPS:
                    self._skip_pending('skipped')
                    self.state = 'failed'
                    self.error = 'The run stopped after %d cell executions (the limit); check the loop budgets.' % MAX_STEPS
                    return
                self.steps += 1
                self.budget['steps'] += 1
                self.current = ident
                with self.lock:
                    passes = self.nodes[ident].get('passes', 0)
                    self.nodes[ident] = {'status': 'running', 'started_at': time.time(), 'passes': passes}
                result = self._execute(ident)
                if passes:
                    result['passes'] = passes
                with self.lock:
                    self.nodes[ident] = dict(result, status=result['status'])
                if self.on_result:
                    try:
                        self.on_result(ident, result)
                    except Exception:  # recording must never break the run
                        pass
                if result['status'] != 'ok':
                    cancelled = result['status'] == 'interrupted' and self.cancel_requested
                    self._skip_pending('cancelled' if cancelled else 'skipped')
                    self.state = 'cancelled' if cancelled else 'failed'
                    why = (result.get('error') or {}).get('evalue') or ''
                    self.error = '%s stopped the run (%s)%s' % (self._title(ident), result['status'], (': ' + why[:300]) if why and not cancelled else '.')
                    return
                self._fire_from(ident)
            self.state = 'finished'
        except ConditionError as error:
            with self.lock:
                info = self.edges.get(error.edge['id'])
                if info is not None:
                    info['state'] = 'error'; info['error'] = str(error)
            self._tell(error.edge, info or {})
            self._skip_pending('skipped')
            self.state = 'failed'
            self.error = 'The condition on the arrow %s → %s could not be answered: %s' % (self._title(error.edge['source']), self._title(error.edge['target']), error)
        except Exception as error:  # a bug must never leave a run "running" forever
            self._skip_pending('skipped')
            self.state = 'failed'
            self.error = 'The run stopped on an internal error: %s: %s' % (type(error).__name__, error)
        finally:
            self.current = None
            self.finished_at = time.time()
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:  # reporting must never break the run
                    pass

    def _skip_pending(self, status):
        with self.lock:
            for ident in self.plan:
                if self.nodes[ident].get('status') in ('pending', 'running'):
                    self.nodes[ident] = {'status': status}

    # ---- reporting ----------------------------------------------------
    def snapshot(self):
        with self.lock:
            nodes = {ident: dict(value) for ident, value in self.nodes.items()}
            fired = list(self.fired)
            edges = {ident: dict(value) for ident, value in self.edges.items()}
        return {'id': self.id, 'mode': self.mode, 'state': self.state, 'plan': list(self.plan), 'current': self.current,
                'nodes': nodes, 'fired': fired, 'edges': edges, 'steps': self.steps, 'started_at': self.started_at, 'finished_at': self.finished_at,
                'error': self.error, 'cancel_requested': self.cancel_requested, 'reason': self.reason,
                'event': (self.bindings or {}).get('event')}
