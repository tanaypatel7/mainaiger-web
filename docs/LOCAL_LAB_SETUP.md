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
