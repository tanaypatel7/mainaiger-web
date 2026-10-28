# Integration plan: one MainAIger from two codebases

Written 27 October 2026 after reading `MainAIger-Lab-Code.zip` (MainAIger v0.6 +
Lab 0.1, the Nexus-hosted application). This document answers four questions:
how the two codebases relate, how they become one app, how to connect the Mac
app to Nexus, and where new features go from now on. Section 5 is a prompt you
can paste into an AI coding assistant in VS Code on the Mac or on Nexus.

## 1. What each codebase is

| | `mainaiger-web` (this repository) | `MainAIger-Lab` (the archive) |
|---|---|---|
| Where it runs | Mac app (`launcher`), serves `local_lab` on loopback | Nexus (`mainaiger.py serve` broker on 8791/8870), hosts on each device, relays in between |
| Owner sign-in | owner key → cookie + CSRF | owner token → cookie + CSRF (same idea, different file) |
| Execution | one persistent kernel **inside a limited Docker container** (no network, read-only, limits) | persistent Python **host processes** on the enrolled account (`lab/runtime.py`, `lab/host.py`), no sandbox |
| Document | `.ipymb` = Jupyter notebook + graph + layout (`local_lab/graph.py`) | project store: blocks with approved revisions, graphs, layouts (`lab/projects.py`) |
| Scheduler | `graph.plan/preflight`, serial run, join-all (`graph_run.py`) | `lab/scheduler.py`: reviewed run admission, version policies, journal, resume |
| Canvas | `local_lab/ui/graph.js` (fast, data edges derived from code) | `static/lab/lab.js` (SVG graph, block editor, process view) |
| Reuse across Mainbooks | cell library with versioned links (`library.py`) | approved block revisions and policies |
| Packages | per-Mainbook derived Docker image (`environment.py`) | enrolled interpreters (`--python`), no install |
| Devices and network | none yet | **broker ↔ connector ↔ host** over SSH/Tailscale (`lab/broker.py`, `connector.py`, `host.py`), device Canvas, Apps |
| Code as a graph | none yet (roadmap B1) | **source workspace**: copy a tree, AST symbols as nodes, import/call edges, edit copy, diff, syntax check, run tests (`lab/source_workspace.py`) |
| Models | Ollama install + loopback chat API | model services, catalogs, Mac-via-Nexus Ollama tunnel, chats database, Google read-only sync |
| Tests | 161 Python + 28 Node, GitHub Actions, Mac builds | 439 Python + browser fixtures, run by hand |

They are complementary, not duplicates: the Mac app is the better *runtime*
(sandboxed kernel, nbformat documents, library, environments, release pipeline);
the archive is the better *fabric* (devices, relays, approvals, the source
workbench, chats). The target is one app with the Mac app's runtime and document
model and the archive's device fabric and source workbench.

## 2. How they become one app

One repository, `mainaiger-web`, is the place. The archive comes in as a
sibling package, unchanged at first, so its tests keep passing and nothing is
rewritten before it is understood:

```
mainaiger-web/
  local_lab/        the Mac runtime (today)
  nexus/            the archive, copied as is: mainaiger.py, lab/, static/, scripts/, docs/, tests/
  docs/INTEGRATION.md
```

Then four integration steps, each a reviewed change with tests, in this order:

**I1. Devices page in the Mac app (connect).** `local_lab/devices.py` speaks the
archive's broker/host protocol as a *client* (read `nexus/lab/connector.py`
`http_json` and `nexus/lab/host.py` routes; token files, mode 0600, loopback
URLs only). The Mac app lists paired devices, each host's health, interpreters
and projects, through an SSH loopback forward you start by hand (section 4).
Nothing executes yet. This is the first time the two meet.

**I2. One document format (import).** `local_lab/graph.py` gains
`from_lab_project()`: a `nexus/lab/projects.py` project (blocks + one graph +
layout) becomes a `.ipymb` Mainbook; block `parameter` nodes become declared
inputs; version policies are recorded as metadata and reported, not enforced
yet. Round trip tested on the archive's own fixtures. From here the Mac canvas
can *show* a Nexus project.

**I3. Run there.** A session kind `remote-host`: `/api/kernel/start` can target
a device from I1; cells run in that device's host process through the archive's
reviewed run admission (`nexus/lab/scheduler.py` preview → run), outputs come
back into the `.ipymb`. The run dialog names the device, the account and the
interpreter, and says plainly that a host process is **not** a sandbox (the
archive's own words). Docker remains the default on the Mac.

**I4. Code map (roadmap B1) from the source workbench.** Port
`nexus/lab/source_workspace.py` into `local_lab/codemap.py` and show its nodes
(modules, classes, functions, files) and edges (containment, imports, calls) on
the fast canvas; the Mac app's approved-folder rules replace the trial copy
directory. First target: this repository, so you see the Lab as a graph. The
Lab's own route map (`/api/...` → handler → UI action) is added on top, as the
roadmap says.

After I1–I4 the archive's broker, chats and models are folded in the same way
(fabric first, then UI), and `nexus/` shrinks as pieces move into `local_lab/`.
Never both ways at once: a feature lives in exactly one package at any moment.

## 3. Where new features go from now on

1. **One repository, one branch per feature** off `feature/persistent-kernel`
   (or `main` once PR #1 is merged by you). Never commit directly to `main`;
   merging deploys the website.
2. **Runtime code** goes in `local_lab/<feature>.py` with routes in
   `server.py`, UI in `local_lab/ui/`, tests in `tests/test_<feature>.py`, and a
   section in `docs/LOCAL_LAB_SETUP.md` (the feature → file map). Nexus-side
   code stays in `nexus/` until its I-step moves it.
3. **Every change** states what it does, how, why, which files, and labels
   results tested / simulated / unverified / future. Commit messages follow the
   same shape (see today's `git log`).
4. **Before each commit**: `python3 -m unittest discover -s tests -p 'test_*.py'`
   and `node --test tests/*.test.js` green; for `nexus/`, also
   `python3 -m unittest discover -s nexus/tests/test_lab -p 'test_*.py'`.
5. **Acceptance** = CI green **and** the step tried on the real device it is
   for (your Mac, Nexus). CI proves the code; your device proves the product.

Where to paste a prompt: open the repository folder in VS Code on the machine
whose files the task touches (Mac for `local_lab/` and the app; Nexus for
anything that must run there), start the AI coding assistant in that window,
paste the prompt in section 5. Code never travels by chat: it goes in through
commits on a feature branch and comes out through `git pull` on the other
machine. Tokens and keys never go into prompts, chats or commits.

## 4. Connect to the second device (Nexus) today, without new code

This uses only the archive and your existing SSH configuration between the Mac
and Nexus. It is the archive's own §6 of `docs/lab/RUN.md`, reduced to the case
"run on Nexus, look from the Mac". Replace `NEXUS` with your SSH alias for the
Nexus host (`ssh NEXUS` must already work). Nothing here touches Tailscale
Serve, DNS, systemd services or your existing `mainaiger-v03` data.

On **Nexus** (over `ssh NEXUS`):

```bash
mkdir -p ~/src && cd ~/src
unzip ~/MainAIger-Lab-Code.zip -d ~/src        # copy the zip there first (scp)
cd ~/src/MainAIger-Lab
python3 --version                                 # needs 3.11+
python3 -m unittest discover -s tests/test_lab -p 'test_*.py'
python3 scripts/run_lab_trial.py --home ~/MainAIger-Lab-Trial --port 8870 --host-port 8792
```

The trial starter prints the `/lab` URL and the path of the owner-token file.
Keep that terminal open; Ctrl-C stops the trial. Read the token with
`cat <printed path>` on Nexus when the browser asks for it; do not paste it
anywhere else.

On the **Mac**, forward the Nexus trial port to your loopback and open it:

```bash
ssh -a -N -T -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
    -L 127.0.0.1:8870:127.0.0.1:8870 NEXUS
```

Then open http://127.0.0.1:8870/lab in the Mac browser, sign in with the Nexus
trial owner token, and follow `docs/lab/RUN.md` §4 (Initialize → Increment
graph, expect `42`). Cells run **on Nexus**, in the trial host's Python, under
your Nexus account. This proves the device link before any integration code.

What this does not do yet: it is the archive's UI, not the Mac app's canvas;
the Mac app is not involved. Steps I1–I3 above are what make the Mac app the
one window onto both.

## 5. The prompt to paste (Mac or Nexus, VS Code AI assistant)

Copy everything between the lines.

---

You are working on MainAIger, a local-first, graph-based coding environment
built by Tanay Patel (a beginner developer). Code cells are nodes, triggers are
arrows. Two codebases must become one app. Read these first, in this order:
`docs/INTEGRATION.md` (this plan), `docs/ROADMAP.md`, `docs/LOCAL_LAB_SETUP.md`
(feature → file map), `docs/SESSION_2026-10-27.md`, then
`nexus/README.md`, `nexus/docs/lab/RUN.md`, `nexus/docs/ARCHITECTURE.md`.

Working rules, all of them, every time:
- Build while teaching: before and after each change explain in plain language
  what it does, how, why, and which files; map every feature to files.
- Work in small integrated steps; each step has tests that run in CI.
- Label every result honestly: tested / simulated / unverified / future.
- Ask before changing the agreed architecture (owner key, loopback only,
  Docker session limits, every system-touching operation a fixed, visible,
  approved step, `.ipymb` as the document, local-only use without a website
  account). Do not stop at a plan once a step is agreed; build it.
- Never merge, tag, deploy, change DNS, or touch Supabase, Resend, Cloudflare,
  Tailscale Serve, systemd services, or the existing `mainaiger-v03` data.
  Never put tokens, keys or token file contents in code, chat or commits.
- Never curse.
- Before every commit run `python3 -m unittest discover -s tests -p 'test_*.py'`,
  `node --test tests/*.test.js`, and
  `python3 -m unittest discover -s nexus/tests/test_lab -p 'test_*.py'`; all
  must pass. Commit as `Tanay Patel <tanaypatel7@users.noreply.github.com>` on
  the feature branch with a message that says what/how/why.

Setup (do this first if not done): in `mainaiger-web`, `git checkout
feature/persistent-kernel && git checkout -b feature/nexus-integration`. Copy
the extracted `MainAIger-Lab` directory into `nexus/` unchanged (no tokens, no
trial data; verify `nexus/SHA256SUMS` against the files). Run its test suite
from the repository root with the command above and record the numbers. Add
one paragraph to `docs/INTEGRATION.md` under "Status" with the result. Commit.

Then do step I1 from `docs/INTEGRATION.md` section 2, and only I1:
`local_lab/devices.py`, a client for the archive's broker/host protocol (study
`nexus/lab/connector.py` and `nexus/lab/host.py`; reuse their token-file rules:
owner-owned regular file, mode 0600, no symlinks; loopback or `https://` URLs
only). Add `GET /api/devices` and `POST /api/devices/pair {name, broker_url,
token_file, confirm}` to `local_lab/server.py` (owner session + CSRF like every
other route), a Devices page at `/devices` in `local_lab/ui/` matching the
existing pages' structure and style, and `tests/test_devices.py` that runs a
real archive host and broker in-process (see how `nexus/tests/test_lab/
test_transport.py` does it) and checks: unpaired → empty list; pair with a bad
token file → refused; pair → the device, its host health, interpreters and
projects appear; a stopped host → shown as unavailable, never guessed. Document
the step in `docs/LOCAL_LAB_SETUP.md` ("Devices") with feature → file lines and
honest labels. Commit and push the branch. Then stop and report: what works,
what is unverified (the real Nexus is not reachable from CI), and the exact
manual check Tanay should run on his Mac against Nexus (section 4 of
`docs/INTEGRATION.md` for the tunnel). Do not start I2 until Tanay says so.

---

## Status

- 27 Oct 2026: plan written; the archive has been read, not yet copied into
  this repository. Nothing in `nexus/` exists yet. The section-4 Nexus trial is
  the archive's own documented procedure and is unverified by me on your
  devices.
