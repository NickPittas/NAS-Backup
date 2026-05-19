#!/usr/bin/env python3
"""Qt/PySide6 UI for NAS Backup Manager.

This keeps the same backup engine/job behavior as backup_manager.py but replaces
Tkinter with a more polished PySide6 interface.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot, QTimer, QTime
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from backup_manager import BackupConfig, BackupEngine, headless_dry_run, run_job

CONFIG_DIR = Path.home() / ".config" / "NAS Backup Manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
APP_NAME = "NAS Backup Manager Qt"


class EngineWorker(QObject):
    finished = Signal(str, object, object)
    log = Signal(str, str)
    progress = Signal(str, int, object)

    def __init__(self, mode, config, changes=None, workers=4):
        super().__init__()
        self.mode = mode
        self.config = config
        self.changes = changes or []
        self.workers = workers
        self.engine = BackupEngine(
            config,
            callbacks={"log": self.log.emit, "progress": self.progress.emit},
        )

    @Slot()
    def run(self):
        try:
            if self.mode == "scan":
                result = self.engine.scan()
            else:
                result = self.engine.copy_files(self.changes, workers=self.workers)
            self.finished.emit(self.mode, result, None)
        except Exception as exc:
            self.finished.emit(self.mode, None, exc)

    def pause(self):
        self.engine.pause()

    def resume(self):
        self.engine.resume()

    def stop(self):
        self.engine.stop()

    @property
    def is_paused(self):
        return self.engine.is_paused


class BackupManagerQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 760)
        self.config = BackupConfig()
        self.pending_changes = []
        self.worker = None
        self.thread = None
        self.copy_start = None
        self.current_log_file = None
        self.run_start_time = None
        self.last_scan_engine = None
        self.copy_engine = None
        self.scheduler_next_run = None
        self.scheduler_pending_copy = False
        self.scheduler_copy_running = False
        self.start_copy_after_thread_finish = False
        self._load_config()
        self._build_ui()
        self._apply_config_to_ui()
        self._apply_theme()
        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.timeout.connect(self._check_schedule)
        self.scheduler_timer.start(30000)
        self._refresh_schedule()

    def _make_card(self, title):
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)
        label = QLabel(title)
        label.setObjectName("CardTitle")
        layout.addWidget(label)
        return card, layout

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 22, 22, 18)
        root.setSpacing(16)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("NAS Backup Manager")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Scan, filter, copy, export, and automate backup jobs")
        subtitle.setObjectName("Subtitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch(1)
        self.import_btn = QPushButton("Import Job")
        self.import_btn.setObjectName("SecondaryButton")
        self.export_btn = QPushButton("Export Job")
        self.export_btn.setObjectName("SecondaryButton")
        self.import_btn.clicked.connect(self._import_job)
        self.export_btn.clicked.connect(self._export_job)
        header.addWidget(self.import_btn)
        header.addWidget(self.export_btn)
        root.addLayout(header)

        settings_card, settings_layout = self._make_card("Job Settings")
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("/mnt/Projects")
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("/mnt/Kessel/Backups")
        self.excludes_edit = QLineEdit()
        self.excludes_edit.setPlaceholderText("#Backup, .backup, *backup*")
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(4)
        src_btn = QPushButton("Browse")
        dst_btn = QPushButton("Browse")
        src_btn.setObjectName("SmallButton")
        dst_btn.setObjectName("SmallButton")
        src_btn.clicked.connect(self._browse_source)
        dst_btn.clicked.connect(self._browse_dest)
        form.addWidget(QLabel("Source"), 0, 0)
        form.addWidget(self.source_edit, 0, 1)
        form.addWidget(src_btn, 0, 2)
        form.addWidget(QLabel("Destination"), 1, 0)
        form.addWidget(self.dest_edit, 1, 1)
        form.addWidget(dst_btn, 1, 2)
        form.addWidget(QLabel("Excludes"), 2, 0)
        form.addWidget(self.excludes_edit, 2, 1)
        helper = QLabel("Comma-separated, case-insensitive. A plain name like #Backup excludes that folder and all children.")
        helper.setObjectName("HelperText")
        form.addWidget(helper, 3, 1)
        form.addWidget(QLabel("Workers"), 2, 2)
        form.addWidget(self.workers_spin, 3, 2)
        settings_layout.addLayout(form)
        root.addWidget(settings_card)

        schedule_card, schedule_layout = self._make_card("Schedule")
        schedule_grid = QGridLayout()
        schedule_grid.setHorizontalSpacing(12)
        schedule_grid.setVerticalSpacing(10)
        self.schedule_enabled = QCheckBox("Run automatically")
        self.schedule_mode = QComboBox()
        self.schedule_mode.addItems(["Every interval", "Daily at time"])
        self.schedule_interval = QSpinBox()
        self.schedule_interval.setRange(1, 10080)
        self.schedule_interval.setSuffix(" min")
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_next = QLabel("Next run: disabled")
        self.schedule_next.setObjectName("HelperText")
        self.schedule_enabled.toggled.connect(self._refresh_schedule)
        self.schedule_mode.currentIndexChanged.connect(self._refresh_schedule)
        self.schedule_interval.valueChanged.connect(self._refresh_schedule)
        self.schedule_time.timeChanged.connect(self._refresh_schedule)
        schedule_grid.addWidget(self.schedule_enabled, 0, 0)
        schedule_grid.addWidget(QLabel("Mode"), 0, 1)
        schedule_grid.addWidget(self.schedule_mode, 0, 2)
        schedule_grid.addWidget(QLabel("Interval"), 1, 1)
        schedule_grid.addWidget(self.schedule_interval, 1, 2)
        schedule_grid.addWidget(QLabel("Daily time"), 2, 1)
        schedule_grid.addWidget(self.schedule_time, 2, 2)
        schedule_grid.addWidget(self.schedule_next, 3, 0, 1, 3)
        schedule_layout.addLayout(schedule_grid)
        root.addWidget(schedule_card)

        actions = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.copy_btn = QPushButton("Copy All")
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        self.scan_btn.setObjectName("PrimaryButton")
        self.copy_btn.setObjectName("SuccessButton")
        self.pause_btn.setObjectName("WarningButton")
        self.stop_btn.setObjectName("DangerButton")
        self.copy_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        for btn in (self.scan_btn, self.copy_btn, self.pause_btn, self.stop_btn, self.import_btn, self.export_btn):
            btn.setMinimumHeight(40)
        self.scan_btn.clicked.connect(self._start_scan)
        self.copy_btn.clicked.connect(self._start_copy)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self._stop_current)
        actions.addWidget(self.scan_btn)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.pause_btn)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        progress_card, progress_layout = self._make_card("Progress")
        self.scan_progress = QProgressBar()
        self.copy_progress = QProgressBar()
        self.scan_progress.setMinimumHeight(30)
        self.copy_progress.setMinimumHeight(30)
        self.scan_status = QLabel("Scan idle")
        self.scan_status.setObjectName("HelperText")
        self.copy_status = QLabel("Copy idle")
        self.copy_status.setObjectName("HelperText")
        progress_layout.addWidget(QLabel("Scan"))
        progress_layout.addWidget(self.scan_progress)
        progress_layout.addWidget(self.scan_status)
        progress_layout.addSpacing(8)
        progress_layout.addWidget(QLabel("Copy"))
        progress_layout.addWidget(self.copy_progress)
        progress_layout.addWidget(self.copy_status)
        root.addWidget(progress_card)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        table_card, table_layout = self._make_card("Files To Process")
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Action", "File"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        table_layout.addWidget(self.table)
        log_card, log_layout = self._make_card("Run Log")
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("JetBrains Mono, monospace", 9))
        log_layout.addWidget(self.log_box)
        splitter.addWidget(table_card)
        splitter.addWidget(log_card)
        splitter.setSizes([460, 190])
        root.addWidget(splitter, 1)

        self.statusBar().showMessage("Ready")
        menu = self.menuBar().addMenu("Job")
        import_action = QAction("Import Job…", self)
        export_action = QAction("Export Job…", self)
        import_action.triggered.connect(self._import_job)
        export_action.triggered.connect(self._export_job)
        menu.addAction(import_action)
        menu.addAction(export_action)

    def _apply_theme(self):
        self.setStyleSheet("""
            QWidget#Root {
                background: #0f172a;
                color: #e5e7eb;
                font-family: Inter, Segoe UI, Arial, sans-serif;
                font-size: 13px;
            }
            QMainWindow, QMenuBar, QStatusBar {
                background: #0f172a;
                color: #e5e7eb;
            }
            QMenuBar::item:selected, QMenu::item:selected { background: #1e293b; }
            QLabel { color: #e5e7eb; }
            QLabel#AppTitle {
                font-size: 28px;
                font-weight: 800;
                color: #f8fafc;
            }
            QLabel#Subtitle, QLabel#HelperText {
                color: #94a3b8;
                font-size: 12px;
            }
            QLabel#CardTitle {
                color: #f8fafc;
                font-size: 15px;
                font-weight: 700;
            }
            QFrame#Card {
                background: #111827;
                border: 1px solid #263244;
                border-radius: 16px;
            }
            QLineEdit, QSpinBox, QTextEdit, QTableWidget {
                background: #0b1220;
                color: #e5e7eb;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 9px 10px;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus, QSpinBox:focus, QTextEdit:focus, QTableWidget:focus {
                border: 1px solid #38bdf8;
            }
            QPushButton {
                background: #1e293b;
                color: #e5e7eb;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 8px 16px;
                font-weight: 700;
            }
            QPushButton:hover { background: #334155; }
            QPushButton:disabled { color: #64748b; background: #111827; border-color: #1f2937; }
            QPushButton#PrimaryButton { background: #2563eb; border-color: #3b82f6; color: white; }
            QPushButton#PrimaryButton:hover { background: #1d4ed8; }
            QPushButton#SuccessButton { background: #16a34a; border-color: #22c55e; color: white; }
            QPushButton#SuccessButton:hover { background: #15803d; }
            QPushButton#WarningButton { background: #ca8a04; border-color: #eab308; color: #111827; }
            QPushButton#DangerButton { background: #dc2626; border-color: #ef4444; color: white; }
            QPushButton#SecondaryButton, QPushButton#SmallButton { background: #0f172a; }
            QProgressBar {
                background: #0b1220;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 12px;
                text-align: center;
                font-weight: 700;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38bdf8, stop:1 #2563eb);
                border-radius: 11px;
            }
            QHeaderView::section {
                background: #1e293b;
                color: #f8fafc;
                border: none;
                padding: 9px;
                font-weight: 700;
            }
            QTableWidget {
                gridline-color: #1f2937;
                alternate-background-color: #101826;
            }
            QTableWidget::item { padding: 8px; }
            QTableWidget::item:selected { background: #1d4ed8; }
            QSplitter::handle { background: #0f172a; height: 8px; }
            QScrollBar:vertical { background: #0b1220; width: 12px; }
            QScrollBar::handle:vertical { background: #334155; border-radius: 6px; }
        """)

    def _refresh_schedule(self):
        if not hasattr(self, "schedule_enabled"):
            return
        self.config = self._config_from_ui()
        self._schedule_next_run()

    def _schedule_next_run(self):
        if not hasattr(self, "schedule_enabled") or not self.schedule_enabled.isChecked():
            self.scheduler_next_run = None
            if hasattr(self, "schedule_next"):
                self.schedule_next.setText("Next run: disabled")
            return
        now = time.time()
        if self.schedule_mode.currentIndex() == 0:
            self.scheduler_next_run = now + self.schedule_interval.value() * 60
        else:
            qtime = self.schedule_time.time()
            now_dt = datetime.now()
            run_dt = now_dt.replace(hour=qtime.hour(), minute=qtime.minute(), second=0, microsecond=0)
            if run_dt.timestamp() <= now:
                run_dt = run_dt + timedelta(days=1)
            self.scheduler_next_run = run_dt.timestamp()
        self.schedule_next.setText(
            "Next run: " + datetime.fromtimestamp(self.scheduler_next_run).strftime("%Y-%m-%d %H:%M:%S")
        )

    def _check_schedule(self):
        if not self.scheduler_next_run or self.worker:
            return
        if time.time() >= self.scheduler_next_run:
            self._log("Info", "Scheduled run starting: scan then copy.")
            self._start_scan_job(auto_copy=True)

    def _browse_source(self):
        path = QFileDialog.getExistingDirectory(self, "Select source", self.source_edit.text() or "/")
        if path:
            self.source_edit.setText(path)

    def _browse_dest(self):
        path = QFileDialog.getExistingDirectory(self, "Select destination", self.dest_edit.text() or "/")
        if path:
            self.dest_edit.setText(path)

    def _config_from_ui(self):
        cfg = BackupConfig()
        cfg.source = self.source_edit.text().strip()
        cfg.destination = self.dest_edit.text().strip()
        cfg.excludes = self.excludes_edit.text().strip()
        cfg.workers = self.workers_spin.value()
        cfg.schedule_enabled = self.schedule_enabled.isChecked()
        cfg.schedule_mode = "daily" if self.schedule_mode.currentIndex() == 1 else "interval"
        cfg.schedule_interval_minutes = self.schedule_interval.value()
        cfg.schedule_time = self.schedule_time.time().toString("HH:mm")
        cfg.last_browse_dir = cfg.source or cfg.destination
        return cfg

    def _apply_config_to_ui(self):
        self.source_edit.setText(self.config.source)
        self.dest_edit.setText(self.config.destination)
        self.excludes_edit.setText(self.config.excludes)
        self.workers_spin.setValue(max(1, min(int(self.config.workers or 4), 16)))
        self.schedule_enabled.setChecked(bool(getattr(self.config, "schedule_enabled", False)))
        self.schedule_mode.setCurrentIndex(1 if getattr(self.config, "schedule_mode", "interval") == "daily" else 0)
        self.schedule_interval.setValue(max(1, int(getattr(self.config, "schedule_interval_minutes", 60) or 60)))
        self.schedule_time.setTime(QTime.fromString(getattr(self.config, "schedule_time", "02:00"), "HH:mm"))

    def _validate_paths(self):
        if not self.source_edit.text().strip() or not self.dest_edit.text().strip():
            QMessageBox.warning(self, "Missing paths", "Please select both source and destination.")
            return False
        return True

    def _start_run_log(self, prefix):
        log_dir = Path(__file__).resolve().parent / ".backup_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.current_log_file = log_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self.run_start_time = time.time()
        self._append_log_file(f"{APP_NAME} {prefix} run")
        self._append_log_file(f"Started: {datetime.now().isoformat(timespec='seconds')}")

    def _append_log_file(self, line):
        if not self.current_log_file:
            return
        with open(self.current_log_file, "a") as f:
            f.write(line + "\n")

    def _write_gui_report(self, mode, engine, changes):
        if not self.current_log_file or not engine:
            return
        self._append_log_file("--- Summary ---")
        self._append_log_file(f"Mode: {mode}")
        self._append_log_file(f"Source: {self.config.source}")
        self._append_log_file(f"Destination: {self.config.destination}")
        self._append_log_file(f"Excludes: {self.config.excludes or '(none)'}")
        elapsed = time.time() - self.run_start_time if self.run_start_time else 0
        self._append_log_file(f"Elapsed seconds: {elapsed:.1f}")
        self._append_log_file(f"Scanned: {engine.stats.get('scanned', 0)}")
        self._append_log_file(f"Skipped by excludes: {engine.stats.get('skipped', 0)}")
        self._append_log_file(f"Needed copy: {engine.stats.get('to_copy', 0)}")
        self._append_log_file(f"Copied: {engine.stats.get('copied', 0)}")
        self._append_log_file(f"Renamed existing: {engine.stats.get('renamed', 0)}")
        self._append_log_file(f"Errors: {engine.stats.get('errors', 0)}")
        self._append_log_file("--- Process summary ---")
        self._append_log_file("File lists intentionally omitted; this report records process totals only.")
        self._append_log_file(f"Finished: {datetime.now().isoformat(timespec='seconds')}")
        self._log("Info", f"Log saved: {self.current_log_file}")

    def _start_scan(self):
        self._start_scan_job(auto_copy=False)

    def _start_scan_job(self, auto_copy=False):
        if not self._validate_paths():
            return
        self.scheduler_pending_copy = auto_copy
        self.config = self._config_from_ui()
        self._start_run_log("scheduled" if auto_copy else "gui_scan")
        self.pending_changes = []
        self.table.setRowCount(0)
        self.copy_btn.setEnabled(False)
        self.scan_progress.setRange(0, 0)
        self.scan_status.setText("Collecting files…")
        self._set_running(True)
        self._run_worker("scan", self.config)

    def _start_copy(self):
        self._start_copy_job(confirm=True)

    def _start_copy_job(self, confirm=True):
        if not self.pending_changes:
            QMessageBox.information(self, "Nothing to copy", "Run a scan first.")
            return
        if confirm:
            answer = QMessageBox.question(
                self,
                "Confirm copy",
                f"Copy {len(self.pending_changes)} file(s)?\nExisting files will be renamed before copying.",
            )
            if answer != QMessageBox.Yes:
                return
        self.config = self._config_from_ui()
        self.scheduler_copy_running = not confirm
        if confirm:
            self._start_run_log("gui_copy")
        self.copy_start = time.time()
        self.copy_progress.setRange(0, len(self.pending_changes))
        self.copy_progress.setValue(0)
        self.copy_status.setText("Starting copy…")
        self._set_running(True)
        self._run_worker("copy", self.config, self.pending_changes, self.config.workers)

    def _run_worker(self, mode, config, changes=None, workers=4):
        self.thread = QThread()
        self.worker = EngineWorker(mode, config, changes=changes, workers=workers)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._worker_done)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    @Slot(str, object, object)
    def _worker_done(self, mode, result, error):
        self._set_running(False)
        if error:
            self._log("Error", str(error))
            QMessageBox.critical(self, "Error", str(error))
            return
        if mode == "scan":
            self.pending_changes = result or []
            engine = self.worker.engine if self.worker else None
            self.last_scan_engine = engine
            self._populate_table()
            self.copy_btn.setEnabled(bool(self.pending_changes))
            self.scan_progress.setRange(0, max(len(self.pending_changes), 1))
            self._write_gui_report("scan", engine, self.pending_changes)
            self.statusBar().showMessage(f"Scan complete: {len(self.pending_changes)} file(s) to copy")
            if self.scheduler_pending_copy:
                self.scheduler_pending_copy = False
                if self.pending_changes:
                    self._log("Info", "Scheduled scan complete; automatic copy will start after scan thread cleanup.")
                    self.start_copy_after_thread_finish = True
                else:
                    self._log("Info", "Scheduled scan complete; nothing to copy.")
                    self._schedule_next_run()
        else:
            engine = self.worker.engine if self.worker else None
            self.copy_engine = engine
            self._write_gui_report("copy", engine, self.pending_changes)
            if not self.scheduler_copy_running:
                QMessageBox.information(self, "Copy complete", "Backup copy operation finished. Check log for details.")
            self.scheduler_copy_running = False
            self.statusBar().showMessage("Copy complete")
            self._schedule_next_run()

    def _thread_finished(self):
        self.worker = None
        self.thread = None
        if self.start_copy_after_thread_finish:
            self.start_copy_after_thread_finish = False
            QTimer.singleShot(0, lambda: self._start_copy_job(confirm=False))

    def _populate_table(self):
        self.table.setRowCount(len(self.pending_changes))
        for row, item in enumerate(self.pending_changes):
            action = "Update" if Path(item["dst"]).exists() else "Copy"
            self.table.setItem(row, 0, QTableWidgetItem(action))
            self.table.setItem(row, 1, QTableWidgetItem(item["rel"]))

    def _toggle_pause(self):
        if not self.worker:
            return
        if self.worker.is_paused:
            self.worker.resume()
            self.pause_btn.setText("Pause")
        else:
            self.worker.pause()
            self.pause_btn.setText("Resume")

    def _stop_current(self):
        if self.worker:
            self.worker.stop()
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)

    @Slot(str, str)
    def _log(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {level}: {msg}"
        self.log_box.append(line)
        self._append_log_file(line)

    @Slot(str, int, object)
    def _progress(self, phase, value, total):
        if phase == "collecting":
            self.scan_progress.setRange(0, 0)
            self.scan_status.setText(f"Collecting: {value} files found…")
            return
        if phase == "scanning":
            total = int(total or 0)
            self.scan_progress.setRange(0, max(total, 1))
            self.scan_progress.setValue(value)
            self.scan_status.setText(f"Scan: {value} / {total}")
            return
        if phase == "copying":
            total = int(total or 0)
            self.copy_progress.setRange(0, max(total, 1))
            self.copy_progress.setValue(value)
            eta = ""
            if self.copy_start and value > 0 and total:
                elapsed = time.time() - self.copy_start
                remaining = max(total - value, 0) * (elapsed / value)
                eta = f" | ETA: {self._format_seconds(remaining)}"
            self.copy_status.setText(f"Copy: {value} / {total}{eta}")

    def _set_running(self, running):
        self.scan_btn.setEnabled(not running)
        self.copy_btn.setEnabled((not running) and bool(self.pending_changes))
        self.import_btn.setEnabled(not running)
        self.export_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        if not running:
            self.pause_btn.setText("Pause")

    def _import_job(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Backup Job", "", "JSON job (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r") as f:
                self.config = BackupConfig.from_json(json.load(f))
            self._apply_config_to_ui()
            self._log("Info", f"Imported job: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    def _export_job(self):
        self.config = self._config_from_ui()
        if not self.config.source or not self.config.destination:
            QMessageBox.warning(self, "Missing paths", "Set source and destination before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Backup Job", "backup_job.json", "JSON job (*.json);;All files (*)")
        if not path:
            return
        data = self.config.to_json()
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        data["app"] = APP_NAME
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self._log("Info", f"Exported job: {path}")
        QMessageBox.information(self, "Exported", f"Job exported:\n{path}\n\nRun with:\npython3 backup_manager_qt.py --job \"{path}\"")

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config = BackupConfig.from_json(json.load(f))
            except Exception:
                self.config = BackupConfig()

    def _save_config(self):
        self.config = self._config_from_ui()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config.to_json(), f, indent=2)

    def closeEvent(self, event):
        if self.worker:
            answer = QMessageBox.question(self, "Running job", "A job is running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.stop()
        self._save_config()
        event.accept()

    @staticmethod
    def _format_seconds(seconds):
        seconds = int(max(seconds, 0))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--source", "-s", help="Source directory")
    parser.add_argument("--destination", "-d", help="Destination directory")
    parser.add_argument("--excludes", "-e", default="", help="Exclude patterns, comma-separated")
    parser.add_argument("--dry-run", action="store_true", help="Scan and exit")
    parser.add_argument("--job", help="Run exported job JSON and copy files")
    parser.add_argument("--job-dry-run", action="store_true", help="Run exported job JSON as dry run")
    args = parser.parse_args()

    if args.job:
        sys.exit(run_job(args.job, dry_run=args.job_dry_run))
    if args.dry_run:
        if not args.source or not args.destination:
            parser.error("--source and --destination are required with --dry-run")
        headless_dry_run(args.source, args.destination, args.excludes)
        return

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = BackupManagerQt()
    if args.source:
        window.source_edit.setText(args.source)
    if args.destination:
        window.dest_edit.setText(args.destination)
    if args.excludes:
        window.excludes_edit.setText(args.excludes)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
