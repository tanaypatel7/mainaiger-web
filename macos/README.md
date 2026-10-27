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

This developer build is neither notarized nor a downloadable public installer.
Build, launching, key handling, Docker detection and runtime behavior need real
Mac validation. Packaging is architecture-specific. A release needs a reviewed
dependency/build process, Developer ID signing, notarization, and upgrade and
uninstall procedures. The first build may be much larger than the launcher
source because it contains Python and its libraries; Docker and its image are
additional downloads. No auto-start, public listener or website pairing is
provided by this launcher.
