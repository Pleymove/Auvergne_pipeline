"""Tests for PR #27 — topology snap (A/Z only) + public-only C0 bridges."""

from __future__ import annotations

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import config, routing


# ---------------------------------------------------------------------------
# 1. Bridge C0 rejected if crossing private
# ---------------------------------------------------------------------------

def test_bridge_rejected_private_crossing():
    """PR #27 Part A: bridge <= 50m but outside public_area is rejected."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((30.0, 0.0))

    public_area = Polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)])

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (30.0, 0.0),
        public_area=public_area,
    )
    assert not bridged


# ---------------------------------------------------------------------------
# 2. Bridge C0 accepted if in public_area
# ---------------------------------------------------------------------------

def test_bridge_accepted_in_public_area():
    """PR #27 Part A: bridge <= 50m fully inside public_area is accepted."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((30.0, 0.0))

    public_area = Polygon([(-10, -10), (40, -10), (40, 10), (-10, 10)])

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (30.0, 0.0),
        public_area=public_area,
    )
    assert bridged


# ---------------------------------------------------------------------------
# 3. Bridge > 50m rejected
# ---------------------------------------------------------------------------

def test_bridge_rejected_too_long():
    """PR #27: bridge > 50m rejected regardless of public_area."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((100.0, 0.0))

    public_area = Polygon([(-20, -20), (120, -20), (120, 20), (-20, 20)])

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (100.0, 0.0),
        public_area=public_area,
    )
    assert not bridged


# ---------------------------------------------------------------------------
# 4. Bridge with public_area=None → fail-closed
# ---------------------------------------------------------------------------

def test_bridge_rejected_no_public_area():
    """PR #27 amend: public_area=None must fail-closed (no blind bridges)."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((30.0, 0.0))

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (30.0, 0.0),
        public_area=None,
    )
    assert not bridged


# ---------------------------------------------------------------------------
# 5. Bridge on boundary → accepted (covers + buffer tolerance)
# ---------------------------------------------------------------------------

def test_bridge_accepted_on_boundary():
    """PR #27 amend: bridge exactly on public_area boundary is accepted."""
    G = nx.Graph()
    G.add_node((0.0, 0.0))
    G.add_node((30.0, 0.0))

    # Public area covers exactly [0..30] on x-axis
    # Bridge at y=0,0 to y=0,30 — ON the boundary
    public_area = Polygon([(0, -5), (30, -5), (30, 5), (0, 5)])

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (30.0, 0.0),
        public_area=public_area,
    )
    assert bridged


# ---------------------------------------------------------------------------
# 6. Endpoint snap: only degree-1 nodes
# ---------------------------------------------------------------------------

def test_endpoints_snapped_to_same_node():
    """PR #27 Part B: two A/Z endpoints at 1m become same node after snap."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_edge((0.0, 1.0), (20.0, 20.0), length=28.28, type="infra")
    assert nx.number_connected_components(G) == 2

    routing._snap_endpoints_topology(G, snap_radius_m=3.0)

    assert nx.number_connected_components(G) == 1


# ---------------------------------------------------------------------------
# 7. Internal vertices NOT merged
# ---------------------------------------------------------------------------

def test_internal_vertices_not_merged():
    """PR #27 amend: internal vertices (degree 2) at <3m must NOT be fused."""
    G = nx.Graph()
    # Chain: (0,0)-(5,0)-(10,0)-(15,0) — all degree-2 internal
    G.add_edge((0.0, 0.0), (5.0, 0.0), length=5, type="infra")
    G.add_edge((5.0, 0.0), (10.0, 0.0), length=5, type="infra")
    G.add_edge((10.0, 0.0), (15.0, 0.0), length=5, type="infra")
    # Second chain close by: (0,2)-(5,2)-(10,2)-(15,2)
    G.add_edge((0.0, 2.0), (5.0, 2.0), length=5, type="infra")
    G.add_edge((5.0, 2.0), (10.0, 2.0), length=5, type="infra")
    G.add_edge((10.0, 2.0), (15.0, 2.0), length=5, type="infra")

    n_before = G.number_of_nodes()
    # Only degree-1 nodes are (0,0), (15,0), (0,2), (15,2) + internal nodes
    # (0,0) and (0,2) are 2m apart → should merge
    # (15,0) and (15,2) are 2m apart → should merge
    # BUT internal nodes like (5,0) and (5,2) at 2m apart → should NOT merge
    routing._snap_endpoints_topology(G, snap_radius_m=3.0)

    # After snap: only the 4 degree-1 endpoints merge (2 pairs),
    # internal nodes untouched. So we lose 2 nodes.
    # Before: 8 nodes. After: 6 nodes (2 pairs merged, 0 internal merged)
    assert G.number_of_nodes() == n_before - 2


# ---------------------------------------------------------------------------
# 8. Geometry visually connected after endpoint snap
# ---------------------------------------------------------------------------

def test_geometry_connected_after_snap():
    """PR #27 amend: after endpoint snap, geometries must touch visually."""
    G = nx.Graph()
    geom1 = LineString([(0.0, 0.0), (10.0, 0.0)])
    geom2 = LineString([(10.0, 5.0), (20.0, 20.0)])
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra", geometry=geom1)
    G.add_edge((10.0, 5.0), (20.0, 20.0), length=18.03, type="infra", geometry=geom2)
    # Endpoint (10,0) and (10,5) are 5m apart, > 3m → won't snap

    # Now add a third edge bridging the gap
    G.add_edge((10.0, 0.0), (10.0, 2.0), length=2, type="infra",
               geometry=LineString([(10, 0), (10, 2)]))
    G.add_edge((10.0, 2.0), (10.0, 5.0), length=3, type="infra",
               geometry=LineString([(10, 2), (10, 5)]))

    routing._snap_endpoints_topology(G, snap_radius_m=3.0)

    # All edges should now be connected — check component count
    assert nx.number_connected_components(G) == 1

    # Verify that edge geometries end at correct snapped coords
    for u, v, data in G.edges(data=True):
        geom = data.get("geometry")
        if geom is not None:
            coords = list(geom.coords)
            # Undirected graph — geometry endpoints must match node
            # endpoints regardless of edge direction.
            geom_endpoints = {tuple(coords[0]), tuple(coords[-1])}
            node_endpoints = {tuple(u), tuple(v)}
            assert geom_endpoints == node_endpoints, (
                f"Geometry endpoints {geom_endpoints} != "
                f"node endpoints {node_endpoints}"
            )


# ---------------------------------------------------------------------------
# 9. No self-loop or zero-length after snap
# ---------------------------------------------------------------------------

def test_no_self_loop_after_endpoint_snap():
    """PR #27 Part B: endpoint snap must not create self-loops."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (0.5, 0.0), length=0.5, type="infra")

    routing._snap_endpoints_topology(G, snap_radius_m=3.0)

    for u, v in G.edges():
        assert u != v
        assert G[u][v].get("length", 0) > 0


# ---------------------------------------------------------------------------
# 10. Output compliance
# ---------------------------------------------------------------------------

def test_output_compliance_pr27():
    """PR #27: output must be clean after topology snap."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra",
               statut="E", mode_pose="1", src="bt", infra_type="bt",
               geometry=LineString([(0, 0), (10, 0)]))

    pa = gpd.GeoDataFrame(
        pd.DataFrame({"id_metier": ["PA1"], "sro": ["TEST"], "geometry": [Point(0, 0)]}),
        geometry="geometry", crs="EPSG:2154",
    )
    pb = gpd.GeoDataFrame(
        pd.DataFrame({"pb_id": ["PB1"], "pa_id": ["PA1"], "geometry": [Point(10, 0)]}),
        geometry="geometry", crs="EPSG:2154",
    )

    import auvergne_pipeline.routing as routing_mod
    _orig = routing_mod._build_graph
    routing_mod._build_graph = lambda infra, ign, **kw: G
    try:
        out = routing_mod.route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
        )
    finally:
        routing_mod._build_graph = _orig

    assert len(out) >= 1
    assert (out["src"] == "gc_neuf_runtime").sum() == 0
    assert (out["src"] == "ign_route").sum() == 0
    assert out["statut"].isna().sum() == 0

    sk = out["statut"].fillna("").astype(str) + out["mode_pose"].fillna("").astype(str)
    assert (sk == "").sum() == 0
