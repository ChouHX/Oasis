#!/usr/bin/env bash
# Cross-build the Windows single-file exe from Linux, using Wine + Windows Python.
#
#   ./build_windows_wine.sh
#
# Produces dist/OasisConsole.exe. Requires: wine (9.x), curl, and network access
# to python.org / PyPI on first run. The Wine prefix and the Windows Python are
# created once under ~/.wine; later runs only re-stage the source and rebuild.
#
# Why Qt 6.6.1 is pinned: PyInstaller has to import every module it packages, and
# Qt 6.11's DLLs do not load under Wine 9 (missing newer Windows APIs). 6.6.x
# loads fine and is a stable release on real Windows too.
set -euo pipefail

PYVER="3.12.10"
PYDIR='C:\Python312'
PREFIX="${WINEPREFIX:-$HOME/.wine}"
STAGE="$PREFIX/drive_c/build/oasis_gui"
HERE="$(cd "$(dirname "$0")" && pwd)"

export WINEDEBUG=-all
# Wine processes inherit these; pip and urllib honour them.
export http_proxy="${http_proxy:-http://127.0.0.1:10808/}"
export https_proxy="${https_proxy:-http://127.0.0.1:10808/}"

wine_py() { wine "$PYDIR\\python.exe" "$@"; }

# ---------------------------------------------------------------- 1. wine prefix
if [ ! -d "$PREFIX/drive_c/windows" ]; then
    echo "[1/4] initialising wine prefix"
    wineboot -i
fi

# --------------------------------------------------------- 2. windows python
if ! wine_py -V >/dev/null 2>&1; then
    echo "[2/4] installing Windows Python $PYVER into $PREFIX"
    mkdir -p /tmp/wbuild && cd /tmp/wbuild
    [ -f python-installer.exe ] || curl -sL -o python-installer.exe \
        "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"
    wine python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 \
        Include_test=0 Include_launcher=0 TargetDir="$PYDIR"
    wine_py -V
else
    echo "[2/4] Windows Python already present: $(wine_py -V 2>&1 | tail -1)"
fi

# ------------------------------------------------------------- 3. dependencies
echo "[3/4] installing build dependencies"
wine_py -m pip install -q --upgrade pip
wine_py -m pip install -q \
    "PyQt6==6.6.1" "PyQt6-Qt6==6.6.1" \
    PyQt6-Fluent-Widgets PySocks pyinstaller

# ------------------------------------------------------------------- 4. build
echo "[4/4] staging source and building"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -r "$HERE/app.py" "$HERE/core" "$HERE/OasisConsole.spec" "$STAGE/"
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cd "$STAGE"
wine_py -m PyInstaller --noconfirm --clean OasisConsole.spec

mkdir -p "$HERE/dist"
cp "$STAGE/dist/OasisConsole.exe" "$HERE/dist/"
echo
echo "done: $HERE/dist/OasisConsole.exe"
ls -la "$HERE/dist/"
