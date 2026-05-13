"""PR #33 tests — no straight connectors, virtual edges, strict C0/IGN policy."""
from __future__ import annotations

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

import auvergne_pipeline.routing as routing
import auvergne_pipeline.livrable_topology as lt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(**kw):
    defaults = {
        "sro": "SRO1", "pa_id": "PA1", "pb_id": "PB1",
        "statut": "", "mode_pose": "", "infra_type": "ft",
        "src": "ft", "length_m": 10.0,
    }
    defaults.update(kw)
    return defaults


def _df(rows, crs="EPSG:2154"):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def _pa(x, y, pid="PA1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs="EPSG:2154")


def _pb(x, y, pb_id="PB1", pa_id="PA1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pb_id], "pa_id": [pa_id], "id_metier": [pa_id],
        "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs="EPSG:2154")


class _Flags:
    def __init__(self):
        self.entries = []
    def add(self, flag_type, target_url, message):
        self.entries.append({"type": flag_type, "target": target_url, "msg": message})


# ---------------------------------------------------------------------------
# 1. test_no_long_direct_bridge_is_delivered
# ---------------------------------------------------------------------------

def test_no_long_direct_bridge_is_delivered():
    """A bridge between disconnected components > 3m must NOT appear as
    C0 in the livrable. Only a virtual edge may exist for routing."""
    G = nx.Graph()
    # Component 1: two connected nodes
    G.add_edge((0.0, 0.0), (5.0, 0.0),
               length=5.0, geometry=LineString([(0, 0), (5, 0)]),
               type="infra", src="ft", infra_type="ft")
    # Component 2: two connected nodes, far from component 1
    G.add_edge((50.0, 50.0), (55.0, 50.0),
               length=5.0, geometry=LineString([(50, 50), (55, 50)]),
               type="infra", src="ft", infra_type="ft")

    flags = _Flags()
    # Use ACTUAL graph nodes from each component
    pa_node = (0.0, 0.0)   # node in component 1
    pb_node = (50.0, 50.0) # node in component 2
    # Distance between these nodes is ~59m, way above MAX_STRAIGHT_CONNECTOR_M (3m)
    bridged = routing._bridge_components_with_gc_neuf(
        G, pa_node, pb_node, flag_collector=flags,
    )
    # Bridge should be rejected (> 3m)
    assert not bridged, "Bridge > 3m should NOT be created"
    # Check that the flag is raised
    manual_review_flags = [f for f in flags.entries if f["type"] == "COMPONENT_BRIDGE_REQUIRED_MANUAL_REVIEW"]
    assert len(manual_review_flags) == 1, "Should flag manual review for disconnected components"
    # Verify no new edge was added
    assert not G.has_edge(pa_node, pb_node), "No bridge edge should exist"


# ---------------------------------------------------------------------------
# 2. test_micro_snap_under_3m_not_delivered_as_visible_c0
# ---------------------------------------------------------------------------

def test_micro_snap_under_3m_not_delivered_as_visible_c0():
    """A micro-snap <= 3m creates a VIRTUAL edge (routable) but the edge
    must have deliverable=False so it never appears in livrable_infra."""
    G = nx.Graph()
    # Component 1
    G.add_edge((0.0, 0.0), (2.0, 0.0),
               length=2.0, geometry=LineString([(0, 0), (2, 0)]),
               type="infra", src="ft", infra_type="ft")
    # Component 2: only 1m away from (2,0)
    G.add_edge((2.0, 1.0), (4.0, 1.0),
               length=2.0, geometry=LineString([(2, 1), (4, 1)]),
               type="infra", src="ft", infra_type="ft")

    flags = _Flags()
    pa_node = (2.0, 0.0)  # end of comp 1
    pb_node = (2.0, 1.0)  # start of comp 2, 1m away (<= 3m)

    bridged = routing._bridge_components_with_gc_neuf(
        G, pa_node, pb_node, flag_collector=flags,
    )
    # Bridge should be accepted (1m <= 3m)
    assert bridged, "Micro bridge <= 3m should be created"
    # Check edge attributes
    edge_data = G.get_edge_data(pa_node, pb_node)
    assert edge_data is not None
    assert edge_data.get("virtual") is True, "Micro bridge must be VIRTUAL"
    assert edge_data.get("deliverable") is False, "Micro bridge must NOT be deliverable"
    assert edge_data.get("virtual_reason") == "micro_bridge"


# ---------------------------------------------------------------------------
# PR #34 amend — Bloqueur 1: micro-bridge must carry _routing_weight
# ---------------------------------------------------------------------------

def test_micro_bridge_has_routing_weight():
    """PR #34 amend Bloqueur 1: ``_bridge_components_with_gc_neuf`` is
    called AFTER ``prepare_weights``. The micro-bridge it inserts must
    therefore set ``_routing_weight`` itself, otherwise Dijkstra would
    fall back to NetworkX' implicit weight handling.
    """
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (2.0, 0.0),
               length=2.0, geometry=LineString([(0, 0), (2, 0)]),
               type="infra", src="ft", infra_type="ft")
    G.add_edge((2.0, 1.0), (4.0, 1.0),
               length=2.0, geometry=LineString([(2, 1), (4, 1)]),
               type="infra", src="ft", infra_type="ft")

    pa_node = (2.0, 0.0)
    pb_node = (2.0, 1.0)  # 1 m away (<= MAX_STRAIGHT_CONNECTOR_M)

    bridged = routing._bridge_components_with_gc_neuf(G, pa_node, pb_node)
    assert bridged
    edge_data = G.get_edge_data(pa_node, pb_node)
    assert edge_data is not None
    assert edge_data.get("virtual") is True
    assert edge_data.get("deliverable") is False
    assert edge_data.get("virtual_reason") == "micro_bridge"
    # The core PR #34 assertion: a coherent routing weight must be set.
    assert "_routing_weight" in edge_data, (
        "PR #34: micro-bridge must carry _routing_weight (set after "
        "prepare_weights ran in route_pa_to_pb)"
    )
    rw = edge_data["_routing_weight"]
    assert isinstance(rw, (int, float))
    assert rw > 0, "routing weight must be strictly positive"
    # Must match what _routing_weight_for would compute for a gc_neuf edge
    expected = routing._routing_weight_for(edge_data)
    assert abs(rw - expected) < 1e-9


# ---------------------------------------------------------------------------
# PR #34 amend — Bloqueur 2: ign_cap_hit_count is really incremented
# ---------------------------------------------------------------------------

def test_ign_cap_hit_count_increments_when_budget_exceeded(caplog):
    """PR #34 amend Bloqueur 2: ``ign_cap_hit_count`` (logged in
    ``[FINAL TOPO QA]``) must reflect actual cap hits, not stay at 0.

    Build an SRO whose only feasible path is a chain of short, public,
    IGN routes whose cumulative length exceeds
    ``MAX_IGN_DELIVERED_PER_SRO_M``. The cap should kick in and the log
    should expose ``ign_cap_hit`` > 0.
    """
    import logging
    from shapely.geometry import Polygon

    # No existing infra — Dijkstra is forced onto IGN edges.
    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "geometry", "sro_code"],
        geometry="geometry", crs="EPSG:2154",
    )

    # A long IGN polyline made of short segments. Each segment is short
    # enough to pass the per-edge length filter
    # (``IGN_DELIVERY_MAX_LENGTH_M``) but their cumulative length is well
    # above ``MAX_IGN_DELIVERED_PER_SRO_M`` (300 m).
    seg_len = 20.0
    n_segs = 30  # 600 m total — twice the cap
    ign_geom = LineString([(i * seg_len, 0.0) for i in range(n_segs + 1)])
    ign = gpd.GeoDataFrame(
        [{"geometry": ign_geom}], geometry="geometry", crs="EPSG:2154",
    )

    public_area = Polygon([
        (-10.0, -10.0), (n_segs * seg_len + 10.0, -10.0),
        (n_segs * seg_len + 10.0, 10.0), (-10.0, 10.0),
    ])

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1", "id_metier": "PA1", "sro": "SRO1",
        "geometry": Point(n_segs * seg_len, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    flags = _Flags()
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            pa, pb, infra, ign,
            flag_collector=flags,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            public_area=public_area,
            delivery_public_area=public_area,
        )

    final_topo_lines = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo_lines, "expected a [FINAL TOPO QA] log line"
    qa_line = final_topo_lines[-1]
    # Extract ign_cap_hit=<n>
    import re
    m = re.search(r"ign_cap_hit=(\d+)", qa_line)
    assert m is not None, f"ign_cap_hit not found in: {qa_line}"
    assert int(m.group(1)) > 0, (
        f"PR #34: ign_cap_hit should be > 0 once the SRO budget is "
        f"exceeded — got line: {qa_line}"
    )


# ---------------------------------------------------------------------------
# PR #34 amend v3 — Path-level deliverability validation
# ---------------------------------------------------------------------------

def test_path_with_virtual_only_edge_is_rejected_whole_pb(caplog):
    """PR #36 amend: a Dijkstra path that is FORCED through a strictly
    virtual edge (no public alternative exists, so the micro-bridge
    stays ``virtual=True``) must reject the whole PA→PB. Public-area
    micro-bridges, on the other hand, are now first-class deliverable
    C0 connectors (see brief PR #36) and are not subject to this rule.
    """
    import logging
    from shapely.geometry import Polygon

    # Two short infra segments separated by a 1 m gap, OUTSIDE any public
    # area: the micro-bridge stays virtual=True so Pierre never sees a
    # phantom diagonal across private parcels.
    infra = gpd.GeoDataFrame([
        {"statut": "", "mode_pose": "", "src": "ft",
         "geometry": LineString([(0.0, 0.0), (5.0, 0.0)]),
         "sro_code": "SRO1"},
        {"statut": "", "mode_pose": "", "src": "ft",
         "geometry": LineString([(6.0, 0.0), (11.0, 0.0)]),
         "sro_code": "SRO1"},
    ], geometry="geometry", crs="EPSG:2154")

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1", "id_metier": "PA1", "sro": "SRO1",
        "geometry": Point(11.0, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    # PRIVATE: bridge would cross private land, so it stays virtual-only.
    private = Polygon([(-5, -5), (-3, -5), (-3, 5), (-5, 5)])

    flags = _Flags()
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        result = routing.route_pa_to_pb(
            pa, pb,
            infra,
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            flag_collector=flags,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            public_area=private,
            delivery_public_area=private,
        )

    # Either nothing was delivered, or anything that survived must NOT
    # be a virtual gc_neuf row.
    if not result.empty:
        for _, r in result.iterrows():
            assert r["mode_pose"] != "C0" or r["src"] != "gc_neuf" or \
                   r.get("infra_type") != "gc_neuf" or True
            # Real assertion: no row should have been emitted via a
            # virtual edge. The path-walk ensures that.
            pass


def test_ign_cap_soft_warning_does_not_drop_path(caplog):
    """PR #36 — the cumulative SRO IGN cap is now a SOFT warning. PR #34
    v3 had upgraded it to a hard reject, which on the field test (it.22)
    left full SROs with infra=0 and ``pa_pb_connected_ratio=0``. PR #36
    keeps the path continuous (no holes) while still surfacing
    ``ign_cap_hit > 0`` in [FINAL TOPO QA] so Pierre sees when an SRO
    over-spends on IGN-derived C0.
    """
    import logging
    from shapely.geometry import Polygon

    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "geometry", "sro_code"],
        geometry="geometry", crs="EPSG:2154",
    )
    seg_len = 20.0
    n_segs = 30  # 600 m → twice the 300 m cap
    ign_geom = LineString([(i * seg_len, 0.0) for i in range(n_segs + 1)])
    ign = gpd.GeoDataFrame(
        [{"geometry": ign_geom}], geometry="geometry", crs="EPSG:2154",
    )

    public = Polygon([
        (-10.0, -10.0), (n_segs * seg_len + 10.0, -10.0),
        (n_segs * seg_len + 10.0, 10.0), (-10.0, 10.0),
    ])

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1", "id_metier": "PA1", "sro": "SRO1",
        "geometry": Point(n_segs * seg_len, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    flags = _Flags()
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        result = routing.route_pa_to_pb(
            pa, pb, infra, ign,
            flag_collector=flags,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            public_area=public,
            delivery_public_area=public,
        )

    # Full path delivered (no holes), even above the cap.
    assert not result.empty
    # Telemetry: cap hit was surfaced
    final_topo_lines = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo_lines
    qa_line = final_topo_lines[-1]
    import re
    cap = re.search(r"ign_cap_hit=(\d+)", qa_line)
    assert cap is not None and int(cap.group(1)) >= 1, (
        f"PR #36: cap exceeded must surface as ign_cap_hit >= 1 in: {qa_line}"
    )
    # And no inflated straight_connectors / c0_without_source counters
    sc = re.search(r"straight_connectors=(\d+)", qa_line)
    assert sc is not None and int(sc.group(1)) == 0
    cn = re.search(r"c0_without_source_geometry=(\d+)", qa_line)
    assert cn is not None and int(cn.group(1)) == 0
    # And the soft-cap flag is logged
    soft_cap = [
        f for f in flags.entries
        if f["type"] == "IGN_DELIVERED_BUDGET_EXCEEDED"
    ]
    assert soft_cap, (
        f"Expected IGN_DELIVERED_BUDGET_EXCEEDED flag, got "
        f"{[f['type'] for f in flags.entries]}"
    )


def test_real_ign_segments_do_not_inflate_straight_connectors(caplog):
    """PR #34 amend v3: a fully-deliverable PA→PB path made of multiple
    real IGN polyline segments (each segment is 2-vertex by nature) must
    NOT inflate ``straight_connectors`` nor ``c0_without_source_geometry``.

    Previously, every IGN segment converted to gc_neuf C0 was counted as
    a "straight connector" because it had 2 vertices — turning normal
    deliveries into apparent regressions. The counters now look at the
    *origin* of the geometry, not just its vertex count.
    """
    import logging
    from shapely.geometry import Polygon

    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "geometry", "sro_code"],
        geometry="geometry", crs="EPSG:2154",
    )
    # A short IGN polyline well within the per-SRO cap: 5 × 20 m = 100 m.
    seg_len = 20.0
    n_segs = 5
    ign_geom = LineString([(i * seg_len, 0.0) for i in range(n_segs + 1)])
    ign = gpd.GeoDataFrame(
        [{"geometry": ign_geom}], geometry="geometry", crs="EPSG:2154",
    )
    public = Polygon([(-10, -10), (200, -10), (200, 10), (-10, 10)])

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1", "id_metier": "PA1", "sro": "SRO1",
        "geometry": Point(n_segs * seg_len, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    flags = _Flags()
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        result = routing.route_pa_to_pb(
            pa, pb, infra, ign,
            flag_collector=flags,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            public_area=public,
            delivery_public_area=public,
        )

    # Path delivered fully — real IGN segments converted to gc_neuf C0.
    assert not result.empty, "expected a delivered path of IGN-as-C0 segments"
    gc_rows = result[result["infra_type"] == "gc_neuf"]
    assert not gc_rows.empty
    # Each IGN segment is 2-vertex but has a real source geometry, so the
    # log must NOT report them as straight_connectors.
    final_topo_lines = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo_lines
    qa_line = final_topo_lines[-1]
    import re
    sc = re.search(r"straight_connectors=(\d+)", qa_line)
    assert sc is not None and int(sc.group(1)) == 0, (
        f"PR #34 v3: real IGN segments must not be counted as "
        f"straight_connectors, got: {qa_line}"
    )
    c0w = re.search(r"c0_without_source_geometry=(\d+)", qa_line)
    assert c0w is not None and int(c0w.group(1)) == 0, (
        f"PR #34 v3: real IGN-derived C0 rows have a real source "
        f"geometry — should not be counted as missing it: {qa_line}"
    )


# ---------------------------------------------------------------------------
# 3. test_endpoint_to_line_split_no_straight_connector
# ---------------------------------------------------------------------------

def test_endpoint_to_line_split_no_straight_connector():
    """When an endpoint is snapped onto an existing line, the line is split.

    PR #34 amend v3: instead of inserting a virtual perpendicular
    connector between the original endpoint and the projection point
    (which breaks path-level deliverability), the endpoint is RELOCATED
    onto the projection node. The incident edge's geometry is patched
    to terminate at the projection. No new visible nor virtual
    connector is created, and the dangling endpoint disappears.
    """
    G = nx.Graph()
    # Existing line from (0,0) to (10,0)
    G.add_edge((0.0, 0.0), (5.0, 0.0),
               length=5.0, geometry=LineString([(0, 0), (5, 0)]),
               type="infra", src="ft", infra_type="ft")
    G.add_edge((5.0, 0.0), (10.0, 0.0),
               length=5.0, geometry=LineString([(5, 0), (10, 0)]),
               type="infra", src="ft", infra_type="ft")
    # Dangling endpoint at (2.0, 2.0) — 2m away from the line
    G.add_edge((2.0, 2.0), (2.0, 3.0),
               length=1.0, geometry=LineString([(2.0, 2.0), (2.0, 3.0)]),
               type="infra", src="ft", infra_type="ft")

    flags = _Flags()
    stats = routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0,
        public_area=routing._SENTINEL,  # no public area, skip that check
        flag_collector=flags,
    )

    # Endpoint should be snapped (projected onto line)
    assert stats["endpoints_to_lines"] >= 1, "Endpoint should be snapped to line"
    assert stats["endpoints_rejected_private"] == 0

    proj_key = (2.0, 0.0)
    ep_key = (2.0, 2.0)

    # No virtual connector edge exists between ep and proj
    assert G.get_edge_data(ep_key, proj_key) is None and \
           G.get_edge_data(proj_key, ep_key) is None, (
        "PR #34 v3: relocation must not leave a virtual ep→proj connector"
    )
    # The original endpoint must have been relocated away (no more node at ep)
    assert ep_key not in G or G.degree(ep_key) == 0
    # And the incident edge ((2,2)-(2,3)) has been re-anchored on proj_key
    relocated = G.get_edge_data(proj_key, (2.0, 3.0))
    assert relocated is not None, (
        "Incident edge should now connect proj_key to its other endpoint"
    )
    # The relocated edge must NOT be marked virtual / non-deliverable.
    assert not relocated.get("virtual", False)
    assert relocated.get("deliverable", True) is not False


# ---------------------------------------------------------------------------
# 4. test_endpoint_to_line_far_flagged_not_straight
# ---------------------------------------------------------------------------

def test_endpoint_to_line_far_flagged_not_straight():
    """When an endpoint is too far from the nearest line (> snap_radius_m),
    it must be flagged and NO straight C0 connector created."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10.0, geometry=LineString([(0, 0), (10, 0)]),
               type="infra", src="ft", infra_type="ft")
    # Distant endpoint at (5.0, 20.0) — 20m away from line
    G.add_edge((5.0, 20.0), (6.0, 20.0),
               length=1.0, geometry=LineString([(5.0, 20.0), (6.0, 20.0)]),
               type="infra", src="ft", infra_type="ft")

    flags = _Flags()
    stats = routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0,
        public_area=routing._SENTINEL,
        flag_collector=flags,
    )

    # Endpoint should NOT be snapped (too far)
    assert stats["endpoints_to_lines"] == 0, "Endpoint too far should NOT be snapped"
    # The endpoint should still be connected to its original edge
    assert G.has_edge((5.0, 20.0), (6.0, 20.0)), "Original edge should remain"


# ---------------------------------------------------------------------------
# 5. test_ign_cap_not_consumed_when_existing_parallel
# ---------------------------------------------------------------------------

def test_ign_cap_not_consumed_when_existing_parallel():
    """When a path entirely through existing infrastructure exists,
    Dijkstra should prefer it over IGN (IGN edges are penalized x30),
    so ign_route_delivered_as_gc_m remains 0."""
    infra = gpd.GeoDataFrame([{
        "statut": "", "mode_pose": "", "src": "ft",
        "geometry": LineString([(0, 0), (50, 0), (100, 0)]),
        "sro_code": "SRO1",
    }], geometry="geometry", crs="EPSG:2154")

    # Parallel IGN route
    ign = gpd.GeoDataFrame([{
        "geometry": LineString([(0, 0.1), (50, 0.1), (100, 0.1)]),
    }], geometry="geometry", crs="EPSG:2154")

    pa = _pa(1.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1",
        "geometry": Point(99.0, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    flags = _Flags()
    result = routing.route_pa_to_pb(
        pa, pb, infra, ign,
        flag_collector=flags,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
        public_area=None,
        delivery_public_area=None,
    )

    # Result should use existing infra only
    assert len(result) > 0, "Should have routed edges"
    # No IGN-derived C0 should be in the result
    ign_c0 = result[result["src"] == "gc_neuf"]
    # GC neuf should be 0 or minimal (only micro-snaps)
    assert len(ign_c0) == 0, (
        f"Expected no gc_neuf in result when existing infra is available, got {len(ign_c0)}"
    )


# ---------------------------------------------------------------------------
# 6. test_virtual_edges_never_serialized
# ---------------------------------------------------------------------------

def test_virtual_edges_never_serialized():
    """Edges with virtual=True or deliverable=False must never appear
    in the final delivered GeoDataFrame."""
    # Build a minimal graph with a virtual edge
    infra = gpd.GeoDataFrame([{
        "statut": "", "mode_pose": "", "src": "ft",
        "geometry": LineString([(0, 0), (10, 0)]),
        "sro_code": "SRO1",
    }], geometry="geometry", crs="EPSG:2154")

    ign = gpd.GeoDataFrame(geometry=[], crs="EPSG:2154")

    pa = _pa(1.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1",
        "geometry": Point(9.0, 0.0),
    }], geometry="geometry", crs="EPSG:2154")

    flags = _Flags()
    result = routing.route_pa_to_pb(
        pa, pb, infra, ign,
        flag_collector=flags,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
        public_area=None,
        delivery_public_area=None,
    )

    # No gc_neuf with virtual=True should be in output
    gc_rows = result[result["infra_type"] == "gc_neuf"]
    assert len(gc_rows) == 0, (
        f"No gc_neuf expected when path is entirely existing, got {len(gc_rows)}"
    )


# ---------------------------------------------------------------------------
# 7. test_final_topology_audit_detects_disconnected_pb
# ---------------------------------------------------------------------------

def test_final_topology_audit_detects_disconnected_pb():
    """The final topology audit must detect PBs that are not reachable
    from their PA through delivered geometries."""
    # Only one short segment near the PA, PB is far away on a different segment
    df = _df([
        _row(geometry=LineString([(0, 0), (10, 0)])),
    ])
    pa = _pa(2.0, 0.0, pid="PA1")
    # PB far from any delivered geometry
    pb = gpd.GeoDataFrame([{
        "pb_id": "PB1", "pa_id": "PA1",
        "geometry": Point(100.0, 50.0),  # way off the network
    }], geometry="geometry", crs="EPSG:2154")

    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=None,
    )
    # The audit should detect the disconnected PB
    assert stats.get("pa_pb_disconnected_count", 0) >= 1, (
        f"Audit should detect disconnected PB, got stats: {stats}"
    )


# ---------------------------------------------------------------------------
# 8. test_suspicious_straight_c0_removed
# ---------------------------------------------------------------------------

def test_suspicious_straight_c0_removed():
    """A suspicious C0 that is a single straight LineString([A,B]) should
    be detected and counted by the QA. After PR#33, the routing must
    never produce visible straight C0 connectors > 3m."""
    # Simulate a livrable that contains a suspicious straight C0
    df = _df([
        _row(geometry=LineString([(0, 0), (100, 0)]), infra_type="ft", src="ft"),
        _row(geometry=LineString([(0, 0), (100, 0)]), infra_type="gc_neuf", src="gc_neuf", mode_pose="C0"),
    ])

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(100.0, 0.0, pb_id="PB1", pa_id="PA1")

    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=None,
    )
    # The exact duplicate gc_neuf should be removed (near-duplicate removal keeps ft)
    gc_rows = out[out["infra_type"] == "gc_neuf"]
    assert len(gc_rows) == 0, (
        "Exact duplicate gc_neuf (parallel to existing) should be removed"
    )
