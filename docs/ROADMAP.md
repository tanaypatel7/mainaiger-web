# MainAIger roadmap · from the Lab of today to MainAIger built with MainAIger

Written 27 October 2026; A1, A2 and the first half of B2 were built on 28 October
(see `SESSION_2026-10-28.md`). Supersedes the milestone list in `DESIGN_PLAN.md`
§10 where they differ; `LOCAL_LAB_SETUP.md` stays the feature → file map.

## The goal, in one paragraph

MainAIger is a local-first environment where **code cells are nodes and triggers
are arrows**, so at every point you can see what runs, when, and why. The Mac app
is the first runtime. It should be able to make a copy of itself for any
compatible device, connect to those devices over your network, and run per-device
code and automations from graphs. Any trigger should be connectable: "when a chat
prompt is sent" → a graph's Start. The same picture should work for *existing*
code: open a repository (MainAIger's own first) and see its modules, functions,
routes and handlers as nodes with edges showing what calls what, navigate to the
source, and modify it from there, with optional local AI labelling the
human-level tasks. The last step is MainAIger developing MainAIger through that
view. Every step along the way must answer: what does it do, how, why, and which
code.

## The rule for every step

Each phase below lands as one or more reviewed changes that each have:
a plain-language explanation of what/how/why in the commit and in
`LOCAL_LAB_SETUP.md`; the files named; tests that run in CI; honest labels
(tested / simulated / unverified / future); and no change to the safety
boundaries (owner key, loopback only, container limits, every system-touching
operation a fixed, visible, approved step) without an explicit decision.

## Where we are (done)

| Capability | State | Code |
|---|---|---|
| Mac app, owner key, Docker install verified | tested on your Mac | `launcher`, `local_lab/setup.py` |
| One persistent kernel in a limited container | tested on your Mac | `runner.py`, `kernel_session.py`, `kernel/kernel.py` |
| Mainbook document, graph view, serial run, preflight | tested; Docker run unverified on Mac | `graph.py`, `graph_run.py`, `ui/graph.js` |
| Mainbooks folder, examples, import/export | tested | `mainbooks.py` |
| Smooth canvas (no forced layouts) | tested headless; Mac feel yours | `ui/graph.js` |
| Cell library with versioned links, starter ML flow | tested | `library.py`, `examples/library/` |
| Environment per Mainbook (derived image) | tested with fake build; real build unverified | `environment.py` |
| Ollama install, model download, one-shot chat API | CI-verified; Mac unverified | `models.py` |
| Website, accounts, directory, releases | separate; not part of the runtime | `site/`, Supabase, Actions |

## Phase A — Mainbooks become general trigger graphs (next)

**Why first:** everything you described is "a trigger fires a graph". Today the
only trigger is "the previous cell finished" inside one approved run. Phase A
makes triggers a first-class, general thing without changing how cells execute.

### A1. Event triggers: Start from the outside — **built (28 Oct 2026)**

*As built:* sources live on the Start node (`events`: timer, file, chat,
manual) instead of extra node kinds; the owner arms the Mainbook in a review
dialog; a firing is one "Run all" with the event bound in the active session;
arming does not survive a restart (the recommendation below, taken); the
first event built was chat, with timer, file and manual in the same step. See
`docs/LOCAL_LAB_SETUP.md` "Start events". The original design follows.

*What:* a Mainbook can have Start nodes of kind `event` with a source: **manual**
(today), **chat prompt sent** (the Models page), **timer** (every N minutes while
the Lab runs), **file changed** (in the read-only workspace folder). When the event
fires, the graph runs exactly as an approved run does, with the event's payload
bound to a variable (`prompt`, `path`, `now`).

*How:* `graph.py` gains node kind `event` with `source` and `binding`; a new
`local_lab/events.py` keeps the registry of armed Mainbooks and a queue; the chat
route and a file watcher enqueue events; `graph_run.py` is reused unchanged. The
owner **arms** a Mainbook once per session in a review dialog listing exactly what
may fire it and the run budget (A2); events never arm anything on their own. A run
log (`runs/` next to Mainbooks, one JSON line per run) records every fired run so
"why did this run?" has an answer.

*Decisions needed from you:* which event first (recommended: chat prompt, because
it connects two things that already exist); whether armed Mainbooks survive a Lab
restart (recommended: no, re-arm, keep the rule "nothing runs that you did not
approve this session").

### A2. Conditional and loop triggers with explicit budgets — **built (28 Oct 2026)**

*What:* an edge can carry a condition and the graph may contain cycles, bounded.

*How (as built):* condition = a Python expression stored on the edge (`when`),
evaluated **in the cell's session** right after the source cell succeeds, by
the kernel's `evaluate` op (protocol 2; `POST /api/sessions/evaluate` on a
device). A back edge is marked `loop: true` with `max_fires` (default 3, at
most 100); the canvas draws it around the right side with a `↻ n/max` label.
`MAX_STEPS` (500) per run is the hard cap. The review dialog lists loops and
conditions; the journal records every decision; cells behind an untaken
branch are `idle`. See `docs/LOCAL_LAB_SETUP.md` "Loop arrows and
conditions". Nested graphs ("a cell whose code is another Mainbook") were
built the same day ("Nested Mainbooks" in the same guide). Still future: a
self-loop on one cell.

*Why expression-on-edge rather than a flag variable:* the edge then shows its own
reason on the canvas ("retrain while `metrics['accuracy'] < 0.9`"), which is the
"see what triggers what and why" requirement.

### A3. Cells in other languages

*What:* each cell declares a language; the Lab runs it in the matching runtime.

*How:* start with **shell** (same container, `sh -c`, output captured) and
**JavaScript** (Node installed into the derived image through the environment
mechanism, so a Mainbook declares `runtime: node` the way it declares packages).
Variables do not cross languages implicitly; a cell passes values on through
files in `/tmp` or JSON on stdout, shown as explicit data edges. Python stays the
only language with a persistent namespace. This keeps the model honest: one
language, one process model.

### A4. Model-backed cells and a streaming chat

*What:* a cell kind `model` with a prompt template; it calls `/api/models/chat`
with variables filled in and binds the reply. The Models page gets a streaming
chat whose "prompt sent" event is the A1 source.

> **Update, later on 27 Oct:** the `MainAIger-Lab-Code.zip` archive (Nexus-hosted
> MainAIger v0.6 + Lab 0.1) already contains a broker/connector/host device
> fabric (Phase C) and a source workbench that maps a code tree into a symbol
> graph with edit, diff and tests (most of B1–B2). `docs/INTEGRATION.md` is the
> plan to bring it into this repository as `nexus/` and fold it in step by
> step (I1 devices, I2 document import, I3 run on a device, I4 code map).
> Phases B and C below are now mostly *porting* work, not new design; Phase A
> is unchanged.

## Phase B — Code map: a repository as a graph

**Why second:** this is the capability you described most: seeing the whole of
MainAIger as navigable graph code, then any repository. It is a *different kind of
Mainbook*: nodes are places in source files, not cells to execute. Building it on
the same canvas and document is the point, so it should come right after the
trigger model is general.

### B1. Static code map of a Python folder (MainAIger's own `local_lab/` first)

*What:* open a folder → a map Mainbook: one node per module, one per function and
class, edges for **imports**, **calls** (static, advisory, like data edges), and
for the Lab specifically **HTTP route → handler** (the `/api/...` checks in
`server.py`) and **UI action → route** (the `api('/api/...')` calls in
`ui/*.js`). Clicking a node shows its source; the inspector shows "called by /
calls / reached from route". Layout by package, collapsible.

*How:* `local_lab/codemap.py` uses `ast` for Python and a small pattern pass for
`ui/*.js` (labelled *approximate*); it emits a `.ipymb` whose cells are
`markdown`-like **source nodes** with `metadata.mainaiger.source = {path, line,
end_line, hash}`. The Lab already mounts your workspace read-only, so reading is
already allowed; editing (B2) needs a write-approved Projects folder like
Mainbooks. Deliverable of B1: the map of `local_lab` itself, reviewed by you for
"is this true?"

### B2. Navigate and modify — **built (28 Oct 2026)**

*Built:* "Make a cell from this…" on the Map turns a function into a form
cell in the open Mainbook (fields = parameters, code = import and call), so a
repository's functions get GUI elements without writing code; **Edit…** on a
map node shows a diff, asks once per Lab session to allow writes under that
folder, writes atomically only if the file is unchanged and still parses,
and **Run tests in session** runs the folder's unittest discovery inside the
container. The running app's own code stays read-only (edit a checkout).

*What:* edit a function in its node; the Lab writes it back to the file (approved
folder, atomic, diff shown before writing), and for this repository runs the test
suite in the session container (`python3 -m unittest`) as the graph's Finish.
That is the first "MainAIger working on MainAIger" loop: map → edit → test.

### B3. Optional AI labels for human-level tasks

*What:* a reviewed step sends each function's source to the local model
(`/api/models/chat`) and stores a one-line task label and a "why it exists"
guess as metadata marked **AI-generated, unverified**. Labels power search and a
"tasks" view (nodes grouped by what they do for the user). Nothing leaves the
machine; labels are never shown as facts.

### B4. Any repository

*What:* the same for a generic Python repo, then JavaScript with a real parser
(Node in the derived image running a small analyzer script). Framework edges
(Flask routes, FastAPI, Express handlers) are plug-ins to `codemap.py`, each
documented as what it detects and what it misses.

## Phase C — Devices and network

**Why third:** a copy that runs on another device is only useful once it has
general triggers (A) and the owner can see what runs there (B gives the view).
The existing Nexus host (Ubuntu) is the natural first target.

### C1. A device runtime and a copy
*What:* the Mac app can produce a **device runtime**: the `local_lab` service
without the installer UI, packaged for Linux (and later other Macs), with its own
owner key, running Mainbooks in Docker there. *How:* `release-linux.yml` builds
it; pairing = you copy the key once; the runtime listens on loopback only and is
reached through an SSH tunnel or Tailscale (already in your network), never an
open port.

### C2. Connect and operate
*What:* the Mac app's Devices page lists paired devices, their Mainbooks, their
sessions and run logs; you open a remote Mainbook in the same canvas and run it
*there*. *How:* `local_lab/devices.py` + a small connector that proxies the same
`/api/*` routes over the tunnel with the device's key; every remote action is the
same reviewed step shown with the device name.

### C3. Cross-device triggers
*What:* an event on device A arms a graph on device B (A1 sources gain a device
field). The run log shows the chain across devices.

## Phase D — MainAIger built with MainAIger

With A–C in place: MainAIger's own repository is a map Mainbook (B1) with
executable cells for build and test (B2), its runtime pieces are graphs with
event triggers (A1) running on your devices (C), and the AI labels (B3) explain
each part in human terms. Developing MainAIger then means editing the graph that
is MainAIger, with every change reviewed and tested from inside it. Signing with a
Developer ID when you have an Apple account fits anywhere; it changes
distribution, not design.

## Done since this roadmap was written (same day)

Resources (several sessions, per-cell "run on"), Files, Log, thin-slice
decisions, the Devices page (read-only tailnet) and model cells: see
`SESSION_2026-10-27.md` §4–7. The AI parts of the vision are designed in
`AI_HARNESS.md`.

## What I recommend doing next, concretely

1. **The first self-improvement loop** as a Mainbook
   (`examples/self-improve.ipymb`, `AI_HARNESS.md` §3): it needs only what
   exists now plus two test folders and one reviewed "apply to checkout" step.
2. **I1 from `docs/INTEGRATION.md`** (the Mac app sees Nexus), after you have
   run the section-4 trial once by hand so the device link is proven.
3. **I4 / B1 on `local_lab/`**, because the source workbench already exists and
   seeing your own harness as a graph is the quickest way to the overview you
   asked for; B3 (AI labels) follows the thin-slice pattern in `AI_HARNESS.md` §4.
4. Then **A1** (chat-prompt event) and **A2** (edge conditions with budgets),
   which also give the loop its repeat and stop rules.

Say which of these to start, or what to change, and I will begin there.
