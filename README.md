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

Account sign-in, installer distribution and device pairing are not connected yet. Current dialogs state that they are design previews. The website does not execute Python, collect credentials, send email, or connect to a device. The invite-only managed-auth and email integration plan is in `docs/AUTH_EMAIL_PLAN.md`; its services have not been provisioned.

Cloudflare domain configuration is pending authenticated account access. Existing mail records must be inspected before changes. No Nexus administration surface is published by this repository.

## Prototype and tools

The earlier notebook/graph design is retained at `site/lab-preview/index.html`. Its setup and runtime displays are simulations. `tools/notebook_bridge.py`, `tests/`, and `examples/` retain the real notebook JSON converter; it does not execute code. Earlier design/deployment documentation is retained under `docs/`.

## Validation

Homepage asset references, JavaScript syntax, exact approved copy/style/Lab-mark digests, 37 SVG rig paths, fixed loop openings, 444 motion geometry samples, Pause/Resume and 19 authored contrast pairs were checked before upload. Browser review covered eye intermediate frames, themes, narrow controls, keyboard gestures and pointer tickling. Reduced-motion behavior was tested with a simulated media query; host OS preference switching and physical touch-device tests were not performed.
