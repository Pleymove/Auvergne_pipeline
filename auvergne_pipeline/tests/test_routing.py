"""Tests for routing (PR #14)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from auvergne_pipeline import config, routing


def _straight_infra() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "statut": ["E"], "mode_pose": ["1"], "src": ["bt"],
        "geometry": [LineString([(0, 0), (100, 0)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pa_at_pt(x, y, pid="PA1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pb_at_pt(x, y, pid="PB1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pid], "pa_id": ["PA1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def test_route_simple_straight_line():
    """PA at (0,0), PB at (100,0), straight infra → 1 edge."""
    routed = routing.route_pa_to_pb(
        _pa_at_pt(0, 0), _pb_at_pt(100, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    assert len(routed) >= 1


def test_route_empty_pa_returns_empty():
    routed = routing.route_pa_to_pb(
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        _pb_at_pt(0, 0), _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    assert len(routed) == 0


def test_route_disconnected_pa_no_path():
    """PA far from any infra → flag but no crash."""
    import networkx as nx
    routed = routing.route_pa_to_pb(
        _pa_at_pt(1000, 1000, "PA_FAR"), _pb_at_pt(0, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    # PA is too far to snap, should return empty
    assert len(routed) == 0


def test_routed_edges_have_statut_mode_pose():
    routed = routing.route_pa_to_pb(
        _pa_at_pt(0, 0), _pb_at_pt(100, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    if len(routed) > 0:
        assert "statut" in routed.columns
        assert "mode_pose" in routed.columns


# ---------------------------------------------------------------------------
# PR #22 tests — welding + GC neuf on demand
# ---------------------------------------------------------------------------


def test_weld_close_nodes_merges_disjoint_islands():
    """PR #22 Spec A: 2 parallel LineStrings at 1m gap → 1 CC after welding."""
    import networkx as nx
    import numpy as np

    G = nx.Graph()
    # Island 1: edge from (0,0) to (10,0)
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    # Island 2: edge from (0, 1.0) to (10, 1.0) — 1m gap
    G.add_edge((0.0, 1.0), (10.0, 1.0), length=10, type="infra")

    # Before welding: 2 components
    assert nx.number_connected_components(G) == 2

    G_w = routing._weld_close_nodes(G, weld_radius_m=2.0)

    # After welding: 1 component (nodes at 1m < 2m threshold)
    assert nx.number_connected_components(G_w) == 1
    assert G_w.number_of_nodes() < G.number_of_nodes()  # nodes merged


def test_bridge_gc_neuf_when_no_path():
    """PR #22 Spec B: short-gap PA/PB (< 3m) get a VIRTUAL C0 bridge.
    PR #33: bridges > 3m are rejected — no visible straight connectors."""
    import networkx as nx

    # Test 1: micro gap (2m) should create a virtual bridge
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((1.5, 0.0))  # only 1.5m from existing node

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (1.5, 0.0)
    )
    assert bridged
    assert G.has_edge((0.0, 0.0), (1.5, 0.0))
    edge = G[(0.0, 0.0)][(1.5, 0.0)]
    assert edge["mode_pose"] == "C0"
    assert edge["src"] == "gc_neuf"
    # PR #33: must be virtual (not delivered to livrable)
    assert edge.get("virtual") is True
    assert edge.get("deliverable") is False

    # Test 2: large gap (30m) should be REJECTED
    G2 = nx.Graph()
    G2.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G2.add_node((30.0, 0.0))

    bridged2 = routing._bridge_components_with_gc_neuf(
        G2, (0.0, 0.0), (30.0, 0.0)
    )
    assert not bridged2, "Bridge > 3m should be rejected"
    assert not G2.has_edge((0.0, 0.0), (30.0, 0.0))


def test_no_self_loop_after_welding():
    """PR #22 Spec A: edge endpoints <2m apart → no self-loop after welding."""
    import networkx as nx
    G = nx.Graph()
    # Single edge whose endpoints are 0.5m apart
    G.add_edge((0.0, 0.0), (0.5, 0.0), length=0.5, type="infra")

    G_w = routing._weld_close_nodes(G, weld_radius_m=2.0)

    # Endpoints at 0.5m both merge into same centroid → self-loop skipped
    # Result: 0 nodes (degenerate edge, no connected component preserved)
    assert G_w.number_of_nodes() <= 1
    assert G_w.number_of_edges() == 0  # self-loop removed


# ---------------------------------------------------------------------------
# PR #22.5 — sklearn-free welding
# ---------------------------------------------------------------------------


def test_weld_uses_scipy_not_sklearn():
    """Garantit qu'on n'a plus de dépendance sklearn dans routing.py."""
    import inspect
    src = inspect.getsource(routing._weld_close_nodes)
    assert 'sklearn' not in src, "routing.py ne doit plus importer sklearn"
    assert 'DBSCAN' not in src, "routing.py ne doit plus utiliser DBSCAN"
    assert 'cKDTree' in src, "routing.py doit utiliser scipy cKDTree"


# ---------------------------------------------------------------------------
# PR #23 — Bug A + Feature D tests
# ---------------------------------------------------------------------------


def test_infra_propagates_statut_mode_pose():
    """Bug A : edges in the graph must carry statut/mode_pose from sources."""
    import networkx as nx
    from shapely.geometry import LineString
    import geopandas as gpd
    from auvergne_pipeline.routing import _build_graph

    bt = gpd.GeoDataFrame({
        "geometry": [LineString([(0, 0), (10, 0)])],
        "statut": [None],
        "mode_pose": ["E1"],
        "src": ["bt"],
    }, crs="EPSG:2154")
    ign = gpd.GeoDataFrame(geometry=[], crs="EPSG:2154")
    G = _build_graph(bt, ign)
    edge_data = list(G.edges(data=True))[0][2]
    assert edge_data.get("mode_pose") == "E1"
    assert edge_data.get("infra_type") == "bt"


def test_pa_to_pbs_share_common_path():
    """Feature D : 2 neighbouring PBs must share edges (mutualisation)."""
    import networkx as nx
    from shapely.geometry import Point
    import geopandas as gpd
    import pandas as pd
    from auvergne_pipeline.routing import route_pa_to_pb

    G_test = nx.Graph()
    # PA at (0,0), common trunk to (30,0), then branches to (35,10) and (35,-10)
    G_test.add_edge((0.0, 0.0), (15.0, 0.0), length=15, type="infra", statut="E", mode_pose="1")
    G_test.add_edge((15.0, 0.0), (30.0, 0.0), length=15, type="infra", statut="E", mode_pose="1")
    G_test.add_edge((30.0, 0.0), (35.0, 10.0), length=11.18, type="infra", statut="E", mode_pose="1")
    G_test.add_edge((30.0, 0.0), (35.0, -10.0), length=11.18, type="infra", statut="E", mode_pose="1")

    pa = gpd.GeoDataFrame(
        pd.DataFrame({"id_metier": ["PA1"], "sro": ["TEST"], "geometry": [Point(0, 0)]}),
        geometry="geometry", crs="EPSG:2154",
    )
    pb = gpd.GeoDataFrame(
        pd.DataFrame({
            "pb_id": ["PB1", "PB2"], "pa_id": ["PA1", "PA1"],
            "geometry": [Point(35, 10), Point(35, -10)],
        }),
        geometry="geometry", crs="EPSG:2154",
    )

    # Hack: replace _build_graph to return our test graph
    import auvergne_pipeline.routing as routing_mod
    _orig_build = routing_mod._build_graph
    routing_mod._build_graph = lambda infra, ign, **kw: G_test
    try:
        routed = route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
        )
    finally:
        routing_mod._build_graph = _orig_build

    # Mutualisation: 4 unique edges (not 6 = 2 paths x 3 edges each)
    # Common trunk: (0,0)-(5,0)-(10,0) shared
    assert len(routed) == 4, f"Expected 4 edges, got {len(routed)}"