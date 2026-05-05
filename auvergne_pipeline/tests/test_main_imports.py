"""Smoke tests: all pipeline modules import cleanly (catches PR #16 UnboundLocalError)."""

from __future__ import annotations


def test_main_module_imports_cleanly():
    """Regression: UnboundLocalError 'pd' (PR #16)."""
    from auvergne_pipeline import main

    assert hasattr(main, "run_for_sro")
    assert hasattr(main, "run_for_sros")
    assert hasattr(main, "main")


def test_all_pipeline_modules_import():
    """Every pipeline module must import without NameError / ImportError."""
    import auvergne_pipeline.config
    import auvergne_pipeline.loader
    import auvergne_pipeline.filters
    import auvergne_pipeline.parcelles
    import auvergne_pipeline.orphans
    import auvergne_pipeline.d3
    import auvergne_pipeline.flags
    import auvergne_pipeline.ign_routes
    import auvergne_pipeline.pb_fictif
    import auvergne_pipeline.routing
    import auvergne_pipeline.writer
    # launcher requires PyQt6 — skip here
    assert True  # reached without exception
