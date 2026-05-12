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


def test_routing_module_uses_math_globally():
    """Régression PR #17 : math doit être importé au top-level de routing.py."""
    import inspect
    import re

    from auvergne_pipeline import routing

    src = inspect.getsource(routing)
    # "import math" indenté (= local) → interdit
    local_imports = re.findall(r"^\s+import math\b", src, re.MULTILINE)
    assert not local_imports, (
        "import math doit être au top-level de routing.py, pas dans une fonction"
    )
    # Il doit y avoir un import math top-level
    top_imports = re.findall(r"^import math\b", src, re.MULTILINE)
    assert top_imports, "routing.py doit avoir 'import math' au top-level"
