# MainAIger vision · as stated by Tanay on 27 October 2026

This file keeps the whole picture in one place, in plain words, with where each
part stands. `ROADMAP.md` orders the work; `INTEGRATION.md` merges the two
codebases; `LOCAL_LAB_SETUP.md` maps features to files. Status words:
**exists** (tested on CI), **partial**, **future**.

## The one-sentence goal

Anyone downloads MainAIger, sets up their own private network of devices
(Tailscale or similar), and then creates and runs experiments quickly by
choosing which code or process uses which device, container, kernel and
resources, while **MainAIger Lab** shows all of that code as a graph of
triggers, devices, compute and kernels that a person can understand and change.

## The parts, in Tanay's words, with status

### Devices and network
- **First download sets up this device; the next devices are discovered,
  installed and get a container copy based on device type** (the device-type
  list is in a Word document uploaded earlier; it is not in this repository
  yet and should be added under `docs/` so the roadmap can refer to it).
  Status: partial. The Mac app installs itself and Docker (exists). Device
  discovery, install-on-another-device and per-type copies: future
  (`ROADMAP.md` C1–C2; the Nexus codebase has the device fabric, see
  `INTEGRATION.md`).
- **MainAIger maintains the Tailscale login and subnet through the GUI.**
  Status: future. The Nexus codebase already observes a tailnet and uses
  Tailscale Serve; the Mac app has nothing yet. Needs: a Devices/Network page
  that shows login state and peers, with every change a reviewed step.
- **Remembers which device you logged in from and shows the GUI for that
  device.** Status: future (the Nexus app records controller sessions; the
  Mac app does not yet).
- **Choose which code/process uses what resources.** Status: partial as of
  today: several sessions per device with their own CPU/memory limits and
  image, and a per-cell "Run on" (exists, `local_lab/sessions.py`). Across
  devices: future (C2–C3).

### MainAIger Lab (the graph of code)
- **Shows code uniquely: triggers, devices, compute, kernels connected.**
  Status: partial. Cells, triggers (control edges), derived data edges,
  sessions and environments are on the canvas (exists). Devices: future.
- **Jupyter-like controls:** kernel panel (exists), variables (exists),
  resources table (exists), files browser (exists, read-only), log (exists),
  terminal (future, as an approved command box only).
- **Visualise and understand any code; eat any repo, see it as a graph, use
  its code for its purpose.** Status: partial. The Nexus codebase's source
  workbench maps modules/functions and import/call edges with edit/diff/test
  (exists there, not in the Mac app). Porting it is `INTEGRATION.md` I4 /
  `ROADMAP.md` B1–B2.
- **Build GUI front-end elements for a repo in a modular way, connecting the
  repo's endpoints to those elements with a page creator or with Lab graph
  code; the user describes in words, the harness uses an LLM to generate.**
  Status: future (B4 plus a "pages" feature: each page is a Mainbook whose
  cells are UI elements bound to endpoints).
- **Website/app pages like Setup; multiple pages and tabs for different
  functions.** Status: exists for Setup, Graph, Models; Devices, Pages,
  Chats and Knowledge are future.

### AI inside MainAIger
- **Avatar animations for the MainAIger "main" agent.** Status: future (the
  Nexus app has an "M" coordinator with states; the Mac app has none).
- **Chats build an Obsidian-like knowledge base and references, so the
  system checks what already exists (with computer automations) before
  regenerating.** Status: future. The Nexus app stores chats and imports;
  the knowledge-base layer (notes with links, search, "already exists?"
  checks) is new work. See `AI_HARNESS.md` for candidate tools.
- **Visualise the organisation of chats and memory.** Status: future (the
  same graph canvas can show notes and links as nodes).
- **LLM/agentic AI can use any other harness or functionality as a
  sub-agent; sub-agents organised and visualised as managers and
  sub-managers; orchestrator/planner models per task; common templates and
  data-specific ones; AI used in small slices.** Status: future. Design in
  `AI_HARNESS.md`: every agent is a Mainbook (cells = steps, triggers =
  hand-offs), so the existing canvas visualises them; a "model cell" calls
  Ollama or an OpenAI-compatible endpoint; sub-agents are nested Mainbooks.
- **LLM creates the graph: reads a repo, maps endpoints to human-level
  features, studies how endpoints are created and what triggers them, draws
  the block diagram; big blocks first, then loops to split each block one
  level deeper each time.** Status: future (B3; depends on B1 for the facts
  and on the model cell for the labels).
- **Each agent visualised, interactable, modifiable, like always-on dots.**
  Status: future (A1 event triggers give "always-on"; the canvas gives the
  dots).
- **Speech to text (AI, automation, or hybrid).** Status: future; candidates
  in `AI_HARNESS.md`.
- **The agentic system thinks and plans *with the human* how to integrate a
  feature, then works toward it slowly with timely check-ins.** Status: this
  is the working method of this project already (plans in `docs/`, approved
  steps, honest labels); automating it is the "self-improvement loop" in
  `AI_HARNESS.md`.
- **Give MainAIger a repo or an idea and have it slowly build up information
  about it, checking in with the human.** Status: future (B3 + A1 timer
  events + the knowledge base).

### Harness and ecosystem
- **Integrate popular open-source harnesses that benefit AI use; the more
  repos, the more ways to use the hardware.** Status: future; shortlist in
  `AI_HARNESS.md`.
- **A "level" of a MainAIger harness based on code, users and compute;
  connect two users' MainAIgers to share compute, memory and workflows.**
  Status: future, after C; needs a trust model (what is shared, with whom,
  revocable).
- **Fine-tune or train open models from chats/memory turned into training
  data, automated.** Status: future; last, after the knowledge base exists.
- **Use Ollama on any device in the network or external APIs (ASU Sol);
  orchestrate prompts in thin slices and turn outputs into decisions, with a
  mix of LLMs and smaller "judge" models.** Status: partial (Ollama chat API
  exists on loopback); the thin-slice pattern is described in `AI_HARNESS.md`.

## The principle that ties it together

Every one of these becomes a Mainbook: cells are steps, arrows are triggers,
each cell says where it runs (device, session, environment) and the journal
says what happened. That is why the canvas, the sessions table, the library,
the environment builder and the journal came first: they are the vocabulary
the rest is written in.
