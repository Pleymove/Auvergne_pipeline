"""PR #29 regression tests.

Covers the 4 main fixes from PR29:

A1 — strict routing weight hierarchy (existing < gc_neuf < ign_route).
A3 — endpoint-to-existing-line preference over a long gc_neuf detour.
B2 — endpoint→line connector in private domain rejected without splitting
     the original edge.
B5 — bridge C0 carries an explicit ``_routing_weight`` (penalised).

Plus a smoke test for ``_routing_weight_for`` and a no-sklearn check.
"""

from __future__ import annotations

import inspect

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import routing


CRS = "EPSG:2154"


def _pa(x=0.0, y=0.0, pid="PA1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


def _pb(coords_pid_paid):
    """coords_pid_paid: list of (x, y, pb_id, pa_id)."""
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [t[2] for t in coords_pid_paid],
        "pa_id": [t[3] for t in coords_pid_paid],
        "geometry": [Point(t[0], t[1]) for t in coords_pid_paid],
    }), geometry="geometry", crs=CRS)


# ---------------------------------------------------------------------------
# A1 — Routing weight hierarchy
# ---------------------------------------------------------------------------


def test_routing_weight_for_hierarchy():
    """Sanity: the helper assigns 1× / 10× / 30× as documented."""
    assert routing._routing_weight_for({"type": "infra", "src": "bt", "length": 10}) == 10.0
    assert routing._routing_weight_for({"type": "gc_neuf", "length": 10}) == 100.0
    assert routing._routing_weight_for({"type": "ign_route", "length": 10}) == 300.0


def test_ign_route_penalized_vs_existing(monkeypatch):
    """Dijkstra must pick the slightly longer existing path over a shorter
    IGN path. Without the hierarchy, the IGN edge would win on length alone.
    """
    G = nx.Graph()
    # PA at (0,0). Two paths to PB at (50,0):
    # - shortcut via IGN: (0,0) -> (25, 0) -> (50, 0)  total length=50
    # - existing detour: (0,0) -> (0, 5) -> (50, 5) -> (50, 0) total length=60
    G.add_edge((0.0, 0.0), (25.0, 0.0),
               length=25, type="ign_route", src="ign_route",
               infra_type="ign_route", statut="", mode_pose="",
               geometry=LineString([(0, 0), (25, 0)]))
    G.add_edge((25.0, 0.0), (50.0, 0.0),
               length=25, type="ign_route", src="ign_route",
               infra_type="ign_route", statut="", mode_pose="",
               geometry=LineString([(25, 0), (50, 0)]))
    G.add_edge((0.0, 0.0), (0.0, 5.0),
               length=5, type="infra", src="bt",
               infra_type="bt", statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (0, 5)]))
    G.add_edge((0.0, 5.0), (50.0, 5.0),
               length=50, type="infra", src="bt",
               infra_type="bt", statut="E", mode_pose="1",
               geometry=LineString([(0, 5), (50, 5)]))
    G.add_edge((50.0, 5.0), (50.0, 0.0),
               length=5, type="infra", src="bt",
               infra_type="bt", statut="E", mode_pose="1",
               geometry=LineString([(50, 5), (50, 0)]))

    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa()
    pb = _pb([(50, 0, "PB1", "PA1")])
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    # The ign_route shortcut would yield 2 edges, the existing detour 3.
    # Crucially, none of the routed edges should retain src='ign_route' or
    # infra_type='ign_route' AND the chosen path must be the existing one.
    srcs = set(out["src"].tolist())
    infra_types = set(out["infra_type"].tolist())
    assert "ign_route" not in srcs, f"IGN should never appear in src, got {srcs}"
    assert "ign_route" not in infra_types, (
        f"IGN should never appear in infra_type, got {infra_types}"
    )
    # The existing-infra path has 3 segments -> 3 edges in output.
    assert len(out) == 3, (
        "Dijkstra should choose the existing 3-edge detour, not the 2-edge "
        f"IGN shortcut (got {len(out)} edges, srcs={srcs})"
    )
    # All routed edges must come from the existing BT branch.
    assert all(out["src"] == "bt"), out["src"].tolist()
    assert all(out["infra_type"] == "bt"), out["infra_type"].tolist()


# ---------------------------------------------------------------------------
# A3 — Existing endpoint → existing line preferred over gc_neuf
# ---------------------------------------------------------------------------


def test_existing_endpoint_to_existing_line_preferred_over_gc(monkeypatch):
    """An existing endpoint dangling 1 m from an existing line gets snapped
    onto the line. The Dijkstra path then prefers the existing edges over
    inventing a long gc_neuf bridge.
    """
    G = nx.Graph()
    # Existing line A: (0,0) -- (10,0)
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (10, 0)]))
    # Existing line B (parallel, 1 m offset, dangling endpoint):
    G.add_edge((5.0, 1.0), (15.0, 1.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(5, 1), (15, 1)]))

    # Provide a public_area large enough that any C0 connector is allowed.
    public_area = Polygon([(-5, -5), (20, -5), (20, 5), (-5, 5)])

    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb([(15, 1, "PB1", "PA1")])
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=public_area,
    )
    assert not out.empty, "Pipeline should produce a route via existing infra"
    # The output must use bt segments — at least one bt edge in the route.
    assert (out["infra_type"] == "bt").any(), out["infra_type"].tolist()
    # The total bt length should dominate any gc_neuf connector.
    bt_len = float(out[out["infra_type"] == "bt"]["length_m"].sum())
    gc_len = float(out[out["infra_type"] == "gc_neuf"]["length_m"].sum())
    assert bt_len >= gc_len, (
        f"Existing infra should dominate the route. bt={bt_len:.1f}, gc={gc_len:.1f}"
    )


# ---------------------------------------------------------------------------
# B2 — Validate connector before splitting edge
# ---------------------------------------------------------------------------


def test_endpoint_to_line_private_connector_does_not_split_edge():
    """A connector that crosses a private parcel must be rejected and the
    target edge must remain untouched (not split).
    """
    G = nx.Graph()
    # Existing line at y=0
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (10, 0)]))
    # Dangling endpoint at y=2 (degree-1 in this subgraph)
    G.add_edge((4.0, 2.0), (4.5, 2.5),
               length=0.71, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(4.0, 2.0), (4.5, 2.5)]))

    # Public area covers ONLY y >= 1.5 — the connector from (4, 2) down to
    # (4, 0) crosses the private band y in [0, 1.5).
    public_area = Polygon([(-5, 1.5), (20, 1.5), (20, 10), (-5, 10)])

    n_edges_before = G.number_of_edges()
    stats = routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0,
        public_area=public_area,
    )
    n_edges_after = G.number_of_edges()
    assert stats["endpoints_to_lines"] == 0, "no successful connector expected"
    assert stats["endpoints_rejected_private"] >= 1, (
        f"connector should be rejected (stats={stats})"
    )
    # CRITICAL: the original edge must NOT have been split.
    assert n_edges_after == n_edges_before, (
        f"edge count changed: {n_edges_before} -> {n_edges_after}"
    )
    assert G.has_edge((0.0, 0.0), (10.0, 0.0)), "target edge must be intact"


def test_endpoint_to_line_public_connector_splits_edge():
    """A connector that lies in the public domain DOES split the target
    edge and adds the gc_neuf connector — the standard happy path.
    """
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (10, 0)]))
    # Dangling endpoint at (5, 2) — connector to (5, 0) is fully in public.
    G.add_edge((5.0, 2.0), (5.5, 2.5),
               length=0.71, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(5.0, 2.0), (5.5, 2.5)]))

    # Public area covers everything.
    public_area = Polygon([(-10, -10), (20, -10), (20, 10), (-10, 10)])

    stats = routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0,
        public_area=public_area,
    )
    assert stats["endpoints_to_lines"] >= 1
    # Target edge replaced by 2 sub-edges + 1 gc_neuf connector.
    assert not G.has_edge((0.0, 0.0), (10.0, 0.0))


# ---------------------------------------------------------------------------
# B5 — bridge C0 keeps explicit weight
# ---------------------------------------------------------------------------


def test_bridge_keeps_explicit_routing_weight():
    """PR #33 amend: micro bridge (<= 3m) created as VIRTUAL edge."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (5.0, 0.0), length=5, type="infra", src="bt")
    G.add_node((2.0, 0.0))  # 2m, micro bridge allowed

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (2.0, 0.0),
    )
    assert bridged
    assert G.has_edge((0.0, 0.0), (2.0, 0.0))
    edge = G[(0.0, 0.0)][(2.0, 0.0)]
    # PR #33: must be virtual and non-deliverable
    assert edge.get("virtual") is True
    assert edge.get("deliverable") is False
    assert edge["length"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Module hygiene: still no sklearn after PR #29
# ---------------------------------------------------------------------------


def test_routing_no_sklearn_dependency():
    src = inspect.getsource(routing)
    assert "sklearn" not in src
    assert "DBSCAN" not in src


# ---------------------------------------------------------------------------
# Amend — [ROUTING QA] counts IGN BEFORE the IGN→gc_neuf conversion
# ---------------------------------------------------------------------------


def test_routing_qa_counts_ign_before_conversion(monkeypatch, caplog):
    """Regression for the false-zero bug: ``ign_route_length_used_m`` must
    be > 0 when Dijkstra actually traverses an IGN edge, even though the
    final livrable_infra row reports ``src='gc_neuf'`` after conversion.

    Setup: graph with ONLY an IGN edge between PA and PB, so Dijkstra is
    forced to use it. The output GeoDataFrame will have ``src=='gc_neuf'``
    (CDC conversion), but the raw-source counters must record
    ``ign_route_length_used_m=20``.
    """
    import logging

    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (20.0, 0.0),
        length=20, type="ign_route", src="ign_route",
        infra_type="ign_route", statut="", mode_pose="",
        geometry=LineString([(0, 0), (20, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa()
    pb = _pb([(20, 0, "PB1", "PA1")])
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            gpd.GeoDataFrame(geometry=[], crs=CRS),
        )

    # 1) The output is converted to gc_neuf (CDC), so reading src would lie.
    assert len(out) == 1
    assert out.iloc[0]["src"] == "gc_neuf"
    assert out.iloc[0]["infra_type"] == "gc_neuf"
    assert out.iloc[0]["mode_pose"] == "C0"

    # 2) The new ROUTING QA log line must contain a non-zero
    #    ign_route_length_used_m AND a converted_ign_to_gc_length_m.
    qa_lines = [r.getMessage() for r in caplog.records if "[ROUTING QA]" in r.getMessage()]
    assert qa_lines, "no [ROUTING QA] log line emitted"
    ign_qa_lines = [m for m in qa_lines if "ign_route_length_used_m" in m]
    assert ign_qa_lines, qa_lines
    msg = ign_qa_lines[0]
    # ign_route_length_used_m=20 (length=20)
    assert "ign_route_length_used_m=20" in msg, msg
    assert "converted_ign_to_gc_length_m=20" in msg, msg

    # 3) raw_src_counts must surface ign_route as a raw family.
    raw_count_lines = [m for m in qa_lines if "raw_src_counts" in m]
    assert raw_count_lines, qa_lines
    assert "ign_route=1" in raw_count_lines[0], raw_count_lines[0]

    # 4) A WARNING line must flag the IGN traversal so Pierre sees it.
    warn_lines = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "ign_route_used_before_conversion" in r.getMessage()
    ]
    assert warn_lines, "expected a [ROUTING WARNING] for ign_route_used_before_conversion"


def test_routing_qa_zero_when_no_ign_used(monkeypatch, caplog):
    """Symmetric: when the path uses only existing infra, the IGN counter
    is 0 and no IGN warning is emitted.
    """
    import logging

    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (20.0, 0.0),
        length=20, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=LineString([(0, 0), (20, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa()
    pb = _pb([(20, 0, "PB1", "PA1")])
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            gpd.GeoDataFrame(geometry=[], crs=CRS),
        )

    assert len(out) == 1
    assert out.iloc[0]["src"] == "bt"
    msgs = " || ".join(r.getMessage() for r in caplog.records)
    assert "ign_route_length_used_m=0" in msgs, msgs
    assert "converted_ign_to_gc_length_m=0" in msgs, msgs
    # No IGN warning when no IGN was used.
    ign_warns = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "ign_route_used_before_conversion" in r.getMessage()
    ]
    assert not ign_warns
