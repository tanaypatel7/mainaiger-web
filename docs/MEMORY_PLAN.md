# Memory, ideas and cost-aware answers · plan (28 October 2026)

What you asked for, in one paragraph: *describe what you want and MainAIger turns
it into a task and keeps it as an idea that grows as you ask more; when you ask
about a topic later it checks its own memory first, tells you what reusing
would cost versus regenerating, picks the cheapest way, and answers in the
fewest words; chats exported from ChatGPT or Claude can be ingested with you
choosing what to keep; chat memory and curated memory stay separate; memory
can hold Mainbooks (code, tests, direct methods); all of it visual and modular,
with an eye on cost, control and security.*

This file is the plan: what each piece is, how it is built on what exists,
what is decided by default, and what I need from you. Status labels as always:
**built** (tested), **unverified** (built, not run on your Mac), **future**.

## 0. Where things stand (your two questions)

**Ingesting a repository and exposing it as GUI elements — built, Python only.**
The Map page reads any folder (the workspace, Mainbooks, Library, or the Lab's
own code) with Python's `ast`: modules → classes → functions, with call,
import, route and UI arrows. "Make a cell from this…" turns a function into a
form cell (one field per parameter) in the open Mainbook; "Edit…" writes a
change back after a diff and a once-per-session folder approval; "Run tests
in session" runs the folder's tests inside the container. **Not yet:**
JavaScript/TypeScript beyond the UI-route pattern (tree-sitter is the route,
`AI_HARNESS.md` §2), AI-written labels for clusters (B3), and a one-click
"control panel": pick several functions and get one Mainbook with a form cell
each. That last one is a small step on top of what exists.

**Devices — built, unverified on your hardware.** Pair another machine that
runs the same Lab (the Devices page has the install steps): the Lab starts
the SSH forward (command shown first), signs in with the key file, and the
device's sessions appear in Resources. A cell can "run on `nexus/main`"; the
terminal and Files panel can point at the device; its Log is readable;
trigger conditions are answered there too. Tested with two Labs in one
process; the real Mac ↔ Nexus pairing is the one thing only you can verify.
**Not yet:** events crossing devices (C3), Tailscale login/Serve from the GUI,
file transfer between devices, GPU choice, copying MainAIger onto a device
(C1). How useful today: as useful as "run this cell on the big machine, see
its output here" — real, but the Lab must already be installed there.

## 1. The memory store (M1) — **next build**

**What:** a folder you approve once (like Mainbooks and Library), holding an
Obsidian-compatible vault: Markdown notes with YAML front matter and
`[[links]]`. Two tiers, two sub-folders:

- `chats/` — **chat memory**: imported conversations, one note per chat,
  never edited by the Lab after import (raw material, with provenance);
- `notes/` — **persistent memory**: curated notes you or the Lab wrote:
  `kind: idea | fact | procedure | snippet | answer | rule | mainbook`,
  `status: draft | growing | stable`, `sources` (which chat and turn, which
  run), `tags`, `visibility: private | shareable`.

**How:** `local_lab/memory.py` (read/write notes atomically, parse front
matter, extract links, build a search index in SQLite FTS5 — standard
library, no model needed — rebuilt on change), `GET/POST /api/memory/*`, a
`/memory` page: list, search, open, edit in place, change status, and the
link graph drawn with the same canvas code as the Map. Every write is a Log
line. Optional later: embeddings through Ollama's `/api/embed` (local,
costs seconds, no tokens) for "similar" rather than "same words".

**Why this shape:** readable and editable outside MainAIger (Obsidian, any
editor, git), it "keeps growing" without a migration, the graph view comes
with the format, and provenance is a field, not a feature.

**Default decisions (say so if you want them different):** Markdown vault +
SQLite index (not a database as the source of truth); the folder lives under
the same approved parent as Mainbooks (`…/MainAIger/Memory`); notes are
plain files, so "security" is the folder approval, the visibility field, the
Log of every model read, and the rule that chats are never rewritten.

## 2. Ideas (M2)

**What:** an "Idea" box on the Memory page. You type what you want; the Lab
creates one idea note with the description, a title, tags and a first task
list; every later question you ask about it (from the ask box below) is
appended to the same note; **Confirm as stable** flips its status. Ideas are
notes, so they link to the chats, answers and Mainbooks that came from them.

**How:** the title, tags and three task bullets come from one thin-slice
decision (`decide.py`, a small schema, your local model); the note is shown
before it is saved (a reviewed step). Growing = appending dated sections.

## 3. Memory-first, cost-aware answers (M3)

**What:** an ask box whose pipeline is itself a **Mainbook** you can open and
rearrange: Question → *Search memory* → *Decide: reuse or regenerate* →
*Answer briefly* → *Save as a draft answer note*.

- *Search memory* finds notes by words (FTS5), later by embeddings; the top
  hits are shown with their status and date.
- *Decide* shows two costs: **reuse** (0 tokens, instant) and **regenerate**
  (estimated tokens = prompt + context characters ÷ 4; estimated seconds =
  tokens ÷ the tokens-per-second the Models page has measured for that model;
  estimated energy = seconds × a watts figure you enter once per device,
  labelled *approximate*). You choose, or a rule note says "reuse when a
  stable note matches" and it chooses for you — visibly.
- *Answer briefly* uses a fixed prompt: assume the reader is competent, no
  preamble, only what is needed to answer, fewest words; "say more" is a
  follow-up, not a default.
- *Least expensive way*: a small, editable routing table (a `rule` note):
  which model for which kind of question (code, fact, writing), starting
  with the smallest; escalate only on request. A custom method you write as
  a Mainbook can be a route too ("for CSV questions run this cell instead of
  asking a model").

**How:** the ask box posts to `/api/memory/ask`, which runs the saved
Mainbook `memory-ask` (nested Mainbooks make this possible — in progress)
with `question` bound like an event; the answer note links to the idea.
Cost figures come from `models.py` (it already measures elapsed time and
tokens per chat) kept as a running average per model in the state folder.

## 4. Chat import (M4)

**What:** upload a ChatGPT or Claude export zip on the Memory page. The Lab
reads it on this Mac (never in the container), writes one chat note per
conversation into `chats/<source>/`, and starts a **review Mainbook**: for
each batch of chats a model proposes memory items (facts, procedures,
snippets, decisions) in a fixed shape; a form cell shows them as checkboxes;
you keep, edit or drop; kept items become notes linked to the chat; a loop
arrow moves to the next batch within a budget. Run it again any time: new
chats only. Manual editing stays possible (the Memory page, or Obsidian).

**How:** `memory_import.py` with one parser per export format. From what I
know of the formats (to be checked against a real file, see the questions):
ChatGPT's zip holds `conversations.json` with a `mapping` of message nodes
(`author.role`, `content.parts`, `create_time`); Claude's data export holds
`conversations.json` with `name`, `created_at` and `chat_messages`
(`sender`, `text`, attachments). Attachments are copied next to the chat
note. The review Mainbook is an example (`examples/memory-review.ipymb`)
like the self-improvement loops, so it is visible and editable.

## 5. Memory as Mainbooks (M5)

**What:** a note of `kind: mainbook` points at a saved Mainbook: a procedure
with its code, its tests, and a direct way to run it ("run this memory").
Rules (`kind: rule`) are notes the ask pipeline reads before deciding
(routing, "reuse when stable", "never send notes marked private to a cloud
model"). Memory logic a model can derive = the links: idea → answers →
Mainbooks → runs, all in the Log.

**How:** nested Mainbooks (a cell that runs a saved Mainbook in the same
session: model and engine written today, server and canvas next) + the
Memory page's "Run" button on such notes.

## 6. Getting chats out of ChatGPT and Claude (M6)

Two routes, in this order:

1. **Official export, then upload (recommended, build first).** ChatGPT:
   Settings → Data controls → Export data → a zip arrives by email. Claude:
   Settings → Privacy → Export data. Once a month, drop the zip on the Memory
   page. Cheap, complete, allowed.
2. **MainAIger drives a browser (later, as a reviewed "browser cell").** A
   cell type that runs on this Mac (not in the container: it needs the
   network and a browser), using Playwright with a recorded script per site,
   each run approved like any system-touching step. Honest limits: the chat
   sites change their pages, log-ins and checks break scripts, and their
   terms restrict automated access — so this is for sites without an export
   and for your own self-hosted tools, with the official export staying the
   path for ChatGPT and Claude. Label when built: *fragile by nature*.

## 7. Order and what I need from you

Build order (each a tested, documented commit): M1 memory store + page →
M2 ideas → M4 import + review loop → M3 ask with costs → M5 memory
Mainbooks (nested Mainbooks first) → M6 browser cell.

Questions (defaults in brackets; I continue with the defaults if you do not
answer):

1. Memory format: Obsidian-style Markdown vault + SQLite index [yes].
2. A real export: upload one small ChatGPT or Claude export zip so the
   parsers are built against the real files [until then: built from the
   documented formats and labelled *unverified*].
3. Energy estimate: you enter a watts figure per device in Settings and the
   estimate is labelled approximate [yes]; tokens and seconds are measured.
4. Where the ask box lives: a new Memory page; the Models page stays the raw
   chat [yes].
5. Browser automation: official export first, browser cell later [yes].
