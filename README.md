# MainAIger website

The public homepage uses the approved Main Manager ribbon avatar and MainAIger Lab identity. GitHub Pages deploys the contents of `site/` using the existing workflow.

## Website

- `site/index.html`: approved homepage and preview entry dialogs.
- `site/styles.css`: approved light/dark palette and responsive layout.
- `site/main-manager-rig.js`: inline SVG animation; staged graphite eye panels, descending hollow pupils, closed-route material highlights, local waves and exact Pause/Resume.
- `site/app.js`: native controls and preview dialogs.
- `site/assets/`: editable SVG avatar and approved Lab marks.
- `site/lab-preview/`: retained earlier notebook-to-graph prototype.

Main Manager's open loops remain the default. Eye appearances have no assigned application meanings. Independent wave/flow speed controls are bounded to 0.5–2×. Reduced-motion preferences keep static appearances available. The artwork is a dimensional SVG projection, not a full 3D mesh. GIFs are review exports and are not runtime dependencies.

## Shared spaces and local ownership

`site/directory/` is the opt-in public directory and private request inbox. Owners
choose which region, device categories, compute totals, membership count and API
categories to disclose. Listings begin private. Public totals describe reported
resources, not all registered users or live network reachability. The browser
refreshes public metadata while visible; GitHub Pages serves the interface and
Supabase supplies the directory records.

The database schema is in `supabase/directory-schema.sql`. RLS and transition
guards enforce ownership and request privacy. `tests/directory-rls.sql` validates
those rules in a transaction that rolls back all synthetic test data. See
`docs/DIRECTORY_API.md` and `docs/DIRECTORY_PRIVACY.md` for fields, trust boundaries
and administrator visibility.

`companion/` provides a separate local owner permission service. Directory
acceptance does not authorize it. Owners import a request and independently
choose a resource, supported read-only operations and expiry. See
`docs/LOCAL_OWNER_PERMISSIONS.md` for setup and current networking limits. The
existing single-owner Nexus runtime is not changed or exposed by this service.

## Current boundaries

Email-code sign-in is enabled. The owner confirmed successful delivery and
sign-in after verifying `notify.mainaiger.org` and configuring Resend SMTP in
Supabase. The existing configuration remains invite-only. No SMTP credentials or
service keys are in this repository. Earlier email planning/validation documents
describe the pre-activation state and are not a current delivery test report.

The client sends email/code requests directly to the configured identity
provider, confirms identity using its authenticated user endpoint, and holds the
session in page memory only. Reloading or navigating to another page requires
signing in there again. Google/GitHub provider buttons remain disabled. The full
graphical macOS installer, automatic website/device pairing, isolated code
execution and integration into the live Nexus runtime remain unimplemented.

The website is hosted at mainaiger.org through GitHub Pages and Cloudflare DNS.
This directory implementation does not change DNS, mail records, Tailscale or
Nexus network exposure. No Nexus administration surface is published here.

## Prototype and tools

The earlier notebook/graph design is retained at `site/lab-preview/index.html`. Its setup and runtime displays are simulations. `tools/notebook_bridge.py`, `tests/`, and `examples/` retain the real notebook JSON converter; it does not execute code. Earlier design/deployment documentation is retained under `docs/`.

## Login preparation

- `site/auth-client.js`: dependency-free, memory-only, invite-only email OTP adapter.
- `site/auth-config.js`: public Supabase URL and publishable key; sign-in enabled.
- `site/auth-ui.js`: modal state, resend cooldown, expiry and logout.
- `tests/auth-client.test.js`: negative authentication, identity, expiry and race checks.
- `templates/`: prepared login-code and welcome messages; not wired or sent.

Run `node tests/auth-client.test.js` for the client tests. Supabase stores the registered identity records; memory-only browser sessions do not mean zero provider-side processing or retention. Login alone does not grant access to Nexus resources.

## Validation

Homepage asset references, JavaScript syntax, exact approved copy/style/Lab-mark digests, 37 SVG rig paths, fixed loop openings, 444 motion geometry samples, Pause/Resume and 19 authored contrast pairs were checked before upload. Browser review covered eye intermediate frames, themes, narrow controls, keyboard gestures and pointer tickling. Reduced-motion behavior was tested with a simulated media query; host OS preference switching and physical touch-device tests were not performed.
