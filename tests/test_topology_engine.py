#!/usr/bin/env python3
"""
Tests pour le moteur topologique PR32 (sections A-E).

Couvre:
  A — enforce_crs (EPSG:2154 force)
  B — ensure_terminals_connected (PA/PB snap existant prioritaire)
  C — split_livrableedges_at_endpoint_projections (T-junction split robuste)
  D — reconnect_after_energy_removal (pas de C0 arbitraire)
  E — drop_c0_when_existing_equivalent (purge C0 superposes)

Usage:
    pytest tests/test_topology_engine.py -v
    python -m pytest tests/test_topology_engine.py -v
"""

import sys
from pathlib import Path

import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, LineString, Polygon

# Ensure scripts/ is on the path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from topology_engine import (
    OUTPUT_CRS,
    enforce_crs,
    _project_point_to_line,
    _snap_to_existing_infra,
    _build_c0_connector,
    ensure_terminals_connected,
    split_livrableedges_at_endpoint_projections,
    _is_bt_or_e1_segment,
    _reconnect_via_existing,
    reconnect_after_energy_removal,
    _c0_overlaps_existing,
    drop_c0_when_existing_equivalent,
    MIN_SEGMENT_M,
    C0_MAX_LENGTH_M,
    SNAP_TOLERANCE_M,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _make_infra() -> gpd.GeoDataFrame:
    """Quelques lignes d'infra existante."""
    return gpd.GeoDataFrame(
        [
            {"id": "e1", "geometry": LineString([(0, 0), (100, 0)])},
            {"id": "e2", "geometry": LineString([(0, 0), (0, 100)])},
            {"id": "e3", "geometry": LineString([(100, 0), (100, 100)])},
            {"id": "e4", "geometry": LineString([(50, 50), (150, 50)])},
        ],
        crs=2154,
    )


# ──────────────────────────────────────────────────────────────────────
# A — CRS
# ──────────────────────────────────────────────────────────────────────

class TestEnforceCrs:

    def test_already_2154(self):
        gdf = gpd.GeoDataFrame(
            [{"geometry": Point(0, 0)}],
            crs=2154,
        )
        out = enforce_crs(gdf)
        assert out.crs.to_epsg() == 2154

    def test_wgs84_converted(self):
        # Petit deplacement pour eviter que to_crs garde le meme point
        gdf = gpd.GeoDataFrame(
            [{"geometry": Point(3.0, 46.0)}],
            crs=4326,
        )
        out = enforce_crs(gdf)
        assert out.crs.to_epsg() == 2154
        # Le point doit etre déplace en metres (Lambert 93)
        assert abs(out.iloc[0].geometry.x) > 10000

    def test_no_crs_assumed_4326(self):
        gdf = gpd.GeoDataFrame(
            [{"geometry": Point(3.0, 46.0)}],
        )
        out = enforce_crs(gdf)
        assert out.crs.to_epsg() == 2154

    def test_unrelated_crs_converted(self):
        gdf = gpd.GeoDataFrame(
            [{"geometry": Point(500000, 300000)}],
            crs=32632,  # UTM 32N
        )
        out = enforce_crs(gdf)
        assert out.crs.to_epsg() == 2154


# ──────────────────────────────────────────────────────────────────────
# B — ensure_terminals_connected
# ──────────────────────────────────────────────────────────────────────

class TestEnsureTerminalsConnected:

    def test_terminal_on_existing_endpoint(self):
        infra = _make_infra()
        terminal = Point(0, 0)  # Exact endpoint de e1 et e2
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_1", "type": "PA", "geometry": terminal}],
            crs=2154,
        )
        result, log = ensure_terminals_connected(term_gdf, infra)
        assert log["terminals_processed"] == 1
        assert log["connected_to_existing"] == 1
        assert log["c0_created"] == 0

    def test_terminal_near_existing_endpoint(self):
        infra = _make_infra()
        terminal = Point(0, 2)  # A 2m de l'endpoint (0,0)
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_2", "type": "PA", "geometry": terminal}],
            crs=2154,
        )
        result, log = ensure_terminals_connected(term_gdf, infra)
        assert log["connected_to_existing"] == 1
        assert log["c0_created"] == 0  # Dans la tolerance → connecté direct

    def test_terminal_midpoint_connect_with_c0(self):
        """Terminal a proximit\u00e9 du milieu d'une ligne → creer C0 court."""
        infra = _make_infra()
        terminal = Point(50, 3)  # Proche du milieu de e1: (0,0)-(100,0)
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_3", "geometry": terminal}],
            crs=2154,
        )
        c0_rows = []
        result, log = ensure_terminals_connected(term_gdf, infra, c0_rows)
        assert log["connected_to_existing"] == 0
        assert log["c0_created"] == 1
        assert len(c0_rows) == 1
        assert c0_rows[0]["src"] == "c0_neuf"
        assert c0_rows[0]["mode_pose"] == "C0"
        # C0 court (< 5m)
        assert c0_rows[0]["length_m"] < 5.0

    def test_terminal_midpoint_exact_no_c0(self):
        """Terminal exactement sur la ligne mid → PAS de C0 zero-longueur."""
        infra = _make_infra()
        terminal = Point(50, 0)  # Exactement sur (50, 0) de e1
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_4", "geometry": terminal}],
            crs=2154,
        )
        c0_rows = []
        result, log = ensure_terminals_connected(term_gdf, infra, c0_rows)
        assert log["c0_created"] == 0
        assert log["connected_to_existing"] == 1
        assert len(c0_rows) == 0

    def test_terminal_too_far_disconnected(self):
        """Terminal hors de toute infra → disconnected, PAS de C0 arbitraire."""
        infra = _make_infra()
        terminal = Point(9000, 9000)  # Tres loin
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_5", "geometry": terminal}],
            crs=2154,
        )
        c0_rows = []
        result, log = ensure_terminals_connected(term_gdf, infra, c0_rows)
        assert log["disconnected"] == 1
        assert log["c0_created"] == 0
        assert len(c0_rows) == 0
        assert "pa_pb_disconnected" in result.columns
        assert result.iloc[0]["pa_pb_disconnected"] == True

    def test_c0_rejected_too_long(self):
        """Snap midpoint au dela de C0_MAX_LENGTH → pas de C0 long."""
        # Infra loin, pas de snap endpoint possible
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (1000, 0)])}],
            crs=2154,
        )
        terminal = Point(500, 200)  # Projet milieu ok mais d=200 > tolerance
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_6", "geometry": terminal}],
            crs=2154,
        )
        c0_rows = []
        result, log = ensure_terminals_connected(
            term_gdf, infra, c0_rows,
        )
        # Dans ce cas: pas de snap midpoint (200 m > 5m tolerance)
        # Donc disconnected, pas de C0
        assert log["c0_created"] == 0

    def test_multiple_terminals(self):
        """Plusieurs terminaux melanges."""
        infra = _make_infra()
        terminals = gpd.GeoDataFrame(
            [
                {"id": "PA_1", "geometry": Point(0, 0)},       # endpoint
                {"id": "PA_2", "geometry": Point(50, 3)},       # midpoint+C0
                {"id": "PA_3", "geometry": Point(9000, 9000)},  # disconnected
            ],
            crs=2154,
        )
        c0_rows = []
        result, log = ensure_terminals_connected(terminals, infra, c0_rows)
        assert log["connected_to_existing"] >= 1
        assert log["disconnected"] >= 1
        assert "pa_pb_disconnected" in result.columns


# ──────────────────────────────────────────────────────────────────────
# C — split_livrableedges_at_endpoint_projections
# ──────────────────────────────────────────────────────────────────────

class TestSplitTJunction:

    def test_no_split_without_terminals(self):
        rows = pd.DataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}]
        )
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(rows)
        assert len(result) == 1
        assert log["splits"] == 0

    def test_t_junction_split(self):
        """Terminal projete sur milieu → split en 2 segments."""
        row_gdf = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_1", "geometry": Point(50, 3)}],
            crs=2154,
        )
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(
            row_gdf, terminals_gdf=term_gdf
        )
        # Le split devrait produire 2 segments
        assert len(result) >= 2
        assert log["splits"] >= 1

    def test_no_split_when_endpoint(self):
        """Terminal sur un endpoint → pas de split."""
        row_gdf = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_1", "geometry": Point(0, 0)}],
            crs=2154,
        )
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(
            row_gdf, terminals_gdf=term_gdf
        )
        assert len(result) == 1
        assert log["splits"] == 0

    def test_no_split_when_too_far(self):
        """Terminal trop loin de la ligne → pas de split."""
        row_gdf = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_1", "geometry": Point(50, 100)}],  # 100m de la ligne
            crs=2154,
        )
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(
            row_gdf, terminals_gdf=term_gdf
        )
        assert len(result) == 1
        assert log["splits"] == 0

    def test_empty_rows(self):
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(
            pd.DataFrame()
        )
        assert len(result) == 0

    def test_old_to_new_mapping(self):
        """Vérifier que le mapping old_idx → new_idx existe."""
        row_gdf = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        term_gdf = gpd.GeoDataFrame(
            [{"id": "PA_1", "geometry": Point(50, 3)}],
            crs=2154,
        )
        result, old_to_new, log = split_livrableedges_at_endpoint_projections(
            row_gdf, terminals_gdf=term_gdf
        )
        assert 0 in old_to_new
        # old_idx 0 mappe vers au moins 2 new_idx
        assert len(old_to_new[0]) >= 2


# ──────────────────────────────────────────────────────────────────────
# D — reconnect_after_energy_removal
# ──────────────────────────────────────────────────────────────────────

class TestReconnectAfterEnergyRemoval:

    def test_bt_segment_reconnected_via_existing(self):
        """Segment BT supprimé → reconnecté via existant."""
        infra = gpd.GeoDataFrame(
            [
                {"geometry": LineString([(0, 0), (50, 0)])},
                {"geometry": LineString([(50, 0), (100, 0)])},
            ],
            crs=2154,
        )
        removed = [
            {
                "geometry": LineString([(10, 0), (20, 0)]),
                "type": "BT",
                "tension": "BT",
            }
        ]
        # Les deux orphelins (10,0) et (20,0) sont déjà sur l'infra existante
        qa = reconnect_after_energy_removal(removed, infra)
        assert qa["segments_analyzed"] == 1
        assert qa["bt_e1_reconnected"] == 1

    def test_non_bt_e1_skipped(self):
        """Segment HT supprimé → pas de reconnexion."""
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        removed = [
            {
                "geometry": LineString([(10, 0), (20, 0)]),
                "type": "HTA",
                "tension": "20000V",
            }
        ]
        qa = reconnect_after_energy_removal(removed, infra)
        assert qa["non_bt_e1_skipped"] == 1
        assert qa["bt_e1_reconnected"] == 0

    def test_reconnect_failed_flagged(self):
        """Orphelins trop loin de tout existant → flag ENERGY_RECONNECT_FAILED."""
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (10, 0)])}],
            crs=2154,
        )
        removed = [
            {
                "geometry": LineString([(9000, 0), (9100, 0)]),
                "type": "BT",
                "tension": "BT",
            }
        ]
        qa = reconnect_after_energy_removal(removed, infra)
        assert qa["reconnect_failed"] >= 1

    def test_no_arbitrary_c0_created(self):
        """Vérifier que NO long C0 droit n'est créé."""
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (50, 0)])}],
            crs=2154,
        )
        # Segment BT supprimé dont les orphelins sont loins de l'infra
        removed = [
            {
                "geometry": LineString([(5000, 0), (8000, 0)]),
                "type": "BT",
                "tension": "BT",
            }
        ]
        c0_rows = []
        qa = reconnect_after_energy_removal(removed, infra, c0_rows)
        assert qa["reconnect_failed"] >= 1
        assert len(c0_rows) == 0


# ──────────────────────────────────────────────────────────────────────
# E — drop_c0_when_existing_equivalent
# ──────────────────────────────────────────────────────────────────────

class TestDropC0WhenExistingEquivalent:

    def test_c0_overlapping_existing_dropped(self):
        """C0 sur ligne existante → supprimé."""
        infra = _make_infra()
        c0_line = LineString([(25, 0), (75, 0)])  # Superposé sur e1
        livrable = gpd.GeoDataFrame(
            [
                {"geometry": c0_line, "src": "c0_neuf", "mode_pose": "C0"},
                {"geometry": LineString([(0, 0), (10, 0)]), "src": "existant"},
            ],
            crs=2154,
        )
        result, log = drop_c0_when_existing_equivalent(livrable, infra)
        assert log["c0_examined"] == 1
        assert log["c0_dropped"] == 1
        assert len(result) == 1  # Seul le non-C0 reste

    def test_c0_not_overlapping_kept(self):
        """C0 ne superposant aucun existant → conservé."""
        infra = _make_infra()
        c0_line = LineString([(200, 200), (250, 200)])  # Loin de toute infra
        livrable = gpd.GeoDataFrame(
            [{"geometry": c0_line, "src": "c0_neuf", "mode_pose": "C0"}],
            crs=2154,
        )
        result, log = drop_c0_when_existing_equivalent(livrable, infra)
        assert log["c0_examined"] == 1
        assert log["c0_kept"] == 1
        assert len(result) == 1

    def test_no_c0_rows(self):
        """Livrable sans C0 → inchangé."""
        infra = _make_infra()
        livrable = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (50, 0)]), "src": "existant"}],
            crs=2154,
        )
        result, log = drop_c0_when_existing_equivalent(livrable, infra)
        assert log["c0_examined"] == 0
        assert len(result) == 1

    def test_parallel_c0_dropped(self):
        """C0 parallèle à existant (dans tolérance angulaire) → dropped."""
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        # Ligne parallèle à 1m de l'existante
        c0_line = LineString([(25, 1), (75, 1)])
        livrable = gpd.GeoDataFrame(
            [{"geometry": c0_line, "src": "c0_neuf", "mode_pose": "C0"}],
            crs=2154,
        )
        result, log = drop_c0_when_existing_equivalent(livrable, infra)
        assert log["c0_dropped"] == 1

    def test_perpendicular_c0_kept(self):
        """C0 perpendiculaire à existant → conservé (probablement un connecteur)."""
        infra = gpd.GeoDataFrame(
            [{"geometry": LineString([(0, 0), (100, 0)])}],
            crs=2154,
        )
        # C0 perpendiculaire qui part de l'existante
        c0_line = LineString([(50, 0), (50, 50)])
        livrable = gpd.GeoDataFrame(
            [{"geometry": c0_line, "src": "c0_neuf", "mode_pose": "C0"}],
            crs=2154,
        )
        result, log = drop_c0_when_existing_equivalent(livrable, infra)
        # Le C0 est proche (midpoint à 25m) mais perpendiculaire → devrait être conservé
        assert log["c0_kept"] == 1


# ──────────────────────────────────────────────────────────────────────
# Helpers tests
# ──────────────────────────────────────────────────────────────────────

class TestHelpers:

    def test_project_point_to_line(self):
        line = LineString([(0, 0), (100, 0)])
        pt = Point(50, 10)
        proj, d = _project_point_to_line(pt, line)
        assert proj is not None
        assert abs(proj.x - 50) < 0.001
        assert abs(proj.y - 0) < 0.001
        assert abs(d - 10) < 0.001

    def test_build_c0_connector_too_short(self):
        c0 = _build_c0_connector(Point(0, 0), Point(0, 0))
        assert c0 is None  # Zero-length → rejected

    def test_build_c0_connector_too_long(self):
        c0 = _build_c0_connector(
            Point(0, 0), Point(C0_MAX_LENGTH_M + 100, 0)
        )
        assert c0 is None

    def test_build_c0_connector_ok(self):
        c0 = _build_c0_connector(Point(0, 0), Point(10, 0))
        assert c0 is not None
        assert c0.length == 10.0

    def test_is_bt_or_e1(self):
        assert _is_bt_or_e1_segment({"type": "BT"}) is True
        assert _is_bt_or_e1_segment({"type": "E1"}) is True
        assert _is_bt_or_e1_segment({"tension": "BT"}) is True
        assert _is_bt_or_e1_segment({"type": "HTA"}) is False
        assert _is_bt_or_e1_segment({"type": "HTB"}) is False


# ──────────────────────────────────────────────────────────────────────
# Test runner
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-v", __file__]))
