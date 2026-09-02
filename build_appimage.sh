#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$SCRIPT_DIR/NASBackupManager.AppDir"
ARCH="$(uname -m)"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

cleanup() { rm -rf "$APPDIR" "$SCRIPT_DIR/build"; }
trap cleanup EXIT

mkdir -p "$SCRIPT_DIR/build"

# ── Dependencies ────────────────────────────────────────────────
info "Setting up Python environment..."
if [ ! -d "$SCRIPT_DIR/build/.venv" ]; then
    python3 -m venv "$SCRIPT_DIR/build/.venv"
fi
source "$SCRIPT_DIR/build/.venv/bin/activate"

pip install --quiet --upgrade pip
pip install --quiet pyinstaller pyside6 2>/dev/null \
    || pip install --quiet --pre pyinstaller pyside6

# ── PyInstaller build (onedir — all Qt libs stay as files) ─────
info "Building with PyInstaller (onedir)..."
cd "$SCRIPT_DIR"
pyinstaller \
    --name backup_manager_qt \
    --windowed \
    --onedir \
    --icon=nas-backup-manager.svg \
    --hidden-import=PySide6.QtCore \
    --hidden-import=PySide6.QtGui \
    --hidden-import=PySide6.QtWidgets \
    backup_manager_qt.py

[ -f "dist/backup_manager_qt/backup_manager_qt" ] \
    || fail "PyInstaller build failed — dist/backup_manager_qt/backup_manager_qt not found"

# ── Build AppDir from the onedir output ────────────────────────
info "Creating AppDir..."
rm -rf "$APPDIR"

# PyInstaller onedir output is already a self-contained directory.
# Put it under usr/ so the AppDir structure is clean.
mkdir -p "$APPDIR/usr"
cp -r dist/backup_manager_qt "$APPDIR/usr/app"

# Desktop file + icon at AppDir root (appimagetool requires this)
cp nas-backup-manager.desktop "$APPDIR/"
cp nas-backup-manager.svg "$APPDIR/"
ln -sf nas-backup-manager.svg "$APPDIR/.DirIcon"

# Also place icon in standard hicolor locations
for size in 16 32 64 128 256; do
    mkdir -p "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps"
    cp nas-backup-manager.svg "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps/nas-backup-manager.svg"
done
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp nas-backup-manager.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/"

# Also install the desktop file in the standard location
mkdir -p "$APPDIR/usr/share/applications"
cp nas-backup-manager.desktop "$APPDIR/usr/share/applications/"

# AppRun: launch the PyInstaller binary
cat > "$APPDIR/AppRun" << 'APPRUN'
#!/bin/bash
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/usr/app/backup_manager_qt" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# ── Package with appimagetool ──────────────────────────────────
APPIMAGETOOL="$SCRIPT_DIR/build/appimagetool"
if [ ! -f "$APPIMAGETOOL" ]; then
    info "Downloading appimagetool..."
    wget -q -O "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

info "Creating AppImage..."
cd "$SCRIPT_DIR"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "NAS_Backup_Manager-${ARCH}.AppImage"

info "Done: $(ls -lh NAS_Backup_Manager-*.AppImage 2>/dev/null | tail -1)"
