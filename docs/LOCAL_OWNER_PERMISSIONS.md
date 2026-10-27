# Local owner permissions — first functional increment

The `companion/` application is a separate local permission panel and read-only resource API. It does not modify the existing Nexus installation, replace MainAIger Lab, run an Ollama model, execute a notebook or launch terminal commands. It deliberately does not accept website logins, cloud JWTs or a directory request's accepted status as device authority.

## What works

- Independent local owner key, stored only in an owner-only local file.
- Same-origin owner panel at `http://127.0.0.1:8793`, with owner sessions, CSRF protection and exact Host validation.
- One explicitly selected local folder, registered separately from private application state.
- Import of the website's request JSON as an untrusted suggestion. No remote URL is fetched and no requested operation is automatically approved.
- Owner review and creation of a capability for an exact local resource and selected read-only operations, expiring within seven days.
- Capability token displayed once. Only its SHA-256 hash is stored in SQLite.
- Per-request permission checks, expiration and revocation.
- Local non-browser API: `resource.info`, `files.list`, `files.read` only.
- POSIX descriptor-relative, no-follow traversal. Absolute paths, parent traversal, symlinks, hardlinks, nonregular files and state/workspace overlap are rejected. Reads support UTF-8 files of at most 256 KiB. Directory responses inspect at most 200 entries.

There is no telemetry, geolocation collection, subnet scan, cloud synchronization or code execution in this companion.

## Run locally on Mac or Linux

Requires Python 3.11 or later. Run these commands from the repository root. Choose a dedicated folder containing only material you intend to make readable. Keep private state outside that folder.

```bash
mkdir -p "$HOME/MainAIger-Shared"
python3 -m companion \
  --state-dir "$HOME/.local/share/mainaiger-permissions" \
  --workspace "$HOME/MainAIger-Shared"
```

The application prints the local panel URL and the path of `owner.key`, never its value. Open the key file locally and paste its contents into the panel's owner sign-in. On a Mac, a separate Terminal tab can open that file with:

```bash
open -a TextEdit "$HOME/.local/share/mainaiger-permissions/owner.key"
```

Do not paste this owner key into mainaiger.org, Supabase, a chat or another person's machine. The ordinary website login cannot unlock this panel. A local owner with control of the OS account can access this state, so this is not protection against a compromised host or malicious code already running as that user.

To stop the local panel, press Ctrl-C in its terminal. State and grants remain saved. Restarting ends the old browser sessions. Existing capabilities remain valid until expiration or revocation.

## Optional container package

Docker Desktop must already be installed and running on Mac. This package is supplied for local testing; it has not been validated on your Mac or Nexus, and the image build has not been run in the development environment.

From the repository root:

```bash
mkdir -p "$HOME/MainAIger-Shared"
export MAINAIGER_WORKSPACE="$HOME/MainAIger-Shared"
docker compose -f companion/compose.yaml up --build
```

In another terminal, retrieve your local owner key from the private named volume:

```bash
docker compose -f companion/compose.yaml exec companion cat /state/owner.key
```

Open `http://127.0.0.1:8793`, paste that key, and clear it from your clipboard after use. This prototype still requires these installation commands; it is not the finished guided desktop installer.

The supplied Compose file:

- Publishes only host `127.0.0.1:8793`.
- Uses a nonroot UID, a read-only root filesystem, dropped Linux capabilities and `no-new-privileges`.
- Mounts only your selected folder at `/workspace`, read-only, and a named private state volume at `/state`.
- Uses an internal Docker network and no outbound cloud service connection.
- Does not mount a Docker socket, SSH key directory, complete home directory or host filesystem.
- Limits memory, CPU and process count.

Inside the container the application must listen on its container interface for Docker port forwarding; this is enabled only by explicit `--container-listen` and the container environment flag. Do not change the host port mapping to `0.0.0.0`, use host networking or publish this service through a tunnel. Remote pairing is not implemented.

The folder's host permissions must allow UID 10001 to read the intended files. Do not recursively make a home directory public to solve a permissions error. Select a dedicated readable folder instead. An existing named volume with incorrect ownership requires an explicit owner repair; the application fails rather than relaxing its state-file checks.

Stop with `docker compose -f companion/compose.yaml down`. This preserves the state volume. Do not add `--volumes` unless you intentionally want to delete owner identity and all grants.

## Review a directory request locally

1. In the public directory, the owner accepts a request as **accepted, pending device setup**.
2. Download that request's JSON file from the website.
3. Sign into this local panel and select that file under **Optional request JSON**.
4. Review the requester and space IDs. The file is unverified input; verify the recipient separately.
5. Select a local resource, explicitly choose supported read-only operations and set expiry.
6. Select **Review exact permission**, inspect the frozen values, then **Approve and generate private capability**.
7. Keep the returned capability receipt private. It is displayed once and cannot be retrieved later from the database. If it is lost, revoke the grant and create a new one.

The accepted request export contract is:

```json
{
  "version": 1,
  "request_id": "directory-request-id",
  "space_id": "directory-space-id",
  "requester_id": "directory-user-id",
  "display_name": "Chosen requester name",
  "requested_capabilities": ["files-read"],
  "status": "accepted_pending_device"
}
```

No tokens, endpoint URLs, local paths or executable code belong in that export. Unsupported requested capabilities, including terminal, notebook and model execution, remain unsupported. Directory acceptance neither creates a token nor changes the local filesystem or network.

The receipt associates `request_id`, `space_id` and `requester_id` with the owner's chosen local resource and operations. This is an audit association, **not cryptographic proof of the recipient's identity**. A bearer capability works for whoever possesses it. Do not publish it or upload it back to the directory. Secure delivery and identity-bound remote pairing remain a separate implementation step; manual delivery alone does not make a private machine reachable.

## Test a capability on the same machine

This example prompts without echoing the capability. Run from a separate terminal on the same machine as the panel. Use the token from the one-time receipt, not the owner key. It only requests the resource label and granted operation names.

```python
import getpass, json, urllib.request
token = getpass.getpass('Local capability token: ')
request = urllib.request.Request(
    'http://127.0.0.1:8793/api/resource',
    data=json.dumps({'resource_id': 'workspace', 'operation': 'resource.info'}).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    method='POST',
)
with urllib.request.urlopen(request, timeout=10) as response:
    print(json.load(response))
```

The same endpoint accepts `files.list` with a relative folder `path`, or `files.read` with a relative text-file `path`, only when the token grants that operation. No endpoint accepts shell commands. Requests carrying browser Origin or Cookie headers are rejected, and no CORS permission is returned. The GitHub frontend therefore cannot call this local API yet.

## Integrating the existing private MainAIger installation later

The inspected MainAIger Development Studio archive is a single-owner backend. Its `mainaiger.py` owner authorization and `lab/broker.py` RPC dispatch currently give an owner access to the configured operation set. Its `lab/host.py` runs Python with the enrolled OS account's permissions, not an arbitrary-code sandbox.

Keep directory publishing/request review separate from those execution endpoints. A future adapter can call this permission policy before every operation, but must also validate the remote principal, device pairing, operation/resource mapping and revocation at the actual execution host. It must not pass a visitor the existing owner, connector or host token. Files and output should pass directly through the owner's chosen interface/transport, not through the central directory.

Before adding terminal or notebook access, introduce isolated per-user execution, scoped filesystem mounts, network/secret controls, quotas and job cancellation. A read-only API or a GUI around a command does not provide that isolation.

## Verification and limits

Run the local behavioral and HTTP checks:

```bash
python3 -m unittest discover -s tests -p test_local_permissions.py -v
node --check companion/ui/app.js
```

Tests cover direct file reads, denial of cloud status/JWT/owner-key substitution, operation/resource isolation, expiry/revocation, path traversal, symlinks/hardlinks/FIFO/oversized files, state exclusion, private storage, owner origin/session/CSRF checks, Host checks and closed browser access. No live Nexus deployment, Mac acceptance, Docker image execution, remote website pairing, federated identity or arbitrary-code sandbox is claimed.

Revocation blocks subsequent authorization checks. It cannot recall information a recipient already read or reliably stop an in-progress read. The owner panel stores local recipient/grant metadata; it sends none of it to the central service.
