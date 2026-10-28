"""Execute a planned list of graph nodes in one kernel session, serially.

Trigger semantics in this version: a node runs when every node before it in
the plan has finished successfully. When a node finishes with status ``ok``,
its outgoing control edges *fire* (recorded in ``fired``) — that is what the
canvas animates. If a node fails, is interrupted or the session dies, the
remaining nodes are marked ``skipped`` and the run ends. Nothing runs in
parallel; a branching picture is still one Python process executing one
cell at a time.
"""
import secrets
import threading
import time

from .kernel_session import KernelError
from . import graph as graph_module


class GraphRun:
    def __init__(self, document, plan_ids, kernel, mode='all', on_result=None, kernel_for=None, on_finish=None):
        self.id = 'run-' + secrets.token_hex(4)
        self.document, self.plan, self.kernel, self.mode = document, list(plan_ids), kernel, mode
        self.on_result = on_result
        # kernel_for(label) returns the session a cell named (metadata.mainaiger.session), or the
        # active one for None; it raises KernelError when that session is missing or dead.
        self.kernel_for = kernel_for
        self.on_finish = on_finish
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
                if not source.strip():
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
