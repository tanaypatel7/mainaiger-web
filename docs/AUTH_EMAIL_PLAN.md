# MainAIger invite-only login and email integration plan

The selected first-release policy is invite-only registration with managed identity. A healthy Supabase Auth project now exists in the Mainaiger Free organization, with public signups disabled, anonymous sign-ins disabled, email confirmation required and 8-digit email codes expiring after 600 seconds. The selected email route is Resend Free via replaceable standard SMTP. The sending domain notify.mainaiger.org has been registered in Resend, but DNS verification, SMTP configuration and delivery testing remain pending. The frontend stays disabled until those checks pass. See EMAIL_DNS_SETUP.md for exact setup status. No authentication or email-delivery success is claimed.

## Service boundaries

GitHub Pages serves only public HTML, CSS, JavaScript and SVG. The managed identity provider (proposed: Supabase Auth) stores account identities, validates email codes and issues short-lived signed access tokens. Nexus receives an access token over an authenticated API path, verifies signature, issuer, audience and expiry, then checks the stable user ID against its own approved membership and device ownership. A browser-supplied email address or decoded, unverified JWT never authorizes access.

The frontend has only public provider configuration. SMTP credentials, provider administration keys and device secrets never enter Git, URLs, email templates or client JavaScript. The initial session can be held in memory; reopening a tab requires signing in again. Nexus needs minimal persistent registration/device ownership metadata even if it stores no passwords or browser sessions. Revocation must be checked independently of JWT signature validity.

## Email first

Inventory current DNS, MX, SPF, DKIM and DMARC before making changes. Use a separate transactional sender such as accounts@notify.mainaiger.org. Cloudflare Email Sending is a current public-beta SMTP/API option; arbitrary-recipient sending requires Workers Paid and must not be enabled as an unapproved subscription. A maintained SMTP provider is an alternative. Cloudflare Email Routing is a separate inbound-forwarding feature; do not overwrite an existing mailbox's MX records to enable it.

Configure sender verification, SPF/DKIM alignment and DMARC monitoring. For Cloudflare Sending, disable stored Email preview for authentication messages; this does not eliminate delivery metadata. Store SMTP credentials only in the identity provider and the minimal onboarding backend. Verify controlled delivery to test recipients before activating login.

Nexus can run an onboarding job/queue in a container and submit messages through the delivery service. A complete self-hosted public mail server is deferred: domain ownership alone does not provide reachable SMTP, static IP/PTR, sender reputation, mailbox operations or backups.

## Account flow

1. An administrator registers or invites an approved email through the provider's server-side admin tools.
2. The visitor requests a sign-in code. Disable public signups in provider settings; also set the client option shouldCreateUser=false. A client flag alone is not an access-control boundary.
3. Show a neutral request result, apply provider rate limits and verification-attempt limits, then verify the expiring one-time code with the identity provider.
4. Obtain a verified session and stable account ID. Each Nexus API call validates the token and approved membership before using user-owned resources.
5. Send one idempotent welcome/setup email after verified account activation. Link to a setup/download page, without embedding bearer tokens, reusable passwords or automatic device authorization.
6. A separate, short-lived pairing code binds a new device key to the signed-in owner. Require explicit device approval. Account login does not grant shell, filesystem or administrator access.

Google/GitHub OAuth can follow after the email flow is verified. Do not enable social login as a bypass around the invitation policy.

## macOS and containers

Distribute a versioned macOS launcher with a Linux container runtime payload, supporting arm64 and amd64 builds. The launcher handles macOS integration and Keychain-backed device identity; the container runs the portable Python/API worker with explicitly selected workspace mounts. A Linux container cannot replace macOS Keychain, GUI or native system integrations. Container-engine installation and app signing/notarization must be covered by the installer. Do not advertise an installer until the artifact exists and has been tested.

Public GitHub release downloads are publicly retrievable even when the website button is hidden behind login. Protect runtime registration and pairing; if private installer downloads are required, use authenticated storage with short-lived download URLs.

## Reachability

Keep the existing private Nexus management path private. A global account service can work while Nexus is offline. User/device pairing and synchronization need a separately designed reachable API or outbound relay; do not expose existing Jupyter, SSH, workers or administration ports just to enable website login. Multiuser ownership checks remain mandatory.

## Acceptance checks

Uninvited email cannot create an account; malformed/expired/replayed codes fail; revoked users lose authorized access; invalid issuer/audience/signature/expiry fails; one user cannot pair or inspect another user's device. No codes/tokens in application logs. Welcome email is sent once per activation. Installer download is real, architecture-appropriate and versioned. Pairing expires, can be revoked and never runs code before approval.

## Official references

- https://supabase.com/docs/guides/auth/auth-email-passwordless
- https://supabase.com/docs/guides/auth/auth-smtp
- https://supabase.com/docs/guides/auth/jwts
- https://developers.cloudflare.com/email-service/
- https://developers.cloudflare.com/email-service/api/send-emails/smtp/
- https://developers.cloudflare.com/email-service/configuration/domains/
- https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits

Before adding commercial SaaS or sensitive credential handling, review GitHub Pages hosting limits and choose an appropriate static hosting/authentication boundary. No payment or password form is added by this homepage update.
