#!/usr/bin/env bash
#
# Build a macOS .app bundle for Bill Generator using PyInstaller.
#
# Usage:
#   ./build_macos.sh
#
# Output:
#   dist/BillGenerator_V4.5.1.app
#   dist/BillGenerator_V4.5.1     (CLI binary, if you want to run from terminal)
#
# Requirements:
#   * Python 3.11+ (Homebrew install recommended: `brew install python@3.11`)
#   * Xcode Command Line Tools (`xcode-select --install`)
#
# Notes:
#   * The resulting .app is unsigned. Gatekeeper will warn on first
#     launch — right-click the app and choose "Open" to bypass.
#     For distribution outside your own machine, sign and notarize it
#     (see https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution).
#   * The bundle is architecture-specific. Run this on the Mac whose
#     architecture (arm64 / x86_64) you want to ship for.

set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="BillGenerator_V4.5.1"
BUILD_VENV=".build-venv"

echo "====================================================="
echo "  Building ${APP_NAME} for macOS"
echo "====================================================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed. Install Python 3.11+ (e.g. brew install python@3.11) and try again."
  exit 1
fi

if [ ! -d "${BUILD_VENV}" ]; then
  echo "Creating local build environment at ${BUILD_VENV}..."
  python3 -m venv "${BUILD_VENV}"
else
  echo "Reusing local build environment at ${BUILD_VENV}..."
fi

# shellcheck source=/dev/null
source "${BUILD_VENV}/bin/activate"

echo ""
echo "Installing or updating build tools..."
python -m pip install --upgrade pip setuptools wheel

echo ""
echo "Installing project dependencies..."
python -m pip install -r requirements.txt

echo ""
echo "Installing PyInstaller..."
python -m pip install --upgrade pyinstaller

echo ""
echo "Cleaning old build folders..."
rm -rf build dist

export BG_DESKTOP=1

echo ""
echo "Running PyInstaller build..."
python -m PyInstaller --noconsole --windowed --onefile \
  --name "${APP_NAME}" \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --add-data "db:db" \
  --hidden-import jinja2.ext \
  --hidden-import waitress \
  desktop_launcher.py

echo ""
echo "====================================================="
echo "Build complete!"
echo "  App bundle: $(pwd)/dist/${APP_NAME}.app"
echo "  CLI binary: $(pwd)/dist/${APP_NAME}"
echo "====================================================="
