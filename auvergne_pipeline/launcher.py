"""Tkinter GUI launcher for the Auvergne pipeline.

Stdlib-only (Tkinter is bundled with the Python that ships in QGIS 4.0.1).
Pierre double-clicks ``start.bat``, this module is launched, and the user
gets a small GUI to:

1. pick the GPKG file (defaults to the standard location on the pro PC);
2. tick which SRO to run (PILOT_SROS by default, with a live filter);
3. click "Lancer" and watch logs stream live in a console widget.

The CLI in ``auvergne_pipeline.main`` remains the single source of truth -
the launcher just builds the right argv and pipes its stdout into a Text.
"""

from __future__ import annotations

import datetime as _dt
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, List

from . import config


DEFAULT_GPKG_HINT = r"C:\Users\pbirau\Downloads\Auvergne_local.gpkg"
LOGS_DIR_NAME = "logs"
QUEUE_POLL_MS = 100


class LauncherApp:
    """Top-level Tkinter application for the Auvergne avant-vente pipeline."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Auvergne Pipeline - Launcher")
        try:
            self.root.minsize(720, 620)
        except tk.TclError:
            pass

        self.queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self._log_fh = None  # type: ignore[var-annotated]

        # Logical SRO model -- kept independent of what is currently shown
        # in the listbox so that selection survives filter changes.
        self.all_sros: List[str] = list(config.PILOT_SROS)
        self.selected_sros: set[str] = set(config.PILOT_SROS)
        self.displayed_sros: List[str] = list(self.all_sros)

        self._build_ui()
        self._refresh_listbox()
        self.root.after(QUEUE_POLL_MS, self._drain_queue)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        bold = tkfont.Font(weight="bold")
        mono = tkfont.nametofont("TkFixedFont")

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # ---- GPKG section ----
        gpkg_frame = ttk.LabelFrame(outer, text="Fichier GPKG", padding=8)
        gpkg_frame.pack(fill=tk.X, pady=(0, 8))
        gpkg_frame.columnconfigure(0, weight=1)

        default_gpkg = (
            DEFAULT_GPKG_HINT
            if os.name == "nt"
            else str(config.DEFAULT_GPKG)
        )
        self.gpkg_var = tk.StringVar(value=default_gpkg)
        self.gpkg_entry = ttk.Entry(gpkg_frame, textvariable=self.gpkg_var)
        self.gpkg_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(
            gpkg_frame, text="Parcourir...", command=self._on_browse_gpkg
        ).grid(row=0, column=1)

        # ---- SRO section ----
        sro_frame = ttk.LabelFrame(outer, text="SRO a traiter", padding=8)
        sro_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 8))
        sro_frame.columnconfigure(1, weight=1)

        ttk.Label(sro_frame, text="Filtre :", font=bold).grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_listbox())
        ttk.Entry(sro_frame, textvariable=self.filter_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 6)
        )

        list_holder = ttk.Frame(sro_frame)
        list_holder.grid(row=1, column=0, columnspan=2, sticky="nsew")
        sro_frame.rowconfigure(1, weight=1)
        list_holder.columnconfigure(0, weight=1)
        list_holder.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            list_holder, selectmode=tk.EXTENDED, height=8, exportselection=False
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(
            list_holder, orient=tk.VERTICAL, command=self.listbox.yview
        )
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.config(yscrollcommand=list_scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        sro_btns = ttk.Frame(sro_frame)
        sro_btns.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(
            sro_btns, text="Cocher les 5 pilotes", command=self._on_check_pilots
        ).pack(side=tk.LEFT)
        ttk.Button(
            sro_btns, text="Tout decocher", command=self._on_uncheck_all
        ).pack(side=tk.LEFT, padx=(6, 0))

        # ---- Console section ----
        console_frame = ttk.LabelFrame(outer, text="Console", padding=8)
        console_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)

        self.console = tk.Text(
            console_frame,
            width=100,
            height=20,
            font=mono,
            state=tk.DISABLED,
            wrap=tk.NONE,
        )
        self.console.grid(row=0, column=0, sticky="nsew")
        cscroll_y = ttk.Scrollbar(
            console_frame, orient=tk.VERTICAL, command=self.console.yview
        )
        cscroll_y.grid(row=0, column=1, sticky="ns")
        cscroll_x = ttk.Scrollbar(
            console_frame, orient=tk.HORIZONTAL, command=self.console.xview
        )
        cscroll_x.grid(row=1, column=0, sticky="ew")
        self.console.config(
            yscrollcommand=cscroll_y.set, xscrollcommand=cscroll_x.set
        )

        # ---- Action buttons ----
        action_bar = ttk.Frame(outer)
        action_bar.pack(fill=tk.X)
        self.launch_button = ttk.Button(
            action_bar, text="Lancer", command=self._on_launch
        )
        self.launch_button.pack(side=tk.LEFT)
        ttk.Button(
            action_bar, text="Ouvrir logs/", command=self._on_open_logs
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(action_bar, text="Quitter", command=self.root.destroy).pack(
            side=tk.RIGHT
        )

    # -------------------------------------------------------- SRO listbox

    def _matches_filter(self, sro_code: str) -> bool:
        flt = self.filter_var.get().strip().lower()
        if not flt:
            return True
        return flt in sro_code.lower()

    def _refresh_listbox(self) -> None:
        # Persist the visible selection state into self.selected_sros first,
        # so we don't lose it when we wipe and re-fill the listbox.
        self._sync_selection_from_listbox()
        self.listbox.delete(0, tk.END)
        self.displayed_sros = [s for s in self.all_sros if self._matches_filter(s)]
        for i, code in enumerate(self.displayed_sros):
            self.listbox.insert(tk.END, code)
            if code in self.selected_sros:
                self.listbox.selection_set(i)

    def _sync_selection_from_listbox(self) -> None:
        if not self.displayed_sros:
            return
        visible = set(self.displayed_sros)
        currently_selected = {
            self.displayed_sros[i] for i in self.listbox.curselection()
        }
        # Replace the visible-slice of the selection with what's now ticked.
        self.selected_sros = (self.selected_sros - visible) | currently_selected

    def _on_listbox_select(self, _event=None) -> None:
        self._sync_selection_from_listbox()

    def _on_check_pilots(self) -> None:
        self.selected_sros = set(config.PILOT_SROS)
        # Wipe the filter so all pilots become visible / re-tickable.
        self.filter_var.set("")
        self._refresh_listbox()

    def _on_uncheck_all(self) -> None:
        self.selected_sros = set()
        self.listbox.selection_clear(0, tk.END)

    # ------------------------------------------------------------- launch

    def _on_browse_gpkg(self) -> None:
        initial = self.gpkg_var.get().strip() or DEFAULT_GPKG_HINT
        initial_dir = (
            str(Path(initial).parent) if Path(initial).parent.exists() else None
        )
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Selectionner le GPKG",
            initialdir=initial_dir,
            filetypes=[("GPKG", "*.gpkg"), ("Tous les fichiers", "*.*")],
        )
        if path:
            self.gpkg_var.set(path)

    def _on_open_logs(self) -> None:
        logs_dir = Path(LOGS_DIR_NAME)
        logs_dir.mkdir(exist_ok=True)
        target = str(logs_dir.resolve())
        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Logs", f"Impossible d'ouvrir {target}\n{exc}")

    def _build_argv(self, gpkg: str, sros: Iterable[str]) -> list[str]:
        sros_set = set(sros)
        if sros_set == set(config.PILOT_SROS):
            return [
                sys.executable, "-u", "-m", "auvergne_pipeline.main",
                "--all-pilots", "--gpkg", gpkg,
            ]
        return [
            sys.executable, "-u", "-m", "auvergne_pipeline.main",
            "--sros", *sorted(sros_set),
            "--gpkg", gpkg,
        ]

    def _on_launch(self) -> None:
        self._sync_selection_from_listbox()

        gpkg = self.gpkg_var.get().strip()
        if not gpkg:
            messagebox.showerror("GPKG", "Aucun fichier GPKG selectionne.")
            return
        if not Path(gpkg).exists():
            messagebox.showerror(
                "GPKG", f"Fichier introuvable :\n{gpkg}"
            )
            return

        if not self.selected_sros:
            messagebox.showwarning(
                "SRO", "Aucun SRO selectionne. Cochez au moins un SRO."
            )
            return

        cmd = self._build_argv(gpkg, self.selected_sros)
        self._console_clear()
        self._console_append(f"[OK] Commande : {' '.join(cmd)}\n")
        self._start_subprocess(cmd)

    def _start_subprocess(self, cmd: list[str]) -> None:
        self.launch_button.config(state=tk.DISABLED)

        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = Path(LOGS_DIR_NAME)
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"gui_run_{ts}.log"
        try:
            self._log_fh = open(log_path, "w", encoding="utf-8")
            self._console_append(f"[OK] Log : {log_path}\n")
        except OSError as exc:
            self._log_fh = None
            self._console_append(f"[!] Log file indisponible : {exc}\n")

        def reader() -> None:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("err", str(exc)))
                self.queue.put(("done", -1))
                return

            self.proc = proc
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    self.queue.put(("line", line))
            finally:
                proc.wait()
                self.queue.put(("done", proc.returncode))

        threading.Thread(target=reader, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == "line":
                    self._console_append(str(data))
                    if self._log_fh is not None:
                        try:
                            self._log_fh.write(str(data))
                            self._log_fh.flush()
                        except Exception:  # noqa: BLE001
                            pass
                elif kind == "err":
                    self._console_append(f"[X] Erreur : {data}\n")
                elif kind == "done":
                    rc = int(data)
                    if rc == 0:
                        self._console_append("\n[OK] Pipeline termine\n")
                    else:
                        self._console_append(
                            f"\n[X] Pipeline echec (code {rc})\n"
                        )
                    self.launch_button.config(state=tk.NORMAL)
                    if self._log_fh is not None:
                        try:
                            self._log_fh.close()
                        finally:
                            self._log_fh = None
                    self.proc = None
        except queue.Empty:
            pass
        finally:
            self.root.after(QUEUE_POLL_MS, self._drain_queue)

    # ------------------------------------------------------------- console

    def _console_append(self, text: str) -> None:
        self.console.config(state=tk.NORMAL)
        self.console.insert(tk.END, text)
        self.console.see(tk.END)
        self.console.config(state=tk.DISABLED)

    def _console_clear(self) -> None:
        self.console.config(state=tk.NORMAL)
        self.console.delete("1.0", tk.END)
        self.console.config(state=tk.DISABLED)


def main() -> int:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
