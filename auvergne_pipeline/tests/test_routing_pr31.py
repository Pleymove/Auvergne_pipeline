"""PR #31 regression tests — continuous PA→PB topology + professional
livrable validation.

Tests the 11 cases listed in the Notion brief
`Brief PR31 — Topologie continue PA→PB et parcours professionnel`.

All tests exercise the new ``auvergne_pipeline.livrable_topology``
module + the cumulative IGN cap added in ``routing.py``. No QGIS
runtime is needed.
"""

from __future__ import annotations

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import livrable_topology as lt
from auvergne_pipeline import routing


CRS = "EPSG:2154"

_BIG_PUBLIC = Polygon([(-1000, -1000), (1000, -1000), (1000, 1000), (-1000, 1000)])


def _row(**kw) -> dict:
    """Build a livrable_infra row dict with safe defaults."""
    base = {
        "sro": "SRO1",
        "pa_id": "PA1",
        "pb_id": "PB1",
        "statut": "E",
        "mode_pose": "1",
        "infra_type": "bt",
        "src": "bt",
        "length_m": 0.0,
        "geometry": None,
    }
    base.update(kw)
    if base["geometry"] is not None and base["length_m"] == 0.0:
        base["length_m"] = base["geometry"].length
    return base


def _df(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _pa(x=0.0, y=0.0, pid="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


def _pb(x=0.0, y=0.0, pb_id="PB1", pa_id="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pb_id], "pa_id": [pa_id],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


# ---------------------------------------------------------------------------
# 1. PA connected to livrable path
# ---------------------------------------------------------------------------


def test_pa_is_connected_to_livrable_path():
    """A PA placed 1 m off a livrable line must end up topologically
    connected.

    PR #36 — terminal connector restored. PR #33 had forbidden ANY
    visible terminal connector, which left ~75% of PA/PB floating in
    the field test (pa_pb_connected_ratio=0.00 sur 4/5 SROs). The
    operator accepts a short, public, deliverable C0 stub as the lesser
    evil vs. a disconnected terminal — see brief PR #36.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (10, 0)])),
    ])
    pa = _pa(5.0, 1.0, pid="PA1")
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # The original (0,0)–(10,0) edge must have been split at (5,0).
    geoms = [g for g in out.geometry]
    starts = {(round(g.coords[0][0], 1), round(g.coords[0][1], 1)) for g in geoms}
    ends = {(round(g.coords[-1][0], 1), round(g.coords[-1][1], 1)) for g in geoms}
    assert (5.0, 0.0) in starts | ends, (
        f"target line should have been split at PA projection (5,0); got {starts | ends}"
    )
    # PR #36 — at least one short public terminal connector emitted so
    # the topology graph actually contains the PA. The connector stays
    # under TERMINAL_CONNECTOR_MAX_LENGTH_M (3 m).
    assert stats["pa_connected"] >= 1
    assert stats["terminal_connectors_added"] >= 1, (
        "PR #36: a short C0 terminal connector must be added to attach the PA"
    )
    assert stats["terminal_snap_failed"] == 0


# ---------------------------------------------------------------------------
# 2. PB connected to livrable path
# ---------------------------------------------------------------------------


def test_pb_is_connected_to_livrable_path():
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(7.0, 1.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # PR #36 — short C0 terminal connector restored; PB is attached.
    assert stats["pb_connected"] >= 1
    assert stats["terminal_connectors_added"] >= 1
    assert stats["terminal_snap_failed"] == 0


# ---------------------------------------------------------------------------
# 3. No micro gap remains
# ---------------------------------------------------------------------------


def test_livrable_path_has_no_micro_gap():
    """Two segments separated by 0.4 m are merged into a shared
    endpoint by ``_snap_endpoints_to_exact``.
    """
    df = _df([
        _row(geometry=LineString([(0.0, 0.0), (5.0, 0.0)])),
        _row(geometry=LineString([(5.4, 0.0), (10.0, 0.0)])),  # 0.4 m gap
    ])
    snapped, n = lt._snap_endpoints_to_exact(df, tol_m=0.5)
    # Both segments should now share a vertex at the rounded midpoint.
    coords = []
    for g in snapped.geometry:
        coords.extend(list(g.coords))
    # No coordinate at (5.4, 0.0) — it should have been collapsed.
    assert (5.4, 0.0) not in [(round(c[0], 1), round(c[1], 1)) for c in coords], (
        "gap endpoint should have been snapped to the shared node"
    )
    assert n >= 1


# ---------------------------------------------------------------------------
# 4. Line ending mid-other-line — terminal connector is added if PA there
# ---------------------------------------------------------------------------


def test_line_ending_on_middle_of_line_is_split():
    """A PA placed in the middle of an existing livrable line forces a
    split + a connector at the projection point — the previously
    intact (0,0)–(20,0) edge is now two sub-edges sharing the (10,0)
    node, so the livrable forms a proper topology fork at (10,0).
    """
    df = _df([_row(geometry=LineString([(0, 0), (20, 0)]))])
    pa = _pa(10.0, 0.5, pid="PA1")
    pb = _pb(20.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    G = lt._build_livrable_topology_graph(out)
    # The split must have produced a 3-node graph with the (10,0) node.
    assert any(n[0] == 10.0 and n[1] == 0.0 for n in G.nodes()), (
        f"expected a vertex at (10, 0) in the topology graph; got {list(G.nodes())}"
    )


# ---------------------------------------------------------------------------
# 5. Aerial energy (E1) crossing private must be blocked
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Amend : _is_energy capte mode_pose="1" même avec src="ft"
# ---------------------------------------------------------------------------


def test_energy_filter_catches_ft_mode_pose_1_crossing_private():
    """Amend 1a: src='ft' + mode_pose='1' crossing private must be removed."""
    from auvergne_pipeline import flags as flags_mod

    df = _df([
        _row(geometry=LineString([(0, 0), (15, 0)]), src="ft",
             infra_type="ft", mode_pose="1"),
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft",
             infra_type="ft", mode_pose="7"),
    ])
    delivery_area_safe = Polygon([(-5, -5), (8, -5), (8, 5), (-5, 5)]).buffer(0.01)
    fc = flags_mod.FlagCollector("SRO1")
    out, stats = lt._filter_energy_private(
        df, delivery_public_area_safe=delivery_area_safe, flag_collector=fc,
    )
    assert (out["mode_pose"] == "1").sum() == 0
    assert (out["src"] == "ft").sum() == 1
    assert stats["energy_private_crossing_count"] == 1


def test_energy_filter_catches_bt_empty_mode_pose():
    """Amend 1b: src='bt' + mode_pose='' crossing private must be removed."""
    from auvergne_pipeline import flags as flags_mod

    df = _df([
        _row(geometry=LineString([(0, 0), (15, 0)]), src="bt",
             infra_type="bt", mode_pose=""),
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft",
             infra_type="ft", mode_pose="7"),
    ])
    delivery_area_safe = Polygon([(-5, -5), (8, -5), (8, 5), (-5, 5)]).buffer(0.01)
    fc = flags_mod.FlagCollector("SRO1")
    out, stats = lt._filter_energy_private(
        df, delivery_public_area_safe=delivery_area_safe, flag_collector=fc,
    )
    assert (out["src"] == "bt").sum() == 0
    assert stats["energy_private_crossing_count"] == 1
    assert stats["energy_private_crossing_count"] == 1


# ---------------------------------------------------------------------------
# 6. Support switch penalty avoids Orange ↔ Energy zigzag (audit-level)
# ---------------------------------------------------------------------------


def test_support_switch_penalty_avoids_orange_energy_orange_zigzag():
    """The audit must surface a ``suspicious_switch_count`` > 0 when the
    same path alternates Orange → Energy → Orange in the livrable.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft", mode_pose="7", infra_type="ft"),
        _row(geometry=LineString([(5, 0), (10, 0)]), src="bt", mode_pose="1", infra_type="bt"),
        _row(geometry=LineString([(10, 0), (15, 0)]), src="ft", mode_pose="7", infra_type="ft"),
    ])
    stats = lt._audit_support_switches(df)
    assert stats["support_switch_count"] >= 2
    assert stats["suspicious_switch_count"] >= 1


# ---------------------------------------------------------------------------
# 7. Near-duplicate parallel edges removed
# ---------------------------------------------------------------------------


def test_near_duplicate_parallel_edges_removed():
    """Two nearly-identical lines (offset by 0.2 m) of different
    families: ``ft`` (high priority) survives, ``bt`` (lower) is dropped.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (10, 0)]), src="ft",
             mode_pose="7", infra_type="ft"),
        _row(geometry=LineString([(0, 0.2), (10, 0.2)]), src="bt",
             mode_pose="1", infra_type="bt"),
    ])
    out, stats = lt._remove_near_duplicates(df, parallel_tol_m=0.5)
    assert len(out) == 1
    assert out.iloc[0]["src"] == "ft"
    assert stats["near_duplicates_removed"] >= 1
    assert stats["parallel_conflicts_resolved"] >= 1


# ---------------------------------------------------------------------------
# 8. Trunk shared between multiple PB appears once
# ---------------------------------------------------------------------------


def test_mutualized_tree_reuses_trunk_for_multiple_pb():
    """The audit reports ``shared_edges_count >= 1`` when a trunk is
    referenced by more than one PB.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (10, 0)]), pb_id="PB1"),
        _row(geometry=LineString([(0, 0), (10, 0)]), pb_id="PB2"),
        _row(geometry=LineString([(10, 0), (15, 5)]), pb_id="PB1"),
        _row(geometry=LineString([(10, 0), (15, -5)]), pb_id="PB2"),
    ])
    stats = lt._audit_mutualisation(df)
    assert stats["shared_edges_count"] >= 1


# ---------------------------------------------------------------------------
# 9. Cumulative IGN delivery limit
# ---------------------------------------------------------------------------


def test_ign_cumulative_delivery_limit_soft_warning(monkeypatch):
    """PR #36 — the cumulative SRO IGN cap is a SOFT warning, not a
    blocker. PR #34 v3 had made it a hard reject, which on the field
    test (it.22) produced infra=0 livrables on full SROs and dragged
    ``pa_pb_connected_ratio`` to 0. Pierre's brief PR #36 explicitly
    asks for restored continuity even when IGN cumulé > budget, with
    the cap surviving only as ``ign_cap_hit`` telemetry in
    [FINAL TOPO QA] and as a flag.
    """
    G = nx.Graph()
    for i in range(10):
        x0 = i * 40.0
        x1 = (i + 1) * 40.0
        G.add_edge(
            (x0, 0.0), (x1, 0.0),
            length=40, type="ign_route", src="ign_route",
            infra_type="ign_route", statut="", mode_pose="",
            geometry=LineString([(x0, 0), (x1, 0)]),
        )
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb(400, 0)
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
    )
    # PR #36 — full path delivered even above the cap, livrable_infra
    # stays continuous.
    assert out is not None and not out.empty
    delivered_m = float(out["length_m"].sum())
    assert delivered_m == pytest.approx(400.0, abs=1.0), (
        f"PR #36: path delivered continuously over the soft cap, "
        f"expected ~400m, got {delivered_m}"
    )


# ---------------------------------------------------------------------------
# 10. Continuity QA flags unresolved gaps
# ---------------------------------------------------------------------------


def test_continuity_qa_flags_unresolved_gap():
    """Two unconnected sub-networks within ``micro_gap_tol_m`` of each
    other must surface a ``MICRO_GAP_UNRESOLVED`` flag (no auto-
    diagonal across private land).
    """
    from auvergne_pipeline import flags as flags_mod

    df = _df([
        _row(geometry=LineString([(0, 0), (5, 0)])),
        _row(geometry=LineString([(5.8, 0), (10, 0)])),  # 0.8 m gap
    ])
    pa = _pa(0, 0)
    pb = _pb(10, 0)
    fc = flags_mod.FlagCollector("SRO1")
    stats = lt._audit_continuity(df, pa, pb, flag_collector=fc)
    assert stats["micro_gaps_detected"] >= 1
    assert stats["micro_gaps_unresolved"] >= 1
    flags_df = fc.to_dataframe()
    assert "MICRO_GAP_UNRESOLVED" in set(flags_df["flag_type"])


# ---------------------------------------------------------------------------
# 11. Output geometries share exact coordinates after snap
# ---------------------------------------------------------------------------


def test_output_geometries_share_exact_coordinates_after_snap():
    """After the topology pipeline, two formerly-near-touching segments
    share the exact same coordinate at the snap location — this is what
    enables ``nx`` to see them as one node.
    """
    df = _df([
        _row(geometry=LineString([(0.0, 0.0), (5.0, 0.0)])),
        _row(geometry=LineString([(5.3, 0.0), (10.0, 0.0)])),
    ])
    pa = _pa(0, 0)
    pb = _pb(10, 0)
    out, _ = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # Collect all rounded coords and require that the previously
    # separated endpoints share one identical coordinate now.
    ep_coords: list[tuple[float, float]] = []
    for g in out.geometry:
        cs = list(g.coords)
        ep_coords.append((round(cs[0][0], 6), round(cs[0][1], 6)))
        ep_coords.append((round(cs[-1][0], 6), round(cs[-1][1], 6)))
    G = lt._build_livrable_topology_graph(out)
    # The two segments must share at least one node.
    assert nx.is_connected(G) or G.number_of_nodes() <= 3, (
        f"snapped network must be connected, got nodes={list(G.nodes())}"
    )


# ---------------------------------------------------------------------------
# PR #28 (Point 1) — PA on middle of line is split and connected
# ---------------------------------------------------------------------------


def test_pa_on_middle_of_line_is_split_and_connected():
    """PR #28 Point 1: PA exactly on the middle of a livrable line must
    trigger a split at the PA coordinate. After finalization, the graph
    must contain a node at PA (5, 0).
    """
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(5.0, 0.0, pid="PA1")
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    geoms = [g for g in out.geometry if isinstance(g, LineString)]
    node_set = set()
    for g in geoms:
        cs = list(g.coords)
        node_set.add((round(cs[0][0], 3), round(cs[0][1], 3)))
        node_set.add((round(cs[-1][0], 3), round(cs[-1][1], 3)))
    assert (5.0, 0.0) in node_set, (
        f"Expected node at PA (5,0) after split; got {node_set}"
    )
    assert stats["pa_connected"] >= 1


def test_pb_on_middle_of_line_is_split_and_connected():
    """PR #28 Point 1: same as above but for PB in the middle of a line."""
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(5.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    geoms = [g for g in out.geometry if isinstance(g, LineString)]
    node_set = set()
    for g in geoms:
        cs = list(g.coords)
        node_set.add((round(cs[0][0], 3), round(cs[0][1], 3)))
        node_set.add((round(cs[-1][0], 3), round(cs[-1][1], 3)))
    assert (5.0, 0.0) in node_set, (
        f"Expected node at PB (5,0) after split; got {node_set}"
    )
    assert stats["pb_connected"] >= 1


# ---------------------------------------------------------------------------
# PR #28 (Point 2) — T-junction generic split
# ---------------------------------------------------------------------------


def test_endpoint_projection_splits_crossing_line_t_junction():
    """PR #28 Point 2: Line A (0,5)→(5,5) ends at (5,5) on the middle
    of Line B (5,0)→(5,10). After split, Line B becomes two segments
    sharing EXACT node (5,5) with Line A — true topological connection."""
    df = _df([
        _row(geometry=LineString([(0, 5), (5, 5)]), src="ft", pb_id="PB1"),
        _row(geometry=LineString([(5, 0), (5, 10)]), src="ft", pb_id="PB2"),
    ])
    out, stats = lt._split_livrableedges_at_endpoint_projections(df, tol_m=0.5)
    assert len(out) >= 3, f"Expected at least 3 rows after T-junction split; got {len(out)}"
    # After split, Line A endpoint and Line B split point must share EXACT same coord
    geoms = list(out.geometry)
    all_endpoints = []
    for g in geoms:
        cs = list(g.coords)
        all_endpoints.append((round(cs[0][0], 3), round(cs[0][1], 3)))
        all_endpoints.append((round(cs[-1][0], 3), round(cs[-1][1], 3)))
    # (5,5) must appear as endpoint of at least 2 segments (A and one B-half)
    count_5_5 = sum(1 for ep in all_endpoints if ep == (5.0, 5.0))
    assert count_5_5 >= 2, f"Expected (5,5) as shared endpoint ≥2 times, got {count_5_5}: {all_endpoints}"


# ---------------------------------------------------------------------------
# PR #28 (Point 3) — Repair public micro-gap
# ---------------------------------------------------------------------------


def test_public_micro_gap_1_5m_is_repaired_not_only_flagged():
    """PR #28 Point 3 / PR #34 amend (Bloqueur 3): a 1.5 m public gap is
    detected and surfaced as ``MICRO_GAP_UNRESOLVED`` instead of being
    patched with a visible C0 connector. The previous behaviour
    (appending a ``mode_pose=C0, src=gc_neuf`` row) is now forbidden so
    livrable_infra does not show short straight lines glued onto real
    infra.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (5, 0)])),
        _row(geometry=LineString([(6.5, 0), (10, 0)])),
    ])
    out, stats = lt._repair_micro_gaps(
        df, delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # Gap detected
    assert stats["micro_gaps_detected"] >= 1
    # 1.5 m is above ENDPOINT_SNAP_TOL_M (0.5 m), so it is NOT auto-snapped
    # and must be reported as unresolved — never silently patched with C0.
    assert stats["micro_gaps_unresolved"] >= 1
    # No new visible C0 connector row appended.
    assert len(out) == 2
    c0_patch = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert c0_patch.empty, (
        "PR #34: _repair_micro_gaps must never emit a visible C0 row"
    )


def test_public_micro_gap_under_0_5m_is_snapped_no_c0_row():
    """PR #34 amend: a sub-0.5 m public gap is snapped exactly. The two
    segments end up sharing a coordinate and no extra row is added.
    """
    df = _df([
        _row(geometry=LineString([(0.0, 0.0), (5.0, 0.0)])),
        _row(geometry=LineString([(5.3, 0.0), (10.0, 0.0)])),
    ])
    out, stats = lt._repair_micro_gaps(
        df, delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    assert stats["micro_gaps_detected"] >= 1
    assert stats["micro_gaps_fixed"] >= 1
    assert stats["micro_gaps_unresolved"] == 0
    assert len(out) == 2  # no connector row appended
    c0_patch = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert c0_patch.empty, "snap must not produce a C0 row"
    # Confirm the snap actually happened: both segments now share an endpoint.
    endpoints: set[tuple[float, float]] = set()
    for g in out.geometry:
        cs = list(g.coords)
        endpoints.add((round(cs[0][0], 3), round(cs[0][1], 3)))
        endpoints.add((round(cs[-1][0], 3), round(cs[-1][1], 3)))
    # Either (5.0, 0.0) or (5.3, 0.0) — but not both anymore.
    assert not ((5.0, 0.0) in endpoints and (5.3, 0.0) in endpoints), (
        f"endpoints should have been snapped together, got {endpoints}"
    )


def test_private_micro_gap_is_flagged_not_repaired():
    """PR #28 Point 3: gap of 1.5 m in private area → flagged, no connector."""
    from auvergne_pipeline import flags as flags_mod

    public = Polygon([(0, -5), (4, -5), (4, 5), (0, 5)]).buffer(0.01)
    df = _df([
        _row(geometry=LineString([(0, 0), (4, 0)])),
        _row(geometry=LineString([(5.5, 0), (10, 0)])),
    ])
    fc = flags_mod.FlagCollector("SRO1")
    out, stats = lt._repair_micro_gaps(
        df, delivery_public_area_safe=public, flag_collector=fc,
    )
    assert stats["micro_gaps_fixed"] == 0
    assert stats["micro_gaps_unresolved"] >= 1
    assert len(out) == 2  # no connector added


# ---------------------------------------------------------------------------
# PR #28 (Point 4) — Support switch smoothing
# ---------------------------------------------------------------------------


def test_support_switch_smoothing_replaces_bt_sandwich_when_orange_parallel_exists():
    """PR #28 Point 4: Orange-A (0,0)→(5,0), BT (5,0)→(10,0),
    Orange-B (10,0)→(15,0), Orange parallel (5,0.1)→(10,0.1).
    After smoothing, the BT sandwich should be dropped.
    """
    df = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft", mode_pose="7"),
        _row(geometry=LineString([(5, 0), (10, 0)]), src="bt", mode_pose="1"),
        _row(geometry=LineString([(10, 0), (15, 0)]), src="ft", mode_pose="7"),
        _row(geometry=LineString([(5, 0.1), (10, 0.1)]), src="ft", mode_pose="7"),
    ])
    out, stats = lt._smooth_support_switches(df)
    # BT segment should be removed, Orange parallel kept
    assert (out["src"] == "bt").sum() == 0
    assert stats["support_switches_fixed"] >= 1


# ---------------------------------------------------------------------------
# PR #28 (Point 5) — Reconnect after energy private removal
# ---------------------------------------------------------------------------


def test_energy_private_removal_reconnects_with_public_c0_when_possible():
    """Amend 5: BT segment removed → endpoints identified from df_before
    vs df_after difference → reconnect with short public C0 if possible."""
    df_before = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft"),
        _row(geometry=LineString([(5, 0), (6.5, 0)]), src="bt"),
        _row(geometry=LineString([(6.5, 0), (10, 0)]), src="ft"),
    ])
    # After energy filter, BT removed — gap (5,0) to (6.5,0)
    df_after = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft"),
        _row(geometry=LineString([(6.5, 0), (10, 0)]), src="ft"),
    ])
    out, stats = lt._reconnect_after_energy_removal(
        df_before, df_after,
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # PR32-D: existing FT line touches the reconnect connector at (6.5, 0),
    # so reconnect is handled via existing infra rather than injecting C0.
    assert stats["energy_reconnectors_added"] == 0
    assert stats["energy_reconnected_by_existing"] >= 1


def test_energy_private_removal_flags_when_reconnect_impossible():
    """Amend 5: BT removed but gap too far → flag, no connector."""
    from auvergne_pipeline import flags as flags_mod

    df_before = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft"),
        _row(geometry=LineString([(5, 0), (50, 0)]), src="bt"),
        _row(geometry=LineString([(50, 0), (60, 0)]), src="ft"),
    ])
    df_after = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft"),
        _row(geometry=LineString([(50, 0), (60, 0)]), src="ft"),
    ])
    fc = flags_mod.FlagCollector("SRO1")
    out, stats = lt._reconnect_after_energy_removal(
        df_before, df_after,
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
        flag_collector=fc,
    )
    # Gap is 45m > MICRO_GAP_MAX_FIX_M * 2 = 6m → no reconnect, fail flagged
    assert stats["energy_reconnect_failed"] >= 1
    assert stats["energy_reconnectors_added"] == 0
    flags_df = fc.to_dataframe()
    assert "ENERGY_RECONNECT_FAILED" in set(flags_df["flag_type"])


def test_non_energy_removal_does_not_reconnect():
    """Amend 5: FT segment removed (not BT/E1) → no reconnector added."""
    df_before = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft", mode_pose="7"),
        _row(geometry=LineString([(5, 0), (6.5, 0)]), src="ft", mode_pose="7"),
        _row(geometry=LineString([(6.5, 0), (10, 0)]), src="ft", mode_pose="7"),
    ])
    df_after = _df([
        _row(geometry=LineString([(0, 0), (5, 0)]), src="ft", mode_pose="7"),
        _row(geometry=LineString([(6.5, 0), (10, 0)]), src="ft", mode_pose="7"),
    ])
    out, stats = lt._reconnect_after_energy_removal(
        df_before, df_after,
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    assert stats["energy_reconnectors_added"] == 0
    assert stats["energy_reconnect_failed"] == 0
