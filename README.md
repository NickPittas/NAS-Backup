# NAS Backup Manager

A Qt/PySide6 desktop app for scanning, filtering, and copying `.aep` and `.nk` project files between NAS locations, with built-in scheduling.

## Features

- **Scan & diff** — compares source and destination by modification time, shows what needs copying
- **Smart excludes** — comma-separated patterns (e.g. `#Backup, .backup, *backup*`), case-insensitive, matches any path component
- **Parallel copy** — configurable worker threads (1–16), existing files are renamed before overwrite
- **Scheduler** — run automatically on a fixed interval (default 240 min) or daily at a set time
- **Auto-save** — all settings (paths, interval, excludes, workers, schedule) persist immediately to `~/.config/NAS Backup Manager/config.json`
- **Import/Export** — save and load job configurations as JSON files
- **AppImage** — portable Linux build, no install needed

## Requirements

- Python 3.10+
- PySide6

```bash
pip install PySide6
```

## Usage

### GUI

```bash
python3 backup_manager_qt.py
```

### Headless (scan only)

```bash
python3 backup_manager_qt.py --dry-run --source /mnt/Projects --destination /mnt/Backups
```

### Run an exported job

```bash
python3 backup_manager_qt.py --job backup_job.json
python3 backup_manager_qt.py --job backup_job.json --job-dry-run  # scan only
```

### AppImage

Download `NAS_Backup_Manager-x86_64.AppImage` from [Releases](../../releases) or build from source:

```bash
./build_appimage.sh
```

The output is a self-contained `.AppImage` — move it anywhere, no install required.

## Building the AppImage

```bash
./build_appimage.sh
```

This will:
1. Create an isolated Python venv in `build/`
2. Install PyInstaller and PySide6
3. Build a self-contained directory with all Qt libs bundled
4. Package it as `NAS_Backup_Manager-x86_64.AppImage`

No external tools needed — `appimagetool` is downloaded automatically.

## Configuration

Settings are stored in `~/.config/NAS Backup Manager/config.json`:

```json
{
  "source": "/mnt/Projects",
  "destination": "/mnt/Backups",
  "excludes": "#Backup",
  "workers": 4,
  "schedule_enabled": true,
  "schedule_mode": "interval",
  "schedule_interval_minutes": 240,
  "schedule_time": "23:00"
}
```

All fields are optional — missing values fall back to defaults.

## Project Structure

```
backup_manager.py        Backup engine + Tkinter GUI + headless mode
backup_manager_qt.py     PySide6 GUI (recommended)
build_appimage.sh        AppImage build script
nas-backup-manager.desktop  freedesktop entry
nas-backup-manager.svg      app icon
```

## License

MIT
