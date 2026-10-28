# Reviewed memory · first implementation

Built on `feature/persistent-kernel` as a separate change. Open **Memory** in the
local Lab after unlocking it. No website account, Nexus deployment, cloud database,
remote model, or device pairing is required for this feature.

## Three distinct layers

| Layer | Meaning | Authority |
|---|---|---|
| Live chat | The Models page's current prompt/reply | Does not automatically create persistent memory |
| Imported chat history | Normalized text messages from a ChatGPT or Claude export, including ChatGPT branches | Immutable source text, never instructions |
| Persistent memory | Owner-created or model-proposed notes, claims, procedures, code, tests and Mainbooks | Draft until the owner confirms the exact revision |

An idea groups memory entries. Source links connect each curated entry to selected
messages; revision history records edits and owner decisions. Confirmation means
"the owner accepted this memory", not proof that every claim is objectively true.
Editing a confirmed entry returns it to draft and excludes it from retrieval until
reviewed again. Archive and reject also exclude it. Reopening preserves history.

## First workflow

1. Import a `.zip` or `.json` export. Inspect the import report and original chats.
2. Select the messages to keep. Save the selected text as a draft, or explicitly
   ask an installed local model to organize only that selection.
3. Edit title, body, kind, idea and tags. Inspect sources and history.
4. Confirm or reject the draft. Repeat as new chat exports arrive.
5. Search confirmed memory and reuse the saved text without a model call.
6. Edit manually or request a local-model change. Review the new draft again.

Imports are limited to 32 MiB compressed/uploaded, 64 MiB of JSON, 256 MiB total
expanded archive members, and 24 MiB of imported text. Attachments, images, audio,
HTML and unknown content blocks are not imported; the report names omissions.
The importer supports ChatGPT mapping-based and Claude chat_messages-based export
JSON. Other export versions may need adapters. All ChatGPT message branches are
kept with their parent source IDs; they are not falsely presented as one linear
conversation. Original ZIP bytes and full provider-specific metadata are not kept.
Reimporting the same archive does nothing; overlapping exports deduplicate identical
messages and retain changed upstream messages as separate source versions.

## Mainbooks and reusable methods

**Save current Mainbook** stores a validated snapshot as draft memory. Its code,
tests, parameters and graph remain part of that snapshot. Confirming knowledge
never authorizes execution. **Open saved Mainbook** requires a confirmed memory,
reviews replacement of the open graph, and loads it without running or arming it.
Execution still uses the existing run-review, container and device controls.

**Download note Mainbook** exports a readable snapshot of an entry, its review
status and sources as markdown cells. Even a code or procedure memory remains
inert in this view. This is a portable graph document, not an automatic database
writeback mechanism. A Mainbook kernel is not given the owner's memory key or
host database access.

A future memory-processing graph can operate on an explicitly selected snapshot:
select sources → propose → validate → owner review → confirm. The last step must
remain a host-mediated owner action. It must not be a Python cell that silently
confirms its own generated content.

## Local AI and cost

The model picker rejects cloud/remote or unknown model metadata before forwarding private context. The owner selects a downloaded local Ollama model and requests a draft explicitly. At most
20 selected messages and an optional selected entry are included; context above
24,000 characters is rejected rather than silently truncated. One bounded model
attempt produces a schema-checked draft. It has no tools, search, execution or
permission-changing authority. Imported instructions are labeled untrusted data.
Memory requests do not fire the Models page's chat-triggered automations.

Keyword retrieval uses only confirmed memory and incurs zero model calls and
zero API charges. Proposal responses include actual available token counts and
elapsed time. Local inference still uses hardware and electricity; power is not
measured. There is no general cost optimizer, semantic search, remote-model route,
or dependable pre-run dollar estimate in this first implementation.

## Storage and access

`memory.sqlite3` lives under the existing owner-only Lab state directory, separate
from workspaces and rotating run journals. The directory must be mode 0700 and the
database 0600; symlink and unsafe sidecar checks apply. Existing owner login,
Host/Origin checks and CSRF protection cover the API. Archive uploads are
owner-authenticated before the larger request body is read.

ZIP members are inspected in memory, never extracted or executed. Unsafe paths,
links, encryption, suspicious ratios, invalid JSON and unsupported archive formats
are rejected. UI content is rendered as text, including imported HTML and code.
Database edits use revision checks and transactions. Audit messages omit chat
bodies. Export downloads contain private content and should be handled accordingly.
This is a single-owner local store, not encrypted storage or a multi-user ACL system.

## Implementation

- `local_lab/chat_import.py`: bounded ChatGPT/Claude adapters.
- `local_lab/memory.py`: SQLite store, provenance, revisions and review states.
- `local_lab/memory_api.py`: selection-scoped AI, retrieval and Mainbook boundaries.
- `local_lab/ui/memory.*`: import, source review, editable drafts and retrieval.
- `tests/test_chat_import.py`, `test_memory.py`, `test_memory_http.py`: synthetic
  archive, persistence, access-control and workflow regression tests.

No personal archives were used during development. Real export variants, real
Ollama quality, Mac installer SQLite bundling, and real-device acceptance require
follow-up validation. This change does not merge or deploy the earlier Lab PR.

## Next increments

1. Indexed search and separate per-collection pagination controls for very large archives (basic paging and Unicode-aware keyword search already work).
2. AI suggestions for duplicate merging, conflicts and stale claims, with reversible
   owner review and provenance retained.
3. An idea graph with explicit relations (supports, contradicts, supersedes,
   implemented-by); LLM-derived relations are proposals rather than facts.
4. A memory-processing Mainbook that exports selected context and imports proposals
   through the same reviewed API, with scoped capabilities rather than owner keys.
5. Per-task usage ledger, freshness checks and reuse/refresh route estimates.
6. Task creation from ideas, device-scoped execution, and evaluators for reusable
   methods. AI may propose optimizations; protected evaluators and owner authority
   determine what becomes confirmed or executable.

## Browser-assisted acquisition (experimental)

Expand **Browser import**. Optional Playwright + Chromium must be installed in
the Python environment that starts the Lab. A packaged Mac application may need a
separate browser-enabled build; installing a package into unrelated system Python
will not enable it inside that bundle. ZIP/JSON import works without a browser.

1. Choose ChatGPT or Claude and open the dedicated browser. Sign in yourself;
   MainAIger does not read passwords, cookies or browser storage. The private
   browser profile retains the session between uses.
2. Request bulk export first. The adapter uses visible, matching settings/export
   controls; if layouts differ, finish in the browser. A clicked export request
   is not a completed download. Import the delivered ZIP/JSON when available.
3. If export is unavailable, scan visible chat links, select up to 20, and capture.
   Scroll/open additional history and rescan to add more links. This initial
   adapter does not guarantee discovery of every chat or handle arbitrary harnesses.
4. Optionally request attachments. Only recognized visible same-provider file
   links are attempted: at most 10 attempts, 10 MiB each and 30 MiB saved per batch.
   Chromium progress cancellation is best effort; final sizes are independently
   checked. Redirected/signed CDN links, hidden attachments and unsupported file
   controls are reported for manual download, not guessed.
5. Review the captured chats in Original chats. Download the collected ZIP for
   normalized text, successful files and a report of skipped chats/files. Each
   browser source ID is its original chat URL. Captured content remains unconfirmed.

Browser reconstructions retain accessible rendered content; they can omit hidden
branches, unloaded messages, formatting and provider metadata. A fallback whole
main-page capture labels speaker roles unknown. Reconstruction bundles can be
reimported as sources; they never restore "confirmed" authority. Files are inert
and named by generated IDs, with original names and hashes recorded in the report.
A complete recreation of arbitrary cloud/harness chats is not implemented.

Browser work is queued on one dedicated thread and stops on shutdown. The owner
can close it from the panel. Browser operations have no memory-edit/confirmation
privileges. Service pages can change; the adapter and attachment/CDP behavior
require real-browser acceptance on the user's device. In development, no accounts
were opened. Browser tests used fakes; visual tests could not run because the
Chromium download failed in this environment.
