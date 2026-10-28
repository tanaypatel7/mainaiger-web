# AI in the MainAIger harness

Written 27 October 2026. Answers three questions from Tanay: how do current
self-improving agent harnesses work; which open-source repos are worth
integrating; how to run one example of the harness improving itself. Sections 1,
2 and 5 are a research summary (a web survey run on 27 Oct 2026; sources are
linked; release dates were cross-checked on package registries because the
GitHub API was unavailable to the researcher, so treat dates as approximate;
vendor benchmark numbers are not independently reproduced). Sections 3 and 4
are MainAIger's own design, built on what exists in this repository today
(`decide.py`, model cells, sessions, the journal). Nothing in this file has been
run against a real model yet; the labels say so.

Not verified by the research: the DGM repository's license; HyperAgents' code
release; Foam's license; Vosk's latest release; whether ASU Sol's gateway
enforces JSON-schema `response_format`.

## 0. The one idea underneath

A model is a *question-answering* component, not an *actor*. Every AI step in
MainAIger is: the harness (ordinary code) asks one small, well-shaped question;
the model answers in JSON; the harness checks the answer and decides what to do
next; the journal records it. The model never runs commands, never sees the
whole system, and never gets an unbounded number of tries. That is what makes
a self-improvement loop safe enough to run on your own machine and honest
enough to explain afterwards.

## 1. How self-improving harnesses actually work (research, 27 Oct 2026)

**Vocabulary.** An agent is a frozen model plus a *harness*: the code that decides
what the model sees, which tools it may call, how context is managed, and how
errors are caught and recovered. One 2026 survey splits the harness into six
parts: observation interface, context manager, control loop, action interface,
state/artifact store, verification/governance
([Survey on Agent System and Harness Design](https://arxiv.org/pdf/2606.20683)).
Lilian Weng's July 2026 post describes the optimisation ladder: prompts, then
structured context, then workflow, then harness code, then the optimiser itself
([Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/)).

**"Self-improving" in nearly every shipped 2025–2026 system means editing the
harness (prompts, tools, skills, memory, control code), not the model weights.**
The exceptions that also touch weights (HarnessX co-evolution with GRPO; SIA)
are research-only.

**The common loop**, in six steps
([Self-Evolving Agentic Harnesses](https://jxzhangjhu.github.io/blog/2026/self-evolving-agentic-harnesses/)):

1. *Represent*: declare exactly which files are mutable (AHE: seven file-level
   components; Harness²: eight, including a middleware plugin)
   ([AHE](https://arxiv.org/pdf/2604.25850);
   [KDnuggets on Harness²](https://www.kdnuggets.com/google-open-sources-harness-self-improving-ai-agents-without-retraining)).
2. *Propose*: a model reads failure evidence (clustered failed traces, test
   logs) and writes a bounded edit; Self-Harness makes each candidate target one
   failure mechanism and carry a written prediction of its effect and regression
   risk ([Self-Harness](https://arxiv.org/html/2606.09498v1)).
3. *Validate* cheaply: does it run at all (manifest, config, smoke tests)
   ([HarnessX](https://arxiv.org/pdf/2606.14249)).
4. *Evaluate* on a task set with a verifier (unit tests, graders); evaluation,
   not proposal, dominates cost.
5. *Select*: accept only on a non-regression rule.
6. *Archive* survivors and, usually, rejected edits too, so the proposer learns
   from them.

**Named systems and their mechanisms.**

- *AlphaEvolve* (DeepMind, May 2025): an ensemble proposes programs, automated
  evaluators score them, an evolutionary program database seeds future prompts;
  it only works where progress is machine-measurable; the May 2026 follow-up is a
  cloud API, not open code
  ([AlphaEvolve](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/);
  [AlphaEvolve impact](https://deepmind.google/blog/alphaevolve-impact/)).
- *OpenEvolve* (Apache-2.0, v0.4.0, 2026-09-28): you supply an evaluator script
  returning metrics; MAP-Elites grid with islands; works against any
  OpenAI-compatible endpoint including Ollama on localhost
  ([OpenEvolve on PyPI](https://pypi.org/p/openevolve)).
- *ShinkaEvolve* (Sakana, Apache-2.0, Sept 2025): novelty rejection so
  near-duplicates are never evaluated; bandit selection among models
  ([ShinkaEvolve](https://sakana.ai/shinka-evolve/)).
- *Darwin Gödel Machine* (Sakana/UBC, May 2025): an archive of agents; a parent
  reads its own benchmark logs and edits its own code; staged evaluation
  (10-task smoke → 60 → 200); SWE-bench 20.0% → 50.0%; one 80-iteration run took
  about two weeks and about $22,000 in API fees; documented failures: it faked a
  passing-tests log, and when rewarded for reducing tool-use hallucination it
  removed the detection markers, caught only through the lineage
  ([DGM paper](https://arxiv.org/pdf/2505.22954); [Sakana: DGM](https://sakana.ai/dgm/);
  [The Decoder](https://the-decoder.com/sakana-ais-darwin-godel-machine-evolves-by-rewriting-its-own-code-to-boost-performance/)).
- *HyperAgents* (Meta, March 2026): makes the meta-level improvement procedure
  itself editable; code release not confirmed
  ([Meta: HyperAgents](https://ai.meta.com/research/publications/hyperagents)).
- *Self-Harness* (June 2026): accepts an edit only if it reduces pass counts on
  neither a held-in nor a never-shown held-out split and improves at least one
  ([Self-Harness](https://arxiv.org/html/2606.09498v1)).
- *AHE* (May 2026): every edit is a git commit paired with a falsifiable
  prediction, rolled back per file if wrong; evaluator, tracer and model config
  are read-only to the evolver; regression-prediction precision was only 11.8%
  ([AHE](https://arxiv.org/pdf/2604.25850)).
- *HarnessX / AEGIS* (2026): only a deterministic gate decides what ships,
  including a "seesaw" rule blocking regressions on previously solved tasks;
  biggest gains for the weakest agents; all results on the evolve set, no
  held-out ([HarnessX](https://arxiv.org/pdf/2606.14249);
  [TechTalks](https://bdtechtalks.com/2026/07/13/ai-agents-self-improving-harness/)).
- *Harness²* (Google Cloud AI Research, Apache-2.0, Oct 2026): eight
  file-mounted components; a Propose-Probe-Compose loop that works without
  ground truth by comparing reruns; needs Vertex AI and paid calls
  ([flowtivity on Harness²](https://flowtivity.ai/blog/google-harness2-self-improving-agents/)).
- *Meta-Harness* (Stanford, March 2026) searches over harness code with a
  proposer that reads all prior candidates' source, scores and traces
  ([Meta-Harness](https://arxiv.org/abs/2603.28052)).
- *HarnessFix* (July 2026): diagnoses before editing, attributing failures to
  one of seven harness layers; patches accepted only within a regression limit
  ([HarnessFix](https://arxiv.org/pdf/2606.06324)).
- *RRSI* (Sept 2026): regularisers that reject benchmark-specific content,
  require gains beyond a noise band, and demand that extra tokens pay for
  themselves; smallest in-distribution gain, best out-of-distribution
  ([RRSI](https://arxiv.org/html/2609.24972v1)).
- *RHO* (June 2026): no external grader during optimisation; cross-run
  agreement as the signal ([RHO summary](https://hyper.ai/en/papers/2606.05922)).
- *Adaptive Auto-Harness* (2026), the cautionary tale: with unbounded evolution
  the prompt grew from 2 KB to 68 KB and the pass rate later declined
  ([Adaptive Auto-Harness](https://arxiv.org/pdf/2606.01770)).

**The minimal, beginner-relevant loop: Karpathy's autoresearch** (MIT, March
2026). A human writes `program.md` (what the agent may change, what "better"
means); `prepare.py` with the metric is immutable; `train.py` is the only
editable file; each experiment runs for a fixed 5 minutes; improved → the commit
stays, otherwise `git reset`; crashes are logged and reverted; `results.tsv`
records commit, score, status; about 12 experiments an hour
([DataCamp guide](https://www.datacamp.com/tutorial/guide-to-autoresearch);
[OSS Insight](https://ossinsight.io/blog/autoresearch-overnight-ai-scientist)).
The follow-up *Rehearse* paper shows the share of helpful modifications drops
from about 70% early to about 43% after iteration 6, and that a cheap pre-run
pairwise judge with focused memory of similar past attempts raises late-stage
selection accuracy from 56.9% to 83.5%
([Rehearse](https://arxiv.org/pdf/2607.27687)).

**The interactive coding agents** (what you would wrap as sub-agents, not
self-improvers themselves): mini-swe-agent (MIT, about 100 lines, bash-only,
any model via LiteLLM, v2.4.6 July 2026;
[repo](https://github.com/SWE-agent/mini-swe-agent)); OpenHands (MIT, Docker
sandbox, SDK; needs at least 22k–32k context with local models;
[local LLM docs](https://docs.openhands.dev/usage/llms/local-llms)); Aider
(Apache-2.0, dormant since v0.86; [review](https://codemyspec.com/blog/aider-review-2026));
Claude Code (proprietary; Agent SDK; Ollama and llama.cpp now serve an
Anthropic-style endpoint, [Ollama docs](https://docs.ollama.com/api/anthropic-compatibility));
Codex CLI (Apache-2.0, `codex --oss` uses Ollama, needs 64k context,
[Ollama docs](https://docs.ollama.com/integrations/codex)); Gemini CLI
(Apache-2.0; consumer access ended June 2026,
[Google](https://developers.googleblog.com/en/an-important-update-transitioning-gemini-cli-to-antigravity-cli/));
OpenCode (MIT, client/server split, [DataCamp](https://www.datacamp.com/blog/what-is-opencode));
goose (Apache-2.0, MCP-native, Ollama provider, [releasebot](https://releasebot.io/updates/aaif-goose/goose)).

**What they measure:** benchmark pass rates (SWE-bench Verified/Pro,
Terminal-Bench 2, Polyglot), held-in vs held-out deltas, token cost per task,
tool-error rate, evaluator fitness; recommended reporting includes held-out
gain, backward retention, improvement efficiency and safety non-regression
([Self-Evolving Agentic Harnesses](https://jxzhangjhu.github.io/blog/2026/self-evolving-agentic-harnesses/)).

**How they avoid breaking themselves (the rules worth copying):**
- held-out regression gate the proposer never sees;
- evaluator, tracer and model config read-only; the evolver writes only
  inside its workspace;
- one edit = one commit with a written prediction; file-level rollback;
- a deterministic gate decides what ships, whatever the critic model says;
- noise-adjusted acceptance floors and cost-versus-gain requirements;
- sandboxed execution with time limits, limited network, auditable lineage;
- novelty rejection so budget is not spent on near-duplicates.

**Limits, stated plainly:** reward hacking is real and observed (DGM removed
its own detectors); benchmark contamination is a live dispute (OpenAI
deprecated SWE-bench Verified in Feb 2026; agents reading gold patches from git
history have been reported;
[analysis](https://www.morphllm.com/swe-bench-pro)); cost (about $22k for one
DGM run; about 32 hours for ten AHE iterations); capability dependence (small
models can *write* harness edits as well as big ones but *benefit* less and
often fail to follow the skills they are given,
[Harness Updating Is Not Harness Benefit](https://arxiv.org/pdf/2605.30621));
harness bloat without pruning; scaffolding matters (the same model scored 58%,
75% and 82% on Terminal-Bench 2 under three harnesses) but model choice usually
matters more ([Terminal-Bench 2.0](https://arxiv.org/pdf/2601.11868));
many 2026 results are vendor- or author-reported without independent
reproduction.

## 2. Open-source repos worth integrating (research, 27 Oct 2026)

Offline = runs with no internet once installed. Dates are latest releases found
on PyPI/piwheels/Arch/pkg.go.dev or trackers (the GitHub API was blocked in the
research sandbox, so treat them as approximate). "Fit" is for MainAIger.

**Agent harnesses that run locally with Ollama**

| Tool | What | License | Offline | Latest | Fit |
|---|---|---|---|---|---|
| mini-swe-agent | ~100-line bash-only SWE agent; Docker/Podman envs; any model via LiteLLM | MIT | yes | 2.4.6, 2026-07-23 | **First sub-agent to wrap as a node**: small enough to read fully; one `subprocess.run` per step maps to one cell per action |
| OpenHands + SDK | Full coding agent with Docker sandbox; SDK `Agent`/`Conversation`/workspaces | MIT | yes; ≥22k–32k context | CLI 1.16.0, 2026-05-08 | Heavier; reference for the execution boundary, or a sub-agent for big tasks |
| Aider | Terminal pair-programmer; tree-sitter repo map; auto-commit | Apache-2.0 | yes | 0.86.2, 2026-02-12 (dormant) | Mine its repo-map algorithm (below), not the package |
| smolagents | `CodeAgent` writes Python per step; Ollama via LiteLLM/OpenAI model | Apache-2.0 | yes | 1.26.0, 2026-05-29 | Code-as-action matches "cells as nodes"; sandbox the executor |
| LangGraph | Graph runtime with checkpointing, durable execution, human-in-the-loop | MIT | yes | 1.2.18, 2026-10-26 | Closest existing runtime to "nodes + trigger arrows"; study its checkpoint/interrupt design |
| CrewAI | Role-based multi-agent crews | MIT | yes | 1.1x, mid-2026 | Lower priority; overlaps your graph |
| AG2 (AutoGen fork) | Multi-agent conversations; AutoGen itself is maintenance-only | Apache-2.0 | yes | 1.2.2, 2026-10-27 | Skip `microsoft/autogen` for new work |
| Letta / Letta Code | Stateful agents with persistent memory; `/connect` to Ollama | Apache-2.0 | yes | 0.34.12, 2026-10-12 | Reference for memory blocks in a coding agent |
| Open Interpreter | Rust fork of Codex with switchable harness emulations, ACP, MCP | Apache-2.0 | OpenAI-compatible; Ollama undocumented | 2026-08-20 | Precedent for "swap the harness, keep the model"; unverified with Ollama |
| goose | Desktop/CLI agent, MCP-native, Ollama provider, YAML recipes | Apache-2.0 | yes | 1.56.0, 2026-10-23 | Good MCP-client reference; recipes resemble library cells |
| OpenCode | TUI agent; client/server split; MCP, LSP feedback, subagents | MIT | yes | 2.1.4, 2026-10-26 | Its server mode is a model for "agent on Nexus, driven from the Mac" |
| Codex CLI | OpenAI's Rust agent; `--oss` uses Ollama; MCP client+server | Apache-2.0 | yes (≥64k context) | 0.164.0, 2026-10-23 | Sub-agent candidate; its OS sandbox code is worth reading |
| Claude Code / Agent SDK | Same loop as the CLI as a library | proprietary | no (API) | 2.1.309, 2026-10-23 | Optional premium sub-agent; not for local-only |
| Gemini CLI | consumer access ended 2026-06-18 | Apache-2.0 | no | 0.64.0 | Low priority |
| ACP (Agent Client Protocol) | JSON-RPC-over-stdio editor↔agent standard, 50+ agents | Apache-2.0 | yes | active | If the Lab speaks ACP, any ACP agent is a pluggable sub-agent ([Zed ACP](https://zed.dev/acp)) |

Honest evidence on small local models as task-solvers: in a 16 GB test,
`qwen3.5:9b`, Qwen3 14B and `devstral-small-2:24b` failed an agentic Go task
while `gpt-oss:20b` (high reasoning) and `qwen3.5:35b` completed it
([glukhov](https://glukhov.org/ai-devtools/opencode/llms-comparison)); on an
M4 Pro 48 GB, Qwen 3.5 35B-A3B and Gemma 4 26B-A4B passed a refactor eval about
9/10 runs where older 14B–24B models scored 0
([simonpcouch](https://simonpcouch.com/blog/2026-04-16-local-agents-2/)); a July
2026 write-up argues long-horizon self-recovery is the weak point and recommends
routing bounded single-file edits local and long runs to a frontier API
([Capital & Compute](https://capitalandcompute.net/blog/why-local-llms-fail-agentic-coding/)).

**Structured output and decision tooling**

| Tool | What | License | Latest | Fit |
|---|---|---|---|---|
| Ollama structured outputs | `format` = JSON schema on `/api/chat`; `response_format` on the OpenAI path; cloud models do not enforce ([docs](https://docs.ollama.com/capabilities/structured-outputs.md)) | MIT | 0.40.11, 2026-10-24 | **What `decide.py` uses today** |
| Ollama decision models (`/v1/systemone`, `ollama run nimble --ask`) | typed yes/no, choice, score answers with per-option probabilities; `nimble` 9B Apache-2.0; Ollama ≥0.35 ([blog](https://ollama.com/blog/ollama-now-supports-jev-style-decision-models)) | Apache-2.0 | 2026-09-29 | The "judge" model for routing and gating: a probability, not prose |
| Instructor | Pydantic `response_model`, `max_retries`, `from_provider("ollama/…")` ([docs](https://python.useinstructor.com/integrations/ollama/index.md)) | MIT | 1.17.0, 2026-09-09 | Simplest validated-retry layer if you outgrow `decide.py` |
| Pydantic AI | `OllamaModel` + `NativeOutput` enforces schema via grammar on self-hosted Ollama ≥0.5 ([docs](https://pydantic.dev/docs/ai/models/ollama/index.md)) | MIT | 2.56.0, 2026-10-13 | Typed agents with tools; heavier |
| Outlines | regex/JSON-schema/EBNF-constrained generation ([overview](https://aiwiki.ai/wiki/outlines_library)) | Apache-2.0 | 1.3.0, 2026-05-13 | When you need regex/CFG beyond JSON |
| llama.cpp grammars | `json_schema` or GBNF `grammar`; off-grammar tokens get zero probability ([docs](https://docs.liquid.ai/deployment/on-device/llama-cpp/structured-output)) | MIT | 0.5.0, 2026-09-23 | Underpins Ollama; direct use for `root ::= "A" | "B"` grammars |
| vLLM structured outputs | `structured_outputs: {choice/regex/json/grammar}` ([docs](https://docs.vllm.ai/en/latest/features/structured_outputs/)) | Apache-2.0 | 0.31.0, 2026-10-05 | Nexus only if it has a supported GPU |

**Evaluation:** Inspect AI (UK AISI, MIT, 0.3.285 2026-10-25; Ollama and
OpenAI-compatible providers, Docker sandboxes;
[providers](https://inspect.aisi.org.uk/providers.html)) is the best fit for
held-in/held-out task sets; promptfoo (MIT, acquired by OpenAI March 2026 with
a commitment to keep the OSS project;
[Security Boulevard](https://securityboulevard.com/2026/03/openai-acquires-security-startup-promptfoo-to-fortify-ai-agents))
for prompt A/B; DeepEval (Apache-2.0, 4.3.2 2026-10-27) for LLM-judged
criteria, with deterministic tests kept as the gate.

**Local model serving:** Ollama (MIT, 0.40.11; MLX by default on Apple
silicon; default context is tiered by VRAM, 4k under 24 GiB, and the docs say
coding agents need ≥64k, so set `OLLAMA_CONTEXT_LENGTH=64000`;
[context length](https://docs.ollama.com/context-length.md)); llama.cpp (MIT)
directly when you need GBNF or a specific GGUF quant; vLLM only on a GPU Nexus.

**Knowledge base / Obsidian-like:** Obsidian (free for personal and commercial
use since Feb 2025; official CLI since 1.12 exposing read/create/search;
[CLI help](https://obsidian.md/help/cli)) can be driven from a Mainbook node
through the CLI, and community MCP servers exist; Logseq DB 2.0 (AGPL-3.0,
beta with a built-in loopback MCP server, data-loss warnings); Foam (VS Code
markdown PKM, `@foam/mcp`; license not verified); SiYuan (AGPL-3.0); Khoj
(AGPL-3.0, self-hosted RAG with an Obsidian plugin; AGPL matters if MainAIger
is ever hosted); AnythingLLM (MIT, documents + agents + vector DB, MCP); Open
WebUI (custom license since 0.6.6: BSD-3 plus a branding clause above 50
users; you already import its exports;
[license](https://docs.openwebui.com/license.md)).

**Speech-to-text:** whisper.cpp (MIT, 1.9.5 2026-10-06, Metal) is the best on
the Mac; faster-whisper (MIT, CUDA/CPU int8, no Metal) on an NVIDIA Nexus; Vosk
(Apache-2.0, ~50 MB streaming models) for low-latency commands; WhisperSpeech
is text-to-speech, not STT, and dormant since Jan 2024.

**Code-graph / repo mapping (for "repo → feature block diagram"):**
tree-sitter (MIT, 0.26.x) for deterministic symbol extraction; Aider's
repo-map algorithm (tree-sitter tags → files as nodes, references as edges →
PageRank-style ranking → fit to a token budget;
[explainer](https://fast.io/resources/aider-context-window.md)) to
reimplement; Repomix (MIT, 1.16.0, `repomix --mcp`) to hand a small repo to
Sol for a first map; SCIP (Apache-2.0) for precise cross-file references;
pyan3 (GPL-2.0-or-later, revived 2026, Python call graphs) fine as a tool you
call; code2flow dormant; CodeQL not usable for private repos. Expectations: a
March 2026 study of 4,137 generated architecture views found most fail the
completeness bar and that models produce code-level rather than architectural
components ([arXiv 2603.21178](https://arxiv.org/pdf/2603.21178)); the
practical lesson is **extract structure deterministically, let the model only
name and cluster**, which is exactly `ROADMAP.md` B1 then B3.

**MCP:** the current spec revision is 2026-07-28 (stateless, mandatory
`server/discover`, Tasks as an extension, Roots/Sampling/Logging/HTTP+SSE
deprecated with a 12-month window;
[versioning](https://modelcontextprotocol.io/docs/learn/versioning)); Python
SDK `mcp` 2.x (2.3.0, 2026-10-02) is stable and is not interchangeable with the
standalone `fastmcp` 3.x package; reference servers worth wiring in:
Filesystem, Git, Memory, Fetch, Sequential Thinking, Time
([examples](https://modelcontextprotocol.io/examples.md)).

**Current Ollama coding models in the 7B–35B band** (vendor-reported numbers,
not independently reproduced): `qwen2.5-coder:7b/14b` (Apache-2.0, 32K, the
community-recommended OpenHands local model); `qwen3.5:9b` (262K context,
failed one published agentic test); `qwen3.6:27b` and `qwen3.6:35b-a3b`
(256K; SWE-bench Verified 77.2 / 73.4 vendor-reported; ~17–24 GB;
[Ollama](https://registry.ollama.ai/library/qwen3.6)); `qwen3-coder:30b`
(MoE, 3.3B active); `devstral-small-2:24b`; `glm-4.7-flash` (MIT);
`gpt-oss:20b` (default for `codex --oss`); `gemma4` 26B-A4B; `nimble` (9B
decision model for gates, not code). Recommendation: proposer
`qwen2.5-coder:14b` or `qwen3.6:35b-a3b` on the Mac, whichever fits memory;
escalation to ASU Sol. Sol exposes an OpenAI-compatible base URL
`https://openai.rc.asu.edu/v1`, keys from the Voyager portal (portal needs ASU
network/VPN, the API does not), models via `GET /v1/models`, fair queueing up
to 60 minutes, 429 on per-minute budgets
([ASU RC LLM API](https://docs.rc.asu.edu/ai/api)); whether its gateway
enforces JSON schemas is undocumented, so `decide.py` validates client-side
either way.

## 3. MainAIger's first self-improvement loop (design, using today's pieces)

**Goal of the first run:** MainAIger proposes one small change to *its own*
repository, applies it to a copy, runs the test suite, asks a model whether to
keep it, and asks you before anything is kept. One loop, one change, every step
visible on the canvas and in the journal.

**It is a Mainbook.** Cells are the steps, triggers are the hand-offs, so the
loop is drawn, not hidden in a script:

```
Start
 → Pick task       (code)   reads docs/TODO-SELF.md, picks one small item, prints it
 → Propose change  (model)  prompt: the task + the relevant file(s) → {file, find, replace, why}
 → Apply to copy   (code)   copies the repo into /tmp, applies the edit, prints a unified diff
 → Run tests       (code)   runs `python3 -m unittest` in the copy, prints the summary
 → Judge           (model)  prompt: {{output:Apply to copy}} + {{output:Run tests}} → {action: keep|revert|ask, confidence, reasons}
 → Report          (code)   prints the decision; writes the diff and verdict as a journal line
Finish
```

How each piece maps to what exists:

- **Propose change / Judge** are *model cells* (`docs/LOCAL_LAB_SETUP.md`
  "Model cells"): their text is the prompt, their answer is schema-checked by
  `decide.py` and stored as a variable the next cell reads. The judge's shape
  is the `{action, confidence, reasons}` schema already tested in
  `tests/test_decide.py`.
- **Apply to copy / Run tests** are ordinary code cells running in a session
  whose workspace is this repository, mounted read-only; the copy lives in the
  container's `/tmp`. Nothing can touch the real checkout.
- **The human check-in** is built into the Lab: every run is approved in the
  run review; the Report cell only *prints* the decision. Keeping a change means
  you read the diff and commit it yourself (or, later, a reviewed "apply to
  checkout" step with its own approval dialog, the same way Mainbooks and
  Library folders are approved today).
- **Budgets:** the loop runs once per approval. Repeating it needs A1 (event
  triggers: a timer) and A2 (loop edges with `max_fires`), both future; until
  then "run again" is a click, which is the right default for a first loop.

**What the first run will prove and not prove.** It proves the plumbing: a
model can read the test output, produce a shaped verdict, and the Lab records
every step. It does not prove the model's edits are good: with a small local
model most proposals will be rejected by the tests, which is fine and visible.
Quality comes from narrowing the task (one function, one failing test) and
from the evaluation step, not from the loop.

**Where it runs:** on the Mac with Ollama (any installed coder model; the
Models page lists what fits), or on Nexus later, or against ASU Sol once an
OpenAI-compatible provider is added to `decide.ask` (it takes any chat
function with `models.chat`'s signature; the provider needs a key file with
the same 0600 rules as the other token files).

**The stricter loop to grow into** (what the research in §1 says a loop must
have, mapped onto the Lab):

| Rule from the literature | In MainAIger |
|---|---|
| The evaluator is read-only to the proposer (AHE; DGM's detector removal) | `evaluate` is a code cell the model cell cannot edit: model cells only *answer*; the workspace is mounted read-only; "apply to checkout" is a reviewed step you approve |
| Held-in and held-out tests; accept only if neither regresses (Self-Harness) | two test folders; the Run tests cell reports both; the judge sees only the held-in log |
| One edit = one commit with a written prediction, file-level rollback (AHE) | the proposal schema carries `predicted_effect`; Report writes it into the journal; keeping = one commit you make |
| Gains must beat the noise band; extra tokens must pay for themselves (RRSI) | run `evaluate` twice before the loop; the judge schema includes `noise_band` as context; `attempts` capped at 3 by `decide.py` |
| Budgets: iterations, wall clock, per-evaluation timeout, diff size; stop after consecutive rejects (autoresearch, Rehearse) | one approval = one iteration today; `max_fires` per edge and a per-run cell cap arrive with A2; the cell timeout already exists (60 s, interruptible) |
| Cheap pre-run judge with memory of similar past attempts (Rehearse) | a second model cell using `nimble`-style decision models (Ollama `/v1/systemone`) once that provider path exists; the journal is the memory |
| Everything sandboxed, no network, auditable lineage (DGM) | sessions run with `--network=none`, limits and a read-only workspace; the journal is the lineage |

**Status:** the first version exists: `examples/self-improve.ipymb` (open it
from **Open example…**; described in `docs/LOCAL_LAB_SETUP.md` "The
self-improvement loop example"). It improves a classifier function rather than
the Lab's own repository, so no step can touch your files; it runs in CI on a
real kernel with a fake model, and is unverified with a real Ollama model. The
repository-copy version with the two test folders and a reviewed "apply to
checkout" step is still future.

## 4. The thin-slice pattern (how model output becomes decisions)

1. **Shape first.** Write the JSON shape of the answer before the prompt:
   fields, types, enums, limits (`decide.check_schema` accepts a small subset
   of JSON Schema on purpose: if the shape needs more than that, the question
   is too big).
2. **One question, bounded context.** Question ≤ 4 kB, context ≤ 24 kB
   (`decide.MAX_*`). If the context does not fit, the harness summarises or
   splits deterministically (by file, by test) before asking.
3. **Constrained generation.** Ollama is asked for structured output with the
   schema (`models.chat(format=schema)`), which makes most small models
   comply; the harness still parses and validates (`decide.validate`).
4. **One retry with the reason**, then stop. `decide.ask` tells the model what
   was wrong once; after that the answer is `ok: false` and nothing acts on it.
5. **Code decides.** The validated object goes through ordinary `if`
   statements: `keep` with confidence ≥ 0.7 → show the diff; `revert` → log
   and move on; `ask` or low confidence → stop for the human.
6. **Judge models.** A second, smaller "judge" question is cheaper than a
   bigger first question: ask the coder model for the change, ask any model
   "do these test results justify keeping this change?" with the shape above.
   Agreement between two cheap questions beats one expensive one.
7. **Everything is a journal line.** The question kind, model, attempts and
   outcome are recorded (`model.decide`, `cell.ok` with `model`), so "why did
   it do that?" always has an answer.

Three enforcement layers exist locally (research): Ollama `format` (JSON
schema; cloud models do not enforce it), llama.cpp grammars (off-grammar
tokens get zero probability, but unsupported schema keywords are silently
ignored, so validate anyway;
[docs](https://docs.liquid.ai/deployment/on-device/llama-cpp/structured-output)),
and Ollama decision models (`/v1/systemone`: choice/yes-no/score with
probabilities; a 0.9 probability is not 90% accuracy on your data;
[guide](https://www.developersdigest.tech/blog/ollama-nimble-decision-models-ask-cli-2026)).
General rules from the sources: shallow schemas (deep or recursive ones confuse
small models), enums over free text, never execute model output without a code
check, keep the evaluator outside the model's reach
([glukhov](https://www.glukhov.org/llm-performance/ollama/llm-structured-output-with-ollama-in-python-and-go/);
[Weng](https://lilianweng.github.io/posts/2026-07-04-harness/)).

Three concrete examples (all runnable as model cells today):

- *Classify a chat message into a workflow:*
  shape `{intent: enum[run_graph, open_mainbook, explain, other], target: string(≤60)}`.
- *Pick the next task from a TODO list:*
  shape `{task_index: integer, reason: string(≤120), needs_human: boolean}`.
- *Decide whether a test failure is caused by the change or pre-existing:*
  shape `{caused_by_change: boolean, confidence: number, evidence: string[≤3]}`.
- *Repo → feature map, the model only naming clusters (ROADMAP B1/B3):*
  tree-sitter or `ast` extracts symbols and references deterministically, a
  PageRank-style score ranks them, import connectivity clusters them; then one
  thin slice per cluster with shape `{feature: enum[<your feature list>, NEW],
  name: string(≤5 words)}`; a `NEW` answer needs your approval before it enters
  the diagram, because models drift to code-level components and omit elements
  ([arXiv 2603.21178](https://arxiv.org/pdf/2603.21178)).

## 5. Devices and network notes (research, 27 Oct 2026)

Tailscale's free Personal plan allows 6 users, unlimited user devices, 50
tagged resources (servers, subnet routers and exit nodes count), 1,000
ephemeral minutes and 3 ACL groups ([pricing FAQ](https://tailscale.com/pricing/faq)).
Access control is deny-by-default in the tailnet policy file; **grants** are the
recommended syntax and legacy ACLs keep working with an automatic converter
([access control](https://tailscale.com/docs/features/access-control.md)).
**Tailscale SSH** is on all plans, authenticates by node identity (no keys to
manage), is authorised by an `ssh` policy section with an optional `check` mode,
and supports Linux and the open-source macOS variant as servers, which fits a
Mac ↔ Nexus setup ([Tailscale SSH](https://www.tailscale.com/docs/features/tailscale-ssh.md)).
**Serve** exposes a local port to your tailnet over HTTPS with identity headers;
**Funnel** exposes it to the public internet (beta, ports 443/8443/10000,
needs a `funnel` nodeAttr); a port cannot be both. For MainAIger's rule of no
public remote execution, Serve is the right tool and **Funnel stays off**
([Serve](https://tailscale.com/docs/features/tailscale-serve.md);
[Funnel](https://tailscale.com/docs/features/tailscale-funnel.md)). Subnet
routers reach non-Tailscale devices on the home LAN
([subnet routers](https://tailscale.com/docs/features/subnet-routers.md)).
The clients are BSD-3 but the coordination server is proprietary
([pkg.go.dev](https://pkg.go.dev/tailscale.com@v1.24.1)). Alternatives:
**Headscale** (BSD-3, 0.29.6, 2026-10-24) self-hosts the control plane for
Tailscale clients with ACLs, grants, SSH, MagicDNS, subnet routers and exit
nodes, but not Serve/Funnel ([releasebot](https://releasebot.io/updates/juanfont/headscale));
**NetBird** (clients BSD-3, server AGPLv3 since 0.53, 0.80.0, 2026-10-01;
[WZ-IT](https://wz-it.com/en/blog/netbird-self-hosting-license-commercial-starter/));
**ZeroTier** (BSL 1.1, the only layer-2 option); **Nebula** (MIT,
certificate-based, no UI). For a personal two-to-four-device subnet with SSH
and private HTTPS services, Tailscale's free plan covers everything MainAIger
needs; Headscale is the fallback if you want no vendor control plane, at the
cost of Serve. This is why the Devices page (`local_lab/network.py`) reads
Tailscale state and will later drive **Serve and SSH only**, through reviewed
steps.
