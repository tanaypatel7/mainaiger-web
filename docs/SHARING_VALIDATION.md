# Shared spaces validation

Validated against repository baseline `0e72eb576f093fe9c68fb84c56b05142f2e19a81`.
The existing homepage layout, palette, avatar geometry/animation and Lab logo
assets are retained. Homepage additions are the directory navigation and a
post-sign-in link explaining page-memory sessions.

## Checks completed

- `node --test tests/*.test.js`: 16 passing test entries, covering existing
  authentication checks, token-origin confinement, public token omission,
  sign-out/expiry races, publication consent and metadata minimization,
  ownership payloads, requests and pagination.
- `python3 -m unittest discover -s tests -p 'test_*.py'`: 27 passing tests,
  including 17 local permission/HTTP tests and existing notebook converter tests.
- JavaScript syntax checks passed for all website and companion UI scripts.
- `git diff --check` passed. Temporary synthetic browser fixture pages were
  removed; deployment CI explicitly rejects `__qa*` files under `site/`.
- Applied named Supabase migration `mainaiger_opt_in_directory`, returned
  migration version `20261026235911`.
- Executed `tests/directory-rls.sql` against Supabase: PASS for anonymous privacy,
  tenant isolation, immutable identities/content, request state transitions,
  withdrawal, input bounds, 20-space/50-request quotas and deletion cascades.
  Synthetic account/listing/request records were created inside one transaction
  and rolled back. No email or login API was invoked for these fixtures.
- Actual anonymous REST calls returned HTTP 200 for public summaries/statistics
  and HTTP 401 for private ownership/request tables. Public totals were zero.
- Browser connected successfully to the real public backend and showed the
  truthful empty directory and zero disclosed-resource counts.

## Browser coverage

The real directory page was visually checked in the cloud browser. A temporary
local-only in-memory fixture exercised private drafts, opt-in region selection,
readable publication preview, publish, unpublish, owner acceptance, outgoing
request creation and cancellation. Light/dark styling and a 390px iframe viewport
were reviewed. The narrow editor remains scrollable and controls remain usable.
Synthetic records were never inserted into the public database or committed.

These checks do not replace a full browser acceptance run using two actual
invited Supabase accounts. No verification emails were sent during this task.
The earlier successful real email login was reported by the owner.

## Local permission review

Tests and independent review cover hashed capability storage, expiry/revocation,
operation/resource separation, rejection of cloud JWTs/status as authority,
no-follow filesystem traversal, symlinks/hardlinks/nonregular files, byte limits,
state/workspace exclusion (including ancestor symlink aliases), owner
session/CSRF/origin/Host checks and denial of cross-origin browser resource calls.

This service remains loopback-only and read-only. It is separate from the
existing single-owner Nexus backend. No live Nexus deployment, remote pairing,
arbitrary-code sandbox, full MainAIger installer or identity-bound capability
delivery has been implemented. Docker is unavailable in this environment;
container build/run and Mac acceptance must still be verified on a real device.

## Database advisory

The post-migration security advisor reported no new directory/RLS finding. It
reported an existing Auth warning: leaked-password protection is disabled.
This release uses email codes and adds no password form. If password login is
enabled later, review the provider's
[password protection guidance](https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection).
