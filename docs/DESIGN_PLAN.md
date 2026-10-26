# MainAIger Lab: notebook to graph

Design and implementation plan · 25 October 2026

Implementation status: the static design prototype is published at
https://tanaypatel7.github.io/mainaiger-web/. The repository is
https://github.com/tanaypatel7/mainaiger-web. Connecting `mainaiger.org` remains
pending because Cloudflare sign-in failed; no DNS records were changed.

## 1. Product decision

MainAIger should retain the familiar notebook as one view of an experiment and add a second, spatial graph view of the same cells. The public website introduces the product, demonstrates the transformation, distributes installers and explains connection setup. An installed, authenticated runtime executes native Python and manages local or remote devices. The static website is not the runtime.

The first experience should present three clear choices: **Explore the demo**, **Use this computer**, and **Connect an existing MainAIger**. A user can inspect the design without creating an account. Authentication is required before accessing a private runtime. A future MainAIger cloud account can synchronize account-level preferences, but must remain distinct from permission to execute on a particular device.

The accompanying web prototype is a design demonstration. Its process metrics, execution, setup progress and output are simulated unless the interface explicitly reports a verified runtime connection. It does not install Python, operate a terminal, authenticate an account or create a sandbox. A click in the public demo must never silently become a real operating-system action later.

The essential relationship is:

> One experiment document → notebook view and graph view → selected runtime → durable run records.

Browser tabs, experiment documents, kernels, OS processes, tmux sessions and devices are separate objects. The interface connects them through visible identifiers rather than treating them as interchangeable.

## 2. What already exists

The inspected MainAIger Development Studio archive identifies Studio 0.2.0 and base application 0.6. It contains a working custom Python Lab, a copied-source Studio, a device-aware broker and connector, and a separate Jupyter launcher. The archive is a source snapshot; it does not establish what is currently deployed on any private device.

Reuse these components:

| Existing source | Existing responsibility | Planned change |
|---|---|---|
| `static/lab/index.html`, `lab.css`, `lab.js` | Lab chrome, SVG graph, block editor, process/run views | Add notebook/graph switch, richer cell cards, stable ports, variable drawer and adapter status |
| `lab/projects.py` | Blocks, graphs, source snapshots and separate layout revisions | Add versioned experiment-document adapter and migration/export hooks |
| `lab/scheduler.py` | DAG validation, selected/from/one execution, declared inputs | Preserve review/readiness rules; add explicit execution-adapter contract |
| `lab/runtime.py` | Persistent Python subprocesses and lifecycle observations | Preserve custom runtime; register Jupyter-backed sessions through an adapter |
| `lab/interpreter.py` | Custom Python execution and bounded namespace summaries | Keep as a supported backend; do not describe it as a Jupyter kernel |
| `lab/host.py`, `broker.py`, `connector.py` | Enrolled target operations, authentication and transport | Add narrowly scoped document/kernel actions and truthful connection observations |
| `jupyter_workspaces.py`, `scripts/jupyter/` | Existing Jupyter connection workflow | Reuse target identity and connection verification |
| `static/studio/`, `lab/studio.py` | Source navigation, editing and copied-app preview | Keep distinct from executable experiment graphs; link selected UI features to their source |

Current Lab supports Start, End, code and parameter nodes; explicit data/control edges; retained Python sessions; missing-input diagnostics; process observations; save/reopen; and run all/from/selected/one. It executes branches serially in one process. Cycles, conditional guards, structured loops, composites, process scopes and notebook import/export are explicitly deferred. The current SVG cards are fixed-size and route right-center to left-center; flexible notebook cards require new rendering work.

Preserve its useful separation between layout revision and executable revision. Moving a card should not invalidate Python results or imply a changed program. The existing source-map graph is an explanation of source relationships, while the experiment graph has executable semantics; keep those views visibly named.

## 3. Public website and private runtime

### Public origin: `https://mainaiger.org`

GitHub Pages can serve HTML, CSS, JavaScript, documentation, a prototype, and downloadable release links. It cannot run a server-side Python interpreter, conda, tmux or an interactive terminal. Browser Python through JupyterLite/Pyodide is a possible separate demo mode, with a clearly labeled browser environment and package/filesystem limitations.

Use GitHub Pages first for the design lab and installation guide. Serve the actual private Lab from the installed companion or an authenticated private server. Initially, **Open my Lab** navigates to that runtime in a top-level tab. This avoids making the public page an unrestricted cross-origin terminal client and avoids relying on inconsistent localhost/local-network permissions across browsers. A later paired bridge can be added only with explicit origin checks, short-lived pairing, authorization and capability enforcement.

A public login page alone cannot protect a native kernel. If hosted accounts are introduced, use an appropriate managed authentication service or owned backend, keep private credentials out of static assets, and still require device pairing. Do not embed a shared owner token or GitHub token in JavaScript.

### Installed runtime

The companion owns environment discovery, approved folders, device identity, process supervision, Jupyter connection and execution. Bind the local service to loopback by default. Remote access uses the existing private network and authenticated HTTPS. The homepage must not expose a private kernel publicly through a new port-forward or Tailscale Funnel.

Use the existing tailnet when available. Joining ordinary Tailscale client devices is not “creating a MainAIger subnet.” Subnet routers are relevant only when deliberately reaching a network that does not run Tailscale directly. Device discovery does not grant execution permission.

## 4. Onboarding as a setup notebook

Replace ten separate blocking pages with a resumable setup notebook and a progress rail: **Choose location → Device → Access → Python → Launch → Connect**. Each section contains a short explanation, a command or form, an observed result and a recovery action. Advanced networking appears after a local experiment works.

1. **Choose a mode.** Demo, new local installation or existing installation. Existing users select a known workspace or locate its manifest; reconstruct the UI from saved configuration and current observations. Never infer that stale saved process IDs are still live.
2. **Choose storage.** Before the companion exists, the website offers a browser folder picker where supported or a download/import alternative. That picker grants browser file access only. The installer confirms the native workspace root separately. Show the exact resulting path before creation.
3. **Describe the device.** Ask for a friendly name and role. Discover OS, interpreter candidates, disk availability and memory through the companion. Use a generated installation/device ID rather than an IP as identity. IPs are connection observations and may change. Show unknown GPU capacity as unknown.
4. **Review access.** A table displays path, read/write scope, execution capability, network access and enforcement mechanism. A folder allowlist can constrain companion file APIs; it does not constrain arbitrary Python running with the user's OS permissions. Label that mode **Trusted local Python**. A real sandbox requires an OS/container boundary and separate acceptance tests.
5. **Choose Python.** Discover existing venv/conda interpreters, allow creation of a project venv, and show interpreter path, Python version, package state and device in an environment card. Package isolation is not a security sandbox. Do not automatically upgrade an established research environment.
6. **Launch.** Show the managed service, its owned child kernels and optional terminal sessions as separate rows. tmux is an optional terminal convenience. Prefer the platform's normal supervisor for long-lived service startup; tmux does not itself manage Jupyter kernel lifecycle or supply isolation.
7. **Connect and verify.** Open the private Lab. Run a small cell and show actual host, interpreter, session ID, working directory and output. Mark setup complete only after this observation succeeds.

Before installation, terminal cells have **Copy command** and **I ran this** actions. They cannot execute shell commands from the static page. After pairing with the companion, cells can display **Run on this Mac** or another exact target, preview the operation, stream stdout/stderr, report exit status and retain the operation record. Prefer fixed structured operations for installation steps over arbitrary shell strings. Failed steps remain retryable with idempotent checks; rerunning setup must not overwrite existing experiments.

Use the requested `<chosen-root>/mainaiger/main/online_exp/` as the initial experiment directory if desired, while separating `config/`, `state/`, `logs/` and optional `artifacts/` under the MainAIger root. Do not mix application source updates with user experiment storage. Keep secrets outside portable experiment files.

## 5. Interface composition

Keep a Jupyter-like menu, document tabs, execution counts, code editor behavior, Markdown cells, output MIME rendering, kernel selector, command palette and keyboard execution conventions. The left rail contains **Files**, **Running**, **Blocks** and **Outline**. The top toolbar adds a **Notebook | Graph** switch, **Run selection**, **Arrange** and **Fit**. The right drawer contains **Variables**, **Node**, **Run** and **Environment** tabs. The bottom status line reports save state, device, interpreter, session and connection freshness.

Maintain MainAIger's current light panels, dark ink and teal actions. Use shape and text as well as color: green circular Start, red outlined shutdown control, rectangular code cells, a double-border subgraph and a distinct service card. A completed workflow uses a neutral **Finish** marker; red is reserved for cancellation, failure or shutdown. Clearly label sample telemetry in the public prototype.

The notebook and graph views share cell IDs and editing state. Changing view does not duplicate code, restart a kernel or run anything. Every code card contains title, execution count/status, device/session badge, collapsible code and an independently collapsible output area. Outputs include their generating run and source revision; older results receive a stale badge after a relevant edit.

### Notebook-to-graph animation

Use a reversible, roughly three-second transformation with **Replay**, **Pause** and a scrubber:

1. Show familiar stacked Markdown, input and output rectangles.
2. Add stable cell labels and declared input/output port badges.
3. Move complete code/output pairs apart while keeping their identity visible.
4. Reveal control edges in order, then named data edges.
5. Place Start and Finish around the graph and settle into the spatial layout.

A caption states: **The same cells, another view. Layout changes do not change execution.** Moving cards apart cannot prove previously sequential cells are safe to run in parallel. The example should contain an explicit branch with separately declared dependencies. Respect reduced-motion preferences by switching immediately to the final view.

### Canvas and layout

Provide pan, scroll, zoom, drag, keyboard movement, multi-selection, a minimap and Fit selection/Fit all. “Infinite” means a pannable coordinate space, not an infinitely rendered DOM. Virtualize large graphs and cap output previews.

Connect semantic ports rather than whichever corner happens to be nearest. The router can select a suitable side while preserving the port identity and label. Use distinct line patterns and labels for control, data and error edges; users can hide a layer. Keep the legend consistent with the rendered view. Changing a route is presentation, while reconnecting a port changes execution.

**Arrange** offers top-down, left-to-right and radial presentation. Top-down is the default: entry points share a rank; fan-out separates branches; joins converge; exits share a final rank. Multiple Start points are independent entry interfaces, not automatically simultaneous launches. Radial layout is optional for exploration, with explicit direction arrows. Fit changes the camera and optionally collapses details; it must not shrink text below legibility or alter graph semantics.

Duplicate Start/Finish/Stop controls are visual aliases pointing to one semantic ID. Their badges say “alias,” and selecting one highlights all copies. Moving or deleting an alias does not create or cancel a process.

## 6. Execution model

### Start, code and Finish

**Start** enters a workflow invocation. A separate runtime action launches or attaches to a kernel. This permits ordinary notebook reuse and repeated experiments in one session. A Start card shows unique workflow/invocation identity, input contract and selected runtime. Another workflow can invoke it through a declared call interface; a user can also explicitly run it.

**Code** executes one cell under the selected adapter. Preserve Jupyter behavior when using a Jupyter kernel, including rich display, errors and execution counts. A code card's output edges activate only according to its declared completion/error rules. One shared mutable kernel serializes execution. A branching picture alone does not enable safe parallel evaluation.

**Finish** completes the invocation and records results; by default it keeps the kernel alive for inspection. **Interrupt**, **Cancel run**, **Restart kernel** and **Shut down kernel** are distinct actions with distinct effects. An optional “shut down this owned kernel when complete” policy is explicit and cannot kill unrelated processes. Cancellation must propagate to owned children according to a defined policy.

For long-running servers use a **Service** node with start, health, ready, stop and exit states. Readiness may trigger downstream work while the service continues. An “indefinite Stop” is ambiguous; a service has an ongoing lifecycle and a concrete stop operation.

### Inputs, partial execution and variables

Each runnable node declares named inputs and outputs. Preflight reports missing values, the declared producer, whether a cached value belongs to this session/epoch, and a suggested prerequisite closure. Actions are **Include prerequisites**, **Provide value**, **Use current session value** where valid, or **Cancel**. Re-executing upstream cells can have side effects, so the user must see the expanded run before dispatch.

Static Python analysis can suggest producers but is advisory. Dynamic names, mutation, external files, imports and side effects prevent complete automatic dependency inference. Shared notebook globals remain supported, with an honest hidden-state warning when graph contracts cannot describe them.

The variable drawer shows name, type, shape, approximate size, owning session, producer if known and freshness. Preview values on demand with limits. Do not automatically serialize every tensor or call arbitrary user-defined `repr` methods. Values in separate processes are not one shared namespace.

### Control edges, joins and custom logic

Control events are immutable execution records, not arbitrary writable booleans injected into Python globals. Expose a read-only event/context object to custom control nodes. Every token identifies run, activation, source port, sequence, iteration and branch. Data values and control completion remain separate.

- **Fan-out:** emit one activation token per outgoing control edge after successful completion.
- **Join all:** wait for every required incoming branch in the same run and iteration. Define skipped/cancelled/error behavior instead of waiting forever.
- **Join any:** accept the first matching activation and explicitly choose whether remaining branches continue or are cancelled. Deduplicate later arrivals.
- **Branch:** evaluate a declared predicate, select named routes and record the decision. Unchosen paths become skipped.
- **Custom control:** consume declared events and emit only declared routes. Enforce timeout, output validation and a maximum activation budget. It cannot mutate the scheduler's internal history.

### Loops, errors and recursion

Do not accept arbitrary back-edges as implicit loops. A structured For/While/Repeat region has entry, body, carry values, exit condition and maximum iteration/time budget. Every iteration receives an ID. Parallel-for requires isolated workers plus an explicit aggregation node and concurrency limit; reductions must declare order sensitivity.

A Try region has body, catch and finally paths. Catch receives a bounded error descriptor; finally runs once per relevant invocation. Retry is an explicit policy and is disabled by default for side-effecting work. “Failed,” “cancelled” and “unknown after connection loss” are different states.

Recursive calls require an explicit subgraph call, depth/activation limit and termination condition. They are a later phase. Until implemented, validation rejects unsupported recursion rather than appearing to run it.

### Subgraphs and processes

A subgraph card references a specific document/revision and exposes named inputs and outputs. **Open tab** navigates into it. **Expand here** is initially a view expansion: gray boundary connectors show the parent call mapping, while internal Start/Finish retain their identity. It does not absorb code or change execution.

**Inline into parent** is a separate reversible edit that creates fresh cell IDs, maps ports and resolves names. **Run in child kernel** creates an isolated session; **Run in current kernel** uses shared state and serial execution. The UI must name that choice.

Cross-process data uses explicit transfer edges. Support JSON values and declared file/array artifact formats first, including size, checksum, source/destination, storage location and transfer status. Local handles remain valid only in their owning session/epoch. Do not promise transfer of all memory, open sockets, arbitrary Python objects or GPU state. Avoid untrusted pickle ingestion. Large arrays can later use shared memory on one host or artifact transfer between hosts.

## 7. Live processes and history

The Running panel groups **Device → service → kernel/process → active run**. Each row shows observed status and observation time. CPU, RSS and GPU metrics must state their scope; current existing CPU sampling is approximate OS process reporting, not per-cell CPU profiling. Unknown values remain unknown.

Clicking a known kernel opens its linked experiment or offers a list when several documents use it. An arbitrary OS process cannot be reconstructed into a notebook. Display such processes as inspectable external processes only if the user has granted that capability.

Closing a browser tab does not stop the runtime. A disconnected device becomes stale/unknown, not successful or stopped. Reconnection reconciles durable run IDs and receipts; it must not automatically replay uncertain mutations. On completion, the run appears in History/calendar with start/end time, duration, source revision, environment, outputs and logs. Long-lived services retain a live row and periodic event history.

## 8. Document format and export

Use the requested `.ipymb` extension for a versioned MainAIger experiment document. Treat it as a defined format, not a renamed notebook. Embed a valid notebook payload for cells/outputs and store graph metadata beside it. The same cell IDs identify both views. Start, Finish and service nodes are graph nodes, not fabricated executable notebook cells.

The initial repository bridge uses the following version-1 wrapper. Its control flow preserves the original sequential code-cell order; it does not implement graph execution. Later runtime metadata and richer graph semantics require a documented schema migration.

```json
{
  "format": "mainaiger",
  "schema_version": 1,
  "notebook": {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
    "cells": [
      {"id": "cell_load", "cell_type": "code", "metadata": {}, "source": "x = [1, 2, 3]", "execution_count": null, "outputs": []},
      {"id": "cell_sum", "cell_type": "code", "metadata": {}, "source": "total = sum(x)\nprint(total)", "execution_count": null, "outputs": []}
    ]
  },
  "graph": {
    "nodes": [
      {"id": "start", "kind": "start"},
      {"id": "cell_load", "kind": "code", "cell_index": 0},
      {"id": "cell_sum", "kind": "code", "cell_index": 1},
      {"id": "finish", "kind": "finish"}
    ],
    "edges": [
      {"id": "e1", "kind": "control", "source": "start", "target": "cell_load"},
      {"id": "e2", "kind": "control", "source": "cell_load", "target": "cell_sum"},
      {"id": "e3", "kind": "control", "source": "cell_sum", "target": "finish"}
    ]
  },
  "layout": {
    "nodes": {"cell_load": {"x": 100, "y": 140}, "cell_sum": {"x": 500, "y": 140}},
    "viewport": {"x": 0, "y": 0, "zoom": 1}
  }
}
```

The bridge preserves the original notebook object, including outputs and unknown metadata. Existing unique cell IDs are reused; absent or conflicting reserved IDs receive graph-only identifiers without rewriting the notebook. Markdown/raw cells remain nonexecuting graph nodes. Version 1 references notebook cells by `cell_index`; an editable implementation must update these mappings atomically whenever cells are reordered, inserted or removed. Stable cell identity is the target design, while the bridge preserves older notebooks that lack unique IDs. The bridge refuses an export that would hide changed graph semantics unless the user explicitly accepts a lossy conversion. It is not a scheduler.

For the later executable document schema, add document identity, independent execution/layout revisions, named ports, edge kinds, alias identities, subgraph revision references, logical runtime preferences and artifact references. Bind output freshness to the source revision and run ID. These are planned extensions, not fields already implemented by the initial bridge.

Persist documents atomically and allow incomplete drafts. Validate before execution; show disconnected nodes and unsupported constructs without blocking Save. Keep live PID, credentials, socket connections and memory outside the portable file. A saved document does not serialize a running kernel.

Provide `.ipynb` import/export through `nbformat`. Preserve cells, IDs, Markdown, attachments, ordinary outputs and notebook metadata; store MainAIger metadata in a namespaced field when appropriate. Export reports constructs that ordinary Jupyter cannot reproduce. Include a migration version and fixture tests for unknown metadata and future fields.

For `.py`, call the action **Export Python** rather than promising universal compilation. A supported serial DAG can export in dependency order with comments and parameters. Parallel workers, services, loops or advanced event scheduling may require a generated script plus MainAIger runtime. Show that dependency in the export dialog. Large output artifacts remain external with explicit references; a portable ZIP export can include approved artifacts and a manifest.

## 9. Open-source implementation strategy

Build a prebuilt JupyterLab extension and compatible Jupyter Server extension where needed. Notebook 7 uses the same extension foundations; this does not mean every JupyterLab extension automatically provides every Notebook 7 UI integration. Verify the chosen frontend explicitly.

Reuse Jupyter document models, code editors, MIME output rendering and kernel messaging. Add the graph editor around those components instead of reproducing Python completion, widgets, execution ordering and notebook trust from scratch. Evaluate React Flow or an equivalent graph library behind an isolated view component; the current application is vanilla DOM/SVG, so introduce a dependency only with a clear integration/build boundary. Use a layout engine such as ELK for nontrivial automatic layout. Keep dependency licenses and third-party notices.

Potential sources include JupyterLab extension APIs, Jupyter Server, `nbformat`, JupyterLite for a limited browser demo, `jupyterlab-variableInspector` for design/reference, and `jupyter-resource-usage` for resource scope. Variable inspection and telemetry still need MainAIger-specific ownership/freshness controls. Reusing open-source UI code makes widgets easier; execution, lifecycle, data ownership and sandboxing remain substantial engineering work.

## 10. First five integrated milestones

### Milestone 1 — publish the design lab

Commit the static prototype, this plan and the feature matrix to a dedicated web path/repository. Serve through GitHub Pages, configure the custom domain, verify HTTPS and retain a known-good deployment. Start with a non-destructive `/design/` path if the domain already serves content. Verify desktop/mobile navigation, setup walkthrough, notebook/graph animation, dragging, zoom/layout controls, exports and visible demo labels. Success means the `.org` URL serves the reviewed design; it does not mean native Python works.

### Milestone 2 — one real notebook on one device

Add `lab/jupyter_adapter.py`, `lab/experiment_document.py`, `lab/document_schema.json` and a small `jupyterlab_mainaiger/` prebuilt extension package. Connect to one local Jupyter kernel through supported APIs and the existing authenticated runtime. Preserve notebook editing, rich outputs, errors and execution counts. Test a small numerical notebook, interrupt, restart, save/reopen and browser reconnect. Success means actual outputs and host/interpreter identity are observed, with no simulated runtime indicator.

### Milestone 3 — same cells, executable serial graph

Add the graph view, cell/port mapping, partial-run preflight, `.ipymb` serialization and `.ipynb` round-trip. Extend `lab/projects.py` and the adapter boundary in `lab/scheduler.py`; keep serial execution in one kernel. Add proposed `static/lab/graph_view.js`, `notebook_view.js` and `document_store.js` or equivalent extension modules. Test cell-ID preservation, missing producer guidance, stale output badges, aliases, layout-only edits and draft save without Finish. Do not add parallelism yet.

### Milestone 4 — guided installation and reliable lifecycle

Add `lab/setup_steps.py`, `lab/device_capabilities.py` and `static/setup/` for resumable setup operations. Integrate environment discovery, explicit directory creation, trusted-local labeling, service startup, process dashboard and optional tmux. Test repeated setup, invalid interpreter, insufficient path access, browser close/reopen, interrupted cell, stale connection and restart epoch. Distinguish finished workflow from running kernel. Verify on the user's actual laptop before claiming local acceptance.

### Milestone 5 — one child worker and one remote route

Add `lab/artifacts.py`, `lab/subgraphs.py` and explicit child-session invocation; extend current connectors rather than building a new network. First transfer a small JSON result and a small array artifact between parent and child, then between the controller and enrolled server. Add join-all and a bounded structured loop only after the basic transfer succeeds. Test checksums, wrong-session handle rejection, child cancellation, offline target and unknown outcome after connection loss. Advanced recursion, arbitrary custom event logic and broad sandbox provisioning remain later milestones.

Each milestone should land as a reviewable change with a working UI, actual-output checkpoint and focused tests. The feature matrix defines the full proposed scope; not every feature belongs in the first release.

## 11. Deployment and acceptance boundaries

Before writing to the domain, establish its current DNS/HTTPS state, repository ownership, Pages configuration and existing content. Preserve the current site until the new path is verified. A static `CNAME` file alone does not configure DNS or prove certificate issuance. Use GitHub's current Pages instructions and repository settings; do not invent a repository URL or DNS records without verified ownership and target values.

Minimum end-to-end checks: the public site is readable without local runtime; unavailable actions explain the missing companion; private execution requires authentication; arbitrary public origins cannot issue runtime commands; a notebook survives view changes and save/reopen; the same cell produces equivalent outputs in notebook and supported graph execution; closing the browser does not fabricate process completion; and reconnection does not duplicate work.

Security controls should match the implemented boundary. This design does not claim unrestricted local Python is contained by a checkbox, virtual environment or tmux session. A future real sandbox needs filesystem, process, network, resource and escape-boundary testing on each supported OS.

## 12. Primary references

- GitHub Pages creation and static hosting: https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site
- JupyterLab extension development: https://jupyterlab.readthedocs.io/en/stable/extension/extension_dev.html
- Notebook 7 extensions: https://jupyter-notebook.readthedocs.io/en/stable/extending/
- Jupyter Server security: https://jupyter-server.readthedocs.io/en/latest/operators/security.html
- JupyterLite limitations: https://jupyterlite.readthedocs.io/en/stable/troubleshooting.html
- Browser directory picker: https://developer.mozilla.org/en-US/docs/Web/API/Window/showDirectoryPicker
- Python virtual environments: https://docs.python.org/3/library/venv.html
- Tailscale Serve: https://tailscale.com/docs/features/tailscale-serve
- Tailscale subnet routers: https://tailscale.com/docs/features/subnet-routers
- Variable inspector: https://github.com/jupyterlab-contrib/jupyterlab-variableInspector
- Resource usage extension: https://github.com/jupyter-server/jupyter-resource-usage/blob/main/README.md
- Chrome local-network access changes: https://developer.chrome.com/release-notes/142

References support platform constraints and implementation options. The proposed MainAIger behaviors, schema and milestones are design decisions, not claims that upstream tools already implement this complete product.
