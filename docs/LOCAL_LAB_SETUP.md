# Local Python Lab prototype

This is a first local, single-cell Python editor. It can open and download a
copy of a standard `.ipynb` notebook containing exactly one code cell. Downloaded
notebooks contain the current source with prior outputs cleared. It is separate
from the existing private Nexus installation. It is **not yet a full notebook editor**,
a macOS app installer, a website/device connection, or a remote execution server.

On your own Mac, install and start Docker Desktop and obtain this repository:

```bash
git clone https://github.com/tanaypatel7/mainaiger-web.git
cd mainaiger-web
docker pull python:3.13.18-slim
python3 -m local_lab
```

The first three commands download the code and official Python container image.
The last starts a panel only on your Mac at `http://127.0.0.1:8794`. Its printed
output identifies the path of a private `owner.key` file, not the key itself.
Open that file locally (for the default location: `open -a TextEdit
~/.local/share/mainaiger-lab/owner.key`) and use its contents to unlock the local
panel. Do not enter that key on mainaiger.org or send it to another person.

In the panel, optionally type an absolute path to a folder you want the Python
container to read. An empty field means no host folder is mounted. Choose a CPU
and memory limit, save the settings, then write a small Python cell. Review the
displayed container command and code, and explicitly approve each run. Output is
displayed in the panel. The selected folder is mounted read-only, so the code
cannot save results there in this version. A new container is created for each
run, so variables do not persist across runs — unless you start a persistent
session (below).

Runs use a nonroot user, no network, a read-only container root, a temporary
directory, dropped Linux capabilities, CPU/memory/process limits, a 20-second
time limit and capped output. Cleanup after a timeout is best-effort: check
`docker ps --filter name=mainaiger-lab-` for a remaining job if Docker stalls.
The browser does not receive the Docker socket.
The local server invokes a fixed Docker command; it never invokes Python code on
the host. GPU use is not implemented. The sandbox is defense in depth, not a
guarantee against vulnerabilities in Docker or the host OS.

## Persistent session (kernel)

Instead of a fresh container per run, **Review and start session** starts one
container that stays up until you stop it. Inside it runs
`local_lab/kernel/kernel.py`, a small standard-library Python program that
reads cells as JSON messages on its standard input and keeps a shared
namespace, so a variable made by one cell is visible to the next. Before each
session the kernel is copied into the Lab's private state directory
(`<state>/kernel/`, world-readable because the container user must read it)
and verified by hash — Docker Desktop cannot mount paths inside
`/Applications` — and that copy is bind-mounted read-only at `/opt/mainaiger`;
every other container
limit (no network, read-only root, dropped capabilities, CPU/memory/process
caps, nonroot user) is identical to the one-shot run. The container cannot
alter the kernel program, and the host never runs cell code itself.

Each cell still needs approval. A result shows status, cell number, elapsed
time and any error with a traceback. The last expression of a cell is echoed,
as in Jupyter. Output is captured from Python's `print`, `sys.stderr` and raw
writes to file descriptors 1 and 2, and is capped at 64 KB per cell. `input()`
raises `EOFError` instead of waiting. **Interrupt cell** sends `SIGINT` to the
kernel (`docker kill --signal=INT`), which raises `KeyboardInterrupt` in the
running cell; the session continues. A cell that runs longer than 60 seconds
is interrupted automatically. If a cell ignores the interrupt for 5 more
seconds, the whole session container is removed and the panel says so.
**Stop session** removes the container; its variables are gone. Quitting the
launcher or pressing Ctrl-C also removes a running session container.

The variables table lists names in the session with a type and a limited
summary. Summaries never call your objects' own `repr` or methods; arrays
from numpy/pandas/torch show shape and dtype only. Settings (folder, limits)
are locked while a session runs because the container was created with them.

A kernel is an execution process, not a security boundary. Cell code can read
and write anything the kernel process can, including the protocol pipe; the
host validates every message and the container is the actual boundary.
Nothing from a session is written to disk by Lab, and a saved notebook never
contains a running kernel.

**Status of this feature:** the kernel protocol, output capture, errors,
interrupt, timeout escalation and HTTP endpoints are tested by running the
real kernel as a plain local process (`tests/test_kernel.py`,
`tests/test_kernel_server.py`) and the panel was exercised in a headless
browser against that local process. Running the kernel inside a Docker
container on a Mac — including the read-only bind mount of the kernel
directory from the app bundle and `docker kill --signal=INT` reaching the
kernel — is **unverified** until it is tried on a real Mac.

## Graph canvas (trigger flow)

**Open graph canvas →** in the Lab header opens `/graph`: a canvas where code
cells are cards and arrows are *triggers*. The document behind it is an
`.ipymb` file (schema version 2): an ordinary nbformat-4 notebook holding the
cells, plus `graph` (nodes and edges) and `layout` (positions). Jupyter can
still read the embedded notebook; MainAIger reads the whole file.

What the pieces do, and which file implements them:

- **Nodes** — one green *Start*, one *Finish*, any number of code cells and
  notes. A cell card shows its execution count, status badge, editable code
  and output. `local_lab/graph.py` validates the document (one start, one
  finish, cells referenced exactly once, no cycles among forward arrows).
- **Triggers (control edges, solid)** — drag from a cell's bottom port to
  another cell's top port. "A → B" means *when A finishes successfully, the
  arrow fires and B runs*. A cell with several incoming arrows runs once all
  of them are decided, if at least one fired. An arrow can carry a
  **condition**, and an arrow drawn backwards is a **loop arrow** with a
  budget: see "Loop arrows and conditions" below.
- **Data edges (dashed, labelled)** — not drawn by hand. `graph.analyze()`
  reads each cell with Python's `ast`: names a cell reads without defining are
  its *inputs*, names it defines are *outputs*; the nearest upstream producer
  of each input is drawn as a data edge. This is advisory (static analysis
  cannot see `globals()[...]`, `exec` or `import *`); the inspector lets you
  override a cell's declared inputs/outputs.
- **Run all / Run from selected / Run selected only** — `graph.plan()` picks the
  cells, in topological order, and `graph.preflight()` lists inputs that would
  be missing, which cell produces each, and whether the value already exists in
  the session. The review dialog can include those prerequisite cells. Then
  `local_lab/graph_run.py` executes the plan **one cell at a time in the same
  kernel session** used by the single-cell editor; after a cell finishes with
  status ok its outgoing triggers *fire* (the canvas animates them). A failure,
  interrupt, timeout or dead kernel marks the remaining cells *skipped* and ends
  the run. Cancel interrupts the current cell and skips the rest.
- **Notebook | Graph** — the same cells in two views. Notebook view stacks
  them in trigger order (no dragging, no edges); graph view shows what
  triggers what. Switching never runs anything. Shift+Enter runs the cell you
  are typing in; Ctrl/Cmd+Enter runs from it onward; the ▶ on Start runs all.
- Each card shows `In [n]`, a status badge with timing, a **stale** badge when
  the code changed after its last output (the service stores a hash of the
  source with each result), and a red **needs: name** line when an input has no
  producer upstream and is not in the session yet. Code and output collapse
  independently; **+** adds a cell triggered by this one.
- **Export Python** writes the cells reachable from Start, in trigger order,
  as one `.py` with the notes as comments. **Clear outputs** resets counts.
- **Open example…** lists the Mainbooks in `examples/` (`signal-demo`,
  `csv-pipeline`, `branching-stats`, `self-improve`); `tests/test_examples.py`
  runs each one with the real kernel, so they cannot rot. **Open .ipymb / .ipynb** sends a
  plain notebook to `POST /api/graph/import` (`graph.from_notebook()`): cells
  are chained in notebook order, notes kept, ids/outputs/counts preserved,
  titles taken from the nearest heading or first comment, and an import
  report lists hidden-state inputs (names no earlier cell defines), IPython
  magics and constructs the analysis cannot see. **Export .ipynb**
  (`graph.to_notebook()`) writes the cells in trigger order and says whether
  a branching graph was flattened.
- **Save as Mainbook… / Open Mainbook…** (`local_lab/mainbooks.py`) — the
  first time you save, the Lab shows the folder it will write to (default
  `~/Documents/MainAIger Lab/Mainbooks`, editable) and creates it only after
  you approve. The choice is remembered in the private state directory. Names
  are letters, digits, spaces, dashes and underscores; files are `name.ipymb`;
  writes are atomic; symlinks are refused; replacing an existing name asks
  first. ⌘S/Ctrl+S opens the save dialog. The folder is a document store only
  and is never mounted into a container.
- Outputs and execution counts are written back into the notebook cells, so
  **Download .ipymb** carries results; **Open** accepts `.ipymb` or a plain
  `.ipynb` (converted into a sequential Start → cells → Finish chain).

The canvas is a view, not a sandbox: all execution happens inside the session
container with the same limits as before. A branching picture is still one
Python process running serially; parallel branches, nested graphs and other
languages are future work and are refused rather than simulated.

**Status:** the document model, analysis, planning, preflight and serial run
(fire, fail-and-skip, cancel) are tested with the real kernel as a local
process (`tests/test_graph.py`) and the canvas was exercised end to end in a
headless browser. Running the graph inside Docker on a Mac is the same
unverified step as the session itself.

### Resources: several sessions, per-cell "run on" (`local_lab/sessions.py`, `/api/sessions*`)

The Lab used to have exactly one kernel. Now the RESOURCES section of the
inspector is a table of sessions on this device (at most 4 running), each with
its own label, CPU and memory limits and image, and one of them is **active**:

- **New session…** → `POST /api/sessions/start {confirm, revision, label,
  cpus, memory}`: one more container with those limits, the same read-only
  workspace, and the Mainbook's environment image when one is declared and
  built. The header's **Start session** button (`/api/kernel/start`) still
  starts one session when none is running; more come from this table.
- **Use / ⏸ / ■ / ✕** → `/api/sessions/select | interrupt | stop | forget`.
  A stopped session stays listed with its reason until removed.
- **Run on** (SELECTED NODE) → `metadata.mainaiger.session = "<label>"`. The
  card shows `on <label>`; cells with no label run in the active session.
  `graph_run.py` resolves a kernel per cell (`sessions.kernel_for`), so one
  run can use several sessions, still one cell at a time.
- **Every session is its own process.** Variables do not cross sessions. The
  run review lists which cells run where, says when a data edge crosses
  sessions ("x is produced in the active session but Use x runs in worker: it
  will not be available there"), and refuses to start while a named session
  is not running (`preflight.sessions / cross_session / missing_sessions`).
  A cell that reads a name from another session fails with the real
  `NameError`; nothing is copied silently.
- Settings (folder, default limits) are frozen while any session runs.

**Status:** registry, cap, label uniqueness, dead sessions, per-cell routing
and the HTTP flow are tested with real kernel processes
(`tests/test_sessions.py`); the panel and a two-session run are tested in
headless Chromium (`tests/test_canvas_ui.py`, `PanelsOnRealKernelTests`),
which also runs the six-cell starter ML flow end to end on a real kernel.
Several Docker containers at once on a Mac is unverified.

### Terminal (`sessions.command`, `POST /api/sessions/command`)

A command line that runs **inside the chosen session's container**: the Lab
wraps the line in a small Python program (`sessions.shell_program`) that calls
`subprocess.run(..., shell=True, timeout=50)` through the session's kernel,
prints stdout, stderr and `[exit N]`, and deletes its helper name afterwards
so the variables table stays clean. The command therefore has exactly the
session's powers and no more: no network, read-only workspace, the CPU/memory
limits, the 60-second cell timeout. It never runs on this Mac. Every line is a
journal entry (`shell.command`). Tested on a real kernel and in the browser
(`tests/test_sessions.py`, `PanelsOnRealKernelTests`).

### Files (`local_lab/files.py`, `GET /api/files`, `GET /api/files/read`)

Read-only browsing of the **workspace** folder (the one mounted into
sessions), the **Mainbooks** folder and the **Library** folder, in the FILES
section: pick the root, click folders, click a file for a bounded text preview
(200 kB; binary files say so). `.ipymb` and `.ipynb` previews offer **Open as
Mainbook** (the latter through the normal import report). Paths stay inside
the root; symlinks are neither listed nor followed; nothing is written.
Tested: listing, previews, every escape attempt (`tests/test_sessions.py`).

### Log (`local_lab/journal.py`, `GET /api/journal?after=`)

One append-only line per thing the Lab did on this device, in
`<state dir>/journal.jsonl`: sessions started/stopped/interrupted, runs
approved and finished, each cell's result with its session, environment
builds, library and Mainbook saves. `seq` only grows, so the LOG section asks
for "everything after N" every 5 s and after each action; the file is kept
under 2 MB by dropping the oldest half. This is the "why did that run?" record
that cross-device runs (roadmap C3) will join by device name. Tested:
rotation, ordering, the sequence of a real run (`tests/test_sessions.py`).

### Cell library (`local_lab/library.py`, `/api/library/*`)

One global folder of reusable cells that any Mainbook can use. Feature → file:

- **Folder** — `<parent of the Mainbooks folder>/Library`, so
  `~/Documents/MainAIger Lab/Library` by default. It is a second write location,
  so the owner approves it once (`POST /api/library/folder {folder, confirm}`);
  the choice is remembered in the private state as `library.json`. Same rules as
  Mainbooks: absolute plain directory, not `/`, not home, not the private state,
  not the Mainbooks folder itself; never mounted into a container.
- **One file per cell** — `<name>.cell.json` (`library.validate_record`):
  `name`, `title`, `source`, declared `inputs`/`outputs`, `tags`, `description`,
  `cell_type`, and an integer `version` that goes up by one on every save of the
  same name (the stored version wins; callers cannot set it). Names are letters,
  digits, dashes and underscores; symlinks are refused; writes are atomic.
- **Browse / search** — `GET /api/library?q=words` (`library.listing`): every
  word must appear in the name, title, tags, description, inputs or outputs.
  The Library section of the inspector shows the list with a search box.
- **Insert** — `POST /api/library/insert {name, position, after}`
  (`library.insert`): adds a cell from the library at a canvas position; with
  `after` it is chained behind that node the way the + button works (between the
  node and Finish when the node pointed at Finish). In the UI: **Insert** on a
  list row (chains after the selected Start or code cell) or drag a row onto the
  canvas (placed where dropped, not chained).
- **Link** — an inserted or saved cell carries
  `metadata.mainaiger.library = {name, version, source_hash}`. Every graph view
  includes `library: {node_id: {name, version, latest, state}}` where state is
  `current`, `behind` (the library has a newer version), `modified` (edited here
  since it was inserted) or `missing`. The SELECTED NODE panel shows this.
- **Save / push** — `POST /api/library/save {node, name, tags, description,
  overwrite}` (`library.record_from_cell` + `library.save`): saves one code cell
  of the current graph with its title and declared-or-analysed names. A new name
  becomes v1; an existing name needs `overwrite` and becomes the next version.
  The cell is then linked to that version. **Push new version** in the UI is this
  call with the linked name.
- **Pull** — `POST /api/library/pull {node}` (`library.pull`): replaces a linked
  cell's code, title and declarations with the latest library version and clears
  its outputs. **Update from Library** in the UI, offered when the state is
  `behind`.
- **Starter cells** — `examples/library/*.cell.json`, copied into the folder by
  `POST /api/library/seed {confirm}` (**Add starter cells…**, existing names are
  skipped): load data → extract features → split train/test → train classifier →
  evaluate → report. They use only the Python standard library (a logistic
  regression by gradient descent on a synthetic two-blob dataset), so they run in
  the session container as it is today; the data edges between them are derived
  from the code. Replace the body of *load data* with a real loader and keep
  `rows = [(features, label), ...]`.

**Status:** folder rules, records, versions, search, insert/pull/link state and
the HTTP routes are tested (`tests/test_library.py`, 13 tests), and the panel is
exercised end to end in headless Chromium against the real local service
(`tests/test_canvas_ui.py`). The starter flow runs end to end in plain Python
(tested) and inside the Docker session only as far as the kernel tests cover
(unverified on a Mac).

### Environment: packages per Mainbook (`local_lab/environment.py`, `/api/environment*`)

The base session image is `python:3.13.18-slim` with the standard library only.
A Mainbook that needs more declares it, and the Lab builds one derived image:

- **Declaration** — `notebook.metadata.mainaiger.environment = {packages: [...]}`,
  validated by `graph.validate` → `environment.parse_packages`: up to 50 entries,
  each an exact pin `name==version` (extras allowed: `pandas[excel]==2.3.2`),
  de-duplicated and sorted. Unpinned or ranged requirements are refused so the
  same list always builds the same image. In the UI: the ENVIRONMENT section of
  the inspector (one package per line, **Apply list** saves it into the Mainbook).
- **Image tag** — `mainaiger-lab-env:<first 12 hex of sha256 of the sorted list>`
  (`environment.image_tag`): the tag names the package list and nothing else, so
  two Mainbooks with the same list share one image and a changed list is a new
  tag that needs its own build.
- **Build** — `GET /api/environment` returns the packages, tag, `ready` (does
  `docker image inspect` find it; answer cached 5 s), the Dockerfile and the
  exact command; `POST /api/environment/build {confirm, image}` runs it in the
  background (`environment.Build`), refusing a tag that no longer matches the
  document (the list changed after review) or a build already running. The
  Dockerfile is `FROM <base>` + one `pip install --no-cache-dir` line and goes
  in over stdin (`docker build --tag <tag> -`): there is no build context, so
  nothing from the Mac is sent. **This is the only step that uses the network**
  (pip downloads through Docker's build network); the log is bounded to 64 kB,
  the build is stopped after 15 minutes. In the UI: **Build environment…** shows
  the Dockerfile and command for approval, then the log and outcome.
- **Session** — `/api/kernel/start` refuses with a plain reason when the Mainbook
  declares packages whose image is not built; otherwise the derived tag goes
  into the session settings and `runner.session_command` uses it in place of
  the base image with every other flag unchanged (`--network=none`, read-only,
  limits). `/api/state` reports `environment` and the session command preview
  shows the image; the Start-session dialog names it.

**Honest limits:** packages are installed as root at build time and imported by
the unprivileged session user, which works for pure-Python and wheel-based
packages; packages that need system libraries not in the slim base image will
fail to build (the log says so) — apt lines are deliberately not supported.
Built images accumulate in Docker; removing old `mainaiger-lab-env:*` images is
a manual `docker image rm` for now. Image digests of the base are still a version
tag only (see Setup).

**Status:** parsing, tag/Dockerfile/command, the background build job (success,
failure, bounded log, timeout) and the routes including the session gate are
tested with a fake `docker build` (`tests/test_environment.py`, 12 tests); the
panel is exercised end to end in headless Chromium (`tests/test_canvas_ui.py`).
A real `docker build` with real packages on a Mac is unverified.

### Loop arrows and conditions (`graph.py` edges, `graph_run.py`, kernel `evaluate`)

The arrows are the program. Two things make them more than "then":

- **A condition on an arrow.** Select an arrow (click it or its label) and type
  one Python expression in the SELECTED ARROW panel, for example
  `verdict['action'] == 'revert'` or `metrics['accuracy'] < 0.9`. After the
  source cell finishes, its *session* answers the expression (the kernel's
  `evaluate` op: no cell runs, the execution count does not move, anything it
  prints is discarded) and the arrow fires only if the answer is true. The
  label shows `when …`; after a run the arrow is green (fired) or red-dotted
  (not fired) and the panel shows the value the condition gave. A condition
  that raises stops the run and names the arrow.
- **A loop arrow.** Drag a port *backwards* onto an earlier cell: the Lab
  draws a dashed purple arrow around the right side with a `↻ 0/3` label. When
  it fires (its condition holds, or it has none), its *body* — the target cell
  and everything forward of it — runs again. The budget (`max_fires`, 1–100,
  default 3) is the most times that can happen in one run; when it is spent
  the arrow turns amber (`exhausted`) and the run carries on past the loop, so
  a run always ends. Whatever the budgets say, a run also stops after
  `MAX_STEPS` (500) cell executions.

How a run decides what to execute (`graph_run.py`, worth reading, it is short):
cells are visited in trigger order; a cell runs when an arrow into it fired
(or it is a seed: the first cell of the plan, or the cell a loop rewound to);
a cell none of whose arrows fired is marked **idle** (dimmed, dashed) and its
own arrows do not fire, so the cells behind an untaken branch stay idle too.
When a loop fires, its body goes back to *pending*, the arrows leaving the
body are forgotten, and the pass count on each card goes up. The review
dialog lists every loop and condition the run will decide; the Log records
each decision (`arrow.fired`, `arrow.not_fired`, `arrow.exhausted`, with the
value) and the run summary counts idle cells and steps. In the document, an
arrow is `{kind: "control", source, target, when?, loop?: true, max_fires?}`;
a loop arrow must lead back to a cell upstream of its source, and a plain
cycle is still refused with a message that says to mark the arrow as a loop.
Export Python writes conditions and loops as comments (the flat file cannot
express them).

Model cells inside a loop see the fresh output of each pass through
`{{output:…}}`, which is what makes a propose → try → judge → *loop* graph
possible (the self-improvement example below).

**Status:** validation, the engine (branches, idle cells, loop counts and
budgets, exhaustion, condition errors, cancel inside a loop, the step cap,
runs started from inside a loop, a model cell in a loop), the `evaluate` op on
the real kernel and on a device, the review check and the journal lines are
tested (`tests/test_triggers.py`, `tests/test_kernel.py`,
`tests/test_devices.py`); drawing a loop arrow, setting its condition and
budget, running it and an untaken branch are tested in headless Chromium
(`LoopArrowPanelTests`). Not yet: a loop arrow from a cell to itself, nested
graphs, event triggers (timer, file change, chat prompt: A1 in the roadmap).

### Start events: a Mainbook fired from the outside (`local_lab/events.py`, `/api/events*`)

Until now a Mainbook ran when you clicked Run. The **START EVENTS** panel lets
the outside world start it instead, once you have armed it:

- **Sources** live on the Start node (`graph.nodes[start].events`, saved with
  the Mainbook): a **timer** (every N seconds, 10–86400), a **file** (a path
  inside the read-only workspace; anything added, changed or removed under it,
  checked every 2 s), a **chat** prompt (sent on the Models page; the chat
  still answers as usual) and **manual** (the Fire now button). The Start card
  shows what is armed.
- **Arming is a reviewed step**: *Arm…* opens a dialog that lists, in plain
  sentences, exactly what may fire the Mainbook and the budget (one run per
  firing, at most 500 cell executions, one run at a time, at most 10 waiting).
  Arming is not saved: it lasts until you disarm, until the cells, arrows or
  sources change (moving cards does not count), or until the Lab stops. A
  restarted Lab has nothing armed. Events never arm anything.
- **A firing is one ordinary run** of the whole graph, the same path as "Run
  all", with the event bound in the active session before the first cell:
  `event` (a dict with `kind`, `at` and the payload) plus `now` (timer),
  `path` (file; `event["changes"]` lists up to 20 changed paths) or `prompt`
  (chat). A firing with no running session is dropped and logged; firings that
  arrive during a run wait in a short queue (timer ticks collapse into one).
- **Everything is in the Log**: `events.arm`, `events.disarm` (with the
  reason), `event.fired` (with the run id), `event.dropped` (why). The run's
  own `run.start` line says "fired by timer/file/chat/manual", so "why did this
  run?" always has an answer. The canvas picks up fired runs on its own.

Where: `events.py` (`check_sources`, `describe`, `bindings_for`,
`EventRegistry` with `arm/disarm/fire/tick`), `graph.py` (`start_events`,
`fingerprint`: cells + nodes + edges, without layout or outputs),
`graph_run.py` (`bindings`, `reason`), `server.py` (`GET /api/events`, `POST
/api/events/arm|disarm|fire`, the chat hook), the panel in `graph.js`.

**Status:** sources, arming, timers (with a fake clock), file changes (added,
changed, removed), chat and manual firings, binding, the queue cap, drops and
disarm-on-edit are tested (`tests/test_events.py`, HTTP tests on a real
kernel); the panel, the review dialog, Fire now and the fired run appearing
on the canvas are tested in headless Chromium (`StartEventsPanelTests`). Not
yet: events from another device (C3), arming that survives a restart (by
design), a file watcher that does not poll.

### Canvas performance (`local_lab/ui/graph.js`)

How the canvas stays smooth, and where to look if it does not:

- **No forced layout while dragging.** `box()` takes each card's size from a
  `ResizeObserver` cache (`sizes`) instead of reading `offsetWidth` after every
  move. Before this change a 30-cell drag forced ~83 layouts per pointermove.
- **Only affected edges move.** `drawEdges()` rebuilds the SVG only when the set
  of edges changes; `updateEdgesFor(ids)` re-routes the paths touching moved or
  resized cards and keeps every other `<path>` as is.
- **One frame per input burst.** `scheduleFrame()` coalesces pointermove, pan,
  wheel-zoom and link previews into a single `requestAnimationFrame`.
- **Selection repaints two cards**, not all of them; edges only change class.
- **`.world` has `will-change: transform`** so pan/zoom scales a cached raster.
- **Run polling adapts**: 250 ms right after a cell finishes, backing off to
  1.5 s while a long cell runs; only cards whose run entry changed repaint.

Measure before touching any of this: `python3 tests/perf/bench_canvas.py 30 mylabel`
(needs Playwright Chromium) prints per-event JS cost, forced-layout counts and
frame gaps and writes `tests/perf/results/mylabel.json`.
`tests/test_canvas_ui.py` checks drag, typing, selection, link-drag and pan/zoom
behaviour in headless Chromium and is skipped when Playwright is absent.

## Local models (Ollama)

**Models (Ollama) →** in the Lab header opens `/models`, a second Mainbook:

1. **Device** — memory, chip and free disk read live (`local_lab/models.py:
   device()`); on Apple silicon Ollama uses the GPU through Metal with the same
   unified memory, so RAM is the budget.
2. **Get Ollama** — Ollama runs natively (not in Docker, which cannot reach the
   Mac GPU). The Lab downloads the pinned release (`OLLAMA_VERSION`,
   `OLLAMA_SHA256` from the release's `sha256sum.txt`), checks the SHA-256,
   mounts the image read-only, requires `Ollama.app` to pass
   `codesign --verify --strict`, to be signed by a Developer ID certificate
   naming Ollama, and to be accepted by Gatekeeper (`spctl --assess`), then
   opens the installer. The owner drags Ollama to Applications and opens it;
   the Lab re-checks every 5 s until Ollama answers on 127.0.0.1:11434.
3. **Models that fit** — a curated catalog with approximate sizes and the RAM
   each needs, marked fits / tight / too large for this device; Ollama reports
   exact sizes when a download starts. **Download** pulls through Ollama's
   local API with progress.
4. **Chat API** — `POST /api/models/chat {model, messages}` on the Lab's
   loopback port, owner session only, forwarded to Ollama's `/api/chat`
   (non-streaming for now). The page includes a one-message test. A chat
   interface, streaming, tool use and model-backed graph cells are future work.

Ollama is never exposed beyond loopback by the Lab, and the Lab never sends
prompts anywhere else.

**Status:** the API client (version, tags, streaming pull, chat), catalog
fit rules, the routes and the page are tested against a fake Ollama server
(`tests/test_models.py`) and in a headless browser. The real installer
download and its signature/notarization checks run in the macOS acceptance
workflow; installing and running Ollama, and model downloads, need a real Mac.

### Form cells (`graph.py` `form_config`, `form_prelude`)

The first GUI element tied to code. Tick "This cell has a form" on a code
cell and list fields in the inspector, one per line (`n: integer = 10`,
`mode: choice = fast | slow`, `verbose: flag`, `note: text`). The card then
shows those controls; what you type is saved in the Mainbook
(`metadata.mainaiger.form.values`). When the cell runs, each field is bound as
a variable first (`form_prelude`: one exact JSON-literal assignment per field),
then the cell's own code runs, so the next cells use the values like any
variable; the analysis lists the fields as the cell's outputs, so data edges
connect them. "▶▶ Run with these values" runs from that cell onward through the
normal review. Exports to `.ipynb` prepend the assignments so Jupyter can run
the cell. Types: text, number, integer, flag, choice; at most 12 fields; values
are type-checked on save and unknown ones dropped.

**Status:** validation, declared names, binding on a real kernel, data edges
and export are tested (`tests/test_sessions.py` `FormCellTests`); the
inspector editor, the card controls, a run from the card and persistence
across reload are tested in headless Chromium (`FormCellPanelTests`).

### Model cells (`graph.py` `model_config`, `graph_run.py` `_model_cell`)

A code cell can **ask a model instead of running Python**: tick "This cell
asks a model" in the SELECTED NODE panel, name an installed Ollama model, the
variable to store the answer in, and optionally the answer's shape (a small
JSON schema). The cell's text is then the prompt. Two placeholders pull in
facts from the graph: `{{output:Cell title}}` pastes what another cell printed
(this run's output, else the saved one) and `{{var:name}}` pastes a variable's
type and short summary from the session. The Lab asks the model **on the host**
(models never run inside the container, which has no network), checks the
answer against the shape through `decide.ask` when one is given, and binds the
answer in the cell's session with one assignment
(`decision = json.loads('…')`), so the next cells use it like any variable.
The card shows `model <name> → <variable>`, the analysis treats the cell as
reading its `{{var:…}}` names and defining its output, so data edges connect
model cells like code cells. Exports to `.ipynb` turn a prompt into a raw cell
that says what it was. Failures are plain: a bad placeholder stops the run
before any model is asked (`PromptError`); a model that is not installed or
answers outside the shape stops it with the reason (`ModelError`); the journal
records the model used for each cell.

**Status:** validation, rendering, binding, data edges, export and every
failure path are tested on a real kernel with a fake model
(`tests/test_sessions.py` `ModelCellTests`); the panel flow is tested in
headless Chromium (`ModelCellPanelTests`). A real Ollama answer is unverified
here; on your Mac, install a model on the Models page first.

### Thin-slice decisions (`local_lab/decide.py`, `POST /api/models/decide`)

The pattern every AI-made decision in MainAIger follows: the harness asks the
model **one small question** and names the **exact shape** of the answer (a
small JSON schema: typed fields, enums, limits); the model answers in JSON
only (Ollama is asked for structured output with that schema); the harness
**parses and checks** the answer in code, tells the model once why it was
wrong if it was, and gives up with `ok: false` after that. Only a validated
object reaches the code that acts on it. `decide.ask(model, question, context,
schema, chat=...)` takes any chat function with `models.chat`'s signature, so
the same decision runs on Ollama today and on an OpenAI-compatible endpoint
(ASU Sol) once a provider is added. Budgets: question ≤ 4 kB, context ≤ 24 kB,
at most 3 attempts. Each call is one journal line (`model.decide`).

Example: "Given this diff and these test results, keep or revert?" with
`{action: keep|revert|ask, confidence: number, reasons: string[≤3]}` is the
decision step of the self-improvement loop in `docs/AI_HARNESS.md`.

**Status:** schema checks, parsing of fenced or chatty replies, retry with
reason, attempt cap and the route are tested against a fake Ollama
(`tests/test_decide.py`); a real model's answers are unverified here.

### The self-improvement loop example (`examples/self-improve.ipymb`)

The loop described in `docs/AI_HARNESS.md` §3, as a Mainbook you can open
from **Open example…**, read on the canvas, and run with a local model. Ten
cells in one chain plus one loop arrow:

```
Start → How this loop works (note) → Load data → Extract features → Split train/test
      → Train classifier (baseline) → Evaluate → Propose change (model)
      → Apply to copy → Judge (model) → Report → Finish
                ↑                        │
                └── loop, at most 2× ────┘   when verdict["action"] != "keep"
```

- **Load → Evaluate** are plain Python (a small synthetic dataset, a
  nearest-centroid classifier written in the standard library, an accuracy
  number). The baseline is the thing to improve.
- **Propose change** is a *model cell*: its prompt pastes the baseline source
  and the accuracy, asks for `{source, why}` (a rewritten `train` function and
  one reason), and stores the answer in `proposal`.
- **Apply to copy** runs the proposed `train` on a *copy* of the data in the
  same session (`exec` into a fresh namespace), evaluates it the same way, and
  prints `baseline accuracy`, `candidate accuracy` and `delta`. The baseline
  function is never replaced; nothing on disk changes.
- **Judge** is a second model cell whose whole prompt is
  `{{output:Apply to copy}}` plus the question; its answer is schema-checked
  as `{action: keep|revert|ask, confidence, reasons[≤3]}` and stored in
  `verdict`.
- **The loop arrow** Judge → Propose fires while the verdict is not "keep",
  at most twice. Each new Propose prompt carries the previous attempt's
  numbers (again through `{{output:Apply to copy}}`) so the model does not
  repeat itself, and the Apply cell keeps a `history` list in the session.
- **Report** prints every attempt with its accuracy and delta, the last
  verdict, and what *you* would do next (keeping a change is a commit you
  make after reading it; the loop never writes to the checkout).

The baseline is deliberately undertrained (10 epochs, learning rate 0.01,
accuracy about 0.86 on the synthetic blobs) so a sensible proposal can win;
the data's ceiling is about 0.90, so most other ideas are honestly rejected.

Why this shape: the thing being improved is a function, not the Lab's own
repository, so the first run proves the plumbing (model reads a result,
answers in a fixed shape, the answer drives code, every step is on the canvas
and in the Log) without any step that can touch your files. The stricter loop
(repository copy, held-out tests, budgets) is the table in `AI_HARNESS.md` §3.

**Status:** tested in CI with a *fake* model (`tests/test_examples.py`): the
fake first proposes a bad `train` (reverted, so the loop arrow fires), then a
good one (kept, so it does not); the test checks that each Judge prompt
carried that pass's numbers, that the second Propose prompt carried the
first attempt, the loop count, the step count and the report's two lines; run
on a real kernel like every other example. With a real Ollama model (`qwen2.5-coder:7b` is named in the
two model cells; change it on the card to any installed coder model) the run
is unverified here: expect most proposals to be rejected or to fail the
schema, which the run shows plainly. Fixed while building it: output
placeholders now accept cell titles with spaces (`{{output:Apply to copy}}`
was left literal before).

## Devices: run cells on another machine (`local_lab/devices.py`, `remote.py`, `/api/devices/*`)

**Plain words.** The other device runs this same code. The Mac reaches it
through an SSH port-forward and signs in with that Lab's owner key, which you
copied over yourself into a private file. From then on that device's sessions
appear in Resources as `device/label`, cells can **Run on** them, and the
Terminal and Files panels work there. Nothing opens a public port anywhere;
the remote Lab stays loopback-only on its own machine.

**On the other device (Linux, for example Nexus):**

```bash
git clone https://github.com/tanaypatel7/mainaiger-web ~/mainaiger-web
cd ~/mainaiger-web && git checkout feature/persistent-kernel
python3 -m local_lab --port 8795      # a port other than this Mac's 8794
```

It prints its owner key path (`~/.local/share/mainaiger-lab/owner.key`).
Copy that file to the Mac (`scp nexus:~/.local/share/mainaiger-lab/owner.key
~/.mainaiger/nexus.key && chmod 600 ~/.mainaiger/nexus.key`). Docker on that
device is needed for sessions there, exactly as on the Mac.

**On the Mac, Devices page → Pair a device…** (`POST /api/devices/pair`):
name, SSH host (an alias from your `~/.ssh/config` or `user@host`), the port,
the key file path, and who runs the forward. Pairing writes
`<state dir>/devices.json` (0600) and validates the key file (owner-owned,
mode 0600, no symlink); nothing is sent.

**Connect…** (`POST /api/devices/connect {name, confirm}`) shows the exact
command first:

```
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -L 127.0.0.1:8795:127.0.0.1:8795 nexus
```

The Lab starts it as a child process (`devices.Tunnel`), waits up to 12 s for
the port to answer, then signs in (`remote.RemoteLab.login`). `BatchMode=yes`
means ssh never prompts; your key or agent must already work for that host.
With "I run it myself" the Lab only signs in to `127.0.0.1:<port>` (use this
with Tailscale SSH or your own tunnel). The same port is used on both ends
because the remote Lab checks the `Host` header against its own port.
**Disconnect** ends the child process; **Forget** removes the pairing.

**Sessions on the device**: Devices page → New session, or the Graph page's
New session… dialog with Device = that device (`POST
/api/devices/session/start`). The Lab calls the device's own
`/api/sessions/start`, then lists the session locally as `nexus/main`
(`sessions.attach`, a `remote.RemoteKernel` that forwards execute, interrupt,
variables and stop to the device's `/api/sessions/execute|interrupt|
variables|stop`). `GET /api/sessions` refreshes device rows each time
(`sync_remote`); rows of a dropped device show as ended. A device session is
never made the active session by itself: cells that name no session always
run here.

**Run on a device**: set Run on = `nexus/main`; the run review lists it; the
cell's code travels to the device, runs in its container, and the output comes
back. The receiving Lab journals every piece of code that arrives through its
API (`session.execute`), and this Mac journals terminal lines and runs as
usual. Variables stay on the device (the cross-session rule). **Files** can
browse the device's workspace/Mainbooks/Library roots (`GET
/api/devices/files`, read-only, same escape rules there); the device's journal
is readable (`GET /api/devices/journal`).

**Status:** the client, kernel adapter, pairing rules, tunnel lifecycle
(refused when the port is busy, ssh errors surfaced, timeout), connect/
disconnect/forget, running a cell on the device, terminal and files there are
tested with two in-process Labs and a stand-in for ssh (`tests/test_devices.py`,
`DevicesPageTests` in the browser). A real SSH forward to Nexus and Docker
sessions there are unverified until you try it.

## Devices page: this device and the tailnet (`local_lab/network.py`, `GET /api/network`)

The fourth page shows **this device** (host name, system, chip, memory, free
disk, Python, whether Docker and Ollama are present), its **sessions** (the
same table as Resources), and the **tailnet** exactly as Tailscale reports it
with `tailscale status --json` (on a Mac the CLI inside `Tailscale.app`,
elsewhere `tailscale` on PATH): logged in or not, tailnet name, this device's
addresses, and the peers with OS, online state, addresses and exit-node flag,
online first. When Tailscale is absent or logged out the page says so; nothing
is guessed. The Lab never runs `tailscale up`, `login`, `serve` or any other
command that changes the network; **Paired MainAIger devices** is shown as
future until `docs/INTEGRATION.md` step I1 lands.

**Status:** parsing, sorting, the read-only guarantee (only `status --json`
is ever run), login/absent/garbage cases and the route are tested with a fake
CLI (`tests/test_network.py`); the page was opened in headless Chromium. A real
tailnet listing on your Mac is unverified.

## Map: code as a graph (`/map`, `local_lab/codemap.py`, `GET /api/codemap`)

The fifth page shows a folder of code as nodes and edges, starting with the
Lab itself (`root=self`, the `local_lab` package) and any Files root. Reading
is done with Python's `ast` and never executes anything.

- **Nodes:** one per module (file), class, function and method; one per HTTP
  route found in request handlers (`do_GET`/`do_POST` comparing `path` to
  literals, including `startswith` prefixes shown with a trailing `/`); one
  per UI file (`.js`/`.html`).
- **Edges:** `contains` (module → class/function, class → method); `imports`
  (inside the folder only); `calls` (resolved as written: a name defined or
  imported in the same module, `alias.name` for imported modules,
  `self.method`, `Class.method`); `routes` (route → handler function); `ui`
  (UI file → route, from `api('/api/…')`, `fetch('/api/…')`, `json('/api/…')`).
- **What it does not see:** dynamic calls, callbacks, reflection, generated
  code, calls into libraries and builtins (counted as "unresolved", not drawn).
  A map is advisory, like data edges.
- **Page:** one column per module; routes first, UI last; click a node for its
  source (`GET /api/codemap/source`, bounded, symlink-safe) and its calls /
  called-by / imports / routes; search highlights; modules collapse (all are
  collapsed by default above 600 members); edge kinds toggle; Fit goes by width.
  Limits: 2,000 files, 1 MB per file, 20,000 nodes; results cached 10 s.

This is roadmap B1. The LLM labelling of clusters (B3) and editing from the
map (B2) come next; both build on these nodes.

### Make a cell from a function (`codemap.cell_for`, `POST /api/codemap/cell`, the Graph page inbox)

The first way GUI elements get tied to repository code. Select a function or
method on the Map and press **Make a cell from this…**: the Lab reads the
function's parameters (`params` on every function node: names, literal
defaults, simple annotations) and queues a cell for the open Mainbook. The
Graph page picks it up within a few seconds (or when you switch to it), chains
it after the selected cell (else after the last cell), and shows it as a
**form cell**: one field per parameter, typed from the default or annotation
(`int` → integer, `float` → number, `bool` → flag, `str` → text; anything else
is a text field read as a Python literal when it looks like one, empty = None),
in front of a few lines of code that put the package's folder on `sys.path`
(`/workspace/…` inside the session when the workspace holds it, else the host
path), import the function and print the result. `*args`/`**kwargs` and
non-literal defaults are left out of the form and said so in a comment;
methods get a cell that constructs an instance and says it needs one. If the
session cannot see the folder, the cell carries a warning naming the workspace
to set. Routes and UI files do not become cells (the session has no network);
their handlers do. Every queued cell is a journal line (`map.cell`).

**Status:** parameters, the generated cell (run for real with form values),
methods, the route, the inbox (queue, replace, take) and the cell running on a
real kernel are tested (`tests/test_codemap.py`); the full path Map → button →
Graph page → form on the card → run is tested in headless Chromium
(`MapToCellTests`).

### Edit from the map, write back as a reviewed step (`codemap.replace_lines`, `/api/codemap/diff|write|approve-write`)

Select a function, class or method of a folder you mapped (not the running
app's own code) and press **Edit…**: its lines become a text box. **Review
changes…** asks the Lab for a unified diff of the whole file (`POST
/api/codemap/diff`, nothing written) and shows it in a dialog. **Write to
file** then:

1. the first time for a folder, asks you to **allow writes under that folder
   until the Lab stops** (`POST /api/codemap/approve-write`, a journal line;
   never persisted, like arming) — the only write permission the Lab has
   besides the Mainbooks and Library folders;
2. checks that the file still hashes to what the editor read (`file_hash`
   from `/api/codemap/source`; a change underneath is refused with "reload
   the map"), that the range is inside the file, that the text is at most
   200 KB, and, for a `.py` file, that the **whole file still parses** (a
   syntax error is refused before anything touches disk, naming the line);
3. writes atomically (a temp file in the same folder, the original's mode,
   `os.replace`), re-reads the map, keeps your selection, and records
   `map.write` with the path, lines and bytes in the Log.

**Run tests in session** runs `python3 -m unittest discover -s tests` from
the mapped folder as the session sees it (`/workspace/…`), the same way a
Terminal line runs: inside the container, nothing on this Mac. Set your
workspace to a checkout of a repository (this one included), map it, edit a
function, run its tests: that is the first "MainAIger working on MainAIger"
loop, by hand. The running app's own copy (`self`) is read-only here by
design: edit the checkout, not the app.

**Status:** diff, hash and parse checks, atomic write with mode kept, the
trailing-newline rule, path safety, the routes (allow, refuse `self`, refuse
without confirm, refuse after the file changed), the re-read map and the
journal lines are tested (`tests/test_codemap.py`); edit → diff → allow →
write → map reloads, a broken edit refused, and the folder's tests running
in the session are tested in headless Chromium (`MapToCellTests`).

**Status:** modules/classes/functions, import and call resolution, routes and
UI edges on the Lab itself, relative imports, hidden/symlink/skipped files and
syntax errors on a synthetic folder, and the routes are tested
(`tests/test_codemap.py`); the page is tested in headless Chromium
(`MapPageTests`).

## Settings

Settings remain in memory while the panel runs and are reset on restart. Stop it
with Ctrl-C. Unmounting or changing a directory requires saving new settings in
the panel. The selected directory may contain sensitive files; Python code can
read everything the container user is permitted to read in that directory.

Docker Desktop and the Python image must be installed separately. The GUI does
not download software or pair with the website. No code, folder path, output or
local key is uploaded to the directory service. Docker image pull uses your
normal network connection; execution containers are configured without network.

This prototype was unit-tested without Docker; the image, actual container
execution, Mac folder permissions and desktop setup have not yet been tested on
a Mac. Next steps include multi-cell notebook editing with a writable workspace,
an executable graph over the same cells, validated installation packaging, and
explicit, authenticated website pairing. Do not
expose this local service to the internet or expand its listener beyond loopback.
