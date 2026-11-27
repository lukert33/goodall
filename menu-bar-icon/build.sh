#!/bin/bash
# Build the menu bar icon's helper, goodall.app, from main.swift (ADR 0024).
#
#     menu-bar-icon/build.sh [output folder]
#
# The application is put in the output folder, which defaults to
# menu-bar-icon/build/ (listed in .gitignore: no compiled program is committed).
# It needs only Apple's Command Line Tools (`xcode-select --install`), not
# Xcode, and builds for Macs with Apple chips, the only Macs goodall supports.
#
# This script only builds. It installs nothing: docs/menu-bar-icon.md says
# how to try the result by hand, and installing it is onboarding's job.
#
# An application on a Mac is a folder with a fixed layout:
#
#     goodall.app/Contents/Info.plist                    what macOS should know about it
#     goodall.app/Contents/MacOS/goodall-menu-bar-icon   the compiled program
#
# The last step signs it "ad hoc" (`-s -`): a signature made on this Mac with
# no Apple developer account, which is enough for macOS to run it here.

set -euo pipefail

source_folder="$(cd "$(dirname "$0")" && pwd)"
output_folder="${1:-$source_folder/build}"
application="$output_folder/goodall.app"
program="$application/Contents/MacOS/goodall-menu-bar-icon"

# The helper carries goodall's own version, read from the line `version = "..."`.
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$source_folder/../pyproject.toml" | head -n 1)"

rm -rf "$application"
mkdir -p "$application/Contents/MacOS"
cp -f "$source_folder/Info.plist" "$application/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$version" "$application/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$version" "$application/Contents/Info.plist"

swiftc -O -target arm64-apple-macos13.0 "$source_folder/main.swift" -o "$program"
codesign --force -s - "$application"

echo "Built $application"
