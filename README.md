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

## Current boundaries

The email-code sign-in client and modal are implemented but deliberately disabled pending SMTP/domain verification and real delivery tests. A healthy Supabase Auth project exists in the Mainaiger Free organization, with public signups and anonymous sign-ins disabled and eight-digit codes expiring after 10 minutes. The selected free email route is Resend via replaceable SMTP, using notify.mainaiger.org. DNS verification and SMTP credentials remain pending; no invitations or emails have been sent. See `docs/EMAIL_DNS_SETUP.md`, `docs/AUTH_EMAIL_PLAN.md` and `docs/AUTH_VALIDATION.json`.

When enabled, the client sends email/code requests directly to the configured identity provider, confirms identity using its authenticated user endpoint, and holds the session in memory only. No passwords, service keys or SMTP credentials are present in the frontend. Google/GitHub provider buttons remain disabled. Installer distribution, device pairing, Python execution and Nexus API integration remain unimplemented.

Cloudflare domain configuration is pending authenticated account access. Existing mail records must be inspected before changes. No Nexus administration surface is published by this repository.

## Prototype and tools

The earlier notebook/graph design is retained at `site/lab-preview/index.html`. Its setup and runtime displays are simulations. `tools/notebook_bridge.py`, `tests/`, and `examples/` retain the real notebook JSON converter; it does not execute code. Earlier design/deployment documentation is retained under `docs/`.

## Login preparation

- `site/auth-client.js`: dependency-free, memory-only, invite-only email OTP adapter.
- `site/auth-config.js`: public URL/key with `enabled:false` until activation checks pass.
- `site/auth-ui.js`: modal state, resend cooldown, expiry and logout.
- `tests/auth-client.test.js`: negative authentication, identity, expiry and race checks.
- `templates/`: prepared login-code and welcome messages; not wired or sent.

Run `node tests/auth-client.test.js` for the client tests. Supabase stores the registered identity records; memory-only browser sessions do not mean zero provider-side processing or retention. Login alone does not grant access to Nexus resources.

## Validation

Homepage asset references, JavaScript syntax, exact approved copy/style/Lab-mark digests, 37 SVG rig paths, fixed loop openings, 444 motion geometry samples, Pause/Resume and 19 authored contrast pairs were checked before upload. Browser review covered eye intermediate frames, themes, narrow controls, keyboard gestures and pointer tickling. Reduced-motion behavior was tested with a simulated media query; host OS preference switching and physical touch-device tests were not performed.
