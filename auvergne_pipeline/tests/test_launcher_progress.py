"""PR #29 partie C — unit tests for the stdout progress parser.

Pure-Python tests, no PyQt6 required (so they run in CI).
"""

from __future__ import annotations

from auvergne_pipeline.launcher_progress import ProgressState, SUBSTEP_ORDER


def test_initial_state_at_zero():
    ps = ProgressState(total_sros=5)
    assert ps.sro_index == 0
    assert ps.progress_percent() == 0
    assert ps.current_sro == ""
    assert ps.current_step_id == "load"


def test_sro_header_advances_index():
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO 63149/M06/PMZ/42478 ===")
    assert ps.sro_index == 1
    assert ps.current_sro == "63149/M06/PMZ/42478"
    assert ps.current_step_id == "load"


def test_substep_progression_within_sro():
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO X ===")
    base_pct = ps.progress_percent()
    for marker, expected_step in [
        ("[IGN] WFS fetch", "ign"),
        ("[CDC] BT clip public", "cdc"),
        ("[PB QA] total=10", "pb"),
        ("[ROUTING] Graphe brut: 100 noeuds", "graph"),
        ("[WELD] 100 -> 90 noeuds", "weld"),
        ("[TOPO SNAP] 5 endpoints", "snap"),
        ("[ROUTING] 3 composantes connexes", "routing"),
        ("[INFRA QA] sro=X total=5", "infra_qa"),
        ("[INFO] X writer : pa=1", "writer"),
        ("[QML] 6 styles appliques", "qml"),
        ("[QGZ] Projet ecrit", "qgz"),
    ]:
        ps.update(marker)
        assert ps.current_step_id == expected_step, marker
    # Progress should monotonically increase.
    assert ps.progress_percent() >= base_pct


def test_progress_clamped_to_one_sro_slot():
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO 1 ===")
    # Run all sub-steps; progress should not exceed 100/5 = 20 %.
    for marker, _ in [
        ("[IGN] x", "ign"),
        ("[CDC] x", "cdc"),
        ("[PB QA] x", "pb"),
        ("[ROUTING] Graphe brut x", "graph"),
        ("[WELD] x", "weld"),
        ("[TOPO SNAP] x", "snap"),
        ("[ROUTING] x", "routing"),
        ("[INFRA QA] x", "infra_qa"),
        ("[INFO] X writer : pa=1", "writer"),
        ("[QML] x", "qml"),
        ("[QGZ] x", "qgz"),
    ]:
        ps.update(marker)
    assert ps.progress_percent() <= 20  # one slot of 100/5


def test_completed_sro_fills_slot():
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO X ===")
    ps.update("[OK] SRO X traite")
    assert ps.completed_sros == 1
    # Once the SRO is marked done, percent should be at the slot boundary.
    assert ps.progress_percent() == 20


def test_progress_never_goes_backwards():
    """A late ``[IGN]`` line after we already saw ``[INFRA QA]`` must not
    rewind the substep — the parser only ratchets forward.
    """
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO X ===")
    ps.update("[INFRA QA] sro=X total=5")
    advanced_pct = ps.progress_percent()
    advanced_step = ps.current_step_id
    ps.update("[IGN] late line, should not rewind")
    assert ps.current_step_id == advanced_step
    assert ps.progress_percent() >= advanced_pct


def test_unknown_lines_are_ignored():
    ps = ProgressState(total_sros=5)
    ps.update("=== SRO X ===")
    pct = ps.progress_percent()
    for noise in ["random log", "[INFO] something else", "DEBUG xxx"]:
        ps.update(noise)
    assert ps.progress_percent() == pct
    assert ps.current_step_id == "load"


def test_routing_more_specific_marker_wins_over_general():
    """[ROUTING] Graphe brut must map to ``graph``, not the general
    ``[ROUTING] `` marker which maps to ``routing``.
    """
    ps = ProgressState(total_sros=1)
    ps.update("=== SRO X ===")
    ps.update("[ROUTING] Graphe brut: 100 noeuds")
    assert ps.current_step_id == "graph"


def test_substep_order_matches_constants():
    """The exported SUBSTEP_ORDER must include every step the parser uses."""
    expected = {
        "load", "ign", "cdc", "pb", "graph", "weld", "snap",
        "routing", "infra_qa", "writer", "qml", "qgz",
    }
    assert expected.issubset(set(SUBSTEP_ORDER))
