"""Smoke tests for the PyQt6 launcher.

Skips gracefully if PyQt6 is unavailable (sandbox CI).
Uses the offscreen QPA platform so no display is needed.
"""
from __future__ import annotations

import os
import sys
import unittest

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401
    PYQT_OK = True
except ImportError:
    PYQT_OK = False

if PYQT_OK:
    from PyQt6.QtWidgets import QApplication

    from auvergne_pipeline import config
    from auvergne_pipeline.launcher import LauncherWindow


@unittest.skipUnless(PYQT_OK, "PyQt6 not available in this environment")
class LauncherSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_instantiation(self) -> None:
        win = LauncherWindow()
        self.assertEqual(win.list_widget.count(), len(config.PILOT_SROS))

    def test_filter_preserves_selection(self) -> None:
        win = LauncherWindow()
        win._check_all_pilots()
        self.assertEqual(set(win.selected_sros), set(config.PILOT_SROS))
        win.filter_edit.setText("63149")
        # Liste filtree mais selected_sros conservee
        self.assertEqual(win.list_widget.count(), 1)
        self.assertEqual(set(win.selected_sros), set(config.PILOT_SROS))
        win.filter_edit.setText("")
        self.assertEqual(win.list_widget.count(), len(config.PILOT_SROS))

    def test_build_argv_all_pilots(self) -> None:
        win = LauncherWindow()
        win._check_all_pilots()
        argv = win._build_argv()
        self.assertIn("--all-pilots", argv)
        self.assertNotIn("--sros", argv)

    def test_build_argv_subset(self) -> None:
        win = LauncherWindow()
        win.selected_sros = {
            "63149/M06/PMZ/42478",
            "63210/M06/PMZ/29655",
        }
        argv = win._build_argv()
        self.assertIn("--sros", argv)
        self.assertNotIn("--all-pilots", argv)
        idx = argv.index("--sros")
        self.assertEqual(
            sorted(argv[idx + 1:idx + 3]),
            sorted(["63149/M06/PMZ/42478", "63210/M06/PMZ/29655"]),
        )

    def test_build_argv_includes_gpkg(self) -> None:
        win = LauncherWindow()
        win._check_all_pilots()
        win.gpkg_edit.setText(r"C:\fake\path.gpkg")
        argv = win._build_argv()
        self.assertIn("--gpkg", argv)
        idx = argv.index("--gpkg")
        self.assertEqual(argv[idx + 1], r"C:\fake\path.gpkg")


if __name__ == "__main__":
    unittest.main()
