# MainAIger directory API

This service stores voluntarily shared descriptions of independent MainAIger installations and coordination requests. It does not discover a local network, connect a device, run code, expose a server, or issue executable permissions.

The static website calls the existing Supabase project's REST API using its public publishable key. A signed-in account's short-lived access token is added for owner/request actions. Never place a Supabase secret/service-role key, an installation owner token, an API credential, or a device credential in website source or a listing.

## Privacy boundary

- `directory_spaces` contains ownership records. App API access is limited to that owner.
- `directory_listings` contains only fields intended for possible publication. Drafts are owner-only; setting `published` to true makes the entire listing readable without sign-in.
- `directory_requests` is readable by the requester and space owner through the app API. It contains the account UUID, chosen display name, requested capabilities and optional message, but no account email.
- Authentication emails and centrally stored listings/requests remain administrable by the platform operator. RLS restricts app users, not the database operator or privileged service roles. Providers may also retain operational connection logs.
- Files, folders, exact network addresses, process history, commands, prompts, API credentials and precise location do not have dedicated fields and must remain on the user's installation. A user can still disclose sensitive text in a free-text field: do not paste it into descriptions or requests.
- Location is an optional manually selected continent. The website does not derive it from GPS or IP.
- Public listings are copyable. Withdrawal stops future directory disclosure; it cannot erase copies already obtained by someone else.

## Endpoints

Use `/rest/v1/` on the configured Supabase project URL. Headers are `apikey: <publishable key>` and, for authenticated operations, `Authorization: Bearer <access token>`. JSON mutations use `Content-Type: application/json`. Use `Prefer: return=representation` where a created row or mutation result is needed.

| Operation | Method and path | Body / behavior |
| --- | --- | --- |
| Browse | `GET directory_listings?published=eq.true&order=updated_at.desc,space_id.asc&limit=50&offset=0` | Only public rows; paginate. |
| Public totals | `GET rpc/directory_public_stats` | One aggregate row across all published listings. |
| My spaces | `GET directory_spaces?order=created_at.desc` | RLS returns only caller-owned rows. |
| Create space | `POST directory_spaces` | `{}`; server assigns `id`, owner and timestamp. |
| Create draft | `POST directory_listings` | Required `space_id` and `title`; other fields optional. |
| Get own draft | `GET directory_listings?space_id=eq.<uuid>` | Owner can read published or draft. |
| Edit/publish/withdraw | `PATCH directory_listings?space_id=eq.<uuid>` | Any editable listing fields; `published:false` withdraws. |
| Delete space | `DELETE directory_spaces?id=eq.<uuid>` | Owner-only; cascades listing and its central requests. |
| Submit request | `POST directory_requests` | `space_id`, `display_name`, nonempty `capabilities`; optional `message`. |
| Inbox/outbox | `GET directory_requests?order=created_at.desc&limit=50&offset=0` | RLS returns requests made by caller or to caller-owned spaces. |
| Decide/cancel | `PATCH directory_requests?id=eq.<uuid>` | Only `{ "status": "<allowed next state>" }`. |

Filter by `requester_id=eq.<current-user-id>` for an outbox or `space_id=in.(<owned ids>)` for an inbox. These filters only select from rows already permitted by RLS. Never treat browser filtering as authorization. Query parameters must be constructed through URL encoding, not interpolated from raw untrusted filter expressions.

An update/delete of an inaccessible or absent row may return zero rows rather than an error. The UI must not claim success unless its expected row changed. Request creation and all transitions must be made using a user access token.

## Listing fields

| Field | Type and limit | Default |
| --- | --- | --- |
| `space_id` | UUID; immutable relation to owned space | Required |
| `title` | Text, trimmed length 1–80 | Required |
| `description` | Text, at most 400 characters | Empty text |
| `region` | One region value below | `null` |
| `device_types` | Array of allowed device type values, at most 6 | `[]` |
| `device_count` | Integer 0–10,000 | `null` |
| `member_count` | Integer 0–10,000 | `null` |
| `cpu_threads` | Integer 0–1,000,000 | `null` |
| `memory_gb` | Integer 0–100,000,000 | `null` |
| `gpu_count` | Integer 0–100,000 | `null` |
| `api_types` | Array of allowed API types, at most 5 | `[]` |
| `published` | Boolean | `false` |
| `updated_at` | Server-generated timestamp; not writable by app clients | Server time |

`null` means not disclosed; zero is an explicitly reported zero. Treat all capacity as owner-reported, not automatically verified availability.

Region values: `africa`, `asia`, `europe`, `north-america`, `south-america`, `oceania`, `antarctica`.

Device type values: `mac`, `windows`, `linux`, `server`, `mobile`, `other`.

API type values: `ollama`, `openai-compatible`, `jupyter`, `mcp`, `custom`. These advertise capability categories, never connection URLs or credentials.

## Requests and decisions

Request capabilities are one or more of: `files-read`, `files-write`, `notebooks-run`, `terminal-run`, `models-use`, `api-use`. They describe requested access, not granted access. Each owner must later choose actual devices, paths, resources, limits and expiry locally.

`display_name` has trimmed length 1–60; `message` defaults to empty and has maximum length 500. `id`, `requester_id`, timestamps and initial `status` are server defaults. App clients may explicitly set their own `requester_id`; another ID is rejected. Identity/content is immutable after creation.

| Actor | Current status | Allowed next status |
| --- | --- | --- |
| Owner | `pending` | `accepted_pending_device`, `declined` |
| Owner | `accepted_pending_device` | `declined` |
| Requester | `pending`, `accepted_pending_device` | `cancelled` |

All other transitions are rejected. Accepted requests remain **awaiting local device approval**. A signed-in account, a UUID, an accepted request or a database administrator change must never be sufficient to run a command on someone's machine. The local installation must enforce its own permissions and explicitly provision/revoke them.

A request can be submitted only to a published listing owned by somebody else. A requester has one retained request per space and a maximum of 50 retained requests total. An owner has a maximum of 20 spaces. Database advisory locks serialize cap checks for the same account. Ordinary app users cannot delete requests or reset terminal request states. Deleting a space removes its central requests. These limits provide a bound for the initial invite-only release, not comprehensive anti-abuse protection for open registration.

## Public statistics

`directory_public_stats()` returns:

```json
[{"public_spaces":0,"reported_devices":0,"reported_members":0,
  "reported_cpu_threads":0,"reported_memory_gb":0,"reported_gpus":0}]
```

It is a security-invoker SQL function and always filters `published=true`. Counts sum only fields owners chose to disclose. `reported_members` is reported memberships, not distinct people. The same person/device can be reported in multiple spaces. These totals are not registered accounts, all installations, online users or currently reachable capacity. Missing disclosure contributes zero to aggregate totals, not evidence that an installation has zero capacity.

## Deployment and verification

The reviewed canonical script is `supabase/directory-schema.sql`. It is intentionally outside `supabase/migrations/` because no migration timestamp was fabricated without the CLI. Apply it once as a named Supabase migration, and record the returned migration version in deployment evidence. Do not rerun it over an existing deployment without a separately reviewed incremental migration.

The script explicitly grants API privileges, enables/forces RLS on all three exposed tables, separates anonymous public reads from ownership policies, limits insert/update columns, uses private security-invoker trigger functions, and restricts the stats RPC to read-only aggregates. No security-definer function or privileged frontend credential is needed.

Run `tests/directory-rls.sql` in an administrative SQL session. It must use synthetic users inside one transaction and end with `ROLLBACK`. It does not send invitations or messages. Verify anonymous reads, draft privacy, ownership isolation, requester spoofing, immutable columns, state transitions, withdrawn-listing behavior and account limits. Then run Supabase security/performance advisors and test the public REST response with the publishable key. Tests of SQL policies do not substitute for a browser end-to-end test or local device-authorization tests.
