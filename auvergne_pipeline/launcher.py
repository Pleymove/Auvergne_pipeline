"""Launcher GUI PyQt6 pour le pipeline Auvergne.

PyQt6 est livre nativement avec QGIS 4.0.1 (a la difference de Tkinter qui
est absent du Python embedded OSGeo4W : ni `_tkinter.pyd`, ni le dossier
`tcl/` ne sont packages par OSGeo4W).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Initialiser l'environnement Qt AVANT d'importer PyQt6.
# o4w_env.bat charge Python + GeoPandas mais ne configure pas les DLLs Qt.
# Sans ca, PyQt6 plante avec :
#   - "DLL load failed while importing QtCore" (Qt6\bin pas dans PATH)
#   - "Could not find the Qt platform plugin windows" (QT_PLUGIN_PATH absent)
# ---------------------------------------------------------------------------
def _init_qt_env() -> None:
    """Detecte QGIS et configure PATH / QT_PLUGIN_PATH avant l'import PyQt6."""
    if os.environ.get("_HERMES_QT_INIT_DONE"):
        return

    # Deduire QGIS_ROOT depuis le chemin de python.exe
    qgis_root = os.environ.get("QGIS_ROOT", "")
    if not qgis_root:
        # python.exe est dans apps\Python3xx\python.exe, on remonte de 3 niveaux
        _py = Path(sys.executable).resolve()
        _candidate = _py.parent.parent.parent  # python.exe -> Python3xx -> apps -> QGIS_ROOT
        if (_candidate / "bin" / "o4w_env.bat").exists():
            qgis_root = str(_candidate)

    if not qgis_root:
        return  # pas dans l'environnement QGIS, on laisse planter

    # Qt bin dans PATH
    for qt_ver in ("Qt6", "Qt5"):
        qt_bin = os.path.join(qgis_root, "apps", qt_ver, "bin")
        if os.path.isdir(qt_bin):
            os.environ["PATH"] = qt_bin + os.pathsep + os.path.join(qgis_root, "bin") + os.pathsep + os.environ.get("PATH", "")
            break

    # Qt platform plugins (evite "Could not find the Qt platform plugin windows")
    for qt_ver in ("Qt6", "Qt5"):
        qt_plugins = os.path.join(qgis_root, "apps", qt_ver, "plugins")
        if os.path.isdir(qt_plugins):
            os.environ["QT_PLUGIN_PATH"] = qt_plugins
            break

    os.environ["_HERMES_QT_INIT_DONE"] = "1"

_init_qt_env()

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import config


REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"


class PipelineWorker(QObject):
    """QThread worker qui lance le pipeline et stream stdout via signal."""

    output_ready = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, argv: list[str], log_path: Path) -> None:
        super().__init__()
        self.argv = argv
        self.log_path = log_path

    def run(self) -> None:
        try:
            with self.log_path.open("w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    self.argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(REPO_ROOT),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    logf.write(line)
                    logf.flush()
                    self.output_ready.emit(line.rstrip("\n"))
                code = proc.wait()
            self.finished.emit(code)
        except Exception as exc:  # pragma: no cover
            self.output_ready.emit(f"[X] Erreur worker: {exc}")
            self.finished.emit(1)


class LauncherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Auvergne Pipeline - Launcher")
        self.resize(700, 600)

        self.selected_sros: set[str] = set()
        self.thread: QThread | None = None
        self.worker: PipelineWorker | None = None
        self._syncing = False

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- GPKG ---
        layout.addWidget(QLabel("GPKG :"))
        gpkg_row = QHBoxLayout()
        default_gpkg = os.environ.get(
            "AUVERGNE_GPKG",
            r"C:\Users\pbirau\Downloads\Auvergne_local.gpkg",
        )
        self.gpkg_edit = QLineEdit(default_gpkg)
        gpkg_row.addWidget(self.gpkg_edit)
        browse_btn = QPushButton("Parcourir...")
        browse_btn.clicked.connect(self._browse_gpkg)
        gpkg_row.addWidget(browse_btn)
        layout.addLayout(gpkg_row)

        # --- Filtre + liste SRO ---
        layout.addWidget(QLabel("SRO pilotes :"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filtrer par SRO...")
        self.filter_edit.textChanged.connect(self._refresh_list)
        layout.addWidget(self.filter_edit)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection
        )
        self.list_widget.itemSelectionChanged.connect(self._sync_selected)
        layout.addWidget(self.list_widget)

        # --- Boutons selection ---
        sel_row = QHBoxLayout()
        check_all_btn = QPushButton("Cocher les 5 pilotes")
        check_all_btn.clicked.connect(self._check_all_pilots)
        sel_row.addWidget(check_all_btn)
        uncheck_btn = QPushButton("Tout decocher")
        uncheck_btn.clicked.connect(self._uncheck_all)
        sel_row.addWidget(uncheck_btn)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        # --- Console ---
        layout.addWidget(QLabel("Console :"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(10000)
        font = self.console.font()
        font.setFamily("Consolas")
        self.console.setFont(font)
        layout.addWidget(self.console)

        # --- Boutons action ---
        action_row = QHBoxLayout()
        self.launch_btn = QPushButton("Lancer")
        self.launch_btn.clicked.connect(self._launch)
        action_row.addWidget(self.launch_btn)
        logs_btn = QPushButton("Ouvrir logs/")
        logs_btn.clicked.connect(self._open_logs)
        action_row.addWidget(logs_btn)
        action_row.addStretch()
        quit_btn = QPushButton("Quitter")
        quit_btn.clicked.connect(QApplication.instance().quit)
        action_row.addWidget(quit_btn)
        layout.addLayout(action_row)

        self._refresh_list()

    # ---------- Liste / selection ----------

    def _visible_sros(self) -> list[str]:
        flt = self.filter_edit.text().strip().lower()
        if not flt:
            return list(config.PILOT_SROS)
        return [s for s in config.PILOT_SROS if flt in s.lower()]

    def _refresh_list(self) -> None:
        self._syncing = True
        try:
            self.list_widget.clear()
            for sro in self._visible_sros():
                item = QListWidgetItem(sro)
                self.list_widget.addItem(item)
                item.setSelected(sro in self.selected_sros)
        finally:
            self._syncing = False

    def _sync_selected(self) -> None:
        if self._syncing:
            return
        visible = set(self._visible_sros())
        currently_selected_visible = {
            self.list_widget.item(i).text()
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).isSelected()
        }
        # Garde la selection des SRO hors-filtre, remplace pour les visibles.
        self.selected_sros = (
            (self.selected_sros - visible) | currently_selected_visible
        )

    def _check_all_pilots(self) -> None:
        self.selected_sros = set(config.PILOT_SROS)
        self._refresh_list()

    def _uncheck_all(self) -> None:
        self.selected_sros = set()
        self._refresh_list()

    # ---------- GPKG / logs ----------

    def _browse_gpkg(self) -> None:
        start_dir = self.gpkg_edit.text().strip() or str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Selectionner le GPKG",
            start_dir,
            "GeoPackage (*.gpkg);;Tous les fichiers (*)",
        )
        if path:
            self.gpkg_edit.setText(path)

    def _open_logs(self) -> None:
        LOGS_DIR.mkdir(exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(LOGS_DIR))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(LOGS_DIR)], check=False)
        else:
            subprocess.run(["xdg-open", str(LOGS_DIR)], check=False)

    # ---------- Lancement ----------

    def _build_argv(self) -> list[str]:
        gpkg = self.gpkg_edit.text().strip()
        argv = [sys.executable, "-m", "auvergne_pipeline.main"]
        if set(self.selected_sros) == set(config.PILOT_SROS):
            argv.append("--all-pilots")
        else:
            argv.append("--sros")
            argv.extend(sorted(self.selected_sros))
        if gpkg:
            argv.extend(["--gpkg", gpkg])
        return argv

    def _launch(self) -> None:
        if not self.selected_sros:
            self.console.appendPlainText("[!] Aucun SRO selectionne")
            return
        if self.thread is not None:
            return  # run en cours

        LOGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"gui_run_{ts}.log"
        argv = self._build_argv()
        self.console.appendPlainText(f"$ {' '.join(argv)}")
        self.console.appendPlainText(f"[i] Log : {log_path}")

        self.launch_btn.setEnabled(False)
        self.thread = QThread()
        self.worker = PipelineWorker(argv, log_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.output_ready.connect(self.console.appendPlainText)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _on_finished(self, code: int) -> None:
        self.console.appendPlainText(f"[i] Exit code: {code}")
        self.launch_btn.setEnabled(True)
        self.thread = None
        self.worker = None


def main() -> int:
    app = QApplication(sys.argv)
    win = LauncherWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
