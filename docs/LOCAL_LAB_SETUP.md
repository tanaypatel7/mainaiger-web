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
  finish, cells referenced exactly once, no control cycles).
- **Triggers (control edges, solid)** — drag from a cell's bottom port to
  another cell's top port. "A → B" means *when A finishes successfully, B may
  run*. A cell with several incoming triggers runs after all of them.
  Loops are refused in this version.
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
  `csv-pipeline`, `branching-stats`); `tests/test_examples.py` runs each one
  with the real kernel, so they cannot rot. **Open .ipymb / .ipynb** sends a
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
Python process running serially; parallel branches, nested graphs, loops,
conditional triggers and other languages are future work and are refused
rather than simulated.

**Status:** the document model, analysis, planning, preflight and serial run
(fire, fail-and-skip, cancel) are tested with the real kernel as a local
process (`tests/test_graph.py`) and the canvas was exercised end to end in a
headless browser. Running the graph inside Docker on a Mac is the same
unverified step as the session itself.

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
