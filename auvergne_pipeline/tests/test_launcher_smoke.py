"""Smoke tests for the Tkinter launcher.

The tests are tolerant: they skip cleanly on a headless environment
(typical CI / dev container) so they can ship in the same suite as the
geo tests without requiring a display.
"""

from __future__ import annotations

import os

import pytest


def test_launcher_imports_without_error():
    """Module import must not raise (no SyntaxError, no top-level Tk init).

    On environments without tkinter (rare CI builds without _tkinter), we
    skip cleanly so the test stays portable. Pierre's QGIS Python has
    tkinter.
    """
    try:
        from auvergne_pipeline import launcher  # noqa: F401
    except ImportError as exc:
        if "tkinter" in str(exc).lower() or "_tkinter" in str(exc).lower():
            pytest.skip(f"Tkinter indisponible: {exc}")
        raise


def _tk_available_or_skip():
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("Pas de display (CI / headless).")
    try:
        import tkinter as tk
    except ImportError:
        pytest.skip("Tkinter indisponible.")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk root non instanciable : {exc}")
    return tk, root


def test_launcher_app_instantiates_and_closes_cleanly():
    tk, root = _tk_available_or_skip()
    try:
        from auvergne_pipeline.launcher import LauncherApp
        from auvergne_pipeline import config

        app = LauncherApp(root)
        # All pilot SROs are pre-selected by default.
        assert app.selected_sros == set(config.PILOT_SROS)
        assert list(app.displayed_sros) == list(config.PILOT_SROS)

        # Filtering hides non-matching SROs but does not lose selection.
        app.filter_var.set("M06")
        app._refresh_listbox()
        for code in app.displayed_sros:
            assert "m06" in code.lower()
        assert app.selected_sros >= set(app.displayed_sros)

        # "Tout decocher" wipes the selection.
        app._on_uncheck_all()
        assert app.selected_sros == set()

        # "Cocher les 5 pilotes" restores the default state.
        app._on_check_pilots()
        assert app.selected_sros == set(config.PILOT_SROS)
        assert app.filter_var.get() == ""

        root.update_idletasks()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_build_argv_uses_all_pilots_when_full_set_selected():
    tk, root = _tk_available_or_skip()
    try:
        from auvergne_pipeline.launcher import LauncherApp
        from auvergne_pipeline import config

        app = LauncherApp(root)
        cmd = app._build_argv("/tmp/x.gpkg", set(config.PILOT_SROS))
        assert "--all-pilots" in cmd
        assert "--gpkg" in cmd and "/tmp/x.gpkg" in cmd
        assert "--sros" not in cmd
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_build_argv_uses_sros_when_subset_selected():
    tk, root = _tk_available_or_skip()
    try:
        from auvergne_pipeline.launcher import LauncherApp
        from auvergne_pipeline import config

        app = LauncherApp(root)
        subset = set(list(config.PILOT_SROS)[:2])
        cmd = app._build_argv("/tmp/x.gpkg", subset)
        assert "--sros" in cmd
        assert "--all-pilots" not in cmd
        for code in subset:
            assert code in cmd
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
