# Shared spaces and local ownership

MainAIger is software people can run on their own equipment. The optional
mainaiger.org directory helps people discover resources and contact an owner.
A directory space is a named collection of resources, not an exposed IP subnet.
Creating or joining a listing does not join a VPN or open a network route.

## What goes where

| Information | Location | Who can access it |
| --- | --- | --- |
| Website account email and authentication records | Supabase Auth | Account holder and platform administration |
| Unpublished directory drafts | Supabase database | Owner through the app; platform database administrators |
| Published listing fields | Supabase database and visiting browsers | Anyone on the internet |
| Access request alias, requested capabilities and optional note | Supabase database | Requester and listing owner through the app; database administrators |
| Files, prompts, code, notebook outputs, terminal history and local folder paths | Owner's equipment | Only according to that installation's local permissions |
| Device access tokens, local roots and permission grants | Owner's equipment | Local owner and explicitly authorized recipient |

The directory has no file upload, command execution, model prompt, device
inventory upload or automatic location collection endpoint. It does not scan a
LAN or Tailscale network. The application does not send local access tokens to
Supabase. Normal hosting and authentication infrastructure can retain connection
metadata such as IP addresses; this is not a promise of anonymous operation.

## Publication consent

Every listing starts unpublished. Only fields selected in the publication form
are sent; clearing an optional sharing choice clears its stored value on save.
Saving a private draft still sends that selected draft to the directory service.
Publishing is a separate deliberate action after reviewing the exact preview.

Region is manually selected and coarse. There is no browser GPS request or
IP-to-location lookup. Device and API categories describe what the owner is
offering; they do not contain hostnames, IP addresses, endpoints, API keys,
directory paths or hardware serial numbers. Avoid putting sensitive information
in free-text names, descriptions or request notes.

Owners may withdraw a listing or delete the space and its directory records.
Withdrawal removes it from subsequent public queries and public totals.
Existing visitor copies, screenshots, search caches and provider backups cannot
be remotely erased by the directory. Request records remain participant-visible
until the space is deleted; there is no claim of timed automatic deletion.

## Counts and updates

The browser refreshes published summaries periodically while visible. Counts
describe published listings and the quantities their owners voluntarily report.
Reported members can be counted in more than one space. These are not a census
of all users, installs, subnet peers, active sessions, real-time utilization or
reachable devices. A listing's timestamp means its metadata was edited, not that
its server was contacted or attested.

## Approval is local

1. A signed-in person sends a request for a published space.
2. The owner accepts for local review or declines it. Acceptance has status
   `accepted_pending_device`; it does not confer device access.
3. The owner can export that request and inspect it in the local companion.
4. The owner separately chooses a local resource, supported operations and
   expiry. The companion creates a revocable capability on that installation.
5. The recipient receives that secret capability privately. It must not be
   pasted into a listing, request note, URL or public repository.

The local companion has its own authorization. It rejects cloud login tokens and
directory approval status as execution credentials. Its initial operation set is
read-only and does not enable terminals, notebook execution or automatic remote
pairing. A private owner API token from the existing Nexus application must never
be handed to another user. See LOCAL_OWNER_PERMISSIONS.md for supported operations
and current transport limitations.

Withdrawing a public listing is not the same as revoking a local grant. Revoke
device access on the owner's installation. Local authorization must remain valid
independently of whether the directory is reachable.

## Builder and user accounts

Maintainers administer the shared website and its identity provider settings.
An ordinary website account can own its own spaces and request others' resources.
Builder status grants no automatic capability on a user's independent device.
Users of the shared website do not need an @mainaiger.org mailbox or their own
Google Cloud project. Existing email sign-in remains invite-only in this release.

Platform administrators can access centrally stored metadata and change hosted
frontend code. This release does not implement end-to-end encryption or make
those administrators cryptographically unable to inspect central records.
People requiring independence should run the interface and authentication on
their own infrastructure and choose when to install software updates.

## Administration without activity surveillance

Use Supabase Auth for invitations and account lifecycle, and the directory for
published metadata and access requests. Do not add a central file browser,
execution log collector, full device scanner or usage tracking to administer
accounts. Revoking a website account and revoking an owner's local grants are
separate actions; installations retain authority over their own resources.

Before expanding beyond invited testers, add abuse reporting/moderation,
appropriate retention controls and an end-to-end multi-account acceptance run.
The server enforces ownership, field bounds and request limits already; these
are not a substitute for community moderation.
