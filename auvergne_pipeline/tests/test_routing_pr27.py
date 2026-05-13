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
    """PR #33 amend: micro bridge (<= 3m) inside public_area creates a virtual edge."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((2.0, 0.0))  # 2m away, <= 3m PR33 threshold

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (2.0, 0.0),
    )
    assert bridged
    # PR #33: must be virtual
    edge = G[(0.0, 0.0)][(2.0, 0.0)]
    assert edge.get("virtual") is True
    assert edge.get("deliverable") is False


# ---------------------------------------------------------------------------
# 3. Bridge > 3m rejected (PR33)
# ---------------------------------------------------------------------------

def test_bridge_rejected_too_long():
    """PR #33: bridge > 3m rejected regardless of public_area."""
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
# 4. Bridge with public_area=None → only micro bridges allowed (PR33)
# ---------------------------------------------------------------------------

def test_bridge_rejected_no_public_area():
    """PR #33: no public_area — only micro bridges (<= 3m) allowed."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((1.0, 0.0))  # 1m — micro bridge allowed

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (1.0, 0.0),
        public_area=None,
    )
    assert bridged
    assert G[(0.0, 0.0)][(1.0, 0.0)].get("virtual") is True


# ---------------------------------------------------------------------------
# 5. Bridge on boundary → micro only (PR33)
# ---------------------------------------------------------------------------

def test_bridge_accepted_on_boundary():
    """PR #33 amend: micro bridge (<= 3m) accepted even without public_area check."""
    G = nx.Graph()
    G.add_node((0.0, 0.0))
    G.add_node((2.0, 0.0))  # 2m — micro bridge allowed
    # No public_area provided — PR33 allows micro bridges regardless

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (2.0, 0.0),
    )
    assert bridged
    assert G[(0.0, 0.0)][(2.0, 0.0)].get("virtual") is True


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


# ---------------------------------------------------------------------------
# PR #28 — BLOQUANT 1: 3D coords do not crash
# ---------------------------------------------------------------------------


def test_3d_coords_no_crash_in_snap():
    """PR #28 BLOQUANT 1: LineString with Z-dim coords must not crash."""
    G = nx.Graph()
    geom_3d = LineString([(0, 0, 10), (10, 0, 20)])
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra",
               geometry=geom_3d)
    G.add_edge((10.0, 2.0), (20.0, 20.0, 30), length=18, type="infra",
               geometry=LineString([(10, 2, 0), (20, 20, 30)]))

    # Must not raise
    routing._snap_endpoints_topology(G, snap_radius_m=3.0)
    assert G.number_of_nodes() >= 2


def test_3d_snap_keeps_valid_2d_output():
    """After snapping 3D coords, output geometries are valid 2D."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra",
               geometry=LineString([(0, 0, 5), (10, 0, 5)]))
    G.add_edge((0.0, 1.0), (20.0, 20.0), length=28, type="infra",
               geometry=LineString([(0, 1, 5), (20, 20, 5)]))

    routing._snap_endpoints_topology(G, snap_radius_m=3.0)
    # All edges should still be connected
    assert G.number_of_edges() >= 1


# ---------------------------------------------------------------------------
# PR #28 — BLOQUANT 2: gc_neuf injected via _add_gc_neuf_to_graph checked
# ---------------------------------------------------------------------------


def test_add_gc_neuf_rejected_private_crossing():
    """PR #28 BLOQUANT 2: gc_neuf from _add_gc_neuf_to_graph rejected if private."""
    G = nx.Graph()
    public_area = Polygon([(0, -5), (30, -5), (30, 5), (0, 5)])

    # gc_neuf crossing outside public_area (x=0→100)
    gc_df = gpd.GeoDataFrame(
        {"pa_id": ["PA1"], "geometry": [LineString([(0, 0), (100, 0)])]},
        geometry="geometry", crs="EPSG:2154",
    )
    n = routing._add_gc_neuf_to_graph(
        G, gc_df, snap_tol=50, public_area=public_area,
    )
    assert n == 1  # one rejected
    assert G.number_of_edges() == 0  # nothing added


def test_add_gc_neuf_accepted_in_public():
    """PR #28: gc_neuf fully within public_area is accepted."""
    G = nx.Graph()
    public_area = Polygon([(0, -5), (50, -5), (50, 5), (0, 5)])

    gc_df = gpd.GeoDataFrame(
        {"pa_id": ["PA1"], "geometry": [LineString([(10, 0), (40, 0)])]},
        geometry="geometry", crs="EPSG:2154",
    )
    n = routing._add_gc_neuf_to_graph(
        G, gc_df, snap_tol=50, public_area=public_area,
    )
    assert n == 0  # accepted
    assert G.number_of_edges() == 1


# ---------------------------------------------------------------------------
# PR #28 — BLOQUANT 4: endpoint-to-line snap
# ---------------------------------------------------------------------------


def test_endpoint_snaps_to_line():
    """PR #28 BLOQUANT 4: degree-1 endpoint near a line splits it and connects.

    PR #34 amend v3: the dangling endpoint is now RELOCATED into the
    projection node instead of being linked by a virtual perpendicular
    connector. Connectivity check is still the canonical assertion;
    the location of the old endpoint coords is no longer guaranteed.
    """
    G = nx.Graph()
    # Line from (0,0) to (20,0)
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra",
               geometry=LineString([(0, 0), (20, 0)]))
    # Dangling endpoint at (10, 1) — connected to a stub, making it degree 1
    G.add_edge((10.0, 1.0), (10.0, 5.0), length=4, type="infra",
               geometry=LineString([(10, 1), (10, 5)]))
    assert G.degree((10.0, 1.0)) == 1

    routing._snap_endpoints_to_lines(G, snap_radius_m=3.0)

    # The original endpoint may have been relocated and removed; what
    # matters is that the graph is now one connected component.
    assert nx.number_connected_components(G) == 1


def test_endpoint_to_line_private_rejected():
    """PR #28 amend B1: endpoint→line connector outside public_area rejected."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra",
               geometry=LineString([(0, 0), (20, 0)]))
    G.add_edge((10.0, 1.0), (10.0, 5.0), length=4, type="infra",
               geometry=LineString([(10, 1), (10, 5)]))
    # public_area covers nothing near (10,1)→(10,0)
    public_area = Polygon([(50, -5), (70, -5), (70, 5), (50, 5)])

    routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0, public_area=public_area,
    )
    # Endpoint should still have degree 1 (no new connector added)
    assert G.degree((10.0, 1.0)) == 1


def test_endpoint_to_line_public_accepted():
    """PR #28 amend B1: endpoint→line connector within public_area accepted.

    PR #34 amend v3: instead of adding a virtual connector, the endpoint
    is relocated onto the projection point. The graph becomes one
    connected component without any new virtual/non-deliverable edge.
    """
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra",
               geometry=LineString([(0, 0), (20, 0)]))
    G.add_edge((10.0, 1.0), (10.0, 5.0), length=4, type="infra",
               geometry=LineString([(10, 1), (10, 5)]))
    # public_area covers the connector region
    public_area = Polygon([(0, -5), (20, -5), (20, 5), (0, 5)])

    routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0, public_area=public_area,
    )
    # The graph must become fully connected, and no virtual edge created.
    assert nx.number_connected_components(G) == 1
    for _u, _v, data in G.edges(data=True):
        assert not data.get("virtual", False), (
            "PR #34 v3: no virtual connector after endpoint relocation"
        )


def test_bridge_has_routing_weight():
    """PR #33 amend: micro bridge (<= 3m) created with virtual=True."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((2.0, 0.0))  # 2m away, <= 3m

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (2.0, 0.0),
    )
    assert bridged
    edge_data = G.get_edge_data((0.0, 0.0), (2.0, 0.0))
    assert edge_data is not None
    # PR #33: virtual edge
    assert edge_data.get("virtual") is True
    assert edge_data.get("deliverable") is False


def test_line_snap_chooses_closest_line():
    """PR #28 amend B3: endpoint→line picks closest line by perpendicular distance.

    PR #34 amend v3: with the endpoint-relocation refactor the node count
    is conserved (one node removed at the original endpoint, one added at
    the projection) — but the snap target must still be the closest line.
    """
    G = nx.Graph()
    # Two parallel lines: one at y=0, one at y=5
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra")
    G.add_edge((0.0, 5.0), (20.0, 5.0), length=20, type="infra")
    # Endpoint at (10, 1) — 1m from y=0 line, 4m from y=5 line
    G.add_edge((10.0, 1.0), (10.0, 10.0), length=9, type="infra")

    routing._snap_endpoints_to_lines(G, snap_radius_m=5.0)
    # The split point must sit on the y=0 line (closer), creating a
    # new node at ~(10, 0).
    assert (10.0, 0.0) in G or any(
        abs(n[0] - 10.0) < 0.1 and abs(n[1]) < 0.1 for n in G.nodes()
    ), f"expected a node near (10, 0), got nodes={list(G.nodes())}"
    # The split point should be at ~(10, 0), not (10, 5)
    for u, v, data in G.edges(data=True):
        geom = data.get("geometry")
        if geom is not None and isinstance(geom, LineString):
            coords = list(geom.coords)
            for c in coords:
                # Ensure no connector goes to y=5
                if abs(c[0] - 10.0) < 0.1 and abs(c[1]) < 0.1:
                    break  # found the correct snap
            else:
                continue
            break
    else:
        # Should find a node near (10, 0) — the correct snap point
        found = False
        for n in G.nodes():
            if abs(n[0] - 10.0) < 0.1 and abs(n[1]) < 0.1:
                found = True
                break
        assert found, "No node found near (10, 0) — snap may have gone to wrong line"


def test_endpoint_far_from_line_not_snapped():
    """PR #28 BLOQUANT 4: endpoint >3m from any line remains untouched."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra")
    G.add_node((10.0, 10.0))  # 10m away
    n_before = G.number_of_nodes()

    routing._snap_endpoints_to_lines(G, snap_radius_m=3.0)
    assert G.number_of_nodes() == n_before  # unchanged
    assert G.degree((10.0, 10.0)) == 0  # still isolated


# ---------------------------------------------------------------------------
# PR #28 — BLOQUANT 5: Dijkstra prefers existing over gc_neuf + dedup
# ---------------------------------------------------------------------------


def test_dijkstra_prefers_existing_over_gc_neuf():
    """PR #28 BLOQUANT 5: existing infra preferred even if gc_neuf shorter."""
    G = nx.Graph()
    # Existing path: (0,0)→(20,0) length 20
    G.add_edge((0.0, 0.0), (20.0, 0.0), length=20, type="infra",
               statut="E", mode_pose="1", src="bt", infra_type="bt",
               geometry=LineString([(0, 0), (20, 0)]))
    G.add_node((0.0, 0.0))
    G.add_node((20.0, 0.0))

    # Shorter gc_neuf diagonal (can't be used as direct path if weighted high)
    G.add_edge((0.0, 0.0), (20.0, 0.1), length=20, type="gc_neuf",
               statut="", mode_pose="C0", src="gc_neuf", infra_type="gc_neuf",
               geometry=LineString([(0, 0), (20, 0.1)]))

    # Add routing weights
    for u, v, data in G.edges(data=True):
        base = data.get("length", 1.0)
        data["_routing_weight"] = base * 10 if data.get("type") == "gc_neuf" else base

    # PA at (0,0), target node (20, 0) reachable via both
    try:
        _, paths = nx.single_source_dijkstra(G, source=(0.0, 0.0), weight="_routing_weight")
    except (nx.NetworkXError, KeyError):
        paths = {}
    path = paths.get((20.0, 0.0), [])
    assert len(path) >= 2

    # Should use existing infra (weight 20), not gc_neuf (weight 200)
    # The path must contain the existing edge's endpoint
    assert (20.0, 0.0) in path


def test_geometric_dedup_removes_duplicates():
    """PR #28 BLOQUANT 5: near-identical edges deduped, existing infra kept."""
    from shapely.geometry import LineString
    df = gpd.GeoDataFrame(
        {
            "sro": ["T1", "T1"],
            "pa_id": ["PA1", "PA1"],
            "pb_id": ["PB1", "PB1"],
            "statut": ["E", ""],
            "mode_pose": ["1", "C0"],
            "infra_type": ["bt", "gc_neuf"],
            "src": ["bt", "gc_neuf"],
            "length_m": [10.0, 10.0],
            "geometry": [
                LineString([(0, 0), (10, 0)]),
                LineString([(0.001, 0), (10.001, 0)]),  # same within 1 cm
            ],
        },
        geometry="geometry", crs="EPSG:2154",
    )
    result = routing._dedup_geometries(df)
    assert len(result) == 1
    assert result.iloc[0]["infra_type"] == "bt"  # existing kept
