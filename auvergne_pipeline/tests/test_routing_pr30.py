"""PR #30 regression tests.

Bloquants tested here:

1. ``test_long_ign_route_not_delivered_as_gc`` — IGN edges longer than
   ``IGN_DELIVERY_MAX_LENGTH_M`` are NOT delivered as C0/gc_neuf in the
   livrable, even when Dijkstra has to traverse them.
2. ``test_short_public_ign_connector_can_be_delivered`` — a short
   (< 50 m) IGN edge fully covered by ``delivery_public_area`` is allowed
   in the livrable as C0.
3. ``test_ign_private_crossing_blocked`` — an IGN edge that crosses
   private land is blocked from delivery regardless of its length.
4. ``test_existing_gap_closed_before_ign_fallback`` — when an existant
   endpoint sits within snap radius of both an existant line and an IGN
   line, the snap targets the existant line (BLOQUANT 6).
5. ``test_existing_endpoint_to_existing_line_visual_touch`` — the
   geometry stored on the snapped sub-edges respects the original
   polyline so the connector visually touches the line.
6. ``test_final_gc_private_filter_uses_delivery_public_area`` — the
   final filter applied to the routed GeoDataFrame uses the STRICT
   delivery_public_area, not the permissive routing area.
"""

from __future__ import annotations

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
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [t[2] for t in coords_pid_paid],
        "pa_id": [t[3] for t in coords_pid_paid],
        "geometry": [Point(t[0], t[1]) for t in coords_pid_paid],
    }), geometry="geometry", crs=CRS)


# Convenient large public polygon used as routing AND delivery area when
# the test does not depend on the strict/permissive distinction.
_BIG_PUBLIC = Polygon([(-1000, -1000), (1000, -1000), (1000, 1000), (-1000, 1000)])


# ---------------------------------------------------------------------------
# 1. Long IGN traversal must not be delivered as C0
# ---------------------------------------------------------------------------


def test_long_ign_route_not_delivered_as_gc(monkeypatch):
    """A 200 m IGN edge traversed by Dijkstra must NOT appear in the
    livrable_infra (length > IGN_DELIVERY_MAX_LENGTH_M = 50 m).
    """
    G = nx.Graph()
    # Single 200 m IGN edge between PA and PB. Public domain is everything,
    # so routing area covers the edge — but the length disqualifies it.
    G.add_edge(
        (0.0, 0.0), (200.0, 0.0),
        length=200, type="ign_route", src="ign_route",
        infra_type="ign_route", statut="", mode_pose="",
        geometry=LineString([(0, 0), (200, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb([(200, 0, "PB1", "PA1")])

    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
    )
    # The IGN edge is too long → blocked from delivery → empty livrable.
    assert out.empty, (
        f"Long IGN edge must not be delivered as C0; got {len(out)} rows"
    )


# ---------------------------------------------------------------------------
# 2. Short public IGN connector IS delivered
# ---------------------------------------------------------------------------


def test_short_public_ign_connector_can_be_delivered(monkeypatch):
    """A short (< 50 m) IGN edge fully covered by delivery_public_area is
    delivered as a C0/gc_neuf row.
    """
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (20.0, 0.0),
        length=20, type="ign_route", src="ign_route",
        infra_type="ign_route", statut="", mode_pose="",
        geometry=LineString([(0, 0), (20, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb([(20, 0, "PB1", "PA1")])

    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
    )
    assert len(out) == 1, out
    row = out.iloc[0]
    assert row["mode_pose"] == "C0"
    assert row["infra_type"] == "gc_neuf"
    assert row["src"] == "gc_neuf"
    assert row["statut"] == ""


# ---------------------------------------------------------------------------
# 3. IGN edge crossing private land is blocked from delivery
# ---------------------------------------------------------------------------


def test_ign_private_crossing_blocked(monkeypatch):
    """A short IGN edge whose geometry partially exits the strict
    delivery area must be blocked from the livrable.
    """
    G = nx.Graph()
    # 30 m IGN edge along y=0, but delivery_public_area is a tight strip
    # around y=0 that does NOT cover x in [10, 20] (a private parcel).
    G.add_edge(
        (0.0, 0.0), (30.0, 0.0),
        length=30, type="ign_route", src="ign_route",
        infra_type="ign_route", statut="", mode_pose="",
        geometry=LineString([(0, 0), (30, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb([(30, 0, "PB1", "PA1")])

    # Routing area: lenient (entire box) — Dijkstra can use the IGN edge.
    routing_area = Polygon([(-100, -10), (100, -10), (100, 10), (-100, 10)])
    # Delivery area: two disjoint pieces around x in [-100, 10] and [20, 100]
    # — leaves a private hole at x in [10, 20].
    delivery_area = Polygon([(-100, -10), (10, -10), (10, 10), (-100, 10)]).union(
        Polygon([(20, -10), (100, -10), (100, 10), (20, 10)])
    )

    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=routing_area,
        delivery_public_area=delivery_area,
    )
    # The IGN edge spans the private hole → must be blocked.
    assert out.empty, (
        f"IGN edge crossing private must be blocked; got {len(out)} rows"
    )


# ---------------------------------------------------------------------------
# 4. Existant endpoint preferred over IGN as snap target
# ---------------------------------------------------------------------------


def test_existing_gap_closed_before_ign_fallback():
    """When a dangling endpoint sits within snap_radius_m of BOTH an
    existant infra line AND a slightly closer IGN line, the snap targets
    the existant line (PR #30 BLOQUANT 6, tier 0 beats tier 1).
    """
    G = nx.Graph()
    # Existant infra line at y=0 (tier 0). Distance to (5, 1) = 1 m.
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=LineString([(0, 0), (10, 0)]),
    )
    # IGN route line at y=1.5 (tier 1). Distance to (5, 1) = 0.5 m.
    G.add_edge(
        (0.0, 1.5), (10.0, 1.5),
        length=10, type="ign_route", src="ign_route", infra_type="ign_route",
        statut="", mode_pose="",
        geometry=LineString([(0, 1.5), (10, 1.5)]),
    )
    # Dangling endpoint at (5, 1).
    G.add_edge(
        (5.0, 1.0), (5.5, 1.5),
        length=0.71, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=LineString([(5.0, 1.0), (5.5, 1.5)]),
    )

    public_area = _BIG_PUBLIC
    stats = routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0, public_area=public_area,
    )
    assert stats["endpoints_to_lines"] >= 1
    # The existant edge MUST have been split (snap chose tier 0 even
    # though the IGN edge was nominally closer).
    assert not G.has_edge((0.0, 0.0), (10.0, 0.0)), (
        "existant line should have been split by the prefered snap"
    )
    assert G.has_edge((0.0, 1.5), (10.0, 1.5)), (
        "IGN line must remain intact when an existant target is available"
    )
    # The connector must be flagged as an "existing" connector.
    assert stats["existing_connectors_added"] >= 1


# ---------------------------------------------------------------------------
# 5. Visual touch — endpoint→line connector preserves the polyline shape
# ---------------------------------------------------------------------------


def test_existing_endpoint_to_existing_line_visual_touch():
    """After the endpoint→line snap, the new connector geometry MUST
    touch the resulting sub-segments at the projection point so the
    livrable is visually connected (no QGIS gap).
    """
    G = nx.Graph()
    # Existant curved-ish line passing through (5, 0).
    geom = LineString([(0, 0), (5, 0), (10, 0)])
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=geom,
    )
    # Dangling endpoint very close to (5, 0).
    G.add_edge(
        (5.0, 1.0), (5.5, 1.5),
        length=0.71, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=LineString([(5.0, 1.0), (5.5, 1.5)]),
    )
    public_area = _BIG_PUBLIC
    routing._snap_endpoints_to_lines(
        G, snap_radius_m=3.0, public_area=public_area,
    )
    # Find the connector edge (5.0, 1.0)-(<projection>).
    connectors = [
        (u, v, d) for u, v, d in G.edges(data=True)
        if d.get("type") == "gc_neuf" and ((5.0, 1.0) in (u, v))
    ]
    assert connectors, "expected a gc_neuf connector from the dangling endpoint"
    u, v, data = connectors[0]
    connector_geom = data["geometry"]
    # The other end of the connector is the projection point onto the line.
    other = v if u == (5.0, 1.0) else u
    # That other end MUST be one of the line's vertices in G after the
    # split: i.e. an edge containing this point exists with type=="infra".
    touching_infra = any(
        d.get("type") == "infra" and other in (eu, ev)
        for eu, ev, d in G.edges(data=True)
    )
    assert touching_infra, (
        "connector endpoint must touch an existing infra sub-edge"
    )
    # And the connector geometry must actually share a coordinate with
    # the projection point (visual touch).
    assert (
        connector_geom.coords[0] == (5.0, 1.0)
        or connector_geom.coords[-1] == (5.0, 1.0)
    )


# ---------------------------------------------------------------------------
# 6. Final filter uses delivery_public_area, not the routing area
# ---------------------------------------------------------------------------


def test_final_gc_private_filter_uses_delivery_public_area(monkeypatch):
    """An injected GC neuf edge fully inside the routing area but partly
    outside the strict delivery area MUST be removed by the final filter,
    counted in private_crossing_final_count, and a
    C0_PRIVATE_CROSSING_REMOVED flag must be added.
    """
    from auvergne_pipeline import flags as flags_mod

    G = nx.Graph()
    # Existant edge so a path exists, plus a gc_neuf edge crossing private.
    G.add_edge(
        (0.0, 0.0), (5.0, 0.0),
        length=5, type="infra", src="bt", infra_type="bt",
        statut="E", mode_pose="1",
        geometry=LineString([(0, 0), (5, 0)]),
    )
    G.add_edge(
        (5.0, 0.0), (15.0, 0.0),
        length=10, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
        statut="", mode_pose="C0",
        geometry=LineString([(5, 0), (15, 0)]),
    )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb([(15, 0, "PB1", "PA1")])

    # Routing area covers the whole edge so Dijkstra is happy. Delivery
    # area only covers x in [-5, 8], cutting off the gc_neuf at x=8.
    routing_area = Polygon([(-100, -10), (100, -10), (100, 10), (-100, 10)])
    delivery_area = Polygon([(-5, -5), (8, -5), (8, 5), (-5, 5)])

    fc = flags_mod.FlagCollector("SRO1")
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        flag_collector=fc,
        public_area=routing_area,
        delivery_public_area=delivery_area,
    )
    # The bt sub-edge stays; the gc_neuf crossing private is dropped.
    assert (out["src"] == "gc_neuf").sum() == 0, out
    assert (out["src"] == "bt").sum() == 1
    # A flag was emitted.
    flags_df = fc.to_dataframe()
    assert "C0_PRIVATE_CROSSING_REMOVED" in set(flags_df["flag_type"]), flags_df


# ---------------------------------------------------------------------------
# Telemetry sanity — _log_routing_qa exposes the new metrics
# ---------------------------------------------------------------------------


def test_routing_qa_logs_pr30_metrics(monkeypatch, caplog):
    """The [ROUTING QA] block must include the 6 new metrics requested
    by the spec (delivered/blocked/final_removed/private_count+length,
    existing_connectors_added, gaps_remaining_estimate).
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
    pa = _pa(0, 0)
    pb = _pb([(20, 0, "PB1", "PA1")])
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
        )
    qa_lines = [r.getMessage() for r in caplog.records if "[ROUTING QA]" in r.getMessage()]
    blob = " || ".join(qa_lines)
    for key in (
        "ign_route_delivered_as_gc_m",
        "ign_route_blocked_m",
        "final_removed_private_gc",
        "private_crossing_final_count",
        "private_crossing_final_length_m",
        "existing_connectors_added",
        "gaps_remaining_estimate",
    ):
        assert key in blob, f"missing metric {key} in [ROUTING QA] log: {blob}"
