"""Tests for PR #26 — livrable_infra geometry + attribute compliance (amend)."""

from __future__ import annotations

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from auvergne_pipeline import config, routing


# ---------------------------------------------------------------------------
# 1. Bridge must not create long diagonal (amend: default threshold 50m)
# ---------------------------------------------------------------------------


def test_bridge_rejects_long_diagonal():
    """PR #26 amend: direct PA→PB > 50 m must be flagged, NOT bridged."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((100.0, 0.0))  # 100 m away, > 50 m default

    # Default threshold is 50 m — 100 m should be rejected
    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (100.0, 0.0),
    )
    assert not bridged
    assert not G.has_edge((0.0, 0.0), (100.0, 0.0))


def test_bridge_allows_short_gap():
    """PR #33: a micro gap (<= 3 m) is bridged as a VIRTUAL edge."""
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (10.0, 0.0), length=10, type="infra")
    G.add_node((2.0, 0.0))  # only 2 m away, <= 3 m PR33 threshold
    assert nx.number_connected_components(G) == 2

    bridged = routing._bridge_components_with_gc_neuf(
        G, (0.0, 0.0), (2.0, 0.0),
    )
    assert bridged
    # PR #33: must be virtual — not delivered to livrable
    edge = G[(0.0, 0.0)][(2.0, 0.0)]
    assert edge.get("virtual") is True
    assert edge.get("deliverable") is False
    assert edge["src"] == "gc_neuf"
    assert edge["infra_type"] == "gc_neuf"
    assert "geometry" in edge
    assert isinstance(edge["geometry"], LineString)


# ---------------------------------------------------------------------------
# 2. Output uses stored geometry
# ---------------------------------------------------------------------------

def test_output_uses_stored_geometry():
    """PR #26: output edges must use edge_data['geometry'] from the graph."""
    G = nx.Graph()
    geom = LineString([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)])
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", statut="E", mode_pose="1",
        geometry=geom,
    )

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

    assert len(out) == 1
    out_geom = out.geometry.iloc[0]
    assert len(list(out_geom.coords)) == 3


# ---------------------------------------------------------------------------
# 3. IGN edges delivered as infra become C0
# ---------------------------------------------------------------------------


def test_ign_route_becomes_c0_in_output():
    """PR #26: a pure IGN edge in the livrable must become mode_pose='C0'."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="ign_route", statut="", mode_pose="",
        src="ign_route", infra_type="ign_route",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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

    assert len(out) == 1
    row = out.iloc[0]
    assert row["mode_pose"] == "C0"
    assert row["infra_type"] == "gc_neuf"
    assert row["src"] == "gc_neuf"
    assert row["statut"] == ""  # PR #26 amend: C0 must have statut=""


# ---------------------------------------------------------------------------
# 4. No gc_neuf_runtime in output
# ---------------------------------------------------------------------------


def test_no_gc_neuf_runtime_in_output():
    """PR #26: the output GPKG must never contain src='gc_neuf_runtime'."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="gc_neuf", statut=None, mode_pose="C0",
        src="gc_neuf_runtime", infra_type="gc_neuf",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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

    assert len(out) == 1
    assert (out["src"] == "gc_neuf_runtime").sum() == 0
    assert out.iloc[0]["src"] == "gc_neuf"
    # PR #26 amend: statut=None from input must become "" in output
    assert out.iloc[0]["statut"] == ""


# ---------------------------------------------------------------------------
# 5. No infra_type="ign_route" in output
# ---------------------------------------------------------------------------


def test_no_ign_route_nu_in_output():
    """PR #26: the output must never contain infra_type='ign_route' without C0."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="ign_route", src="ign_route",
        infra_type="ign_route", statut="", mode_pose="",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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

    assert len(out) == 1
    assert (out["infra_type"] == "ign_route").sum() == 0
    assert out.iloc[0]["infra_type"] == "gc_neuf"


# ---------------------------------------------------------------------------
# 6. Output columns exist and are QML-compatible
# ---------------------------------------------------------------------------


def test_output_columns_present():
    """PR #26: output must have the QML-expected columns."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", statut="E", mode_pose="1",
        src="bt", infra_type="bt",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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

    assert len(out) == 1
    for col in ("sro", "pa_id", "pb_id", "statut", "mode_pose", "infra_type", "src", "length_m"):
        assert col in out.columns, f"Missing column: {col}"
    assert out.iloc[0]["statut"] == "E"
    assert out.iloc[0]["mode_pose"] == "1"
    assert out.iloc[0]["infra_type"] == "bt"


# ---------------------------------------------------------------------------
# 7. PR #26 amend: statut never None, C0 compliance, style_key non-empty
# ---------------------------------------------------------------------------


def test_output_statut_never_none():
    """PR #26 amend: no feature in the output may have statut=None (NULL)."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="gc_neuf", statut=None, mode_pose="C0",
        src="gc_neuf", infra_type="gc_neuf",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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
    assert out["statut"].isna().sum() == 0, "No NULL statut allowed in output"


def test_c0_output_has_correct_attributes():
    """PR #26 amend: C0 features must have statut='' and mode_pose='C0'."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="gc_neuf", statut="", mode_pose="C0",
        src="gc_neuf", infra_type="gc_neuf",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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

    assert len(out) == 1
    c0_rows = out[out["mode_pose"] == "C0"]
    assert len(c0_rows) == 1
    assert c0_rows.iloc[0]["statut"] == ""
    assert c0_rows.iloc[0]["mode_pose"] == "C0"
    assert c0_rows.iloc[0]["infra_type"] == "gc_neuf"
    assert c0_rows.iloc[0]["src"] == "gc_neuf"


def test_style_key_non_empty():
    """PR #26 amend: every output feature must have a non-empty style_key."""
    G = nx.Graph()
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", statut="E", mode_pose="1",
        src="bt", infra_type="bt",
        geometry=LineString([(0, 0), (10, 0)]),
    )

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
    # style_key = coalesce(statut, "") + coalesce(mode_pose, "")
    out_copy = out.copy()
    out_copy["style_key"] = (
        out_copy["statut"].fillna("").astype(str)
        + out_copy["mode_pose"].fillna("").astype(str)
    )
    assert (out_copy["style_key"] == "").sum() == 0, "All features must have non-empty style_key"


def test_edge_split_with_geometry_does_not_crash():
    """PR #26 amend: splitting an edge that already has 'geometry' must not
    cause TypeError (multiple values for keyword argument 'geometry')."""
    G = nx.Graph()
    geom = LineString([(0.0, 0.0), (10.0, 0.0)])
    G.add_edge(
        (0.0, 0.0), (10.0, 0.0),
        length=10, type="infra", statut="E", mode_pose="1",
        src="bt", infra_type="bt",
        geometry=geom,  # <-- edge already has geometry
    )

    # Force a PB projection onto the middle of this edge
    # PB at (5, 50) — far enough to trigger edge projection, not node snap
    pa = gpd.GeoDataFrame(
        pd.DataFrame({"id_metier": ["PA1"], "sro": ["TEST"], "geometry": [Point(0, 0)]}),
        geometry="geometry", crs="EPSG:2154",
    )
    pb = gpd.GeoDataFrame(
        pd.DataFrame({"pb_id": ["PB1"], "pa_id": ["PA1"], "geometry": [Point(5, 50)]}),
        geometry="geometry", crs="EPSG:2154",
    )

    import auvergne_pipeline.routing as routing_mod
    _orig = routing_mod._build_graph
    routing_mod._build_graph = lambda infra, ign, **kw: G
    try:
        # This should NOT raise TypeError
        out = routing_mod.route_pa_to_pb(
            pa, pb,
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
            gpd.GeoDataFrame(geometry=[], crs="EPSG:2154"),
        )
    finally:
        routing_mod._build_graph = _orig

    # PB at (5, 50) projects onto edge at (5, 0) — edge is split
    # Should produce at least one edge in output
    assert len(out) >= 1
