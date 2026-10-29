"""Builds examples/ask-memory.ipymb: the ask pipeline as a Mainbook you can rearrange.

Run: python3 examples/build_ask_memory.py

Question (form) → Search memory (memory cell) → Choose notes (code: the rule for what to reuse)
→ Ask with notes (memory cell) → Keep the answer (memory cell: write) → Report. Every step is a
card; the rule in "Choose notes" is yours to edit — that is the point.
"""
import json
from pathlib import Path

README = """# Ask memory, as a Mainbook

The same three stages as the ASK box on the Memory page, laid out as cards so you can change the rule in the middle. **Question** is a form; **Search memory** is a Memory cell (the Lab searches your notes on this Mac and stores `hits`); **Choose notes** is ordinary Python — it decides which hits to hand to the model (edit it: prefer stable notes, require a title match, cap the count, or stop the run with `raise SystemExit` when a stable note already answers); **Ask with notes** is a Memory cell that asks your local model with those notes and stores `answer`; **Keep the answer** writes it as a draft note; **Report** prints it. Needs a running session, an approved Memory folder and an installed model.
"""

CHOOSE = """# The rule for what memory the model may see. Edit freely; it is plain Python over `hits`.
stable = [h for h in hits if h["status"] == "stable"]
chosen = (stable or hits)[:3]
use = [h["path"] for h in chosen]
print("question:", question)
print("%d hit(s); using %d:" % (len(hits), len(use)))
for h in chosen:
    print(" -", h["title"], "(%s)" % h["status"])
if not hits:
    print("nothing in memory matches; the model answers from scratch")
"""

KEEP = """title = "Q: " + question[:70]
body = answer["reply"] + "\\n\\n## Question\\n\\n" + question + ("\\n\\n## Notes used\\n\\n" + "\\n".join("- " + p for p in answer["used"]) if answer["used"] else "")
kind = "answer"
tags = ["ask", "mainbook"]
print("writing a draft answer note…")
"""

REPORT = """print("=" * 48)
print(answer["reply"].strip())
print("=" * 48)
print("model:", answer["model"], "| %d ms" % answer["elapsed_ms"], "| notes used:", len(answer["used"]), "| saved as:", note_path)
"""


def cell(ident, title, source, kind='code', **own):
    c = {'id': ident, 'cell_type': kind, 'metadata': {'mainaiger': dict(title=title, **own)}, 'source': source}
    if kind == 'code':
        c.update(execution_count=None, outputs=[])
    return c


def build():
    cells = [
        cell('readme', 'Ask memory, as a Mainbook', README, kind='markdown'),
        cell('question', 'Question', 'print("asking:", question)\n', form={'fields': [{'name': 'question', 'type': 'text', 'label': 'Your question', 'default': 'What do I know about this project?'}], 'values': {}}),
        cell('search', 'Search memory', '', memory={'op': 'search', 'output': 'hits'}),
        cell('choose', 'Choose notes', CHOOSE),
        cell('ask', 'Ask with notes', '', memory={'op': 'ask', 'output': 'answer'}),
        cell('keep', 'Keep the answer', KEEP),
        cell('write', 'Write the note', '', memory={'op': 'write', 'output': 'note_path'}),
        cell('report', 'Report', REPORT),
    ]
    order = ['question', 'search', 'choose', 'ask', 'keep', 'write', 'report']
    nodes = [{'id': 'start', 'kind': 'start'}, {'id': 'readme', 'kind': 'markdown', 'cell_index': 0}]
    nodes += [{'id': c['id'], 'kind': 'code', 'cell_index': i} for i, c in enumerate(cells) if c['cell_type'] == 'code']
    nodes.append({'id': 'finish', 'kind': 'finish'})
    chain = ['start'] + order + ['finish']
    edges = [{'id': 'e-%s-%s' % (a, b), 'kind': 'control', 'source': a, 'target': b} for a, b in zip(chain, chain[1:])]
    layout = {'readme': {'x': 40, 'y': 40}, 'start': {'x': 40, 'y': 520}, 'finish': {'x': 2060, 'y': 520}}
    columns = [['question', 'search'], ['choose', 'ask'], ['keep', 'write'], ['report']]
    for col, ids in enumerate(columns):
        for row, ident in enumerate(ids):
            layout[ident] = {'x': 460 + col * 400, 'y': 40 + row * 380}
    return {'format': 'mainaiger', 'schema_version': 2,
            'notebook': {'nbformat': 4, 'nbformat_minor': 5,
                         'metadata': {'kernelspec': {'name': 'python3', 'display_name': 'Python 3', 'language': 'python'},
                                      'mainaiger': {'example': True, 'name': 'ask-memory', 'title': 'Ask memory as a Mainbook (search, choose, ask, keep)'}},
                         'cells': cells},
            'graph': {'nodes': nodes, 'edges': edges},
            'layout': {'nodes': layout, 'viewport': {'x': 40, 'y': 30, 'zoom': 0.6}}}


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from local_lab import graph as g
    doc = g.validate(build())
    out = Path(__file__).resolve().parent / 'ask-memory.ipymb'
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print('wrote', out, g.order(doc))
