#!/bin/bash
# Package the developer-built app into a downloadable disk image.
#
# Usage: bash macos/package-release.sh <version>
#   <version>  e.g. 0.1.0 or 0.1.0-dev.1a2b3c4; written into the app's Info.plist
#
# Input : build/macos-dev/MainAIger Lab.app (from macos/build-dev-app.sh)
# Output: dist/MainAIger-Lab-<version>-<arch>.dmg and a matching .sha256 file
#
# The app is signed with an ad-hoc signature ("-"): every binary gets a valid
# signature so macOS will run it, but there is no Apple Developer ID and no
# notarization. Gatekeeper therefore warns before the first launch; see
# docs/RELEASE.md. Signing with a real Developer ID is a later, separate step.
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Package the app on a Mac.' >&2
  exit 1
fi
version=${1:-}
if [[ -z "$version" || ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][A-Za-z0-9.]+)?$ ]]; then
  echo "Usage: $0 <version>   (for example 0.1.0 or 0.1.0-dev.1a2b3c4)" >&2
  exit 1
fi
root=$(cd "$(dirname "$0")/.." && pwd)
arch=$(uname -m)
app="$root/build/macos-dev/MainAIger Lab.app"
dist="$root/dist"
name="MainAIger-Lab-$version-$arch"
[[ -d "$app" ]] || { echo "Build the app first: bash macos/build-dev-app.sh" >&2; exit 1; }
mkdir -p "$dist"

# Record the version and a build number derived from the date so Finder shows it.
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(date -u +%Y%m%d%H%M)" "$app/Contents/Info.plist"

# Ad-hoc sign the bundled service binaries and libraries, then the launcher bundle.
# PyInstaller's onedir output contains many Mach-O files; each needs a signature
# on Apple silicon or the OS refuses to load it.
find "$app/Contents/Resources/mainaiger-lab-service" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 \
  | while IFS= read -r -d '' file; do
      if file "$file" | grep -q 'Mach-O'; then
        codesign --force --sign - --timestamp=none "$file"
      fi
    done
codesign --force --sign - --timestamp=none "$app/Contents/Resources/mainaiger-lab-service/mainaiger-lab-service"
codesign --force --sign - --timestamp=none --identifier org.mainaiger.lab.dev "$app"
codesign --verify --strict --verbose=2 "$app"

# Stage the app plus an Applications shortcut, then build a compressed, read-only image.
stage=$(mktemp -d "$dist/stage-XXXXXX")
cp -R "$app" "$stage/"
ln -s /Applications "$stage/Applications"
rm -f "$dist/$name.dmg"
hdiutil create -volname "MainAIger Lab" -srcfolder "$stage" -ov -format UDZO -fs HFS+ "$dist/$name.dmg" >/dev/null
rm -rf "$stage"

# Checksum the image so a downloader can verify it against the release page.
( cd "$dist" && shasum -a 256 "$name.dmg" > "$name.dmg.sha256" && cat "$name.dmg.sha256" )
echo "Packaged: $dist/$name.dmg (ad-hoc signed, not notarized)"
