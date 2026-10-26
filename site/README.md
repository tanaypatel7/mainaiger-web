# MainAIger static frontend

Open index.html directly or serve this folder. GitHub Pages deploys this folder. The approved homepage and inline SVG avatar remain unchanged outside the sign-in modal.

Email OTP is prepared in auth-client.js/auth-ui.js. auth-config.js contains only the public project URL and publishable key. Its enabled flag stays false until SMTP, DNS, invitation policy and controlled end-to-end delivery/login checks pass. The current site sends no login email or signup request. When enabled, sign-in requests go directly to Supabase Auth, not to GitHub or Nexus.

Sessions are memory-only; closing/reloading the tab loses them. No browser storage or frontend service keys are used. Nexus must independently verify signed tokens and device ownership before permitting access. It is not connected yet. Installer and pairing controls remain design previews.

See ../docs/EMAIL_DNS_SETUP.md and ../docs/AUTH_VALIDATION.json for current setup and test scope. Continuous avatar motion remains user controlled with Pause/Resume and reduced-motion support. No GitHub Pages custom-domain configuration or DNS record was changed by this login update.
