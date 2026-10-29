"""Minimal stand-in for the local Lab service, used only by the canvas benchmark.

It serves the real UI files from local_lab/ui and answers the handful of /api
endpoints graph.js needs at boot and on save, using the real graph.py analysis so
the numbers reflect a realistic document. No kernel, no Docker, no owner key.

Usage: python3 tests/perf/stub_server.py <port> <cells>
"""
import json, sys, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from local_lab import graph as graph_module  # noqa: E402

UI = os.path.join(ROOT, 'local_lab', 'ui')
CELLS = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def make_document(n):
    """A branching ML-style graph of n code cells plus a few notes.

    Cells are arranged in layers of up to four. Every cell in a layer is triggered by one
    or two cells of the previous layer (fan-out from one parent, join-all into a child),
    so the graph has parallel branches that re-merge. Cell i defines v{i} from the
    variables of its parents, so static analysis derives matching data edges.
    """
    doc = graph_module.new_document()
    doc['notebook']['cells'] = []; doc['graph']['nodes'] = [{'id': 'start', 'kind': 'start'}]; doc['graph']['edges'] = []; doc['layout']['nodes'] = {}
    cells, nodes, edges, layout = doc['notebook']['cells'], doc['graph']['nodes'], doc['graph']['edges'], doc['layout']['nodes']
    widths = [1, 3, 4, 2, 4, 3, 1]  # repeating layer pattern: a single step fans out into 3, then 4, joins to 2, ...
    layers, i = [], 1
    while i <= n:
        w = widths[len(layers) % len(widths)]
        layer = []
        for _ in range(w):
            if i > n: break
            layer.append(f'cell-{i}'); i += 1
        layers.append(layer)
    for li, layer in enumerate(layers):
        parents = layers[li - 1] if li else ['start']
        for k, cid in enumerate(layer):
            # Parent 1: spread the previous layer across this one. Parent 2 (every other cell): the next parent over, creating a join.
            srcs = [parents[k % len(parents)]]
            if len(parents) > 1 and k % 2 == 1: srcs.append(parents[(k + 1) % len(parents)])
            reads = [s.replace('cell-', 'v') for s in srcs if s != 'start'] or ['1']
            num = cid.split('-')[1]
            src = f'v{num} = ' + ' + '.join(reads) + '\n' + f'print(v{num})\n'
            cells.append({'id': cid, 'cell_type': 'code', 'metadata': {'mainaiger': {'title': f'Step {num}'}}, 'source': src, 'execution_count': None, 'outputs': []})
            nodes.append({'id': cid, 'kind': 'code', 'cell_index': len(cells) - 1})
            for s in srcs: edges.append({'id': f'e-{s}-{cid}', 'kind': 'control', 'source': s, 'target': cid})
            layout[cid] = {'x': 60 + k * 380, 'y': 60 + li * 300}
    for cid in layers[-1]: edges.append({'id': f'e-{cid}-finish', 'kind': 'control', 'source': cid, 'target': 'finish'})
    for k in range(3):
        nid = f'note-{k+1}'
        cells.append({'id': nid, 'cell_type': 'markdown', 'metadata': {'mainaiger': {'title': 'Note'}}, 'source': 'A note about the pipeline.\n'})
        nodes.append({'id': nid, 'kind': 'markdown', 'cell_index': len(cells) - 1})
        layout[nid] = {'x': 60 + k * 380, 'y': 60 + (len(layers) + 1) * 300}
    nodes.append({'id': 'finish', 'kind': 'finish'})
    layout['start'] = {'x': 60, 'y': -140}; layout['finish'] = {'x': 60, 'y': 60 + len(layers) * 300}
    return graph_module.validate(doc)


STATE = {'doc': make_document(CELLS), 'revision': 0, 'saves': 0, 'analysis_ms': []}


def view():
    doc = STATE['doc']
    t = time.perf_counter()
    out = {'document': doc, 'revision': STATE['revision'], 'analysis': graph_module.analysis(doc), 'data_edges': graph_module.data_edges(doc),
           'order': graph_module.order(doc), 'disconnected': graph_module.disconnected(doc), 'run': None,
           'session': {'alive': False, 'executing': False}, 'csrf': 'bench'}
    STATE['analysis_ms'].append((time.perf_counter() - t) * 1000)
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/api/graph': return self.send_json(view())
        if p == '/api/examples': return self.send_json({'examples': []})
        if p == '/api/mainbooks': return self.send_json({'configured': False, 'folder': None, 'default': '', 'mainbooks': []})
        if p == '/api/kernel': return self.send_json({'alive': False, 'executing': False})
        if p == '/api/graph/run': return self.send_json({'run': None})
        if p == '/__bench/stats': return self.send_json({'saves': STATE['saves'], 'analysis_ms': STATE['analysis_ms']})
        if p == '/graph' or p == '/': p = '/graph.html'
        f = os.path.join(UI, p.lstrip('/'))
        if not os.path.isfile(f): return self.send_json({'error': 'not found'}, 404)
        ctype = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}.get(os.path.splitext(f)[1], 'application/octet-stream')
        data = open(f, 'rb').read(); self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0); body = json.loads(self.rfile.read(n) or b'{}')
        p = self.path.split('?')[0]
        if p == '/api/graph':
            STATE['doc'] = graph_module.validate(body['document']); STATE['revision'] += 1; STATE['saves'] += 1
            return self.send_json(view())
        return self.send_json({'error': 'not supported in benchmark'}, 400)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
