"""Builds examples/review-chats.ipymb: the chat-review loop as a Mainbook with your rule in the middle.

Run: python3 examples/build_review_chats.py

Review next chat (memory cell) → Choose items (code: the rule for what to keep) → Keep (memory cell,
only when the chat is real and the model answered) → Report → loop back to Review while chats are
pending (at most 3 more). Kept notes are drafts: the Memory page is still where you promote them.
"""
import json
from pathlib import Path

README = """# Review imported chats, as a Mainbook

The REVIEW section of the Memory page, laid out as cards. **Review next chat** is a Memory cell: the Lab takes the next unreviewed chat in `chats/` and asks your local model what is worth keeping (`proposal`; nothing is written). **Choose items** is plain Python — the rule for what to keep (edit it: keep only facts and procedures, require a minimum length, drop items whose title repeats a note you have, or `raise SystemExit` to stop and look). **Keep** writes the chosen items as *draft* notes linked to the chat and marks the chat reviewed — it only runs when the arrow's condition holds (a real chat, and the model answered). **Report** prints what happened, and its loop arrow goes back to **Review next chat** while chats are pending, at most 3 more per run. Needs a running session, an approved Memory folder with imported chats, and an installed model. Drafts stay drafts until you mark them stable on the Memory page.
"""

CHOOSE = """# The rule for what to keep from each chat. Plain Python over `proposal`; edit freely.
WANTED = ("fact", "procedure", "rule", "preference")
path = proposal["chat"]["path"] if proposal["chat"] else None
model = proposal["model"]
items = [i for i in proposal["items"] if i["kind"] in WANTED and len(i["text"]) >= 20]
if path is None:
    print("nothing left to review")
else:
    print("chat:", proposal["chat"]["title"], "| %d proposed, keeping %d | %d chat(s) still pending" % (len(proposal["items"]), len(items), proposal["pending"]))
    for i in items:
        print(" +", i["title"], "(%s)" % i["kind"])
    if proposal["error"]:
        print("model problem, nothing will be kept:", proposal["error"])
"""

REPORT = """print("kept %d draft note(s) from %s; %d chat(s) still pending" % (len(kept["written"]), kept["chat"], kept["review"]["pending"]))
for w in kept["written"]:
    print(" -", w["path"])
"""


def cell(ident, title, source, kind='code', **own):
    c = {'id': ident, 'cell_type': kind, 'metadata': {'mainaiger': dict(title=title, **own)}, 'source': source}
    if kind == 'code':
        c.update(execution_count=None, outputs=[])
    return c


def build():
    cells = [
        cell('readme', 'Review imported chats, as a Mainbook', README, kind='markdown'),
        cell('review', 'Review next chat', '', memory={'op': 'review', 'output': 'proposal'}),
        cell('choose', 'Choose items', CHOOSE),
        cell('keep', 'Keep', '', memory={'op': 'keep', 'output': 'kept'}),
        cell('report', 'Report', REPORT),
    ]
    nodes = [{'id': 'start', 'kind': 'start'}, {'id': 'readme', 'kind': 'markdown', 'cell_index': 0}]
    nodes += [{'id': c['id'], 'kind': 'code', 'cell_index': i} for i, c in enumerate(cells) if c['cell_type'] == 'code']
    nodes.append({'id': 'finish', 'kind': 'finish'})
    edges = [
        {'id': 'e-start-review', 'kind': 'control', 'source': 'start', 'target': 'review'},
        {'id': 'e-review-choose', 'kind': 'control', 'source': 'review', 'target': 'choose'},
        {'id': 'e-choose-keep', 'kind': 'control', 'source': 'choose', 'target': 'keep', 'when': 'path is not None and not proposal["error"]'},
        {'id': 'e-keep-report', 'kind': 'control', 'source': 'keep', 'target': 'report'},
        {'id': 'e-report-review', 'kind': 'control', 'source': 'report', 'target': 'review', 'loop': True, 'when': 'kept["review"]["pending"] > 0', 'max_fires': 3},
        {'id': 'e-report-finish', 'kind': 'control', 'source': 'report', 'target': 'finish'},
    ]
    layout = {'readme': {'x': 40, 'y': 40}, 'start': {'x': 40, 'y': 520}, 'finish': {'x': 2060, 'y': 520},
              'review': {'x': 460, 'y': 420}, 'choose': {'x': 860, 'y': 420}, 'keep': {'x': 1260, 'y': 420}, 'report': {'x': 1660, 'y': 420}}
    return {'format': 'mainaiger', 'schema_version': 2,
            'notebook': {'nbformat': 4, 'nbformat_minor': 5,
                         'metadata': {'kernelspec': {'name': 'python3', 'display_name': 'Python 3', 'language': 'python'},
                                      'mainaiger': {'example': True, 'name': 'review-chats', 'title': 'Review imported chats as a Mainbook (review, choose, keep, loop)'}},
                         'cells': cells},
            'graph': {'nodes': nodes, 'edges': edges},
            'layout': {'nodes': layout, 'viewport': {'x': 40, 'y': 30, 'zoom': 0.6}}}


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from local_lab import graph as g
    doc = g.validate(build())
    out = Path(__file__).resolve().parent / 'review-chats.ipymb'
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print('wrote', out, g.order(doc))
