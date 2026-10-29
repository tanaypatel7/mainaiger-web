"""Builds examples/self-improve-repo.ipymb: the repository-level self-improvement loop.

Run: python3 examples/build_self_improve_repo.py   (writes the .ipymb next to this file)

The Mainbook copies the repository (the session's read-only /workspace) into the session's
/tmp, asks a model for one find/replace change to a file you name, applies it to the copy,
runs the test module you name there, runs a second, held-out test module the proposer never
sees, asks a second model to judge from the diff and both test outputs, and loops back for another
proposal (at most twice) while the verdict is not "keep". Nothing is ever written to the checkout:
keeping a change is the patch the Report prints, applied through the Map page's reviewed PATCH step.
"""
import json
from pathlib import Path

MODEL = 'qwen2.5-coder:7b'

README = """# Self-improvement loop on a repository

The session's workspace must be a checkout of a repository (for MainAIger itself: this repository). **Copy repository** copies it into the session's own `/tmp` (the workspace stays read-only), **Baseline tests** runs the test module you name there, **Read the file** prints the file you name, **Propose change** asks a model for ONE find/replace change for the task you typed, **Apply to copy** restores the file from the workspace and applies the change to the copy (never the checkout), **Run tests** runs the same tests in the copy, **Held-out tests** runs a second test module whose output goes only to the judge — the proposer never sees it, so it cannot write to the test — **Judge** decides keep / revert / ask from the diff and both test outputs, and the dashed loop arrow sends the run back to **Propose** (at most twice) while the verdict is not "keep", with the previous attempt pasted into the next prompt. **Report** prints every attempt and, for a keep, the patch: paste it into the Map page's PATCH box, where every hunk is checked and shown before a reviewed, atomic write into your checkout.

Fill the form on **Task**: what to change, which file, which test module (dotted, e.g. `tests.test_decide`) and which held-out module. Needs a running session, a workspace, and an installed Ollama model named in the two model cells (Models page). Keep the test modules small: a cell has 60 seconds.
"""

COPY = """# Copy the repository into the session's own temporary folder; the workspace stays read-only.
import os, shutil, sys
SRC = "/workspace" if os.path.isdir("/workspace") else os.getcwd()
DST = "/tmp/mainaiger-repo"
SKIP = {".git", "node_modules", "__pycache__", "dist", ".venv", "venv", ".pytest_cache", "build"}
shutil.rmtree(DST, ignore_errors=True)
copied = 0
for root, dirs, files in os.walk(SRC):
    dirs[:] = [d for d in dirs if d not in SKIP]
    rel = os.path.relpath(root, SRC)
    os.makedirs(os.path.join(DST, rel) if rel != "." else DST, exist_ok=True)
    for name in files:
        try:
            shutil.copy2(os.path.join(root, name), os.path.join(DST, rel, name) if rel != "." else os.path.join(DST, name))
            copied += 1
        except OSError:
            pass
print("copied %d files from %s to %s" % (copied, SRC, DST))
if not os.path.isfile(os.path.join(DST, file)):
    raise SystemExit("the file %r is not in the repository" % file)
"""

BASELINE = """# The named tests, in the untouched copy: the bar every proposal must clear.
import subprocess, sys
def run_tests(module=None):
    r = subprocess.run([sys.executable, "-m", "unittest", module or test_module], cwd=DST, capture_output=True, text=True, timeout=50)
    tail = "\\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
    return r.returncode == 0, tail
baseline_ok, baseline_text = run_tests()
print("baseline:", "OK" if baseline_ok else "FAILED")
print(baseline_text)
"""

READ = """# What the model sees: the file, with line numbers, up to max_lines lines.
import os
with open(os.path.join(DST, file), encoding="utf-8", errors="replace") as handle:
    file_lines = handle.read().splitlines()
print("file:", file, "(%d lines%s)" % (len(file_lines), "" if len(file_lines) <= max_lines else ", first %d shown" % max_lines))
for number, text in enumerate(file_lines[:max_lines], 1):
    print("%4d  %s" % (number, text))
"""

PROPOSE = """You change code in a repository, one small step at a time. Standard library Python only unless the file already imports more.

{{output:Task}}

The previous attempt, if there was one (do not repeat it):
{{output:Apply to copy}}
{{output:Run tests}}

The file (line numbers are not part of the file):
{{output:Read the file}}

Propose ONE change as a find/replace. "find" must be copied verbatim from the file (at least one complete line, without the line numbers) and occur exactly once; "replace" is the new text for those lines; "why" is one sentence. Keep the file valid Python.
"""

APPLY = """# Restore the file from the workspace, then apply the proposal to the COPY only; show the diff.
import difflib, os, shutil
original_path = os.path.join(SRC, file); copy_path = os.path.join(DST, file)
shutil.copy2(original_path, copy_path)
with open(copy_path, encoding="utf-8", errors="replace") as handle:
    before = handle.read()
try:
    history
except NameError:
    history = []
attempt = {"why": proposal.get("why", ""), "applied": False, "diff": "", "error": None}
count = before.count(proposal["find"]) if proposal.get("find") else 0
if count != 1:
    attempt["error"] = "the 'find' text occurs %d times in %s (it must occur exactly once)" % (count, file)
else:
    after = before.replace(proposal["find"], proposal["replace"], 1)
    try:
        compile(after, file, "exec")
    except SyntaxError as error:
        attempt["error"] = "the result is not valid Python: line %s: %s" % (error.lineno, error.msg)
    else:
        with open(copy_path, "w", encoding="utf-8") as handle:
            handle.write(after)
        attempt["applied"] = True
        attempt["diff"] = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "a/" + file, "b/" + file))
history.append(attempt)
print("attempt %d" % len(history))
print("why:", attempt["why"])
if attempt["applied"]:
    print(attempt["diff"])
else:
    print("the proposal could not be applied:", attempt["error"])
"""

TESTS = """# The same tests, in the changed copy.
if history[-1]["applied"]:
    tests_ok, tests_text = run_tests()
else:
    tests_ok, tests_text = False, "(not run: the proposal could not be applied)"
history[-1]["tests_ok"] = tests_ok
print("tests after the change:", "OK" if tests_ok else "FAILED")
print(tests_text)
"""

HELDOUT = """# Tests the proposer never sees: their output goes to the judge only (no {{output:Held-out tests}} in the Propose prompt).
if history[-1]["applied"]:
    heldout_ok, heldout_text = run_tests(heldout_module)
else:
    heldout_ok, heldout_text = False, "(not run: the proposal could not be applied)"
history[-1]["heldout_ok"] = heldout_ok
print("held-out tests (%s): %s" % (heldout_module, "OK" if heldout_ok else "FAILED"))
print(heldout_text)
"""

JUDGE = """A change was applied to a copy of a repository and its tests were run.
{{output:Task}}

The change:
{{output:Apply to copy}}

The tests afterwards:
{{output:Run tests}}

Held-out tests the proposer never saw:
{{output:Held-out tests}}

Decide: "keep" only if the change was applied, both test runs pass, and it does what the task asked; "revert" if the tests fail, the change could not be applied, or it does something else; "ask" if you cannot tell. Give up to three short reasons.
"""

REPORT = """print("=" * 56)
print("Repository self-improvement loop: %d attempt%s" % (len(history), "" if len(history) == 1 else "s"))
print("=" * 56)
print("task:", task)
print("file:", file, "| tests:", test_module, "| baseline:", "OK" if baseline_ok else "FAILED")
for i, h in enumerate(history, 1):
    state = ("tests OK" if h.get("tests_ok") else "tests FAILED") + (", held-out OK" if h.get("heldout_ok") else ", held-out FAILED") if h["applied"] else "not applied: " + str(h["error"])
    print("  %d. %s - %s" % (i, state, h["why"]))
print("last verdict:", verdict["action"], "(confidence %.2f)" % verdict["confidence"])
for r in verdict.get("reasons", []):
    print(" -", r)
if verdict["action"] == "keep" and history[-1]["applied"]:
    print()
    print("Nothing was written to your checkout. To keep this change, paste the patch below into the")
    print("Map page's PATCH box (every hunk is checked and shown before a reviewed write), or save it as")
    print("change.patch and run: git apply change.patch   (then read it, run the full suite, commit)")
    print("-" * 56)
    print(history[-1]["diff"])
elif verdict["action"] == "ask":
    print("Could not tell after the loop's budget: run again, narrow the task, or name a more specific test module.")
else:
    print("Every attempt was reverted: the checkout is untouched. Run again for new proposals, or narrow the task.")
"""


def cell(ident, title, source, kind='code', **own):
    meta = {'mainaiger': dict(title=title, **own)}
    c = {'id': ident, 'cell_type': kind, 'metadata': meta, 'source': source}
    if kind == 'code':
        c.update(execution_count=None, outputs=[])
    return c


def build():
    cells = [
        cell('readme', 'How this loop works', README, kind='markdown'),
        cell('task', 'Task', 'print("task:", task)\nprint("file:", file, "| tests:", test_module, "| held-out:", heldout_module)\n', form={'fields': [
            {'name': 'task', 'type': 'text', 'label': 'What to change (one small step)', 'default': 'Add a one-line comment above extract_json explaining what it does'},
            {'name': 'file', 'type': 'text', 'label': 'File to change (relative to the repository)', 'default': 'local_lab/decide.py'},
            {'name': 'test_module', 'type': 'text', 'label': 'Test module to run (dotted)', 'default': 'tests.test_decide'},
            {'name': 'heldout_module', 'type': 'text', 'label': 'Held-out test module (the proposer never sees its output)', 'default': 'tests.test_graph'},
            {'name': 'max_lines', 'type': 'integer', 'label': 'Lines of the file shown to the model', 'default': 400}],
            'values': {}}),
        cell('copy', 'Copy repository', COPY),
        cell('baseline', 'Baseline tests', BASELINE),
        cell('read', 'Read the file', READ),
        cell('propose', 'Propose change', PROPOSE, model={'model': MODEL, 'output': 'proposal', 'attempts': 2, 'schema': {
            'type': 'object', 'properties': {'find': {'type': 'string'}, 'replace': {'type': 'string'}, 'why': {'type': 'string', 'maxLength': 300}}, 'required': ['find', 'replace', 'why']}}),
        cell('apply', 'Apply to copy', APPLY),
        cell('tests', 'Run tests', TESTS),
        cell('heldout', 'Held-out tests', HELDOUT),
        cell('judge', 'Judge', JUDGE, model={'model': MODEL, 'output': 'verdict', 'schema': {
            'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['keep', 'revert', 'ask']}, 'confidence': {'type': 'number'},
                                             'reasons': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3}}, 'required': ['action', 'confidence']}}),
        cell('report', 'Report', REPORT),
    ]
    order = ['task', 'copy', 'baseline', 'read', 'propose', 'apply', 'tests', 'heldout', 'judge', 'report']
    nodes = [{'id': 'start', 'kind': 'start'}, {'id': 'readme', 'kind': 'markdown', 'cell_index': 0}]
    nodes += [{'id': c['id'], 'kind': 'code', 'cell_index': i} for i, c in enumerate(cells) if c['cell_type'] == 'code']
    nodes.append({'id': 'finish', 'kind': 'finish'})
    chain = ['start'] + order + ['finish']
    edges = [{'id': 'e-%s-%s' % (a, b), 'kind': 'control', 'source': a, 'target': b} for a, b in zip(chain, chain[1:])]
    edges.append({'id': 'e-judge-propose', 'kind': 'control', 'source': 'judge', 'target': 'propose', 'loop': True, 'max_fires': 2, 'when': 'verdict["action"] != "keep"'})
    layout = {'readme': {'x': 40, 'y': 40}, 'start': {'x': 40, 'y': 620}, 'finish': {'x': 2460, 'y': 620}}
    columns = [['task', 'copy'], ['baseline', 'read'], ['propose', 'apply'], ['tests', 'heldout'], ['judge', 'report']]
    for col, ids in enumerate(columns):
        for row, ident in enumerate(ids):
            layout[ident] = {'x': 460 + col * 400, 'y': 40 + row * 420}
    doc = {'format': 'mainaiger', 'schema_version': 2,
           'notebook': {'nbformat': 4, 'nbformat_minor': 5,
                        'metadata': {'kernelspec': {'name': 'python3', 'display_name': 'Python 3', 'language': 'python'},
                                     'mainaiger': {'example': True, 'name': 'self-improve-repo', 'title': 'Self-improvement loop on a repository (copy, change, test, judge, loop)'}},
                        'cells': cells},
           'graph': {'nodes': nodes, 'edges': edges},
           'layout': {'nodes': layout, 'viewport': {'x': 40, 'y': 30, 'zoom': 0.55}}}
    return doc


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from local_lab import graph as g
    doc = g.validate(build())
    out = Path(__file__).resolve().parent / 'self-improve-repo.ipymb'
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print('wrote', out, 'order', g.order(doc))
