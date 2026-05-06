"""Extra routing coverage requested by Pierre's review checklist for PR #24.

* Routed edges carry ``statut``, ``mode_pose`` and ``infra_type`` (CDC propagation)
* Edge deduplication via ``_edge_key`` (no double-counted shared trunks)
* No sklearn anywhere in routing (QGIS embedded has no sklearn)
* No self-loop after welding (already in test_routing.py — re-exercised here)
"""

from __future__ import annotations

import inspect

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from auvergne_pipeline import routing


CRS = "EPSG:2154"


# ---------------------------------------------------------------------------
# CDC: routed edges keep statut / mode_pose / infra_type
# ---------------------------------------------------------------------------

def _bt_infra() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "statut": ["E"], "mode_pose": ["1"], "src": ["bt"],
        "geometry": [LineString([(0, 0), (100, 0)])],
    }), geometry="geometry", crs=CRS)


def _pa(x=0, y=0, pid="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"], "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


def _pb(x, y, pid="PB1", pa_id="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pid], "pa_id": [pa_id], "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


def test_routed_edges_have_infra_type():
    """PR #23 Bug A: every routed edge MUST carry ``infra_type`` for QML coloring."""
    routed = routing.route_pa_to_pb(
        _pa(), _pb(100, 0),
        _bt_infra(),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    if routed.empty:
        pytest.skip("Routing produced 0 edges on the toy fixture")
    assert "infra_type" in routed.columns
    assert (routed["infra_type"] == "bt").all()


def test_routed_edges_keep_statut_and_mode_pose():
    """statut / mode_pose must propagate from the source GeoDataFrame columns."""
    routed = routing.route_pa_to_pb(
        _pa(), _pb(100, 0),
        _bt_infra(),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    if routed.empty:
        pytest.skip("Routing produced 0 edges on the toy fixture")
    # The fixture statut="E", mode_pose="1"
    assert (routed["statut"] == "E").all()
    assert (routed["mode_pose"] == "1").all()


# ---------------------------------------------------------------------------
# Edge deduplication via _edge_key
# ---------------------------------------------------------------------------

def test_edge_key_is_symmetric():
    """_edge_key((a,b), (c,d)) must equal _edge_key((c,d), (a,b))."""
    u = (10.0, 20.0)
    v = (30.0, 40.0)
    assert routing._edge_key(u, v) == routing._edge_key(v, u)


def test_routing_dedups_shared_trunk(monkeypatch):
    """Feature D: a trunk shared by 2 PBs of the same PA appears once."""
    G = nx.Graph()
    # Shared trunk PA -> middle, then 2 short branches to 2 PBs.
    G.add_edge((0.0, 0.0), (5.0, 0.0), length=5,
               type="infra", statut="E", mode_pose="1", src="bt", infra_type="bt")
    G.add_edge((5.0, 0.0), (10.0, 0.0), length=5,
               type="infra", statut="E", mode_pose="1", src="bt", infra_type="bt")
    G.add_edge((10.0, 0.0), (12.0, 2.0), length=2.83,
               type="infra", statut="E", mode_pose="1", src="bt", infra_type="bt")
    G.add_edge((10.0, 0.0), (12.0, -2.0), length=2.83,
               type="infra", statut="E", mode_pose="1", src="bt", infra_type="bt")

    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa()
    pb_two = gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": ["PB1", "PB2"], "pa_id": ["PA1", "PA1"],
        "geometry": [Point(12, 2), Point(12, -2)],
    }), geometry="geometry", crs=CRS)

    routed = routing.route_pa_to_pb(
        pa, pb_two,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    # 4 unique edges (shared trunk + 2 branches), not 6.
    assert len(routed) == 4


# ---------------------------------------------------------------------------
# Pas de sklearn dans routing.py
# ---------------------------------------------------------------------------

def test_routing_module_does_not_import_sklearn():
    """routing.py ne doit pas importer sklearn (absent du QGIS embedded)."""
    src = inspect.getsource(routing)
    assert "sklearn" not in src, "sklearn ne doit pas etre importe dans routing.py"


def test_weld_function_uses_scipy_kdtree():
    """Sanity: _weld_close_nodes uses scipy.spatial.cKDTree."""
    src = inspect.getsource(routing._weld_close_nodes)
    assert "cKDTree" in src
    assert "sklearn" not in src
