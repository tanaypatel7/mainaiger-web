"""Execute a planned list of graph nodes in one kernel session, serially.

Trigger semantics in this version: a node runs when every node before it in
the plan has finished successfully. When a node finishes with status ``ok``,
its outgoing control edges *fire* (recorded in ``fired``) — that is what the
canvas animates. If a node fails, is interrupted or the session dies, the
remaining nodes are marked ``skipped`` and the run ends. Nothing runs in
parallel; a branching picture is still one Python process executing one
cell at a time.
"""
import json
import secrets
import threading
import time

from .kernel_session import KernelError
from . import graph as graph_module


class GraphRun:
    def __init__(self, document, plan_ids, kernel, mode='all', on_result=None, kernel_for=None, on_finish=None, ask_model=None):
        self.id = 'run-' + secrets.token_hex(4)
        self.document, self.plan, self.kernel, self.mode = document, list(plan_ids), kernel, mode
        self.on_result = on_result
        # kernel_for(label) returns the session a cell named (metadata.mainaiger.session), or the
        # active one for None; it raises KernelError when that session is missing or dead.
        self.kernel_for = kernel_for
        self.on_finish = on_finish
        # ask_model(config, prompt) -> {'ok', 'value', 'raw', 'error', 'elapsed_ms', 'attempts', 'model'}; the server
        # supplies it (local_lab/decide.py or models.chat). Model cells run on the host, never inside the container.
        self.ask_model = ask_model
        self.current_kernel = None
        self.state = 'pending'
        self.current = None
        self.nodes = {ident: {'status': 'pending'} for ident in self.plan}
        self.fired = []
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.cancel_requested = False
        self.thread = None
        self.lock = threading.Lock()

    # ---- control ------------------------------------------------------
    def start(self):
        self.state, self.started_at = 'running', time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def cancel(self):
        """Stop after the current cell: interrupt it and skip the rest."""
        self.cancel_requested = True
        if self.state == 'running':
            kernel = self.current_kernel or self.kernel
            if kernel is not None:
                kernel.interrupt()
        return True

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

    def _outgoing(self, ident):
        return [e['id'] for e in graph_module.control_edges(self.document) if e['source'] == ident]

    def _run(self):
        try:
            for index, ident in enumerate(self.plan):
                if self.cancel_requested:
                    self._skip_from(index, 'cancelled')
                    self.state = 'cancelled'
                    return
                self.current = ident
                with self.lock:
                    self.nodes[ident] = {'status': 'running', 'started_at': time.time()}
                source = self._source(ident)
                label = graph_module.session_of(self.document, ident)
                cfg = graph_module.model_config(self._cell(ident))
                form = graph_module.form_config(self._cell(ident))
                if form:
                    # Bind the form's values first; the cell's own code (if any) runs after them in the same request.
                    source = graph_module.form_prelude(form) + source
                if cfg:
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
                with self.lock:
                    self.nodes[ident] = dict(result, status=result['status'])
                if self.on_result:
                    try:
                        self.on_result(ident, result)
                    except Exception:  # recording must never break the run
                        pass
                if result['status'] != 'ok':
                    self._skip_from(index + 1, 'skipped')
                    self.state = 'cancelled' if result['status'] == 'interrupted' and self.cancel_requested else 'failed'
                    self.error = '%s stopped the run (%s).' % (ident, result['status'])
                    return
                with self.lock:
                    self.fired.extend(self._outgoing(ident))
            self.state = 'finished'
        except Exception as error:  # a bug must never leave a run "running" forever
            with self.lock:
                for ident in self.plan:
                    if self.nodes[ident].get('status') in ('pending', 'running'):
                        self.nodes[ident] = {'status': 'skipped'}
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

    def _skip_from(self, index, status):
        with self.lock:
            for ident in self.plan[index:]:
                if self.nodes[ident].get('status') in ('pending', 'running'):
                    self.nodes[ident] = {'status': status}

    # ---- reporting ----------------------------------------------------
    def snapshot(self):
        with self.lock:
            nodes = {ident: dict(value) for ident, value in self.nodes.items()}
            fired = list(self.fired)
        return {'id': self.id, 'mode': self.mode, 'state': self.state, 'plan': list(self.plan), 'current': self.current,
                'nodes': nodes, 'fired': fired, 'started_at': self.started_at, 'finished_at': self.finished_at,
                'error': self.error, 'cancel_requested': self.cancel_requested}
