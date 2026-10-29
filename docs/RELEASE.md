# Getting a downloadable MainAIger Lab build

The workflow `.github/workflows/release-macos.yml` builds the Mac app on
GitHub's macOS runners (Apple silicon and Intel), runs the test suites there,
ad-hoc signs the app, packs it into a `.dmg` with a SHA-256 file and performs a
smoke test of the bundled service. Nothing in this pipeline deploys the
website, changes DNS or contacts any other service.

**What this is not:** the app is signed with an ad-hoc signature, not an Apple
Developer ID, and it is not notarized. macOS Gatekeeper will warn before the
first launch. A release that opens without a warning needs an Apple Developer
Program membership, Developer ID signing and notarization; that is a separate
later step and must not be described as done until it is.

## Download a build without a terminal

Every push to `main` or a `feature/**` branch that touches the Lab produces
run artifacts:

1. Open the repository on GitHub → **Actions** → **Build MainAIger Lab for macOS**.
2. Select the newest successful run for your branch.
3. Under **Artifacts**, download `MainAIger-Lab-<version>-arm64` for an Apple
   silicon Mac or `…-x86_64` for an Intel Mac. (Artifacts require being signed
   in to GitHub and expire after 30 days.)
4. Double-click the downloaded `.zip` in Finder; it contains the `.dmg` and a
   `.dmg.sha256` file.

A public pre-release is produced when a tag named `lab-v<version>` exists, for
example `lab-v0.1.0`. To create one from the web UI: **Releases** → **Draft a
new release** → type the new tag `lab-v0.1.0` → publish. The workflow builds
both architectures and attaches the disk images and checksums to that release.

## Verify the download

The `.dmg.sha256` file holds the checksum the workflow computed. macOS has no
built-in graphical checksum viewer, so this check currently needs either a
checksum utility app or one terminal command: `shasum -a 256 <file>.dmg`. The
download already travels over HTTPS from GitHub; the checksum is an extra check
you can skip for a developer preview if you prefer to stay out of the terminal.
A mismatch means the file is not the one the workflow built; do not open it.

## Open the app for the first time (Gatekeeper)

1. Open the `.dmg` and drag **MainAIger Lab** to **Applications**.
2. Open it from Applications. macOS shows *"MainAIger Lab" cannot be opened
   because Apple cannot verify it* (wording varies by macOS version). Close
   that dialog.
3. Open **System Settings → Privacy & Security**, scroll to **Security**, and
   select **Open Anyway** next to the MainAIger Lab message. Confirm with your
   password or Touch ID. On older macOS versions, right-click the app →
   **Open** → **Open** does the same.
4. The launcher window appears, starts the local service and opens the Lab
   page in your browser. Select **Open owner key** to unlock it.

This prompt appears because the app has no Developer ID signature. It is the
expected behavior for this developer preview, not a sign of tampering, as long
as the checksum matched.

## Status

- **Tested (GitHub runners, 27 Oct 2026):** both architectures built, were
  ad-hoc signed and packaged, and passed the smoke test: the bundled service
  started, served the panel on loopback and contained the kernel program
  (run 40554004016, ~9.5 MB per disk image). The full Python and Node test
  suites also pass on macOS runners.
- **Unverified:** that the downloaded app opens on a real Mac after the
  Gatekeeper step, that Docker Desktop installation from the setup Mainbook
  works, and that a session container starts. Those need a real Mac.
- **Future:** Developer ID signing and notarization; a download button on
  mainaiger.org (changing `site/` deploys the website and is a separate,
  explicitly approved step); automatic update checks.
