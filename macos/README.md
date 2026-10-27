# macOS developer launcher

This is an early developer build, not a signed or notarized release. Build and
test it on the Mac architecture being targeted. The builder needs Python 3,
Xcode Command Line Tools, network access to download pinned PyInstaller, and
permission to write the repository's `build/macos-dev` directory. The resulting
app bundles the Python service, so a person running the built app does not need
to install Python separately. Docker Desktop and the Python image are still
separate requirements; the Lab setup page explains their status.

From a repository checkout on a Mac, run:

```bash
bash macos/build-dev-app.sh
open 'build/macos-dev/MainAIger Lab.app'
```

The launcher starts the bundled service at `127.0.0.1:8794`, waits for its
readiness message, and opens the local browser. If that port is already in use,
the service exits and the browser should not open. Select **Open owner key** in
the launcher, copy the private key from TextEdit into the local Lab login, and
close the key file. Do not send the key to anyone or enter it on a website. The
key is stored in `~/Library/Application Support/MainAIger Lab/owner.key`, in a
private directory checked by the service. **Quit Lab** stops this service.

## Setup Mainbook

After unlocking the local page:

1. Select **Run check** to inspect this Mac and detect Docker Desktop. The check
   looks for Docker's user-level CLI as well as its standard application path.
2. If Docker is absent, select **Review download**, review the source and disk
   and network access, then approve. The cell shows download progress. A staged
   installer is stored in the Lab's private `installers` state directory.
3. Select **Review and open installer**, then approve. MainAIger checks the
   staged file again before asking macOS to open the disk image. In the native
   window, move Docker to Applications, start Docker, and respond to Docker's
   terms and any macOS prompts yourself. MainAIger does not accept the terms
   or collect an administrator password.
4. Return to the Mainbook and select **Check Docker again** until the local
   Docker Desktop engine is ready. Then review and download the Python image.
5. Select limits, review the example code, and approve the run. The optional
   folder is read-only and settings last only for the current session.
6. Optionally select **Review and start session** to keep one kernel container
   running so variables persist between cells; see `docs/LOCAL_LAB_SETUP.md`.
   The build bundles `local_lab/kernel/` and bind-mounts it read-only into the
   container; that mount from inside the app bundle is untested on a Mac.

If Docker is already installed, start the existing app and run the check; the
setup does not overwrite it. An incomplete or rejected download does not open
the installer. Cancelling an active download from the GUI is not implemented.

The installer is pinned to Docker Desktop **4.96.3**, build **244472**, for
Apple silicon and Intel Macs. The backend downloads from Docker's versioned
HTTPS URL, checks its pinned SHA-256 against the published release checksum,
mounts the image read-only, verifies the expected Docker app signing identity,
and detaches the verification volume. Before opening the staged image it
repeats the checks. A failed check blocks opening. This verification path and
the mounted image still need real Mac acceptance testing; a signature failure
must be investigated rather than bypassed. The separate Python image uses a
mutable tag and is **not** pinned by digest in this preview.

Maintainers: Docker removes older releases over time. To refresh the pinned
installer, select a supported release from the [official Docker release notes](https://docs.docker.com/desktop/release-notes/),
independently check the versioned Mac URLs and published SHA-256 checksums for
both architectures, update the build and hashes in `local_lab/setup.py`, and
test download and signing verification on real Macs. Do not replace the pinned
hash with a checksum fetched from an unverified source at install time.

This developer build is neither notarized nor a downloadable public installer.
The earlier launcher was opened on one Apple-silicon Mac after fixing its Swift
deployment target. The new installer download, mount, signature verification,
Docker installation and container execution have not yet been validated on a
Mac. Automated setup tests use mocked network and system operations and cannot
establish those real behaviors. Packaging is architecture-specific. A release needs a reviewed
dependency/build process, Developer ID signing, notarization, and upgrade and
uninstall procedures. The first build may be much larger than the launcher
source because it contains Python and its libraries; Docker and its image are
additional downloads. No auto-start, public listener or website pairing is
provided by this launcher.
