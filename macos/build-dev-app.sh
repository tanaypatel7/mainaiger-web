#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Build this developer app on a Mac with Xcode Command Line Tools and Python 3.' >&2
  exit 1
fi
root=$(cd "$(dirname "$0")/.." && pwd)
build="$root/build/macos-dev"
app="$build/MainAIger Lab.app"
mkdir -p "$build"
python3 -m venv "$build/venv"
"$build/venv/bin/python" -m pip install 'pyinstaller==6.16.0'
"$build/venv/bin/python" -m PyInstaller --noconfirm --clean --onedir \
  --name mainaiger-lab-service --paths "$root" \
  --add-data "$root/local_lab/ui:local_lab/ui" \
  --distpath "$build/dist" --workpath "$build/work" --specpath "$build" \
  "$root/macos/service_entry.py"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
rm -rf "$app/Contents/Resources/mainaiger-lab-service"
cp -R "$build/dist/mainaiger-lab-service" "$app/Contents/Resources/"
xcrun swiftc -O "$root/macos/Launcher.swift" -o "$app/Contents/MacOS/MainAIgerLab"
cp "$root/macos/Info.plist" "$app/Contents/Info.plist"
echo "Developer build created: $app"
echo 'This build is unsigned and has not been notarized or tested as a release.'
